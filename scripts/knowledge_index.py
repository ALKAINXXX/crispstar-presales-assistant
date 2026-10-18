"""Build or check the formal knowledge index; real inference, no retrieval/chat."""

import argparse
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def print_summary(index, corpus, client, *, label: str) -> None:
    counts = [client.count_tokens(source.embedding_text()) for source in index.inputs.sources]
    print(f"{label}：{len(corpus.manifest.documents)} 篇正文；{corpus.body_han_characters} 个正文汉字；{len(index.inputs.sources)} 个完整小节。")
    print(f"资料版本：{index.inputs.dataset_version}；向量：{len(index.vectors)}×{index.inputs.embedding.dimension}；最大输入：{max(counts)} tokens。")
    print(f"模型：{index.inputs.embedding.model}；revision：{index.inputs.embedding.revision}；CPU；线程：{index.inputs.embedding.threads}。")
    print(f"Source 身份唯一；原文、位置、条件关联和向量完整性通过；构建输入摘要：{index.inputs_sha256}")
    for source in index.inputs.sources:
        if source.condition_ids:
            print(f"关键条件：{','.join(source.condition_ids)}；套餐：{','.join(source.plan_ids)}；能力：{','.join(source.feature_ids)}")
            print(f"来源：{source.relative_path}:{source.line_start}-{source.line_end}；{source.source_id}\n{source.raw_text}")


def run_worker(args) -> int:
    os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
    from app.config import get_settings
    from app.llm.embedding import EmbeddingError, LocalEmbeddingClient
    from app.services.knowledge import KnowledgeError, build_index, load_index, prepare_corpus

    started = time.perf_counter()
    try:
        # Validate corpus before spending time loading the model.
        corpus = prepare_corpus(args.corpus_dir)
        client = LocalEmbeddingClient(get_settings(), offline=args.offline)
        if args.command == "build":
            index = build_index(client, index_path=args.index_path, corpus_dir=args.corpus_dir)
            reloaded = load_index(client, index_path=args.index_path, corpus_dir=args.corpus_dir)
            if index != reloaded:
                raise KnowledgeError("reload_failed", "保存后重载与实际构建结果不同。")
            print_summary(reloaded, corpus, client, label="真实构建、保存后重载通过（等待父进程发布）")
        else:
            index = load_index(client, index_path=args.index_path, corpus_dir=args.corpus_dir)
            print_summary(index, corpus, client, label="从磁盘加载并核对通过")
        print(f"本次 {args.command} 耗时：{time.perf_counter() - started:.3f} 秒；未执行查询检索或聊天。")
        return 0
    except (KnowledgeError, EmbeddingError, ValueError) as exc:
        print(f"索引 {args.command} 未完成：{exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"索引 {args.command} 未完成（{type(exc).__name__}）；未验证的结果不能作为有效索引。", file=sys.stderr)
        return 1


def main() -> int:
    from app.services.knowledge import DEFAULT_CORPUS_DIR, DEFAULT_INDEX_PATH
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("build", "check"))
    parser.add_argument("--offline", action="store_true", help="Use the prepared local model cache only.")
    parser.add_argument("--corpus-dir", type=Path, default=DEFAULT_CORPUS_DIR)
    parser.add_argument("--index-path", type=Path, default=DEFAULT_INDEX_PATH)
    parser.add_argument("--timeout-seconds", type=int, default=180)
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.timeout_seconds < 1:
        parser.error("timeout-seconds must be positive")
    args.corpus_dir = args.corpus_dir.resolve()
    args.index_path = args.index_path.resolve()
    if args.worker:
        return run_worker(args)
    deadline = time.monotonic() + args.timeout_seconds

    def run_bounded(worker_command):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired(worker_command, args.timeout_seconds)
        return subprocess.run(worker_command, cwd=PROJECT_ROOT, timeout=remaining)

    command = [sys.executable, "-X", "utf8", "-m", "scripts.knowledge_index", args.command,
               "--worker", "--corpus-dir", str(args.corpus_dir)]
    if args.offline:
        command.append("--offline")
    try:
        if args.command == "build":
            # Only the parent may publish. A killed/timed-out worker cannot
            # replace a previously valid index or leave a partial final file.
            args.index_path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(prefix="knowledge-index-build-", dir=args.index_path.parent) as temp:
                staged = Path(temp) / "index.json"
                result = run_bounded(command + ["--index-path", str(staged)])
                if result.returncode != 0:
                    print("本次没有发布新索引；已有文件仅代表以前的构建。", file=sys.stderr)
                    return result.returncode
                # All native model loading also stays in a killable process.
                # The second worker shares the remaining deadline and checks
                # the staged file against current facts/config, without embed.
                check_command = [sys.executable, "-X", "utf8", "-m", "scripts.knowledge_index", "check",
                                 "--worker", "--offline", "--corpus-dir", str(args.corpus_dir),
                                 "--index-path", str(staged)]
                verified = run_bounded(check_command)
                if verified.returncode != 0:
                    print("发布前独立检查失败；原索引未被替换。", file=sys.stderr)
                    return verified.returncode
                if time.monotonic() >= deadline:
                    raise subprocess.TimeoutExpired(check_command, args.timeout_seconds)
                staged.replace(args.index_path)
            print(f"完整索引已原子发布：{args.index_path}")
            print("本次索引真实构建与独立重载检查通过。")
            return 0
        result = run_bounded(command + ["--index-path", str(args.index_path)])
        if result.returncode == 0:
            print("本次索引与资料来源检查通过。")
        return result.returncode
    except subprocess.TimeoutExpired:
        print("索引操作超时，工作进程已停止；本次没有发布新索引。已有文件仅代表以前的构建。", file=sys.stderr)
        return 1
    except Exception as exc:
        from app.llm.embedding import EmbeddingError
        from app.services.knowledge import KnowledgeError
        detail = str(exc) if isinstance(exc, (KnowledgeError, EmbeddingError)) else type(exc).__name__
        print(f"索引检查或发布失败：{detail}；不得把旧文件当作本次成功。", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
