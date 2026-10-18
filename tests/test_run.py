"""Application startup configuration without a model call or live server."""

from pathlib import Path

import pytest

from app.config import Settings, get_settings
import run as app_runner


@pytest.fixture
def entry_environment(monkeypatch):
    # Configure writes this variable directly; registering the original through
    # monkeypatch also restores it when an assertion fails.
    monkeypatch.setenv("SAAS_DATABASE_URL", "sqlite:///controlled-original.db")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def port_probe(monkeypatch):
    probes = []

    class Probe:
        def __init__(self):
            self.bound = None
            self.closed = False
            probes.append(self)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.closed = True

        def bind(self, address):
            self.bound = address

    monkeypatch.setattr(app_runner.socket, "socket", Probe)
    return probes


def test_configure_selects_retained_database_and_refreshes_cached_settings(
    tmp_path, monkeypatch, entry_environment, port_probe
):
    old_settings = get_settings()
    database = tmp_path / "体验数据" / "saas.sqlite3"
    configured = app_runner.configure(database, 8790)

    assert configured == database.resolve()
    assert get_settings().saas_database_url == "sqlite:///" + database.resolve().as_posix()
    assert get_settings() is not old_settings
    assert port_probe[0].bound == ("127.0.0.1", 8790)
    assert port_probe[0].closed
    # The probe must not initialize a database or create an empty history.
    assert not database.exists()
    assert not database.parent.exists()


@pytest.mark.parametrize("database_name,port", [
    ("discard.txt", 8790),
    ("saas.sqlite3", 1023),
    ("saas.sqlite3", 65536),
])
def test_invalid_entry_configuration_leaves_storage_and_environment_untouched(
    tmp_path, monkeypatch, entry_environment, port_probe, database_name, port
):
    database = tmp_path / database_name
    database.write_bytes(b"existing retained content")

    with pytest.raises(ValueError):
        app_runner.configure(database, port)

    assert get_settings().saas_database_url == "sqlite:///controlled-original.db"
    assert database.read_bytes() == b"existing retained content"
    assert not port_probe


def test_occupied_port_refuses_before_changing_database_configuration(
    tmp_path, monkeypatch, entry_environment
):
    class OccupiedProbe:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def bind(self, address):
            raise OSError("controlled occupied port")

    monkeypatch.setattr(app_runner.socket, "socket", OccupiedProbe)
    database = tmp_path / "owned" / "saas.sqlite3"

    with pytest.raises(OSError, match="occupied port"):
        app_runner.configure(database, 8790)

    assert get_settings().saas_database_url == "sqlite:///controlled-original.db"
    assert not database.parent.exists()


def test_normal_runner_uses_single_production_app_and_preserves_existing_file(
    tmp_path, monkeypatch, capsys
):
    database = tmp_path / "saas.sqlite3"
    database.write_bytes(b"retained history and quotes")
    calls = []
    monkeypatch.setattr(app_runner, "configure", lambda path, port: path.resolve())
    monkeypatch.setattr(app_runner.uvicorn, "run", lambda *args, **kwargs: calls.append((args, kwargs)))

    app_runner.run(database, 8790)

    assert calls == [(("app.main:app",), {
        "host": "127.0.0.1", "port": 8790, "log_level": "info", "workers": 1,
    })]
    assert database.read_bytes() == b"retained history and quotes"
    assert "服务退出不会清理聊天或草案" in capsys.readouterr().out


def test_relative_database_is_anchored_to_project_root(
    tmp_path, monkeypatch, entry_environment, port_probe
):
    project = tmp_path / "project"
    project.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.setattr(app_runner, "ROOT", project)
    monkeypatch.chdir(elsewhere)

    configured = app_runner.configure(Path("data/local.sqlite3"), 8797)

    assert configured == project / "data/local.sqlite3"
    assert not (project / "data").exists()
    assert not (elsewhere / "data").exists()


def test_main_uses_configured_database_when_invoked_from_another_directory(
    tmp_path, monkeypatch, entry_environment
):
    project = tmp_path / "project"
    project.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (project / ".env").write_text("SAAS_DATABASE_URL=sqlite:///data/dotenv.sqlite3\n", encoding="utf-8")
    monkeypatch.setitem(Settings.model_config, "env_file", ".env")
    monkeypatch.setenv("SAAS_DATABASE_URL", "sqlite:///data/configured.sqlite3")
    monkeypatch.setattr(app_runner, "ROOT", project)
    monkeypatch.chdir(elsewhere)
    started = []
    monkeypatch.setattr(app_runner, "run", lambda *args: started.append(args))

    app_runner.main([])

    assert Path.cwd() == project
    assert started == [(project / "data/configured.sqlite3", 8000, None)]
    assert not (project / "data").exists()


def test_main_loads_project_dotenv_without_reading_callers_configuration(
    tmp_path, monkeypatch, entry_environment
):
    project = tmp_path / "project"
    project.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (project / ".env").write_text("SAAS_DATABASE_URL=sqlite:///data/project.sqlite3\n", encoding="utf-8")
    (elsewhere / ".env").write_text("SAAS_DATABASE_URL=sqlite:///data/caller.sqlite3\n", encoding="utf-8")
    monkeypatch.setitem(Settings.model_config, "env_file", ".env")
    monkeypatch.delenv("SAAS_DATABASE_URL", raising=False)
    monkeypatch.setattr(app_runner, "ROOT", project)
    monkeypatch.chdir(elsewhere)
    started = []
    monkeypatch.setattr(app_runner, "run", lambda *args: started.append(args))

    app_runner.main([])

    assert started == [(project / "data/project.sqlite3", 8000, None)]
    assert not (project / "data").exists()
    assert not (elsewhere / "data").exists()


def test_main_preserves_explicit_database_port_and_ui_options(
    tmp_path, monkeypatch, entry_environment
):
    monkeypatch.setattr(app_runner, "ROOT", tmp_path)
    monkeypatch.chdir(tmp_path)
    started = []
    monkeypatch.setattr(app_runner, "run", lambda *args: started.append(args))

    app_runner.main(["--database", "data/retained.sqlite3", "--port", "8797", "--ui-dir", "app/ui"])

    assert started == [(Path("data/retained.sqlite3"), 8797, Path("app/ui"))]
    assert not (tmp_path / "data").exists()


def test_main_default_database_uses_clean_application_path(
    tmp_path, monkeypatch, entry_environment
):
    monkeypatch.setitem(Settings.model_config, "env_file", None)
    monkeypatch.delenv("SAAS_DATABASE_URL", raising=False)
    monkeypatch.setattr(app_runner, "ROOT", tmp_path)
    monkeypatch.chdir(tmp_path)
    started = []
    monkeypatch.setattr(app_runner, "run", lambda *args: started.append(args))

    app_runner.main([])

    assert started == [(tmp_path / "data/saas_assistant.db", 8000, None)]
    assert not (tmp_path / "data").exists()
