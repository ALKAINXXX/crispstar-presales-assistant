"""Real retrieval and matching checks, with an isolated negative corpus.

The experimental builder below deliberately omits the formal index's mandatory condition
Source. It exists only in this verification script, uses another dataset
version and never publishes into the formal corpus/index. It does not relax
the application loader or replace model inference.
"""

import argparse
from datetime import datetime, timezone
from hashlib import sha256
import json
import math
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tempfile
from uuid import uuid4

from app.config import get_settings
from app.llm.embedding import LocalEmbeddingClient
from app.schemas.knowledge import (
    CorpusManifest, EmbeddingProfile, FileDigest, IndexFile, KnowledgeIndex,
)
from app.schemas.matching import ConditionAssertion, HardRequirement, RequirementSet
from app.schemas.retrieval import RetrievalEvidence, RetrievalOptions
from app.services.catalog import DEFAULT_CATALOG_PATH, get_catalog, load_catalog
from app.services.knowledge import (
    DEFAULT_CORPUS_DIR, DEFAULT_INDEX_PATH, DEFAULT_POLICY, PROJECT_ROOT,
    KnowledgeError, PreparedCorpus, _check_inputs, _parse_document, _read_index,
    _unique, load_index, model_digest,
)
from app.services.matching import match_requirements
from app.services.retrieval import KnowledgeService
from app.services.retrieval_worker import rank_sources


# Serialized negative-index identities remain stable for existing evidence.
NEGATIVE_VERSION = "chengxing-demo-v1-w05-negative"
CONDITION_ID = "saml_sso_compatible_idp"
DEFAULT_OUTPUT = PROJECT_ROOT / "data/vector_store/check_retrieval.json"
SSO_QUERY = "启用企业统一登录前，我们需要先准备什么身份系统？"
REPORT_SCOPE = "finite_retrieval_and_matching_checks"
# Recognize this script's earlier reports without emitting their old status.
LEGACY_REPORT_SCOPE = "w05_code_verification_user_acceptance_pending"


def guard_report_target(path, settings=None):
    """A verification report may never overwrite accepted project inputs."""
    path = Path(path).resolve()
    if path.suffix.lower() != ".json":
        raise ValueError("Verification report must use a .json extension")
    settings = get_settings() if settings is None else settings
    cache = Path(settings.embedding_cache_dir)
    cache = (cache if cache.is_absolute() else PROJECT_ROOT / cache).resolve()
    if path in {DEFAULT_INDEX_PATH.resolve(), DEFAULT_CATALOG_PATH.resolve()}:
        raise ValueError("Verification report cannot replace the formal index or Catalog")
    for protected_root in (DEFAULT_CORPUS_DIR.resolve(), cache):
        if path == protected_root or path.is_relative_to(protected_root):
            raise ValueError("Verification report cannot be stored inside corpus/model inputs")
    if path.exists():
        try:
            if not path.is_file() or path.stat().st_size > 8 * 1024 * 1024:
                raise ValueError("Existing target is not a bounded verification report")
            previous = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(previous, dict) or previous.get("scope") not in {REPORT_SCOPE, LEGACY_REPORT_SCOPE}:
                raise ValueError("Existing target belongs to another artifact")
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError("Existing target is not this script's previous report") from exc
    return path


def stop_worker_tree(process):
    """Stop only the child tree created by this invocation, including ONNX."""
    if process.poll() is not None:
        return
    if os.name == "nt":
        try:
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                           capture_output=True, timeout=10, check=False)
        except (OSError, subprocess.TimeoutExpired):
            pass
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    if process.poll() is None:
        process.kill()
    process.wait(timeout=10)


