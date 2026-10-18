"""Killable local-model worker; stdin request and stdout JSON only."""

from datetime import datetime, timezone
import json
import math
from pathlib import Path
import sys

from app.config import Settings
from app.llm.embedding import LocalEmbeddingClient
from app.schemas.knowledge import ChunkingPolicy, KnowledgeIndex
from app.schemas.retrieval import RetrievalEvidence, RetrievalFailure, RetrievalHit, RetrievalOptions
from app.services.knowledge import KnowledgeError, load_index


def rank_sources(index: KnowledgeIndex, query_vector: tuple[float, ...], options: RetrievalOptions):
    """Dot product of checked L2 vectors = cosine; stable ties by Source ID."""
    if (len(query_vector) != index.inputs.embedding.dimension
            or not all(math.isfinite(x) for x in query_vector)
            or not math.isclose(math.sqrt(sum(x*x for x in query_vector)), 1.0, abs_tol=1e-5)):
        raise ValueError("Invalid query vector; no retrieval performed")
    scored = sorted(
        ((max(-1.0, min(1.0, sum(a*b for a, b in zip(query_vector, row)))), source)
         for source, row in zip(index.inputs.sources, index.vectors)),
        key=lambda pair: (-pair[0], pair[1].source_id),
    )[:options.top_k]
    hits, excluded, used = [], [], 0
    for rank, (score, source) in enumerate(scored, 1):
        # Exclude a whole section, never truncate its premise or limitation.
        if len(source.raw_text) > options.max_source_chars or used + len(source.raw_text) > options.max_total_chars:
            excluded.append(source.source_id)
            continue
        hits.append(RetrievalHit(rank=rank, score=score,
                                 relevance="related" if score >= options.min_score else "low", source=source))
        used += len(source.raw_text)
    return tuple(hits), tuple(excluded)


def perform_retrieval(request: dict) -> RetrievalEvidence:
    base = dict(retrieval_id=request["retrieval_id"], chain_id=request["chain_id"],
                query=request["query"], retrieved_at=datetime.now(timezone.utc).isoformat(),
                options=RetrievalOptions.model_validate_json(json.dumps(request["options"])))
    step = "embedding_initialization"
    try:
        settings = Settings(_env_file=None, embedding_cache_dir=request["cache_dir"],
                            embedding_threads=request["threads"])
        client = LocalEmbeddingClient(settings, offline=True)
        step = "index_validation"
        index = load_index(client, index_path=Path(request["index_path"]),
                           corpus_dir=Path(request["corpus_dir"]),
                           policy=ChunkingPolicy.model_validate_json(json.dumps(request["chunking"])))
        step = "query_embedding"
        vector = client.embed_query(request["query"])
        step = "retrieval"
        hits, excluded = rank_sources(index, vector, base["options"])
        status = "empty" if not hits else "retrieved" if any(h.relevance == "related" for h in hits) else "low_relevance"
        return RetrievalEvidence(**base, status=status, hits=hits,
            reason_codes=("text_budget_excluded",) if excluded else (), excluded_source_ids=excluded,
            product_id=index.inputs.product_id, dataset_version=index.inputs.dataset_version,
            index_build_id=index.build_id, index_inputs_sha256=index.inputs_sha256,
            embedding=index.inputs.embedding)
    except Exception as exc:
        code = exc.code if isinstance(exc, KnowledgeError) else (
            "invalid_query" if step == "query_embedding" and isinstance(exc, ValueError)
            else f"{step}_failed")
        # Fixed messages do not leak dependency exceptions or settings.
        return RetrievalEvidence(**base, status="unavailable", reason_codes=(code,),
            error=RetrievalFailure(step=step, code=code, message="本次知识检索步骤未完成，未产生可用证据。",
                                   retry_action="核对本地缓存、资料与索引后重新检索；无关 Catalog 查询仍可继续。"))


def main() -> int:
    request = json.loads(sys.stdin.read())
    result = perform_retrieval(request)
    print(result.model_dump_json())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
