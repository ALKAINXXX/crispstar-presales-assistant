"""Startup selection only: no HTTP server, real database or provider opened."""

from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace

import pytest

import run as app_runner


def test_invalid_explicit_ui_fails_before_database_configuration(tmp_path, monkeypatch):
    configured = []
    monkeypatch.setattr(app_runner, "configure", lambda *args: configured.append(args))
    monkeypatch.setattr(app_runner.uvicorn, "run", lambda *args, **kwargs: pytest.fail("Server must not start"))
    with pytest.raises(ValueError, match="index.html"):
        app_runner.run(tmp_path / "must-not-initialize.sqlite3", 54321, tmp_path / "missing-ui")
    assert configured == [] and not (tmp_path / "must-not-initialize.sqlite3").exists()


def test_default_startup_retains_existing_app_string_without_importing_main(tmp_path, monkeypatch):
    captured = []
    monkeypatch.setattr(app_runner, "configure", lambda database, port: database)
    monkeypatch.setattr(app_runner.uvicorn, "run", lambda app, **kwargs: captured.append((app, kwargs)))
    app_runner.run(tmp_path / "synthetic-default.sqlite3", 54321)
    assert captured == [("app.main:app", {"host": "127.0.0.1", "port": 54321, "log_level": "info", "workers": 1})]
    assert not (tmp_path / "synthetic-default.sqlite3").exists()


def test_explicit_ui_changes_only_new_process_static_mount_and_index_root(tmp_path, monkeypatch):
    ui = tmp_path / "delivered-ui"
    (ui / "assets").mkdir(parents=True)
    (ui / "index.html").write_text("<html>delivered synthetic UI</html>", encoding="utf-8")
    (ui / "assets" / "app.js").write_text("// synthetic build", encoding="utf-8")
    original_ui = tmp_path / "untouched-user-ui"
    original_ui.mkdir()
    (original_ui / "index.html").write_text("original", encoding="utf-8")
    route = SimpleNamespace(path="/assets", app="original mount")
    unrelated = SimpleNamespace(path="/chat", app="ordinary chat endpoint")
    main = ModuleType("app.main")
    main.UI_DIR = original_ui
    main.app = SimpleNamespace(routes=[route, unrelated])
    import app
    monkeypatch.setitem(sys.modules, "app.main", main)
    monkeypatch.setattr(app, "main", main, raising=False)
    configured, started = [], []
    monkeypatch.setattr(app_runner, "configure", lambda database, port: configured.append((database, port)) or database)
    monkeypatch.setattr(app_runner.uvicorn, "run", lambda application, **kwargs: started.append(application))
    database = tmp_path / "never-opened.sqlite3"
    app_runner.run(database, 54321, ui)
    assert configured == [(database, 54321)] and started == [main.app]
    assert main.UI_DIR == ui.resolve()
    assert Path(route.app.directory) == ui.resolve() / "assets"
    assert unrelated.app == "ordinary chat endpoint"
    assert (original_ui / "index.html").read_text(encoding="utf-8") == "original"
    assert not database.exists()
