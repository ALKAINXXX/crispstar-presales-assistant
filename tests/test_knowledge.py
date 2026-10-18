"""Knowledge business and persistence checks using the real cached CPU model.

The normal build fixture never substitutes inference. Controlled inference
failures below replace only the output/failure being tested; they do not count
as model-success evidence. Every mutation is confined to temporary copies.
"""

from hashlib import sha256
import json
from pathlib import Path
import shutil
import socket

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.llm.embedding import LocalEmbeddingClient
from app.services import catalog as catalog_service
from app.services import knowledge
from app.schemas.knowledge import ChunkingPolicy


@pytest.fixture(scope="session")
def real_client():
    """Missing/corrupt real cache is a failure, never a skipped real test."""
    import httpx

    def network_forbidden(*args, **kwargs):
        pytest.fail("The cached embedding CPU client attempted network access")

    settings = Settings(
        _env_file=None,
        embedding_cache_dir=str(knowledge.PROJECT_ROOT / "data/embedding_models"),
        embedding_threads=2,
    )
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(socket, "create_connection", network_forbidden)
        patch.setattr(socket.socket, "connect", network_forbidden)
        patch.setattr(httpx.Client, "request", network_forbidden)
        patch.setattr(httpx.Client, "send", network_forbidden)
        client = LocalEmbeddingClient(settings, offline=True)
    assert client.configuration()["execution_providers"] == ["CPUExecutionProvider"]
    assert client.configuration()["dimension"] == 512
    return client


@pytest.fixture(scope="session")
def real_build(real_client, tmp_path_factory):
    """One authentic full-corpus build can supply isolated corruption tests."""
    root = tmp_path_factory.mktemp("real-knowledge-index")
    corpus = root / "knowledge"
    shutil.copytree(knowledge.DEFAULT_CORPUS_DIR, corpus)
    index_path = root / "vector_store" / "index.json"
    index = knowledge.build_index(real_client, corpus_dir=corpus, index_path=index_path)
    return {"corpus": corpus, "path": index_path, "index": index}


@pytest.fixture
def isolated_corpus(tmp_path):
    corpus = tmp_path / "knowledge"
    shutil.copytree(knowledge.DEFAULT_CORPUS_DIR, corpus)
    return corpus


@pytest.fixture
def index_copy(real_build, tmp_path):
    path = tmp_path / "index.json"
    path.write_bytes(real_build["path"].read_bytes())
    return path


def assert_failure(code, action):
    with pytest.raises(knowledge.KnowledgeError) as caught:
        action()
    assert caught.value.code == code


def alter_manifest(corpus, change):
    path = corpus / "manifest.json"
    value = json.loads(path.read_text(encoding="utf-8"))
    change(value)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def alter_index(path, change, *, seal=True):
    """Resealing isolates source/schema checks from the payload checksum check."""
    envelope = json.loads(path.read_text(encoding="utf-8"))
    change(envelope["payload"])
    if seal:
        payload = envelope["payload"]
        payload["inputs_sha256"] = sha256(
            knowledge.canonical_json(payload["inputs"]).encode("utf-8")
        ).hexdigest()
        envelope["payload_sha256"] = sha256(
            knowledge.canonical_json(payload).encode("utf-8")
        ).hexdigest()
    path.write_text(json.dumps(envelope, ensure_ascii=False), encoding="utf-8")


def test_real_build_and_reload_without_inference(real_client, real_build, monkeypatch):
    index = real_build["index"]
    assert len(index.inputs.documents) == 6
    assert len(index.inputs.sources) == len(index.vectors) == 24
    assert index.inputs.embedding.dimension == 512
    assert all(len(row) == 512 for row in index.vectors)
    assert len(real_build["path"].read_bytes()) > 0

    def must_not_embed(*args, **kwargs):
        pytest.fail("Loading a saved knowledge index must not embed or silently rebuild")

    monkeypatch.setattr(real_client, "embed_documents", must_not_embed)
    reloaded = knowledge.load_index(
        real_client, index_path=real_build["path"], corpus_dir=real_build["corpus"]
    )
    assert reloaded == index
    assert reloaded.vectors == index.vectors


def test_sources_are_immutable_original_line_slices(real_build):
    index = real_build["index"]
    for source in index.inputs.sources:
        lines = (real_build["corpus"] / source.relative_path).read_text(encoding="utf-8").splitlines()
        assert "\n".join(lines[source.line_start - 1:source.line_end]) == source.raw_text
        assert source.raw_text.startswith("## " + source.section_title + "\n")
        assert source.embedding_text() == source.document_title + "\n" + source.raw_text
        assert source.text_sha256 == sha256(source.raw_text.encode("utf-8")).hexdigest()
        assert source.document_sha256 == sha256((real_build["corpus"] / source.relative_path).read_bytes()).hexdigest()
        assert source.relative_path in {item.file for item in index.inputs.documents}
        assert isinstance(source.plan_ids, tuple)
        with pytest.raises(ValidationError):
            source.raw_text = "rewritten evidence"


