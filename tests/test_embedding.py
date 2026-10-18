"""Failure/configuration checks only. Real model proof is the separate CLI."""

from pathlib import Path
import subprocess
import sys

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.llm.embedding import DIMENSION, EmbeddingError, LocalEmbeddingClient


def test_embedding_threads_environment_text(monkeypatch):
    # Regression for the actual first implementation's rejected ENV value "2".
    monkeypatch.setenv("EMBEDDING_THREADS", "2")
    assert Settings(_env_file=None).embedding_threads == 2
    monkeypatch.setenv("EMBEDDING_THREADS", "0")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_adapter_import_does_not_load_model_stack():
    script = (
        "import sys; import app.llm.embedding; "
        "assert not {'fastembed','onnxruntime','huggingface_hub','numpy'} & sys.modules.keys()"
    )
    subprocess.run([sys.executable, "-c", script], check=True, timeout=10)


def failure_client(rows):
    """Controlled inference output, never used as proof of model success."""
    from types import SimpleNamespace
    client = object.__new__(LocalEmbeddingClient)
    client._tokenizer = SimpleNamespace(encode=lambda text: SimpleNamespace(ids=[1] * len(text)))
    client._model = SimpleNamespace(embed=lambda *args, **kwargs: iter(rows))
    return client


@pytest.mark.parametrize("rows", [
    [], [[1.0]], [[0.0] * DIMENSION],
    [[float("nan")] + [0.0] * (DIMENSION - 1)],
    [[float("inf")] + [0.0] * (DIMENSION - 1)],
    [[2.0] + [0.0] * (DIMENSION - 1)],
    [[1.0] + [0.0] * (DIMENSION - 1)] * 2,
])
def test_invalid_output_is_a_failure(rows):
    with pytest.raises(EmbeddingError):
        failure_client(rows).embed_documents(["test"])


@pytest.mark.parametrize("texts", [None, "text", [], [""], ["  "], [7], ["中" * 513]])
def test_invalid_input_does_not_reach_inference(texts):
    client = failure_client([])
    client._model = None
    with pytest.raises(ValueError):
        client.embed_documents(texts)


def test_lazy_inference_failure_is_not_partial_success():
    def broken(*args, **kwargs):
        yield [1.0] + [0.0] * (DIMENSION - 1)
        raise TimeoutError("controlled inference failure")
    client = failure_client([])
    client._model.embed = broken
    with pytest.raises(EmbeddingError, match="inference failed"):
        client.embed_documents(["first", "second"])


def test_missing_offline_cache_fails_without_network(monkeypatch, tmp_path):
    import httpx
    def forbidden(*args, **kwargs):
        pytest.fail("Offline cache failure must not request the network")
    monkeypatch.setattr(httpx.Client, "request", forbidden)
    monkeypatch.setattr(httpx.Client, "send", forbidden)
    settings = Settings(_env_file=None, embedding_cache_dir=str(tmp_path))
    with pytest.raises(EmbeddingError, match="cached loading failed"):
        LocalEmbeddingClient(settings, offline=True)


def test_checksum_mismatch_stops_before_model_loading(monkeypatch, tmp_path):
    from fastembed import TextEmbedding
    import huggingface_hub
    from app.llm.embedding import MODEL_FILES
    for name in MODEL_FILES:
        (tmp_path / name).write_bytes(b"controlled corrupt artifact")
    monkeypatch.setattr(huggingface_hub, "snapshot_download", lambda **kwargs: str(tmp_path))
    monkeypatch.setattr(TextEmbedding, "__init__", lambda *args, **kwargs: pytest.fail("Corrupt model was loaded"))
    with pytest.raises(EmbeddingError, match="checksum mismatch"):
        LocalEmbeddingClient(Settings(_env_file=None), offline=True)


def test_failed_cli_does_not_overwrite_an_existing_record(tmp_path):
    import os
    output = tmp_path / "sample.json"
    output.write_text("old record", encoding="utf-8")
    env = dict(os.environ, EMBEDDING_CACHE_DIR=str(tmp_path / "missing-cache"))
    completed = subprocess.run(
        [sys.executable, "-X", "utf8", "-m", "scripts.check_embedding", "--offline", "--output", str(output)],
        cwd=Path(__file__).resolve().parents[1], env=env,
        capture_output=True, text=True, encoding="utf-8", timeout=30,
    )
    assert completed.returncode == 1
    assert "cached loading failed" in completed.stderr
    assert output.read_text(encoding="utf-8") == "old record"
