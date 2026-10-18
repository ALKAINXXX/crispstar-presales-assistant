"""Run real embedding samples; this is a sample record, not an index."""

import argparse
from datetime import datetime, timezone
from hashlib import sha256
import json
import math
import os
from pathlib import Path
import platform
import subprocess
import sys
import tempfile
import time


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = PROJECT_ROOT / "data/vector_store/embedding_sample.json"


def run_sample(*, offline: bool, output: Path) -> dict:
    # Opt out of Hub telemetry before importing it; chat keys are never passed
    # to the embedding stack and no chat client is created.
    os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
    from app.config import get_settings
    from app.llm.embedding import EmbeddingError, LocalEmbeddingClient
    from app.services.catalog import get_catalog

    started = time.perf_counter()
    catalog = get_catalog()
    condition = catalog.conditions[0]
    documents = [
        condition.statement + "该前提适用于企业版 Business；管理员后续配置属于实施说明。",
        "澄星协作的产品 API 与 Webhook 通知适用于团队版 Team 和企业版 Business，用于系统集成。",
        "澄星协作支持数据导出，便于保存团队项目资料；私有化部署在三档中均不支持。",
    ]
    queries = [
        "启用企业统一登录前，我们需要先准备什么身份系统？",
        "想让员工通过公司现有账号登录，客户应具备什么条件？",
    ]
    client = LocalEmbeddingClient(get_settings(), offline=offline)
    vectors = client.embed_documents(documents)
    query_vectors = tuple(client.embed_query(query) for query in queries)
    scores = [[sum(a * b for a, b in zip(query, doc)) for doc in vectors] for query in query_vectors]
    winners = [max(range(len(row)), key=row.__getitem__) for row in scores]
    if winners != [0, 0]:
        raise EmbeddingError("Sample synonymous queries did not rank the SSO text first.")
    repeated = client.embed_documents([documents[0]])[0]
    repeat_delta = max(abs(a - b) for a, b in zip(repeated, vectors[0]))
    if repeat_delta > 1e-5:
        raise EmbeddingError("Sample batch/single repeat differs beyond 1e-5.")
    all_vectors = vectors + query_vectors
    result = {
        "status": "sample_passed", "scope": "embedding_sample_only",
        "run_id": os.urandom(8).hex(),
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "environment": {"python": platform.python_version(), "platform": platform.platform()},
        "configuration": client.configuration(),
        "catalog_version": catalog.version, "condition_id": condition.condition_id,
        "documents": documents, "queries": queries,
        "input_sha256": sha256(json.dumps([documents, queries], ensure_ascii=False).encode("utf-8")).hexdigest(),
        "document_shape": [3, 512], "query_shape": [2, 512],
        "vectors": vectors, "query_vectors": query_vectors,
        "all_finite": all(math.isfinite(x) for row in all_vectors for x in row),
        "l2_norms": [math.sqrt(sum(x * x for x in row)) for row in all_vectors],
        "cosine_scores": scores, "top_document_indices": winners,
        "repeat_max_abs_delta": repeat_delta,
        "offline": offline, "elapsed_seconds": round(time.perf_counter() - started, 3),
    }
    # Publish the sample evidence only after all checks; never write an index.
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=output.parent, delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(result, stream, ensure_ascii=False, indent=2, allow_nan=False)
        temporary.replace(output)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    print(f"模型：{result['configuration']['model']}；CPU；512 维；模型版本：{result['configuration']['revision']}")
    print("正文 3×512；查询 2×512；数值有限、L2 长度检查通过。")
    for query, row in zip(queries, scores):
        print(f"查询：{query}\n  SSO/API/导出边界余弦：" + ", ".join(f"{score:.6f}" for score in row))
    print(f"重复最大差值：{repeat_delta:.9g}；耗时：{result['elapsed_seconds']} 秒；offline={offline}")
    print(f"小样记录：{output}")
    print("本次真实 Embedding 小样检查通过；未构建或验证完整资料索引。")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--offline", action="store_true", help="Use only the pinned local model cache.")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--timeout-seconds", type=int, default=180, help="Overall download/inference deadline.")
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.timeout_seconds < 1:
        parser.error("timeout-seconds must be positive")
    if args.worker:
        try:
            run_sample(offline=args.offline, output=args.output.resolve())
            return 0
        except Exception as exc:
            from app.llm.embedding import EmbeddingError
            detail = str(exc) if isinstance(exc, (EmbeddingError, ValueError)) else type(exc).__name__
            print(f"Embedding 小样未完成：{detail}；本次未发布成功记录，已有记录仅代表旧运行。", file=sys.stderr)
            return 1
    command = [sys.executable, "-X", "utf8", "-m", "scripts.check_embedding", "--worker", "--output", str(args.output.resolve())]
    if args.offline:
        command.append("--offline")
    try:
        completed = subprocess.run(command, cwd=PROJECT_ROOT, timeout=args.timeout_seconds)
        return completed.returncode
    except subprocess.TimeoutExpired:
        print("Embedding 小样超时，工作进程已停止；本次结果未获验证。已有记录仅代表旧运行。", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