def test_reprepare_preserves_stable_source_identity(real_build):
    first = knowledge.prepare_corpus(real_build["corpus"])
    second = knowledge.prepare_corpus(real_build["corpus"])
    assert first == second
    assert first.sources == real_build["index"].inputs.sources
    assert len({source.source_id for source in first.sources}) == 24
    assert first.body_han_characters <= 8000
    for source in first.sources:
        assert source.source_id == (
            f"{first.manifest.version}/{source.document_id}/{source.section_id}/{source.text_sha256[:16]}"
        )


def test_sso_condition_statement_and_business_link_survive_real_build(real_build):
    catalog = catalog_service.get_catalog()
    condition = catalog.conditions[0]
    matching = [source for source in real_build["index"].inputs.sources if condition.condition_id in source.condition_ids]
    assert len(matching) == 1
    source = matching[0]
    assert source.plan_ids == ("business",)
    assert source.feature_ids == (condition.feature_id,)
    assert condition.statement in source.raw_text
    assert next(plan.name for plan in catalog.plans if plan.plan_id == "business") in source.raw_text
    assert "情况未知或答复有歧义时，前提待确认" in source.raw_text
    assert "未来计划准备不能当作当前具备" in source.raw_text
    assert "助手不连接客户身份系统验证真实环境" in source.raw_text


def test_same_version_body_edit_requires_real_rebuild_then_loads(real_client, real_build, isolated_corpus, index_copy, monkeypatch):
    path = isolated_corpus / "01_overview.md"
    path.write_text(path.read_text(encoding="utf-8").replace("产品身份与资料用途\n", "产品身份与资料用途\n本节有新补充。\n", 1), encoding="utf-8")
    assert_failure("index_corpus_mismatch", lambda: knowledge.load_index(real_client, corpus_dir=isolated_corpus, index_path=index_copy))
    rebuilt = knowledge.build_index(real_client, corpus_dir=isolated_corpus, index_path=index_copy)
    original = real_build["index"]
    assert rebuilt.inputs.dataset_version == original.inputs.dataset_version
    assert rebuilt.inputs.embedding == original.inputs.embedding
    assert rebuilt.build_id != original.build_id
    assert rebuilt.inputs.sources[0].source_id != original.inputs.sources[0].source_id
    assert rebuilt.inputs.sources[1].source_id == original.inputs.sources[1].source_id
    monkeypatch.setattr(real_client, "embed_documents", lambda *args: pytest.fail("Rebuilt index reload must not infer"))
    assert knowledge.load_index(real_client, corpus_dir=isolated_corpus, index_path=index_copy) == rebuilt


def test_manifest_byte_change_rejects_saved_index(real_client, isolated_corpus, index_copy):
    path = isolated_corpus / "manifest.json"
    path.write_bytes(path.read_bytes() + b"\n")
    assert_failure("index_corpus_mismatch", lambda: knowledge.load_index(real_client, corpus_dir=isolated_corpus, index_path=index_copy))


@pytest.mark.parametrize("field,value", [("version", "other-version"), ("product_id", "other-product")])
def test_manifest_must_match_catalog(isolated_corpus, field, value):
    alter_manifest(isolated_corpus, lambda data: data.__setitem__(field, value))
    assert_failure("corpus_catalog_mismatch", lambda: knowledge.prepare_corpus(isolated_corpus))


@pytest.mark.parametrize("association", ["plan_ids", "feature_ids", "condition_ids"])
def test_manifest_cannot_reference_identity_outside_catalog(isolated_corpus, association):
    alter_manifest(isolated_corpus, lambda data: data["documents"][0]["sections"][0].__setitem__(association, ["unregistered"]))
    assert_failure("source_association_invalid", lambda: knowledge.prepare_corpus(isolated_corpus))


def test_missing_manifest(isolated_corpus):
    (isolated_corpus / "manifest.json").unlink()
    assert_failure("corpus_unavailable", lambda: knowledge.prepare_corpus(isolated_corpus))


def test_missing_declared_document(isolated_corpus):
    (isolated_corpus / "03_api_webhook.md").unlink()
    assert_failure("corpus_invalid", lambda: knowledge.prepare_corpus(isolated_corpus))


