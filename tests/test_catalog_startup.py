import json

import pytest
from fastapi.testclient import TestClient

from app import main as main_module
from app.config import Settings
from app.services.catalog import DEFAULT_CATALOG_PATH, get_catalog, load_catalog


def test_actual_lifespan_loads_catalog_and_starts_with_isolated_database(tmp_path, monkeypatch):
    saas_path = tmp_path / "startup_saas.db"
    settings = Settings(
        _env_file=None,
        saas_database_url=f"sqlite:///{saas_path}",
    )
    monkeypatch.setattr(main_module, "settings", settings)
    with TestClient(main_module.app) as client:
        assert client.get("/health").json() == {"status": "ok", "env": "development"}
        assert client.get("/").status_code == 200
        assert main_module.app.state.catalog == get_catalog()
    assert saas_path.is_file()


def test_invalid_catalog_stops_startup_before_database_initialization(tmp_path, monkeypatch):
    raw = json.loads(DEFAULT_CATALOG_PATH.read_text(encoding="utf-8"))
    raw["plans"][2]["features"][5].pop("condition_id")
    path = tmp_path / "bad_catalog.json"
    path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    database_calls = []
    monkeypatch.setattr(main_module, "get_catalog", lambda: load_catalog(path))
    monkeypatch.setattr(main_module, "init_saas_db", lambda settings: database_calls.append(settings))
    with pytest.raises(ValueError, match="Supported SAML SSO must reference"):
        with TestClient(main_module.app):
            pass
    assert database_calls == []