def prepare_negative_corpus(corpus_dir, catalog_path):
    """Check the explicitly incomplete experimental corpus against its copy.

    All ordinary identity/path/source checks remain. The sole permitted
    departure is zero controlled condition Sources after deleting that whole
    section. Current disk files are checked again on every experimental load.
    """
    corpus_dir, catalog_path = Path(corpus_dir).resolve(), Path(catalog_path).resolve()
    catalog = load_catalog(catalog_path)
    if catalog.version != NEGATIVE_VERSION:
        raise KnowledgeError("negative_scope_invalid", "隔离目录版本标识不正确。")
    manifest_raw = (corpus_dir / "manifest.json").read_bytes()
    manifest = CorpusManifest.model_validate_json(manifest_raw)
    if manifest.version != catalog.version or manifest.product_id != catalog.product.product_id:
        raise KnowledgeError("negative_scope_invalid", "隔离资料和隔离 Catalog 身份不一致。")
    _unique([d.document_id for d in manifest.documents], "隔离文档")
    _unique([d.file for d in manifest.documents], "隔离文件")
    if len(manifest.documents) != 6 or {p.name for p in corpus_dir.glob("*.md")} != {d.file for d in manifest.documents}:
        raise KnowledgeError("negative_scope_invalid", "隔离实验必须保留六篇正文且不引入额外文件。")
    allowed = {
        "plan_ids": {p.plan_id for p in catalog.plans},
        "feature_ids": {f.feature_id for f in catalog.features},
        "condition_ids": {c.condition_id for c in catalog.conditions},
    }
    files, sources = [], []
    for doc in manifest.documents:
        _unique([s.section_id for s in doc.sections], "隔离小节")
        _unique([s.title for s in doc.sections], "隔离小节标题")
        for section in doc.sections:
            for name, identities in allowed.items():
                values = getattr(section, name)
                _unique(values, "隔离关联")
                if not set(values) <= identities:
                    raise KnowledgeError("negative_scope_invalid", "隔离资料存在目录外身份。")
            if section.condition_ids or section.section_id == "sso_prerequisite":
                raise KnowledgeError("negative_scope_invalid", "隔离实验仍包含已移除的条件小节。")
        path = (corpus_dir / doc.file).resolve()
        if path.parent != corpus_dir:
            raise KnowledgeError("negative_scope_invalid", "隔离资料路径越界。")
        raw = path.read_bytes()
        files.append(FileDigest(file=doc.file, sha256=sha256(raw).hexdigest()))
        sources.extend(_parse_document(doc, raw, manifest.version, DEFAULT_POLICY))
    _unique([s.source_id for s in sources], "隔离 Source")
    if len(sources) != 23 or any(CONDITION_ID in s.condition_ids or catalog.conditions[0].statement in s.raw_text for s in sources):
        raise KnowledgeError("negative_scope_invalid", "隔离实验必须恰好移除完整唯一条件 Source。")
    han_count = sum(len(re.findall(r"[\u4e00-\u9fff]", s.raw_text.split("\n", 1)[1])) for s in sources)
    return PreparedCorpus(manifest, model_digest(catalog), sha256(manifest_raw).hexdigest(),
                          tuple(files), tuple(sources), DEFAULT_POLICY, han_count), catalog


def build_negative_index(client, corpus_dir, catalog_path, index_path):
    prepared, catalog = prepare_negative_corpus(corpus_dir, catalog_path)
    profile = EmbeddingProfile.from_configuration(client.configuration())
    inputs = prepared.inputs(profile)
    for source in inputs.sources:
        if client.count_tokens(source.embedding_text()) > profile.max_tokens:
            raise KnowledgeError("negative_section_too_long", "隔离资料超出真实 tokenizer 限制。")
    vectors = client.embed_documents([s.embedding_text() for s in inputs.sources])
    index = KnowledgeIndex(scope="w05_negative_experiment", build_id=uuid4().hex,
                           built_at=datetime.now(timezone.utc).isoformat(),
                           inputs_sha256=model_digest(inputs), inputs=inputs, vectors=vectors)
    current, current_catalog = prepare_negative_corpus(corpus_dir, catalog_path)
    _check_inputs(index, current.inputs(EmbeddingProfile.from_configuration(client.configuration())))
    assert current_catalog == catalog
    envelope = IndexFile(payload_sha256=model_digest(index), payload=index)
    Path(index_path).write_text(envelope.model_dump_json(indent=2), encoding="utf-8")
    return load_negative_index(client, corpus_dir, catalog_path, index_path)


