"""Fixed corpus -> complete Sources -> real Embedding -> checked local index.

Corpus preparation and index integrity are separate from similarity search.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import tempfile
from uuid import uuid4

from app.llm.embedding import LocalEmbeddingClient
from app.schemas.catalog import Catalog
from app.schemas.knowledge import (
    ChunkingPolicy, CorpusManifest, DocumentSpec, EmbeddingProfile, FileDigest,
    IndexFile, IndexInputs, KnowledgeIndex, Source,
)
from app.services import catalog as catalog_service


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CORPUS_DIR = PROJECT_ROOT / "app/data/knowledge"
DEFAULT_INDEX_PATH = PROJECT_ROOT / "data/vector_store/chengxing_index.json"
DEFAULT_POLICY = ChunkingPolicy()


class KnowledgeError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


def canonical_json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def model_digest(model) -> str:
    return sha256(canonical_json(model.model_dump(mode="json")).encode("utf-8")).hexdigest()


def _current_catalog() -> Catalog:
    try:
        current = catalog_service.get_catalog()
        disk = catalog_service.load_catalog(catalog_service.DEFAULT_CATALOG_PATH)
    except ValueError as exc:
        raise KnowledgeError("catalog_invalid", "当前目录读取或校验失败。") from exc
    if model_digest(current) != model_digest(disk):
        raise KnowledgeError("catalog_restart_required", "磁盘目录与进程缓存不一致；核对资料、重启后再构建或加载。")
    return current


def _read(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError as exc:
        raise KnowledgeError("corpus_unavailable", f"无法读取固定资料 {path.name}。") from exc


def _unique(values, label: str) -> None:
    if len(values) != len(set(values)):
        raise KnowledgeError("corpus_invalid", f"{label}出现重复身份或标题。")


def _parse_document(doc: DocumentSpec, raw: bytes, version: str, policy: ChunkingPolicy) -> tuple[Source, ...]:
    try:
        lines = raw.decode("utf-8").splitlines()
    except UnicodeError as exc:
        raise KnowledgeError("corpus_invalid", f"{doc.file} 不是有效 UTF-8。") from exc
    if not lines or lines[0] != "# " + doc.title:
        raise KnowledgeError("corpus_invalid", f"{doc.file} 标题与清单不一致。")
    starts = [i for i, line in enumerate(lines) if line.startswith("## ")]
    if not starts or any(line.strip() for line in lines[1:starts[0]]):
        raise KnowledgeError("corpus_invalid", f"{doc.file} 有未归入小节的正文。")
    if [lines[i][3:] for i in starts] != [s.title for s in doc.sections]:
        raise KnowledgeError("corpus_invalid", f"{doc.file} 小节顺序或标题与清单不一致。")
    if any(line.startswith("#") and i != 0 and i not in starts for i, line in enumerate(lines)):
        raise KnowledgeError("corpus_invalid", f"{doc.file} 仅支持完整的二级小节，不接受隐含嵌套切块。")
    document_hash = sha256(raw).hexdigest()
    sources = []
    for j, section in enumerate(doc.sections):
        start = starts[j]
        end = starts[j + 1] if j + 1 < len(starts) else len(lines)
        while end > start + 1 and not lines[end - 1].strip():
            end -= 1
        if not any(line.strip() for line in lines[start + 1:end]):
            raise KnowledgeError("corpus_invalid", f"{doc.file} 的 {section.title} 没有正文。")
        text = "\n".join(lines[start:end])
        text_hash = sha256(text.encode("utf-8")).hexdigest()
        source = Source(
            source_id=f"{version}/{doc.document_id}/{section.section_id}/{text_hash[:16]}",
            document_id=doc.document_id, document_title=doc.title,
            relative_path=doc.file, dataset_version=version,
            section_id=section.section_id, section_title=section.title,
            line_start=start + 1, line_end=end, raw_text=text,
            document_sha256=document_hash, text_sha256=text_hash,
            plan_ids=section.plan_ids, feature_ids=section.feature_ids,
            condition_ids=section.condition_ids,
        )
        if len(source.embedding_text()) > policy.max_embedding_chars:
            raise KnowledgeError("section_too_long", f"{doc.file}:{start + 1} 超过完整小节字符预算；按语义调整标题，不截断前提。")
        sources.append(source)
    return tuple(sources)


@dataclass(frozen=True)
class PreparedCorpus:
    manifest: CorpusManifest
    catalog_sha256: str
    manifest_sha256: str
    documents: tuple[FileDigest, ...]
    sources: tuple[Source, ...]
    chunking: ChunkingPolicy
    body_han_characters: int

    def inputs(self, profile: EmbeddingProfile) -> IndexInputs:
        return IndexInputs(
            dataset_version=self.manifest.version, product_id=self.manifest.product_id,
            catalog_sha256=self.catalog_sha256, manifest_sha256=self.manifest_sha256,
            documents=self.documents, chunking=self.chunking, embedding=profile,
            sources=self.sources,
        )


def prepare_corpus(corpus_dir: Path = DEFAULT_CORPUS_DIR, *, policy: ChunkingPolicy = DEFAULT_POLICY) -> PreparedCorpus:
    """Read fixed UTF-8 files and keep each complete H2 with its original lines."""
    catalog = _current_catalog()
    root = Path(corpus_dir).resolve()
    manifest_raw = _read(root / "manifest.json")
    try:
        manifest = CorpusManifest.model_validate_json(manifest_raw)
    except ValueError as exc:
        raise KnowledgeError("corpus_invalid", "资料清单结构无效。") from exc
    if manifest.version != catalog.version or manifest.product_id != catalog.product.product_id:
        raise KnowledgeError("corpus_catalog_mismatch", "资料集版本或产品身份与当前 Catalog 不一致。")
    _unique([doc.document_id for doc in manifest.documents], "文档")
    _unique([doc.file for doc in manifest.documents], "文件")
    if {p.name for p in root.glob("*.md")} != {doc.file for doc in manifest.documents}:
        raise KnowledgeError("corpus_invalid", "实际 Markdown 文件与固定清单不一致。")
    known = {
        "plan_ids": {plan.plan_id for plan in catalog.plans},
        "feature_ids": {feature.feature_id for feature in catalog.features},
        "condition_ids": {condition.condition_id for condition in catalog.conditions},
    }
    files, sources = [], []
    for doc in manifest.documents:
        _unique([s.section_id for s in doc.sections], f"{doc.file} 小节")
        _unique([s.title for s in doc.sections], f"{doc.file} 标题")
        for section in doc.sections:
            for name, allowed in known.items():
                identities = getattr(section, name)
                _unique(identities, f"{doc.file} {name}")
                if not set(identities) <= allowed:
                    raise KnowledgeError("source_association_invalid", f"{doc.file} 存在目录外的 {name} 关联。")
        path = (root / doc.file).resolve()
        if path.parent != root:
            raise KnowledgeError("corpus_invalid", "文档路径必须位于固定资料目录内。")
        raw = _read(path)
        files.append(FileDigest(file=doc.file, sha256=sha256(raw).hexdigest()))
        sources.extend(_parse_document(doc, raw, manifest.version, policy))
    _unique([s.source_id for s in sources], "Source")
    for condition in catalog.conditions:
        associated = [s for s in sources if condition.condition_id in s.condition_ids]
        plans = {p.plan_id for p in catalog.plans for f in p.features if f.condition_id == condition.condition_id}
        if len(associated) != 1:
            raise KnowledgeError("condition_source_invalid", "唯一前提必须有且仅有一处完整受控来源。")
        source = associated[0]
        if (condition.statement not in source.raw_text
                or set(source.feature_ids) != {condition.feature_id}
                or set(source.plan_ids) != plans
                or any(p.name not in source.raw_text for p in catalog.plans if p.plan_id in plans)):
            raise KnowledgeError("condition_source_invalid", "条件原句、适用套餐名称和条件/能力关联必须同节完整保留。")
    han_count = sum(len(re.findall(r"[\u4e00-\u9fff]", s.raw_text.split("\n", 1)[1])) for s in sources)
    if han_count > 8000:
        raise KnowledgeError("corpus_invalid", "固定知识正文超过约 8000 中文字预算。")
    return PreparedCorpus(manifest, model_digest(catalog), sha256(manifest_raw).hexdigest(), tuple(files), tuple(sources), policy, han_count)


def _profile(client: LocalEmbeddingClient) -> EmbeddingProfile:
    try:
        return EmbeddingProfile.from_configuration(client.configuration())
    except (ValueError, KeyError, TypeError) as exc:
        raise KnowledgeError("embedding_configuration_invalid", "无法建立实际 Embedding 配置。") from exc


def _read_index(path: Path) -> KnowledgeIndex:
    try:
        if path.stat().st_size > 8 * 1024 * 1024:
            raise KnowledgeError("index_invalid", "索引超过此固定资料集的文件预算。")
        raw = path.read_bytes()
        envelope = IndexFile.model_validate_json(raw)
        if model_digest(envelope.payload) != envelope.payload_sha256:
            raise KnowledgeError("index_corrupt", "索引内容摘要不一致。")
        if model_digest(envelope.payload.inputs) != envelope.payload.inputs_sha256:
            raise KnowledgeError("index_corrupt", "构建输入摘要不一致。")
        return envelope.payload
    except KnowledgeError:
        raise
    except OSError as exc:
        raise KnowledgeError("index_unavailable", "无法读取索引；先完成真实构建。") from exc
    except ValueError as exc:
        raise KnowledgeError("index_invalid", "索引结构、来源或向量完整性校验失败。") from exc


def _check_inputs(index: KnowledgeIndex, expected: IndexInputs) -> None:
    actual = index.inputs
    if (actual.dataset_version, actual.product_id, actual.catalog_sha256) != (expected.dataset_version, expected.product_id, expected.catalog_sha256):
        raise KnowledgeError("index_catalog_mismatch", "索引与当前目录不一致；同步资料并重建。")
    if actual.embedding != expected.embedding:
        raise KnowledgeError("index_embedding_mismatch", "模型、维度、文件或执行配置已变化；须重建索引。")
    if actual.chunking != expected.chunking:
        raise KnowledgeError("index_chunking_mismatch", "分块策略已变化；须重建索引。")
    if actual.manifest_sha256 != expected.manifest_sha256 or actual.documents != expected.documents:
        raise KnowledgeError("index_corpus_mismatch", "正文或资料清单内容已变化，即使版本字符串相同也须重建。")
    if actual.sources != expected.sources:
        raise KnowledgeError("index_source_mismatch", "Source 原文、身份、位置或受控关联与当前资料不一致。")


def load_index(client: LocalEmbeddingClient, *, index_path: Path = DEFAULT_INDEX_PATH, corpus_dir: Path = DEFAULT_CORPUS_DIR, policy: ChunkingPolicy = DEFAULT_POLICY) -> KnowledgeIndex:
    """Validate current facts/configuration, then load; never embeds or rebuilds."""
    expected = prepare_corpus(corpus_dir, policy=policy).inputs(_profile(client))
    index = _read_index(Path(index_path))
    if index.scope != "w04_local_index":
        raise KnowledgeError("index_scope_invalid", "隔离负面实验索引不能作为正式知识索引加载。")
    _check_inputs(index, expected)
    return index


def build_index(client: LocalEmbeddingClient, *, index_path: Path = DEFAULT_INDEX_PATH, corpus_dir: Path = DEFAULT_CORPUS_DIR, policy: ChunkingPolicy = DEFAULT_POLICY) -> KnowledgeIndex:
    """Build using real client; publish only a complete, reloaded index."""
    prepared = prepare_corpus(corpus_dir, policy=policy)
    inputs = prepared.inputs(_profile(client))
    for source in inputs.sources:
        if client.count_tokens(source.embedding_text()) > inputs.embedding.max_tokens:
            raise KnowledgeError("section_too_long", f"{source.relative_path}:{source.line_start} 超过实际模型 token 上限；未截断正文。")
    try:
        vectors = client.embed_documents([s.embedding_text() for s in inputs.sources])
        index = KnowledgeIndex(
            build_id=uuid4().hex, built_at=datetime.now(timezone.utc).isoformat(),
            inputs_sha256=model_digest(inputs), inputs=inputs, vectors=vectors,
        )
    except Exception as exc:
        raise KnowledgeError("embedding_build_failed", f"向量构建未完成（{type(exc).__name__}）；没有发布新索引。") from exc
    # Catch source/catalog/config edits made while the batch was running.
    current = prepare_corpus(corpus_dir, policy=policy).inputs(_profile(client))
    _check_inputs(index, current)
    envelope = IndexFile(payload_sha256=model_digest(index), payload=index)
    destination = Path(index_path).resolve()
    temporary = None
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=destination.parent, prefix=destination.name + ".", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(envelope.model_dump_json(indent=2))
            stream.flush()
            os.fsync(stream.fileno())
        checked = _read_index(temporary)
        _check_inputs(checked, current)
        temporary.replace(destination)
        return checked
    except (OSError, ValueError) as exc:
        raise KnowledgeError("index_write_failed", "索引保存未完成；原索引未被替换。") from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
