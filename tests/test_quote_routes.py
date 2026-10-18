"""Production Quote HTTP contracts over private real SQLite databases.

No live user service is replaced and no model call occurs. All request bodies
exercise the production routes: there is no test-only fact-package endpoint.
"""

import asyncio
from concurrent.futures import ThreadPoolExecutor
import threading

from fastapi.testclient import TestClient
import pytest

import app.main as main
from app.agents.orchestrator import Orchestrator
from app.config import Settings
from app.db.session import connect_saas, init_saas_db
from app.services.conversation_store import ConversationStore
from test_proposals import seed


@pytest.fixture
def isolated_app(tmp_path, monkeypatch):
    settings = Settings(_env_file=None, deepseek_api_key="controlled",
        saas_database_url=f"sqlite:///{tmp_path / 'quotes-isolated-http.db'}")
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


def count_quotes(engine):
    with connect_saas(engine.settings) as db:
        return db.execute("SELECT COUNT(*) FROM quotes").fetchone()[0]


def test_submit_repeat_resolve_and_number_read_share_real_receipt(isolated_app, monkeypatch):
    client, engine, _ = isolated_app
    sid, runtime, proposal = asyncio.run(seed(engine, monkeypatch, sso=True))
    assert count_quotes(engine) == 0
    response = client.post(f"/sessions/{sid}/quotes", json={"action_ref": proposal.action_ref})
    assert response.status_code == 200
    saved = response.json()
    assert saved["status"] == "saved" and saved["quote"]["snapshot"] == proposal.model_dump(mode="json")
    assert saved["quote"]["snapshot"]["pricing"]["base_price_minor"] == 11_520_000
    assert saved["quote"]["saved_at"] and runtime.proposal is None
    repeated = client.post(f"/sessions/{sid}/quotes", json={"action_ref": proposal.action_ref})
    checked = client.post(f"/sessions/{sid}/quote-actions/resolve", json={"action_ref": proposal.action_ref})
    assert repeated.status_code == checked.status_code == 200
    assert repeated.json()["quote"] == checked.json()["quote"] == saved["quote"]
    read = client.get(f"/quotes/{saved['quote']['quote_id']}")
    assert read.status_code == 200 and read.json() == saved["quote"]
    assert count_quotes(engine) == 1


@pytest.mark.parametrize("suffix", ["quotes", "quote-actions/resolve"])
@pytest.mark.parametrize("extra", [{"confirmed": True}, {"amount": 1}, {"base_price_minor": 1},
    {"snapshot": {"claimed": "server"}}, {"session_id": "other"}, {"quote_id": "Q-" + "0" * 32}])
def test_request_cannot_upload_confirmation_snapshot_or_amount(isolated_app, suffix, extra):
    client, engine, _ = isolated_app
    sid, runtime, proposal = asyncio.run(seed(engine))
    response = client.post(f"/sessions/{sid}/{suffix}", json={"action_ref": proposal.action_ref, **extra})
    assert response.status_code == 422 and runtime.proposal is proposal
    assert count_quotes(engine) == 0


@pytest.mark.parametrize("suffix", ["quotes", "quote-actions/resolve"])
@pytest.mark.parametrize("body", [{}, {"action_ref": "short"}, {"action_ref": "伪造" * 20}, {"action_ref": 123}])
def test_malformed_action_contract_is_rejected_without_writes(isolated_app, suffix, body):
    client, engine, _ = isolated_app
    response = client.post(f"/sessions/absent/{suffix}", json=body)
    assert response.status_code == 422 and count_quotes(engine) == 0


def test_fake_and_cross_session_actions_cannot_create_or_expose_owned_result(isolated_app):
    client, engine, store = isolated_app
    sid, _, proposal = asyncio.run(seed(engine))
    other = store.ensure_conversation(None, "other isolated chat")
    fake = client.post(f"/sessions/{sid}/quotes", json={"action_ref": "f" * 48})
    assert fake.status_code == 200 and fake.json()["status"] == "not_saved"
    assert count_quotes(engine) == 0
    saved = client.post(f"/sessions/{sid}/quotes", json={"action_ref": proposal.action_ref}).json()["quote"]
    for suffix in ("quotes", "quote-actions/resolve"):
        rejected = client.post(f"/sessions/{other}/{suffix}", json={"action_ref": proposal.action_ref})
        assert rejected.status_code == 200 and rejected.json()["status"] == "not_saved"
        assert rejected.json()["code"] == "action_not_owned" and rejected.json()["quote"] is None
    assert client.get(f"/quotes/{saved['quote_id']}").json() == saved
    assert count_quotes(engine) == 1


def test_resolve_non_save_closes_original_permission_before_late_http_submit(isolated_app):
    client, engine, _ = isolated_app
    sid, runtime, proposal = asyncio.run(seed(engine))
    resolved = client.post(f"/sessions/{sid}/quote-actions/resolve", json={"action_ref": proposal.action_ref})
    assert resolved.status_code == 200 and resolved.json()["status"] == "not_saved"
    assert resolved.json()["quote"] is None and runtime.proposal is None
    late = client.post(f"/sessions/{sid}/quotes", json={"action_ref": proposal.action_ref})
    assert late.status_code == 200 and late.json()["status"] == "not_saved"
    assert count_quotes(engine) == 0