def load_negative_index(client, corpus_dir, catalog_path, index_path):
    index = _read_index(Path(index_path))
    if index.scope != "w05_negative_experiment":
        raise KnowledgeError("negative_scope_invalid", "不是隔离实验索引。")
    prepared, _ = prepare_negative_corpus(corpus_dir, catalog_path)
    _check_inputs(index, prepared.inputs(EmbeddingProfile.from_configuration(client.configuration())))
    return index


class NegativeKnowledgeService(KnowledgeService):
    """Verification-only service; the outer worker bounds genuine inference."""

    def __init__(self, *, client, catalog_path, **kwargs):
        super().__init__(**kwargs)
        self._negative_client = client
        self._negative_catalog_path = Path(catalog_path).resolve()
        self.actual_query_vectors = {}

    def _execute(self, request):
        index = load_negative_index(self._negative_client, self._corpus_dir,
                                    self._negative_catalog_path, self._index_path)
        options = RetrievalOptions.model_validate_json(json.dumps(request["options"]))
        vector = self._negative_client.embed_query(request["query"])
        self.actual_query_vectors[request["retrieval_id"]] = vector
        hits, excluded = rank_sources(index, vector, options)
        status = "empty" if not hits else "retrieved" if any(h.relevance == "related" for h in hits) else "low_relevance"
        return RetrievalEvidence(
            retrieval_id=request["retrieval_id"], chain_id=request["chain_id"],
            query=request["query"], retrieved_at=request["retrieved_at"], options=options,
            status=status, scope="negative_experiment", hits=hits,
            reason_codes=("text_budget_excluded",) if excluded else (), excluded_source_ids=excluded,
            product_id=index.inputs.product_id, dataset_version=index.inputs.dataset_version,
            index_build_id=index.build_id, index_inputs_sha256=index.inputs_sha256,
            embedding=index.inputs.embedding,
        )

    def _validate_current(self, result):
        if result.scope != "negative_experiment":
            raise KnowledgeError("negative_scope_invalid", "实验不能使用正式或别处的证据。")
        index = load_negative_index(self._negative_client, self._corpus_dir,
                                    self._negative_catalog_path, self._index_path)
        if (result.index_inputs_sha256, result.dataset_version, result.product_id, result.embedding) != (
                index.inputs_sha256, index.inputs.dataset_version, index.inputs.product_id, index.inputs.embedding):
            raise KnowledgeError("evidence_index_mismatch", "实验结果与当前隔离索引不一致。")
        sources = {s.source_id: s for s in index.inputs.sources}
        ids = [h.source.source_id for h in result.hits]
        if len(ids) != len(set(ids)) or any(sources.get(h.source.source_id) != h.source for h in result.hits):
            raise KnowledgeError("evidence_source_mismatch", "实验返回的原文或位置不一致。")
        if (len(result.hits) > result.options.top_k
                or any(len(h.source.raw_text) > result.options.max_source_chars for h in result.hits)
                or sum(len(h.source.raw_text) for h in result.hits) > result.options.max_total_chars):
            raise KnowledgeError("evidence_budget_invalid", "实验返回超出全文预算。")
        return index


def requirement(value, *, revision=1):
    quotes = {"yes": "我们明确确认已经具备兼容的身份提供方。",
              "unknown": "我们还不清楚是否已具备兼容的身份提供方。",
              "no": "我们明确没有兼容的身份提供方。"}
    return RequirementSet(requirement_id="sso-audit", revision=revision,
        items=(HardRequirement(requirement_id="sso", statement="必须有 SAML SSO",
                               user_quote="我们必须使用 SAML SSO", feature_id="saml_sso"),
               HardRequirement(requirement_id="audit", statement="必须有审计日志",
                               user_quote="我们还需要审计日志", feature_id="audit_logs")),
        condition_assertions=(ConditionAssertion(condition_id=CONDITION_ID, value=value, user_quote=quotes[value]),))