def test_unlisted_document_is_not_silently_indexed(isolated_corpus):
    (isolated_corpus / "extra.md").write_text("# unapproved product claims", encoding="utf-8")
    assert_failure("corpus_invalid", lambda: knowledge.prepare_corpus(isolated_corpus))


def test_duplicate_document_identity(isolated_corpus):
    alter_manifest(isolated_corpus, lambda data: data["documents"][1].__setitem__("document_id", data["documents"][0]["document_id"]))
    assert_failure("corpus_invalid", lambda: knowledge.prepare_corpus(isolated_corpus))


def test_condition_source_cannot_lose_controlled_statement(isolated_corpus):
    condition = catalog_service.get_catalog().conditions[0]
    path = isolated_corpus / "04_saml_sso.md"
    original = path.read_text(encoding="utf-8")
    assert condition.statement in original
    path.write_text(original.replace(condition.statement, "使用前请另行了解。"), encoding="utf-8")
    assert_failure("condition_source_invalid", lambda: knowledge.prepare_corpus(isolated_corpus))


def test_condition_source_cannot_lose_business_association(isolated_corpus):
    alter_manifest(isolated_corpus, lambda data: data["documents"][3]["sections"][0].__setitem__("plan_ids", ["team"]))
    assert_failure("condition_source_invalid", lambda: knowledge.prepare_corpus(isolated_corpus))


def test_condition_cannot_be_duplicated_between_chunks(isolated_corpus):
    alter_manifest(isolated_corpus, lambda data: data["documents"][3]["sections"][1].__setitem__("condition_ids", ["saml_sso_compatible_idp"]))
    assert_failure("condition_source_invalid", lambda: knowledge.prepare_corpus(isolated_corpus))


@pytest.mark.parametrize("mode", ["wrong_title", "unassigned_text", "nested_heading", "overlong_section", "invalid_utf8"])
def test_document_structure_does_not_silently_drop_text(isolated_corpus, mode):
    path = isolated_corpus / "01_overview.md"
    original = path.read_text(encoding="utf-8")
    if mode == "invalid_utf8":
        path.write_bytes(b"\xff\xfe")
    elif mode == "wrong_title":
        path.write_text(original.replace("# ", "# changed ", 1), encoding="utf-8")
    elif mode == "unassigned_text":
        path.write_text(original.replace("\n\n", "\n遗漏的独立正文\n\n", 1), encoding="utf-8")
    elif mode == "nested_heading":
        path.write_text(original + "\n### 隐藏子节\n正文\n", encoding="utf-8")
    else:
        path.write_text(original + "\n" + "前提不可丢失" * 100, encoding="utf-8")
    expected = "section_too_long" if mode == "overlong_section" else "corpus_invalid"
    assert_failure(expected, lambda: knowledge.prepare_corpus(isolated_corpus))


def test_catalog_cache_and_disk_difference_requires_restart(monkeypatch, tmp_path):
    cached = catalog_service.get_catalog()
    path = tmp_path / "catalog.json"
    data = cached.model_dump(mode="json")
    data["plans"][0]["unit_price_minor"] += 1
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(catalog_service, "DEFAULT_CATALOG_PATH", path)
    monkeypatch.setattr(catalog_service, "get_catalog", lambda: cached)
    assert_failure("catalog_restart_required", knowledge.prepare_corpus)
    assert catalog_service.load_catalog(path).plans[0].unit_price_minor == cached.plans[0].unit_price_minor + 1


def test_consistent_catalog_change_still_rejects_old_index(real_client, index_copy, monkeypatch, tmp_path):
    data = catalog_service.get_catalog().model_dump(mode="json")
    data["plans"][0]["unit_price_minor"] += 1
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    updated = catalog_service.load_catalog(path)
    monkeypatch.setattr(catalog_service, "DEFAULT_CATALOG_PATH", path)
    monkeypatch.setattr(catalog_service, "get_catalog", lambda: updated)
    assert_failure("index_catalog_mismatch", lambda: knowledge.load_index(real_client, index_path=index_copy))


@pytest.mark.parametrize("changed", ["dimension", "revision", "threads", "query_prefix", "file_sha256"])
def test_embedding_representation_change_requires_rebuild(real_client, index_copy, monkeypatch, changed):
    configuration = real_client.configuration()
    if changed == "dimension":
        configuration[changed] += 1
    elif changed == "threads":
        configuration[changed] += 1
    elif changed == "file_sha256":
        configuration[changed]["tokenizer.json"] = "0" * 64
    else:
        configuration[changed] = "different"
    monkeypatch.setattr(real_client, "configuration", lambda: configuration)
    assert_failure("index_embedding_mismatch", lambda: knowledge.load_index(real_client, index_path=index_copy))


