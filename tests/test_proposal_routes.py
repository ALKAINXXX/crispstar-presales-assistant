"""Production lifecycle HTTP contracts; no model calls or Quote writes."""

from fastapi.testclient import TestClient
import pytest

import app.main as main
from app.agents.orchestrator import Orchestrator
from app.config import Settings
from app.db.session import init_saas_db
from app.services.conversation_store import ConversationStore


@pytest.fixture
def isolated_app(tmp_path, monkeypatch):
    settings = Settings(_env_file=None,
        saas_database_url=f"sqlite:///{tmp_path / 'proposal-route-chat.db'}")
    init_saas_db(settings)
    engine = Orchestrator(settings)
    store = ConversationStore(settings)
    monkeypatch.setattr(main, "settings", settings)
    monkeypatch.setattr(main, "orchestrator", engine)
    monkeypatch.setattr(main, "conversations", store)
    with TestClient(main.app) as client:
        scope = engine.data.info()
        client.headers.update({"X-Data-Epoch": scope["epoch"],
                               "X-Data-Location": scope["location_id"]})
        yield client, engine, store


def test_history_reentry_closes_permission_and_preserves_text(isolated_app):
    client, engine, store = isolated_app
    sid = store.ensure_conversation(None, "历史")
    store.add_turn(sid, "原问题", "原正式回答")
    runtime = engine.work.get(sid)
    # Only a sentinel pending permission is injected; this is NOT evidence
    # of real Proposal formation. Route invalidation must clear any pending.
    runtime.proposal = object()
    before = runtime.interaction_revision
    response = client.get(f"/sessions/{sid}")
    assert response.status_code == 200
    assert runtime.proposal is None and runtime.interaction_revision > before
    assert [m["content"] for m in response.json()["messages"]] == ["原问题", "原正式回答"]
    assert "proposal" not in response.json()


def test_health_distinguishes_preview_pause_from_connected_service(isolated_app, monkeypatch):
    client, _, _ = isolated_app
    monkeypatch.setattr(main.app.state, "preview_chat_availability", None, raising=False)
    assert "chat_availability" not in client.get("/health").json()
    paused = {"available":False,"message":"体验额度已用完","remaining_turns":1,"remaining_calls":0}
    monkeypatch.setattr(main.app.state, "preview_chat_availability", lambda: paused)
    result = client.get("/health").json()
    assert result["status"] == "ok" and result["chat_availability"] == paused
    assert client.get("/sessions").status_code == 200


@pytest.mark.parametrize("event", ["refresh", "switch", "history_reenter", "new_chat", "view_exit"])
def test_session_lifecycle_events_revoke_without_creating_state(isolated_app, event):
    client, engine, _ = isolated_app
    runtime = engine.work.get("known-session")
    runtime.proposal = object()
    response = client.post("/sessions/known-session/proposal/invalidate", json={"event": event})
    assert response.status_code == 200 and runtime.proposal is None
    assert response.json()["save_available"] is False
    response = client.post("/sessions/absent/proposal/invalidate", json={"event": event})
    assert response.status_code == 200
    assert engine.work.peek("absent") is None


@pytest.mark.parametrize("extra", [{"confirmed": True}, {"base_price_minor": 1}, {"action_ref": "fake"}])
def test_lifecycle_route_cannot_authorize_or_upload_facts(isolated_app, extra):
    client, engine, _ = isolated_app
    runtime = engine.work.get("sid")
    permission = object()
    runtime.proposal = permission
    response = client.post("/sessions/sid/proposal/invalidate", json={"event": "refresh", **extra})
    assert response.status_code == 422 and runtime.proposal is permission


def test_page_exit_before_session_exists_revokes_view_without_chat_or_save(isolated_app):
    client, engine, store = isolated_app
    view = "page-first-unknown-session"
    response = client.post("/proposal/view/invalidate", json={"view_id": view, "event": "view_exit"})
    assert response.status_code == 200
    assert response.json()["save_available"] is False
    assert store.list_conversations() == []
    routes = {route.path for route in main.app.routes}
    # Guarded business APIs are tested separately; this lifecycle event neither
    # creates a Quote nor exposes a client-fact/confirmed write path.
    assert "/sessions/{session_id}/quotes" in routes
    assert not any("confirm" in path for path in routes)
    assert client.post("/proposal/save", json={"confirmed": True, "amount": 1}).status_code == 404
