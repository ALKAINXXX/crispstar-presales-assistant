"""CLI publishing failures; valid old files come from actual CPU inference."""

from pathlib import Path
import subprocess
import sys

import pytest

from app.config import Settings
from app.llm.embedding import LocalEmbeddingClient
from app.services.knowledge import build_index
from scripts import knowledge_index


@pytest.fixture(scope="module")
def real_index_bytes(tmp_path_factory):
    client = LocalEmbeddingClient(Settings(_env_file=None), offline=True)
    target = tmp_path_factory.mktemp("cli-real-index") / "index.json"
    build_index(client, index_path=target)
    return target.read_bytes()


@pytest.mark.parametrize("stall_phase", ["build", "check"])
def test_deadline_kills_stalled_worker_and_preserves_old_index(monkeypatch, tmp_path, real_index_bytes, stall_phase):
    """Replace only one subprocess with a truly stalled process, not vectors."""
    target = tmp_path / "index.json"
    target.write_bytes(real_index_bytes)
    monkeypatch.setattr(sys, "argv", ["knowledge_index", "build", "--offline", "--index-path", str(target), "--timeout-seconds", "10"])
    original_run = subprocess.run
    invoked = []

    def stalled(command, **kwargs):
        if command[5] == stall_phase:
            invoked.append(stall_phase)
            # The parent really kills/waits for this replacement process.
            return original_run([sys.executable, "-c", "import time; time.sleep(30)"], **kwargs)
        return original_run(command, **kwargs)

    monkeypatch.setattr(knowledge_index.subprocess, "run", stalled)
    assert knowledge_index.main() == 1
    assert invoked == [stall_phase]
    assert target.read_bytes() == real_index_bytes
    assert list(tmp_path.iterdir()) == [target]


def test_worker_model_cache_failure_does_not_publish(monkeypatch, tmp_path, real_index_bytes):
    target = tmp_path / "index.json"
    target.write_bytes(real_index_bytes)
    monkeypatch.setenv("EMBEDDING_CACHE_DIR", str(tmp_path / "no-model-cache"))
    monkeypatch.setattr(sys, "argv", ["knowledge_index", "build", "--offline", "--index-path", str(target)])
    assert knowledge_index.main() == 1
    assert target.read_bytes() == real_index_bytes
    assert not list(tmp_path.glob("knowledge-index-build-*"))


def test_final_publish_io_failure_preserves_old_index(monkeypatch, tmp_path, real_index_bytes):
    target = tmp_path / "index.json"
    target.write_bytes(real_index_bytes)
    original_replace = Path.replace

    def failed_replace(path, destination):
        if Path(destination) == target:
            raise PermissionError("controlled final rename failure")
        return original_replace(path, destination)

    monkeypatch.setattr(Path, "replace", failed_replace)
    monkeypatch.setattr(sys, "argv", ["knowledge_index", "build", "--offline", "--index-path", str(target)])
    assert knowledge_index.main() == 1
    assert target.read_bytes() == real_index_bytes
    assert list(tmp_path.iterdir()) == [target]