def test_changed_chunking_budget_requires_rebuild(real_client, index_copy):
    assert_failure("index_chunking_mismatch", lambda: knowledge.load_index(real_client, index_path=index_copy, policy=ChunkingPolicy(max_embedding_chars=400)))


def test_unsealed_payload_edit_is_corruption(real_client, index_copy):
    alter_index(index_copy, lambda data: data.__setitem__("build_id", "tampered"), seal=False)
    assert_failure("index_corrupt", lambda: knowledge.load_index(real_client, index_path=index_copy))


@pytest.mark.parametrize("mode", ["dimension", "count", "normalization"])
def test_bad_saved_vectors_are_rejected(real_client, index_copy, mode):
    def change(payload):
        if mode == "dimension":
            payload["vectors"][0].pop()
        elif mode == "count":
            payload["vectors"].pop()
        else:
            payload["vectors"][0] = [0.0] * 512
    alter_index(index_copy, change)
    assert_failure("index_invalid", lambda: knowledge.load_index(real_client, index_path=index_copy))


@pytest.mark.parametrize("field,value", [("line_start", 4), ("relative_path", "fake.md"), ("plan_ids", ["business"])])
def test_resealed_false_source_cannot_pass_original_location_check(real_client, index_copy, field, value):
    alter_index(index_copy, lambda data: data["inputs"]["sources"][0].__setitem__(field, value))
    assert_failure("index_source_mismatch", lambda: knowledge.load_index(real_client, index_path=index_copy))


def test_missing_index_is_not_implicitly_rebuilt(real_client, tmp_path, monkeypatch):
    monkeypatch.setattr(real_client, "embed_documents", lambda *args: pytest.fail("Missing index was silently rebuilt"))
    assert_failure("index_unavailable", lambda: knowledge.load_index(real_client, index_path=tmp_path / "missing.json"))


def test_truncated_index_is_rejected(real_client, index_copy):
    index_copy.write_bytes(index_copy.read_bytes()[:100])
    assert_failure("index_invalid", lambda: knowledge.load_index(real_client, index_path=index_copy))


@pytest.mark.parametrize("failure", ["timeout", "bad_vector"])
def test_inference_failure_keeps_previous_index(real_client, real_build, isolated_corpus, index_copy, monkeypatch, failure):
    """Substitute only inference failure/output; success stays the real fixture."""
    old_bytes = index_copy.read_bytes()
    if failure == "timeout":
        def controlled_failure(*args, **kwargs):
            raise TimeoutError("controlled local inference timeout")
    else:
        def controlled_failure(*args, **kwargs):
            vectors = list(real_build["index"].vectors)
            vectors[0] = (0.0,) * 512
            return tuple(vectors)
    monkeypatch.setattr(real_client, "embed_documents", controlled_failure)
    assert_failure("embedding_build_failed", lambda: knowledge.build_index(real_client, corpus_dir=isolated_corpus, index_path=index_copy))
    assert index_copy.read_bytes() == old_bytes
    assert list(index_copy.parent.glob(index_copy.name + ".*.tmp")) == []


def test_replace_failure_keeps_previous_index_after_real_inference(real_client, isolated_corpus, index_copy, monkeypatch):
    """Real model inference; only final filesystem publication is substituted."""
    old_bytes = index_copy.read_bytes()
    original_replace = Path.replace

    def publication_failure(path, target):
        if Path(target) == index_copy:
            raise PermissionError("controlled publication failure")
        return original_replace(path, target)

    monkeypatch.setattr(Path, "replace", publication_failure)
    assert_failure("index_write_failed", lambda: knowledge.build_index(real_client, corpus_dir=isolated_corpus, index_path=index_copy))
    assert index_copy.read_bytes() == old_bytes
    assert list(index_copy.parent.glob(index_copy.name + ".*.tmp")) == []


def test_edit_during_real_inference_does_not_publish_stale_index(real_client, isolated_corpus, index_copy, monkeypatch):
    """Run genuine inference, then emulate a source edit before publication."""
    old_bytes = index_copy.read_bytes()
    original_embed = real_client.embed_documents

    def embed_then_edit(texts):
        rows = original_embed(texts)
        path = isolated_corpus / "01_overview.md"
        path.write_bytes(path.read_bytes() + b"\n")
        return rows

    monkeypatch.setattr(real_client, "embed_documents", embed_then_edit)
    assert_failure("index_corpus_mismatch", lambda: knowledge.build_index(real_client, corpus_dir=isolated_corpus, index_path=index_copy))
    assert index_copy.read_bytes() == old_bytes
    assert list(index_copy.parent.glob(index_copy.name + ".*.tmp")) == []