def business_item(result, identity="sso"):
    return next(item for plan in result.plans if plan.plan_id == "business"
                for item in plan.items if item.requirement_id == identity)


def show_evidence(label, evidence, *, only_condition=False):
    print(f"\n{label}：{evidence.query}")
    print(f"检索状态：{evidence.status}；语义支持：{evidence.semantic_support}；范围：{evidence.scope}。")
    hits = [h for h in evidence.hits if h.source.condition_ids] if only_condition else evidence.hits[:2]
    for hit in hits:
        source = hit.source
        print(f"原文来源：{source.relative_path}:{source.line_start}-{source.line_end}；{source.source_id}")
        print(f"排序：{hit.rank}；余弦：{hit.score:.6f}；相关状态：{hit.relevance}（不是支持概率）。")
        print(source.raw_text)


def show_match(label, result):
    sso = business_item(result)
    print(f"{label} Match：{result.status}；最低完整满足档：{result.lowest_plan_id}；Business SSO：{sso.status}；原因：{sso.reason_codes}；用户自述：{sso.condition_user_quote}")


def run_checks(args):
    os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
    settings = get_settings()
    args.output = guard_report_target(args.output, settings)
    # Only the explicitly selected public local model may download if the
    # caller omits --offline; ordinary KnowledgeService workers remain offline.
    client = LocalEmbeddingClient(settings, offline=args.offline)
    formal = load_index(client)
    formal_bytes = DEFAULT_INDEX_PATH.read_bytes()
    tracked_inputs = [DEFAULT_CATALOG_PATH, DEFAULT_CORPUS_DIR / "manifest.json"]
    tracked_inputs += sorted(DEFAULT_CORPUS_DIR.glob("*.md"))
    original_hashes = {str(p): sha256(p.read_bytes()).hexdigest() for p in tracked_inputs}
    service = KnowledgeService(settings=settings, timeout_seconds=30)
    catalog = get_catalog()
    evidence = service.retrieve(SSO_QUERY)
    assert evidence.status == "retrieved" and evidence.scope == "formal"
    check = service.check_condition(CONDITION_ID, plan_id="business", catalog=catalog,
                                    retrieval_ids=(evidence.retrieval_id,))
    assert check.status == "valid" and check.source_ids
    show_evidence("真实同义问题与条件原文", evidence, only_condition=True)
    cases = []
    second_synonym = service.retrieve("接企业身份系统前要具备什么？")
    second_check = service.check_condition(CONDITION_ID, plan_id="business", catalog=catalog,
        retrieval_ids=(second_synonym.retrieval_id,))
    assert second_synonym.status == "retrieved" and second_check.status == "valid"
    second_source = next(h for h in second_synonym.hits if CONDITION_ID in h.source.condition_ids)
    assert catalog.conditions[0].statement in second_source.source.raw_text
    print(f"第二条真实同义问题：{second_synonym.query}；条件状态 valid；余弦 {second_source.score:.6f}；来源 {second_source.source.relative_path}:{second_source.source.line_start}-{second_source.source.line_end}。")
    cases.append({"case": "second_synonym_complete_condition", "evidence": second_synonym.model_dump(mode="json"),
                  "condition_check": second_check.model_dump(mode="json")})
    for value, expected in (("yes", "supported"), ("unknown", "pending_condition"), ("no", "unsupported")):
        result = match_requirements(requirement(value), knowledge=service,
                                    retrieval_ids=(evidence.retrieval_id,))
        assert business_item(result).status == expected
        assert business_item(result, "audit").status == "supported"
        assert result.lowest_plan_id == ("business" if value == "yes" else None)
        show_match(f"用户前提={value}", result)
        cases.append({"case": f"sso_{value}", "evidence": evidence.model_dump(mode="json"),
                      "condition_check": check.model_dump(mode="json"), "match": result.model_dump(mode="json")})
    # Independently prove that the normal matcher performs real retrieval.
    normal = match_requirements(requirement("yes"))
    assert normal.status == "matched" and normal.lowest_plan_id == "business"
    assert normal.evidence_origin == "retrieval"
    cases.append({"case": "normal_match_entry", "match": normal.model_dump(mode="json")})
    missing = match_requirements(requirement("yes"), knowledge=service, retrieval_ids=())
    assert business_item(missing).status == "pending_condition"
    assert "condition_evidence_missing" in business_item(missing).reason_codes
    show_match("明确不提供本次检索记录", missing)
    cases.append({"case": "no_retrieval_evidence", "match": missing.model_dump(mode="json")})

    details = service.retrieve("导出的文件包含哪些字段？")
    assert details.status == "retrieved" and any(h.relevance == "related" for h in details.hits)
    details = service.record_semantic_review(details.retrieval_id, verdict="insufficient",
        conclusion="当前返回原文不足以给出导出字段清单。",
        source_ids=tuple(h.source.source_id for h in details.hits),
        rationale="独立阅读本次实际返回全文：正文说明导出能力及边界，没有逐项字段清单；不能从能力名称补写。")
    assert details.semantic_support == "insufficient"
    show_evidence("相关资料仍未给出字段清单", details)
    print("独立语义审阅：正文没有字段清单，相关但不足以回答哪些字段；普通 data_export 支持仍由 Catalog 判断。")
    cases.append({"case": "related_but_insufficient", "evidence": details.model_dump(mode="json"),
                  "semantic_review": {"status": "related_but_insufficient", "question": "导出字段清单",
                                      "finding": "返回正文没有字段清单，不能从导出能力名称补写。", "reviewer": "verification_author"}})
    weather = service.retrieve("今天天气怎样？")
    assert weather.status == "low_relevance" and not any(h.relevance == "related" for h in weather.hits)
    show_evidence("资料无天气依据", weather)
    print("独立语义审阅：没有天气依据；不能把资料缺失改写为产品不支持。")
    cases.append({"case": "not_supported_by_evidence", "evidence": weather.model_dump(mode="json"),
                  "semantic_review": {"status": "not_supported", "finding": "无天气依据；不是产品 unsupported。",
                                      "reviewer": "verification_author"}})

    scim = service.retrieve("SCIM 自动开通账号支持吗？")
    assert scim.status == "retrieved" and scim.hits[0].relevance == "related"
    scim_requirements = RequirementSet(requirement_id="scim-audit", items=(HardRequirement(
        requirement_id="scim", statement="必须有 SCIM 自动开通账号", user_quote=scim.query,
        kind="unmodeled"),))
    scim_match = match_requirements(scim_requirements, knowledge=service, retrieval_ids=(scim.retrieval_id,))
    assert scim_match.lowest_plan_id is None
    assert all(p.items[0].status == "unknown" for p in scim_match.plans)
    print(f"\nSCIM 对照：真实首位余弦 {scim.hits[0].score:.6f} / {scim.hits[0].source.relative_path}:{scim.hits[0].source.line_start}-{scim.hits[0].source.line_end}；仍为全部套餐 unknown / unmodeled_requirement。")
    print(scim.hits[0].source.raw_text)
    cases.append({"case": "high_relevance_scim_remains_unknown", "evidence": scim.model_dump(mode="json"),
                  "match": scim_match.model_dump(mode="json"),
                  "semantic_review": {"status": "insufficient", "finding": "实际正文明确区分 SSO 和 SCIM；SSO 相关性不能替代目录外 SCIM 支持依据。"}})

    conflict_service = KnowledgeService(settings=settings, timeout_seconds=30)
    conflict_observation = conflict_service.retrieve(SSO_QUERY)
    assert conflict_observation.status == "retrieved"
    condition_sources = tuple(h.source.source_id for h in conflict_observation.hits if CONDITION_ID in h.source.condition_ids)
    conflict = conflict_service.record_condition_conflict(conflict_observation.retrieval_id, condition_id=CONDITION_ID,
        source_ids=condition_sources, reason="受控异常替身：模拟服务端已识别本次实际条件来源相关冲突。",
        origin="controlled_test")
    conflict_match = match_requirements(requirement("yes"), knowledge=conflict_service,
                                       retrieval_ids=(conflict.retrieval_id,))
    assert business_item(conflict_match).status == "pending_condition"
    assert "condition_evidence_conflict" in business_item(conflict_match).reason_codes
    cases.append({"case": "recognized_conflict_substitute", "evidence": conflict.model_dump(mode="json"),
                  "match": conflict_match.model_dump(mode="json"),
                  "substitution_scope": "仅替代服务端识别后的冲突记录，证明冲突阻断传播；原库没有已知冲突，不证明自动理解任意矛盾。"})
    print("\n冲突异常替身通过：仅证明已识别冲突会阻断，原库无已知冲突，不证明自动冲突识别。")

    with tempfile.TemporaryDirectory(prefix="retrieval-negative-") as temporary:
        root = Path(temporary)
        corpus_dir, catalog_path, index_path = root / "knowledge", root / "catalog.json", root / "index.json"
        shutil.copytree(DEFAULT_CORPUS_DIR, corpus_dir)
        copied_catalog = catalog.model_dump(mode="json")
        copied_catalog["version"] = NEGATIVE_VERSION
        catalog_path.write_text(json.dumps(copied_catalog, ensure_ascii=False, indent=2), encoding="utf-8")
        manifest_path = corpus_dir / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["version"] = NEGATIVE_VERSION
        removed = next(s for s in formal.inputs.sources if CONDITION_ID in s.condition_ids)
        lines = (corpus_dir / removed.relative_path).read_text(encoding="utf-8").splitlines()
        del lines[removed.line_start - 1:removed.line_end]
        (corpus_dir / removed.relative_path).write_text("\n".join(lines) + "\n", encoding="utf-8")
        for doc in manifest["documents"]:
            doc["sections"] = [s for s in doc["sections"] if s["section_id"] != removed.section_id]
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        negative = build_negative_index(client, corpus_dir, catalog_path, index_path)
        isolated_catalog = load_catalog(catalog_path)
        negative_service = NegativeKnowledgeService(client=client, catalog_path=catalog_path,
            settings=settings, corpus_dir=corpus_dir, index_path=index_path)
        negative_evidence = negative_service.retrieve(SSO_QUERY)
        assert negative_evidence.status == "retrieved" and negative_evidence.scope == "negative_experiment"
        assert all(not h.source.condition_ids for h in negative_evidence.hits)
        negative_check = negative_service.check_condition(CONDITION_ID, plan_id="business", catalog=isolated_catalog,
                                                          retrieval_ids=(negative_evidence.retrieval_id,))
        assert negative_check.status == "missing"
        negative_match = match_requirements(requirement("yes"), knowledge=negative_service,
            retrieval_ids=(negative_evidence.retrieval_id,), catalog=isolated_catalog)
        assert business_item(negative_match).status == "pending_condition"
        assert "condition_evidence_missing" in business_item(negative_match).reason_codes
        assert business_item(negative_match, "audit").status == "supported"
        assert negative_match.lowest_plan_id is None
        vector = negative_service.actual_query_vectors[negative_evidence.retrieval_id]
        assert len(vector) == 512 and math.isclose(math.sqrt(sum(x*x for x in vector)), 1, abs_tol=1e-5)
        show_evidence("隔离删除唯一条件原文后的真实召回", negative_evidence)
        show_match("原文移除后停止的判断", negative_match)
        print(f"隔离真实重建：{len(negative.inputs.sources)}×512；独立版本：{isolated_catalog.version}；实验目录会自动删除。")
        cases.append({"case": "isolated_condition_source_removed", "removed_formal_source": removed.model_dump(mode="json"),
                      "experiment_index": negative.model_dump(mode="json"),
                      "actual_query_vector": list(vector), "evidence": negative_evidence.model_dump(mode="json"),
                      "condition_check": negative_check.model_dump(mode="json"), "match": negative_match.model_dump(mode="json"),
                      "validation_scope": "真实重建与查询；实验加载仅放宽必有条件 Source 规则；正式加载和资料完全未改变。"})
    assert DEFAULT_INDEX_PATH.read_bytes() == formal_bytes
    assert original_hashes == {str(p): sha256(p.read_bytes()).hexdigest() for p in tracked_inputs}
    assert load_index(client) == formal
    report = {"schema_version": 1, "run_id": uuid4().hex,
              "completed_at": datetime.now(timezone.utc).isoformat(),
              "scope": REPORT_SCOPE,
              "offline": args.offline, "formal_index_inputs_sha256": formal.inputs_sha256,
              "formal_index_build_id": formal.build_id, "embedding": client.configuration(),
              "formal_inputs_unchanged": True, "cases": cases,
              "limitations": ["语义审阅结论属于本批次明确问题与实际原文，不是任意自然语言蕴涵算法。",
                              "本次结论仅覆盖记录中的有限检索用例及其原文判读。",
                              "不证明聊天编排或最终回答忠实度。"]}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary_output = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=args.output.parent,
                                          prefix=args.output.name + ".", suffix=".tmp", delete=False) as stream:
            temporary_output = Path(stream.name)
            stream.write(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False))
            stream.flush()
            os.fsync(stream.fileno())
        json.loads(temporary_output.read_text(encoding="utf-8"))
        guard_report_target(args.output, settings)
        temporary_output.replace(args.output)
    finally:
        if temporary_output is not None:
            temporary_output.unlink(missing_ok=True)
    print("\n本次完整代码验证记录已生成，等待父进程确认成功后发布。")
    print("本次真实查询、条件事实分支及移除来源对照通过；正式资料/索引未改，未验证聊天编排或最终回答忠实度。")
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--timeout-seconds", type=int, default=180)
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if not 1 <= args.timeout_seconds <= 600:
        parser.error("timeout-seconds must be 1 to 600")
    args.output = args.output.resolve()
    try:
        args.output = guard_report_target(args.output)
    except ValueError as exc:
        print(f"检索验证输出路径被拒绝：{exc}。", file=sys.stderr)
        return 1
    if args.worker:
        try:
            return run_checks(args)
        except Exception as exc:
            detail = str(exc) if isinstance(exc, KnowledgeError) else type(exc).__name__
            print(f"本次检索验证未完成：{detail}；旧 JSON 仅代表旧运行。", file=sys.stderr)
            return 1
    worker = None
    try:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="retrieval-check-", dir=args.output.parent) as temporary:
            staged = Path(temporary) / "report.json"
            command = [sys.executable, "-X", "utf8", "-m", "scripts.check_retrieval", "--worker", "--output", str(staged)]
            if args.offline:
                command.append("--offline")
            process_options = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
            worker = subprocess.Popen(command, cwd=PROJECT_ROOT, **process_options)
            try:
                returncode = worker.wait(timeout=args.timeout_seconds)
            except subprocess.TimeoutExpired:
                # Kill before TemporaryDirectory cleanup so live descendants
                # cannot retain or recreate staged output on Windows.
                stop_worker_tree(worker)
                raise
            if returncode:
                return returncode
            report = json.loads(staged.read_text(encoding="utf-8"))
            if not report["formal_inputs_unchanged"] or not report["cases"]:
                raise ValueError("Incomplete verification report")
            guard_report_target(args.output)
            staged.replace(args.output)
        print(f"本次完整代码验证记录已原子发布：{args.output}")
        return 0
    except subprocess.TimeoutExpired:
        if worker is not None:
            stop_worker_tree(worker)
        print("检索验证总期限已到，本次实验工作进程树已停止；旧 JSON 不代表本次成功。", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"检索验证记录未发布（{type(exc).__name__}）；旧 JSON 不代表本次成功。", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
