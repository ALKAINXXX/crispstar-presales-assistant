"""Pinned local Embedding adapter, independent of chat and retrieval."""

from hashlib import sha256
from importlib.metadata import version
import math
from pathlib import Path

from app.config import Settings


MODEL_NAME = "BAAI/bge-small-zh-v1.5"
MODEL_REPOSITORY = "Qdrant/bge-small-zh-v1.5"
MODEL_REVISION = "46fbe35fd4374a00fee7de77dfddaeb6dd6a2c59"
MODEL_FILE = "model_optimized.onnx"
MODEL_SHA256 = "1294ea4b6331115a353d81f96b85e8c8d7fdcc284453d5b2fab5b016230aad38"
DIMENSION = 512
MAX_TOKENS = 512
QUERY_PREFIX = "为这个句子生成表示以用于检索相关文章："
MODEL_FILES = (
    MODEL_FILE, "config.json", "tokenizer.json", "tokenizer_config.json",
    "special_tokens_map.json", "vocab.txt", "README.md",
)
PROJECT_ROOT = Path(__file__).resolve().parents[2]


class EmbeddingError(RuntimeError):
    """The embedding step failed; no substitute vectors are returned."""


class LocalEmbeddingClient:
    """Load one pinned ONNX snapshot and run bounded-length CPU inputs.

    Downloading is explicit. Cached loading and inference use no API credential.
    Heavy dependencies are imported only when constructing this client.
    """

    def __init__(self, settings: Settings, *, offline: bool = True) -> None:
        try:
            from fastembed import TextEmbedding
            from huggingface_hub import snapshot_download
            from tokenizers import Tokenizer
        except ImportError as exc:
            raise EmbeddingError("Embedding dependencies unavailable; install requirements.txt.") from exc

        cache = Path(settings.embedding_cache_dir)
        self.cache_dir = cache if cache.is_absolute() else PROJECT_ROOT / cache
        self.threads = settings.embedding_threads
        try:
            folder = Path(snapshot_download(
                repo_id=MODEL_REPOSITORY,
                revision=MODEL_REVISION,
                cache_dir=str(self.cache_dir),
                allow_patterns=list(MODEL_FILES),
                local_files_only=offline,
                token=False,
                max_workers=2,
                etag_timeout=20,
            ))
            self.file_hashes = {}
            for filename in MODEL_FILES:
                with (folder / filename).open("rb") as stream:
                    digest = sha256()
                    for block in iter(lambda: stream.read(1024 * 1024), b""):
                        digest.update(block)
                self.file_hashes[filename] = digest.hexdigest()
            if self.file_hashes[MODEL_FILE] != MODEL_SHA256:
                raise EmbeddingError("ONNX checksum mismatch; embedding initialization stopped.")
            # Count the complete input before FastEmbed's automatic truncation.
            self._tokenizer = Tokenizer.from_file(str(folder / "tokenizer.json"))
            self._tokenizer.no_truncation()
            self._tokenizer.no_padding()
            self._model = TextEmbedding(
                model_name=MODEL_NAME,
                specific_model_path=str(folder),
                cache_dir=str(self.cache_dir),
                local_files_only=True,
                providers=["CPUExecutionProvider"],
                threads=self.threads,
            )
            # This surface was checked against the pinned FastEmbed version.
            self.providers = tuple(self._model.model.model.get_providers())
            if self.providers != ("CPUExecutionProvider",):
                raise EmbeddingError("Unexpected execution provider; CPU configuration not established.")
        except EmbeddingError:
            raise
        except Exception as exc:
            # Do not expose remote exception text, headers or credentials.
            mode = "cached loading" if offline else "download/loading"
            raise EmbeddingError(
                f"Embedding {mode} failed ({type(exc).__name__}); no vectors produced. "
                "Check dependencies/cache; run the check without --offline to prepare the model."
            ) from exc

    def configuration(self) -> dict:
        """Record the actual representation, including tokenizer content hashes."""
        return {
            "provider": "local_fastembed_cpu", "model": MODEL_NAME,
            "repository": MODEL_REPOSITORY, "revision": MODEL_REVISION,
            "file_sha256": dict(self.file_hashes), "dimension": DIMENSION,
            "max_tokens": MAX_TOKENS, "pooling": "CLS", "normalization": "L2",
            "document_prefix": "", "query_prefix": QUERY_PREFIX,
            "execution_providers": list(self.providers), "threads": self.threads,
            "batch_size": 4, "parallel": None,
            "versions": {name: version(name) for name in (
                "fastembed", "onnxruntime", "numpy", "huggingface-hub", "tokenizers",
            )},
        }

    def embed_documents(self, texts: list[str] | tuple[str, ...]) -> tuple[tuple[float, ...], ...]:
        if not isinstance(texts, (list, tuple)) or not texts:
            raise ValueError("Embedding input must be a nonempty list or tuple of strings.")
        for text in texts:
            if not isinstance(text, str) or not text.strip():
                raise ValueError("Embedding input must contain nonblank strings.")
            if self.count_tokens(text) > MAX_TOKENS:
                raise ValueError("Embedding input exceeds 512 tokens; split it without losing conditions.")
        try:
            rows = tuple(tuple(float(x) for x in row) for row in self._model.embed(
                texts, batch_size=4, parallel=None,
            ))
        except Exception as exc:
            raise EmbeddingError(f"Embedding inference failed ({type(exc).__name__}); no result.") from exc
        if len(rows) != len(texts) or any(
            len(row) != DIMENSION or not all(math.isfinite(x) for x in row)
            or not math.isclose(math.sqrt(sum(x * x for x in row)), 1.0, abs_tol=1e-5)
            for row in rows
        ):
            raise EmbeddingError("Invalid embedding count, dimension, values or normalization.")
        return rows

    def count_tokens(self, text: str) -> int:
        """Count full input with special tokens, before any truncation."""
        if not isinstance(text, str) or not text.strip():
            raise ValueError("Embedding input must be a nonblank string.")
        return len(self._tokenizer.encode(text).ids)

    def embed_query(self, text: str) -> tuple[float, ...]:
        if not isinstance(text, str) or not text.strip():
            raise ValueError("Embedding query must be a nonblank string.")
        return self.embed_documents([QUERY_PREFIX + text])[0]
