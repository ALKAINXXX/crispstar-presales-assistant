"""Chain-local evidence authority; no chat, pricing or persistence."""

from collections import OrderedDict
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

from app.config import Settings, get_settings
from app.schemas.catalog import Catalog
from app.schemas.knowledge import ChunkingPolicy
from app.schemas.retrieval import (
    ConditionEvidenceCheck, RecognizedConflict, RetrievalEvidence, RetrievalFailure, RetrievalOptions, SemanticReview,
)
from app.services.knowledge import (
    DEFAULT_CORPUS_DIR, DEFAULT_INDEX_PATH, DEFAULT_POLICY, PROJECT_ROOT,
    KnowledgeError, _check_inputs, _read_index, model_digest, prepare_corpus,
)


class KnowledgeService:
    """Create one per chain, retaining 16 recent observations and known conflicts.

    A serialized Evidence is an observation for display, never a credential.
    Consumers supply IDs to this owning service, which rechecks current facts.
    A new instance has no previous evidence. Work state owns session/revision policy.
    """

    def __init__(self, *, settings: Settings | None = None, corpus_dir: Path = DEFAULT_CORPUS_DIR,
                 index_path: Path = DEFAULT_INDEX_PATH, policy: ChunkingPolicy = DEFAULT_POLICY,
                 timeout_seconds: float = 30):
        if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)) or not 0 < timeout_seconds <= 180:
            raise ValueError("timeout_seconds must be finite, positive and at most 180")
        settings = get_settings() if settings is None else settings
        cache = Path(settings.embedding_cache_dir)
        self._cache_dir = cache if cache.is_absolute() else PROJECT_ROOT / cache
        self._threads = settings.embedding_threads
        self._corpus_dir, self._index_path = Path(corpus_dir).resolve(), Path(index_path).resolve()
        self._policy = ChunkingPolicy.model_validate(policy)
        self._timeout_seconds = timeout_seconds
        self.chain_id = uuid4().hex
        self._issued: OrderedDict[str, RetrievalEvidence] = OrderedDict()
        # A later clean query or ordinary result eviction cannot erase an
        # already recognized, relevant contradiction in this business chain.
        self._conflict_evidence: dict[str, RetrievalEvidence] = {}

    def _execute(self, request: dict) -> RetrievalEvidence:
        # Loading, validation and native inference share a killable deadline.
        completed = subprocess.run(
            [sys.executable, "-X", "utf8", "-m", "app.services.retrieval_worker"],
            input=json.dumps(request, ensure_ascii=False), capture_output=True, encoding="utf-8",
            cwd=PROJECT_ROOT, timeout=self._timeout_seconds,
        )
        if completed.returncode != 0:
            raise RuntimeError("Retrieval worker failed")
        return RetrievalEvidence.model_validate_json(completed.stdout)

    def retrieve(self, query: str, *, options: RetrievalOptions | None = None) -> RetrievalEvidence:
        if not isinstance(query, str) or not query.strip():
            raise ValueError("query must be a nonblank string")
        if len(query) > 2000:
            raise ValueError("query exceeds request character budget")
        options = RetrievalOptions() if options is None else RetrievalOptions.model_validate(options)
        base = dict(retrieval_id=uuid4().hex, chain_id=self.chain_id, query=query,
                    retrieved_at=datetime.now(timezone.utc).isoformat(), options=options)
        request = {**base, "options": options.model_dump(mode="json"),
                   "cache_dir": str(self._cache_dir), "threads": self._threads,
                   "corpus_dir": str(self._corpus_dir), "index_path": str(self._index_path),
                   "chunking": self._policy.model_dump(mode="json")}
        try:
            result = RetrievalEvidence.model_validate(self._execute(request))
            if (result.retrieval_id, result.chain_id, result.query, result.options) != (
                    base["retrieval_id"], self.chain_id, query, options):
                raise ValueError("Worker association mismatch")
            if result.status != "unavailable":
                self._validate_current(result)
        except Exception as exc:
            code = "retrieval_timeout" if isinstance(exc, subprocess.TimeoutExpired) else (
                exc.code if isinstance(exc, KnowledgeError) else "retrieval_failed")
            result = RetrievalEvidence(**base, status="unavailable", reason_codes=(code,),
                error=RetrievalFailure(step="retrieval", code=code,
                    message="本次检索未完成；没有沿用之前的证据。",
                    retry_action="核对本地缓存、资料与索引后重新检索；无关 Catalog 查询仍可继续。"))
        self._issued[result.retrieval_id] = result
        while len(self._issued) > 16:
            self._issued.popitem(last=False)
        return result

    def _validate_current(self, result: RetrievalEvidence):
        index = _read_index(self._index_path)
        if index.scope != "w04_local_index" or result.scope != "formal":
            raise KnowledgeError("index_scope_invalid", "负面实验不能进入正式证据链。")
        expected = prepare_corpus(self._corpus_dir, policy=self._policy).inputs(index.inputs.embedding)
        _check_inputs(index, expected)
        if (result.index_inputs_sha256, result.dataset_version, result.product_id, result.embedding) != (
                index.inputs_sha256, index.inputs.dataset_version, index.inputs.product_id, index.inputs.embedding):
            raise KnowledgeError("evidence_index_mismatch", "证据不属于当前有效资料与表示配置。")
        sources = {s.source_id: s for s in index.inputs.sources}
        ids = [h.source.source_id for h in result.hits]
        if len(ids) != len(set(ids)) or any(sources.get(h.source.source_id) != h.source for h in result.hits):
            raise KnowledgeError("evidence_source_mismatch", "实际返回原文或来源与当前索引不一致。")
        if (len(result.hits) > result.options.top_k
                or any(len(h.source.raw_text) > result.options.max_source_chars for h in result.hits)
                or sum(len(h.source.raw_text) for h in result.hits) > result.options.max_total_chars):
            raise KnowledgeError("evidence_budget_invalid", "返回原文超过证据预算。")
        return index

    def evidence(self, retrieval_id: str) -> RetrievalEvidence:
        if not isinstance(retrieval_id, str) or (retrieval_id not in self._issued and retrieval_id not in self._conflict_evidence):
            raise KnowledgeError("evidence_not_in_chain", "没有本业务链实际取得的该检索记录。")
        return self._issued.get(retrieval_id) or self._conflict_evidence[retrieval_id]

    def condition_conflict_ids(self, condition_ids: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(identity for identity, result in self._conflict_evidence.items()
                     if any(c.condition_id in condition_ids for c in result.conflicts))

    def validate_citations(self, retrieval_id: str, source_ids: tuple[str, ...]):
        result = self.evidence(retrieval_id)
        if not isinstance(source_ids, tuple) or any(not isinstance(s, str) for s in source_ids):
            raise TypeError("source_ids must be a tuple of Source IDs")
        if result.status == "unavailable":
            raise KnowledgeError("evidence_unavailable", "失败检索没有可用引用。")
        returned = {h.source.source_id: h.source for h in result.hits}
        if any(source_id not in returned for source_id in source_ids):
            raise KnowledgeError("citation_not_returned", "引用来源不在这次实际返回项中。")
        self._validate_current(result)
        # This establishes membership only, not entailment.
        return tuple(returned[source_id] for source_id in source_ids)

    def record_condition_conflict(self, retrieval_id: str, *, condition_id: str,
                                  source_ids: tuple[str, ...], reason: str,
                                  origin: str = "server_review") -> RetrievalEvidence:
        """Server review seam, never a model boolean or automatic detector."""
        result = self.evidence(retrieval_id)
        from app.services.catalog import get_catalog
        condition = next((c for c in get_catalog().conditions if c.condition_id == condition_id), None)
        sources = self.validate_citations(retrieval_id, source_ids)
        if condition is None or not sources or any(condition.feature_id not in s.feature_ids for s in sources):
            raise ValueError("Conflict must refer to the relevant condition and returned sources")
        conflict = RecognizedConflict(condition_id=condition_id, source_ids=source_ids, reason=reason, origin=origin)
        updated = RetrievalEvidence.model_validate(result.model_copy(update={"conflicts": result.conflicts + (conflict,)}))
        self._issued[retrieval_id] = updated
        self._conflict_evidence[retrieval_id] = updated
        return updated

    def record_semantic_review(self, retrieval_id: str, *, verdict: str, conclusion: str,
                               source_ids: tuple[str, ...], rationale: str,
                               origin: str = "server_review") -> RetrievalEvidence:
        """Record a server-side prose review; membership is checked, meaning is not.

        This is not a model tool or an entailment classifier. It records
        the finite, independently read examples for human comparison. Matcher
        always performs its own controlled condition check, ignoring this flag.
        """
        result = self.evidence(retrieval_id)
        self.validate_citations(retrieval_id, source_ids)
        review = SemanticReview(verdict=verdict, conclusion=conclusion, source_ids=source_ids,
                                rationale=rationale, origin=origin)
        updated = RetrievalEvidence.model_validate(result.model_copy(
            update={"semantic_support": verdict, "semantic_review": review}))
        self._issued[retrieval_id] = updated
        if result.conflicts:
            self._conflict_evidence[retrieval_id] = updated
        return updated

    def check_condition(self, condition_id: str, *, plan_id: str, catalog: Catalog,
                        retrieval_ids: tuple[str, ...]) -> ConditionEvidenceCheck:
        common = dict(condition_id=condition_id, retrieval_ids=retrieval_ids)
        condition = next(c for c in catalog.conditions if c.condition_id == condition_id)
        relevant, conflicts, errors = [], [], []
        for identity in retrieval_ids:
            try:
                result = self.evidence(identity)
                if result.status == "unavailable":
                    errors.extend(result.reason_codes)
                    continue
                index = self._validate_current(result)
                if model_digest(catalog) != index.inputs.catalog_sha256:
                    errors.append("condition_evidence_version_mismatch")
                    continue
                for conflict in result.conflicts:
                    if conflict.condition_id == condition_id:
                        conflicts.extend(conflict.source_ids)
                for hit in result.hits:
                    s = hit.source
                    if (hit.relevance == "related" and condition_id in s.condition_ids
                            and condition.statement in s.raw_text and plan_id in s.plan_ids
                            and s.feature_ids == (condition.feature_id,)):
                        relevant.append(s.source_id)
            except KnowledgeError as exc:
                errors.append(exc.code)
        source_ids = tuple(dict.fromkeys(relevant + conflicts))
        reasons = tuple(dict.fromkeys(errors + (["condition_evidence_conflict"] if conflicts else [])))
        if errors or conflicts:
            invalid = {"evidence_not_in_chain", "evidence_source_mismatch", "evidence_index_mismatch",
                       "condition_evidence_version_mismatch"}
            status = "conflict" if conflicts else "invalid" if any(e in invalid for e in errors) else "unavailable"
            return ConditionEvidenceCheck(**common, status=status,
                                          reason_codes=reasons, source_ids=source_ids)
        if not relevant:
            return ConditionEvidenceCheck(**common, status="missing", reason_codes=("condition_evidence_missing",))
        return ConditionEvidenceCheck(**common, status="valid", source_ids=source_ids)