def test_action_read_failure_returns_unknown_503_without_fake_receipt(isolated_app, monkeypatch):
    client, engine, _ = isolated_app
    sid, _, proposal = asyncio.run(seed(engine))
    def fail_read(*args):
        raise OSError("controlled isolated read unavailable")
    monkeypatch.setattr(engine.quotes, "find_action", fail_read)
    for suffix in ("quotes", "quote-actions/resolve"):
        response = client.post(f"/sessions/{sid}/{suffix}", json={"action_ref": proposal.action_ref})
        assert response.status_code == 503
        result = response.json()
        assert result["status"] == "unknown" and result["quote"] is None
        assert "待核对" in result["message"]
    assert count_quotes(engine) == 0


def test_inflight_http_check_is_unknown_until_original_transaction_finishes(isolated_app, monkeypatch):
    client, engine, _ = isolated_app
    sid, _, proposal = asyncio.run(seed(engine))
    entered, release = threading.Event(), threading.Event()
    def pause_before_commit(db, value):
        entered.set()
        assert release.wait(10), "isolated transaction barrier not released"
    monkeypatch.setattr(engine.quotes, "_before_commit", pause_before_commit)
    with ThreadPoolExecutor(max_workers=1) as pool:
        submitting = pool.submit(client.post, f"/sessions/{sid}/quotes", json={"action_ref": proposal.action_ref})
        try:
            assert entered.wait(5), "isolated transaction did not begin"
            checking = client.post(f"/sessions/{sid}/quote-actions/resolve", json={"action_ref": proposal.action_ref})
            assert checking.status_code == 503 and checking.json()["status"] == "unknown"
            assert checking.json()["code"] == "action_in_flight" and count_quotes(engine) == 0
        finally:
            release.set()
        saved = submitting.result(timeout=5)
    checked = client.post(f"/sessions/{sid}/quote-actions/resolve", json={"action_ref": proposal.action_ref})
    assert saved.status_code == checked.status_code == 200
    assert saved.json()["quote"] == checked.json()["quote"] and count_quotes(engine) == 1


def test_http_transaction_rollback_returns_definitive_failure_without_receipt(isolated_app, monkeypatch):
    client, engine, _ = isolated_app
    sid, _, proposal = asyncio.run(seed(engine))
    def fault_inside_quote_transaction(db, value):
        assert db.in_transaction and db.execute("SELECT COUNT(*) FROM quotes").fetchone()[0] == 1
        raise RuntimeError("controlled Quote transaction fault")
    monkeypatch.setattr(engine.quotes, "_before_commit", fault_inside_quote_transaction)
    response = client.post(f"/sessions/{sid}/quotes", json={"action_ref": proposal.action_ref})
    assert response.status_code == 200
    assert response.json()["status"] == "not_saved" and response.json()["quote"] is None
    assert response.json()["code"] == "quote_transaction_rolled_back" and count_quotes(engine) == 0


@pytest.mark.parametrize("quote_id,status,code", [("bad-id", 400, "invalid_quote_id"),
    ("Q-" + "0" * 32, 404, "quote_not_found")])
def test_number_read_distinguishes_invalid_and_absent_without_writes(isolated_app, quote_id, status, code):
    client, engine, _ = isolated_app
    response = client.get(f"/quotes/{quote_id}")
    assert response.status_code == status and response.json()["detail"]["code"] == code
    assert count_quotes(engine) == 0


def test_number_database_read_fault_is_503_not_404(isolated_app, monkeypatch):
    client, engine, _ = isolated_app
    def fail_connection(*args):
        raise OSError("controlled isolated read connection failure")
    monkeypatch.setattr("app.services.quotes.connect_saas", fail_connection)
    response = client.get("/quotes/Q-" + "0" * 32)
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "quote_read_unavailable"
    assert count_quotes(engine) == 0


def test_http_restart_read_survives_absent_chat_runtime_and_pending_is_not_replayed(isolated_app, monkeypatch):
    client, engine, _ = isolated_app
    sid, _, proposal = asyncio.run(seed(engine))
    pending_sid, _, pending = asyncio.run(seed(engine))
    saved = client.post(f"/sessions/{sid}/quotes", json={"action_ref": proposal.action_ref}).json()["quote"]
    restarted = Orchestrator(engine.settings)
    monkeypatch.setattr(main, "orchestrator", restarted)
    assert restarted.work.peek(sid) is None
    assert client.get(f"/quotes/{saved['quote_id']}").json() == saved
    for suffix in ("quotes", "quote-actions/resolve"):
        response = client.post(f"/sessions/{sid}/{suffix}", json={"action_ref": proposal.action_ref})
        assert response.status_code == 200 and response.json()["quote"] == saved
    late = client.post(f"/sessions/{pending_sid}/quotes", json={"action_ref": pending.action_ref})
    assert late.status_code == 200 and late.json()["status"] == "not_saved"
    assert count_quotes(engine) == 1


def test_no_general_fact_write_or_snapshot_edit_route_exists(isolated_app):
    client, engine, _ = isolated_app
    sid, _, proposal = asyncio.run(seed(engine))
    saved = client.post(f"/sessions/{sid}/quotes", json={"action_ref": proposal.action_ref}).json()["quote"]
    # Data management exposes a read-only directory here; a client fact-package write is
    # still forbidden, independently of controlled lifecycle management.
    assert client.post("/quotes", json={"snapshot": {}, "confirmed": True}).status_code == 405
    assert client.patch(f"/quotes/{saved['quote_id']}", json={"amount": 1}).status_code == 405
    assert client.delete(f"/quotes/{saved['quote_id']}").status_code == 405
    assert client.get(f"/quotes/{saved['quote_id']}").json() == saved
    assert count_quotes(engine) == 1
