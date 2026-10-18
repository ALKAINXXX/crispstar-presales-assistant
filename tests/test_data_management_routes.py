"""Data-management production routes against private SQLite records only.

These checks exercise real migrations, transactions and stored snapshots. The
model is forbidden except where a controlled response or barrier specifically
tests request ordering; no configured upstream or real user database is used.
"""

import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import sqlite3
import threading
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest
from starlette.requests import Request

import app.main as main
from app.agents.orchestrator import Orchestrator
from app.config import Settings
from app.db.session import connect_saas, init_saas_db, saas_database_path
from app.schemas.chat import ChatRequest
from app.services.conversation_store import ConversationStore
from app.tools.business import BusinessToolRegistry
from test_proposals import call, completion, seed
from controlled_review import answer_review, is_review_request


@pytest.fixture
def isolated_app(tmp_path, monkeypatch):
    unrelated = tmp_path / "untouched-unrelated.db"
    with sqlite3.connect(unrelated) as db:
        db.execute("CREATE TABLE sentinel (value TEXT)")
        db.execute("INSERT INTO sentinel VALUES ('preserved unrelated record')")
    before = unrelated.read_bytes()
    settings = Settings(_env_file=None, deepseek_api_key="controlled",
        saas_database_url=f"sqlite:///{tmp_path / 'management-private-http.sqlite3'}")
    init_saas_db(settings)
    engine = Orchestrator(settings)
    monkeypatch.setattr(main, "settings", settings)
    monkeypatch.setattr(main, "orchestrator", engine)
    monkeypatch.setattr(main, "conversations", engine.conversations)
    class ForbiddenModel:
        def __init__(self, *args, **kwargs):
            pytest.fail("Data management unexpectedly used a model")
    monkeypatch.setattr("app.agents.orchestrator.DeepSeekClient", ForbiddenModel)
    with TestClient(main.app) as client:
        yield client, engine
    assert unrelated.read_bytes() == before


def info(client):
    response = client.get("/data-management")
    assert response.status_code == 200, response.text
    return response.json()


def headers(client, session_id=None):
    state = info(client)
    result = {"X-Data-Epoch": state["epoch"], "X-Data-Location": state["location_id"]}
    if session_id:
        response = client.get(f"/sessions/{session_id}")
        assert response.status_code == 200, response.text
        result["X-Session-Version"] = str(response.json()["session_version"])
    return result


def manage(client, operation, object_type=None, object_id=None, *, ref=None,
           expected_revision=None, epoch=None):
    state = info(client)
    body = {"action_ref": ref or uuid4().hex, "operation": operation,
            "object_type": object_type, "object_id": object_id,
            "expected_revision": state["revision"] if expected_revision is None else expected_revision}
    response = client.post("/data-management/actions", json=body,
        headers={"X-Data-Epoch": epoch or state["epoch"], "X-Data-Location": state["location_id"]})
    return response, body


def completed(client, operation, object_type=None, object_id=None, **kwargs):
    response, body = manage(client, operation, object_type, object_id, **kwargs)
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "completed", response.text
    checked = client.get(f"/data-management/actions/{body['action_ref']}")
    assert checked.status_code == 200
    assert checked.json() == response.json()
    return response.json()


def saved(client, engine, monkeypatch=None, *, sso=False):
    sid, runtime, proposal = asyncio.run(seed(engine, monkeypatch, sso=sso))
    response = client.post(f"/sessions/{sid}/quotes",
        json={"action_ref": proposal.action_ref}, headers=headers(client))
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "saved", response.text
    return sid, runtime, proposal, response.json()["quote"]


def table_counts(engine):
    with connect_saas(engine.settings) as db:
        return {name: db.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
                for name in ("conversations", "conversation_messages", "quotes",
                             "quote_actions", "quote_states", "conversation_tombstones")}


def database_values(engine):
    """Logical stored values, including management receipts and tombstones.

    This does not promise forensic disk erasure of freed SQLite pages.
    """
    with connect_saas(engine.settings) as db:
        tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        return json.dumps({name: [list(row) for row in db.execute(f'SELECT * FROM "{name}"')]
                           for name in tables}, ensure_ascii=False)


def test_actual_location_complete_catalog_and_counts_ignore_browser_references(isolated_app):
    client, engine = isolated_app
    first = saved(client, engine)
    second = saved(client, engine)
    state = info(client)
    assert state["database_path"] == str(saas_database_path(engine.settings).resolve())
    assert state["dataset_id"] and state["epoch"] and state["revision"] >= 0
    catalog = client.get("/quotes")
    assert catalog.status_code == 200
    assert {q["quote_id"] for q in catalog.json()["quotes"]} == {first[3]["quote_id"], second[3]["quote_id"]}
    assert len(client.get("/sessions").json()["sessions"]) == 2
    assert table_counts(engine)["quotes"] == 2


def test_chat_trash_restore_keeps_full_history_and_independent_frozen_quote(isolated_app):
    client, engine = isolated_app
    sid, runtime, proposal, quote = saved(client, engine)
    # Exceed the old 100-message page cap to test complete restoration.
    for n in range(65):
        engine.conversations.add_turn(sid, f"historical question {n}", f"historical answer {n}")
    original = engine.conversations.get_messages(sid, limit=None)
    old_version = client.get(f"/sessions/{sid}").json()["session_version"]
    completed(client, "trash", "chat", sid)
    assert sid not in {r["id"] for r in client.get("/sessions").json()["sessions"]}
    assert client.get(f"/sessions/{sid}").status_code in (404, 410)
    assert runtime.proposal is None
    assert client.get(f"/quotes/{quote['quote_id']}").json()["snapshot"] == quote["snapshot"]
    completed(client, "restore", "chat", sid)
    restored = client.get(f"/sessions/{sid}").json()
    assert restored["messages"] == [m.model_dump() for m in original]
    assert restored["session_version"] > old_version
    assert restored["business_state_available"] is False and restored["current_task"] is None
    assert client.post(f"/sessions/{sid}/quotes", json={"action_ref": proposal.action_ref},
                       headers=headers(client)).json()["quote"]["quote_id"] == quote["quote_id"]


def test_quote_trash_hides_body_preserves_original_success_and_restore_snapshot(isolated_app, monkeypatch):
    client, engine = isolated_app
    sid, _, proposal, quote = saved(client, engine, monkeypatch, sso=True)
    chat_before = engine.conversations.get_messages(sid, limit=None)
    completed(client, "trash", "quote", quote["quote_id"])
    assert client.get("/quotes").json()["quotes"] == []
    read = client.get(f"/quotes/{quote['quote_id']}")
    assert read.status_code in (404, 410) and "snapshot" not in read.text
    for suffix in ("quotes", "quote-actions/resolve"):
        result = client.post(f"/sessions/{sid}/{suffix}", json={"action_ref": proposal.action_ref},
                             headers=headers(client)).json()
        assert result["quote"] is None
        assert "保存成功" in result["message"] and "最近删除" in result["message"]
    assert engine.conversations.get_messages(sid, limit=None) == chat_before
    completed(client, "restore", "quote", quote["quote_id"])
    restored = client.get(f"/quotes/{quote['quote_id']}").json()
    assert restored["quote_id"] == quote["quote_id"] and restored["saved_at"] == quote["saved_at"]
    assert restored["snapshot"] == quote["snapshot"]
    assert table_counts(engine)["quotes"] == 1


def test_direct_quote_read_never_caches_old_success_or_deletion_after_restore(isolated_app):
    client, engine = isolated_app
    sid, runtime, _, quote = saved(client, engine)
    path = f"/quotes/{quote['quote_id']}"
    active = client.get(path)
    assert active.status_code == 200 and active.headers["cache-control"] == "no-store"
    assert active.json() == quote
    completed(client, "trash", "quote", quote["quote_id"])
    deleted = client.get(path)
    assert deleted.status_code == 410 and deleted.headers["cache-control"] == "no-store"
    assert deleted.json()["detail"]["code"] == "quote_in_trash"
    assert "snapshot" not in deleted.text
    completed(client, "restore", "quote", quote["quote_id"])
    restored = client.get(path)
    assert restored.status_code == 200 and restored.headers["cache-control"] == "no-store"
    assert restored.json() == quote
    assert runtime.proposal is None and table_counts(engine)["quotes"] == 1
    completed(client, "trash", "quote", quote["quote_id"])
    completed(client, "purge", "quote", quote["quote_id"])
    purged = client.get(path)
    assert purged.status_code == 410 and purged.headers["cache-control"] == "no-store"
    assert purged.json()["detail"]["code"] == "quote_permanently_deleted"
    assert "snapshot" not in purged.text
    absent = client.get("/quotes/Q-" + "F" * 32)
    assert absent.status_code == 404 and absent.headers["cache-control"] == "no-store"
    assert table_counts(engine)["quotes"] == 0 and table_counts(engine)["quote_actions"] == 1
    stale_scope = client.get(path, headers={"X-Data-Epoch": "stale-generation"})
    assert stale_scope.status_code == 409 and stale_scope.headers["cache-control"] == "no-store"


def test_independent_restore_does_not_restore_other_kind(isolated_app):
    client, engine = isolated_app
    sid, _, _, quote = saved(client, engine)
    completed(client, "trash", "chat", sid)
    completed(client, "trash", "quote", quote["quote_id"])
    completed(client, "restore", "quote", quote["quote_id"])
    assert client.get(f"/sessions/{sid}").status_code in (404, 410)
    assert client.get(f"/quotes/{quote['quote_id']}").status_code == 200
    completed(client, "restore", "chat", sid)
    assert client.get(f"/sessions/{sid}").status_code == 200


def test_permanent_quote_removes_snapshot_preserves_minimal_fact_and_blocks_recreation(isolated_app, monkeypatch):
    client, engine = isolated_app
    sid, _, proposal, quote = saved(client, engine, monkeypatch, sso=True)
    # Purge the originating chat too so a legitimately retained chat copy does
    # not obscure whether Quote/management storage has a hidden body copy.
    completed(client, "trash", "chat", sid)
    completed(client, "purge", "chat", sid)
    completed(client, "trash", "quote", quote["quote_id"])
    completed(client, "purge", "quote", quote["quote_id"])
    values = database_values(engine)
    for secret in (proposal.conditions[0].user_quote, proposal.adopted_sources[0].source.raw_text,
                   "snapshot_json", "base_price_minor", "11520000"):
        assert secret not in values
    assert table_counts(engine)["quotes"] == 0 and table_counts(engine)["quote_actions"] == 1
    for suffix in ("quotes", "quote-actions/resolve"):
        response = client.post(f"/sessions/{sid}/{suffix}", json={"action_ref": proposal.action_ref},
                               headers=headers(client))
        assert response.status_code == 200, response.text
        result = response.json()
        assert result["quote"] is None and "保存成功" in result["message"]
        assert "永久删除" in result["message"]
    assert client.get(f"/quotes/{quote['quote_id']}").status_code in (404, 410)
    assert table_counts(engine)["quotes"] == 0


def test_quote_model_read_boundary_never_reads_deleted_body(isolated_app):
    client, engine = isolated_app
    sid, runtime, _, quote = saved(client, engine)
    registry = BusinessToolRegistry(engine.settings, runtime.state, "查询草案")
    completed(client, "trash", "quote", quote["quote_id"])
    result = asyncio.run(registry.execute("query_quote", {"quote_id": quote["quote_id"]}))
    assert result["ok"] is False and "snapshot" not in json.dumps(result)
    assert "最近删除" in json.dumps(result, ensure_ascii=False)
    completed(client, "purge", "quote", quote["quote_id"])
    result = asyncio.run(registry.execute("query_quote", {"quote_id": quote["quote_id"]}))
    assert result["ok"] is False and "snapshot" not in json.dumps(result)
    assert "永久删除" in json.dumps(result, ensure_ascii=False)


def test_clear_removes_all_current_records_rotates_epoch_and_keeps_only_own_result(isolated_app):
    client, engine = isolated_app
    sid, _, proposal, quote = saved(client, engine)
    pending_sid, _, pending = asyncio.run(seed(engine))
    engine.conversations.add_turn(pending_sid, "private pending requirement", "unsaved answer")
    completed(client, "trash", "quote", quote["quote_id"])
    old = info(client)
    old_scope = headers(client, sid)
    result = completed(client, "clear")
    current = info(client)
    assert current["dataset_id"] == old["dataset_id"] and current["epoch"] != old["epoch"]
    assert current["last_clear"] is not None
    assert all(count == 0 for count in table_counts(engine).values())
    assert client.get("/quotes").json()["quotes"] == []
    assert client.get("/sessions").json()["sessions"] == []
    assert client.get("/data-management/trash").json()["items"] == []
    assert client.get(f"/quotes/{quote['quote_id']}").status_code == 404
    with connect_saas(engine.settings) as db:
        assert db.execute("SELECT COUNT(*) FROM management_actions").fetchone()[0] == 1
    assert "private pending requirement" not in database_values(engine)
    assert quote["quote_id"] not in database_values(engine)
    assert proposal.action_ref not in database_values(engine)
    assert pending.action_ref not in database_values(engine)
    assert engine.work.peek(sid) is None and engine.work.peek(pending_sid) is None
    # No old-generation save/chat/invalidation/management may write or reclaim
    # authority after an empty record has been confirmed.
    for endpoint in ("quotes", "quote-actions/resolve"):
        response = client.post(f"/sessions/{sid}/{endpoint}", json={"action_ref": proposal.action_ref},
                               headers=old_scope)
        assert response.status_code in (409, 410)
    late = client.post("/chat", json={"session_id": sid, "message": "late private body"}, headers=old_scope)
    assert late.status_code in (409, 410)
    legacy = client.post("/chat", json={"message": "missing epoch body"})
    assert legacy.status_code in (409, 410)
    assert all(count == 0 for count in table_counts(engine).values())


def test_same_location_restart_keeps_trash_and_clear_but_never_old_business_permission(isolated_app, monkeypatch):
    client, engine = isolated_app
    sid, _, proposal, quote = saved(client, engine)
    completed(client, "trash", "chat", sid)
    completed(client, "trash", "quote", quote["quote_id"])
    epoch = info(client)["epoch"]
    restarted = Orchestrator(engine.settings)
    monkeypatch.setattr(main, "orchestrator", restarted)
    monkeypatch.setattr(main, "conversations", restarted.conversations)
    assert info(client)["epoch"] == epoch
    assert len(client.get("/data-management/trash").json()["items"]) == 2
    completed(client, "restore", "quote", quote["quote_id"])
    assert client.get(f"/quotes/{quote['quote_id']}").json()["snapshot"] == quote["snapshot"]
    assert restarted.work.peek(sid) is None
    completed(client, "clear")
    cleared_epoch = info(client)["epoch"]
    restarted_again = Orchestrator(engine.settings)
    monkeypatch.setattr(main, "orchestrator", restarted_again)
    monkeypatch.setattr(main, "conversations", restarted_again.conversations)
    assert info(client)["epoch"] == cleared_epoch
    assert all(count == 0 for count in table_counts(restarted_again).values())


def test_stale_chat_version_cannot_write_after_delete_and_restore(isolated_app, monkeypatch):
    client, engine = isolated_app
    sid, _, _, _ = saved(client, engine)
    old_scope = headers(client, sid)
    completed(client, "trash", "chat", sid)
    completed(client, "restore", "chat", sid)
    old_count = table_counts(engine)["conversation_messages"]
    stale = client.post("/chat", json={"session_id": sid, "message": "stale body"}, headers=old_scope)
    assert stale.status_code in (409, 410)
    unversioned = client.post("/chat", json={"session_id": sid, "message": "unversioned body"},
                              headers={"X-Data-Epoch": info(client)["epoch"]})
    assert unversioned.status_code in (409, 410)
    assert table_counts(engine)["conversation_messages"] == old_count


def test_original_management_action_rechecks_same_result_without_second_mutation(isolated_app):
    client, engine = isolated_app
    sid, _, _, quote = saved(client, engine)
    response, body = manage(client, "trash", "quote", quote["quote_id"])
    assert response.status_code == 200 and response.json()["status"] == "completed"
    after = info(client)
    scope = {"X-Data-Epoch": after["epoch"], "X-Data-Location": after["location_id"]}
    replay = client.post("/data-management/actions", json=body, headers=scope)
    assert replay.status_code == 200 and replay.json() == response.json()
    assert info(client)["revision"] == after["revision"]
    changed = {**body, "operation": "clear"}
    conflict = client.post("/data-management/actions", json=changed, headers=scope)
    assert conflict.status_code in (409, 422)
    assert table_counts(engine)["quotes"] == 1


def test_stale_clear_confirmation_cannot_clear_new_storage(isolated_app):
    client, engine = isolated_app
    state = info(client)
    saved(client, engine)
    response, _ = manage(client, "clear", expected_revision=state["revision"])
    assert response.status_code in (409, 200)
    if "status" in response.json():
        assert response.json()["status"] != "completed"
    assert table_counts(engine)["quotes"] == 1


def test_snapshot_edit_and_accidental_uncontrolled_delete_remain_rejected(isolated_app):
    client, engine = isolated_app
    sid, _, _, quote = saved(client, engine)
    for statement in ("UPDATE quotes SET snapshot_json = '{}'", "DELETE FROM quotes",
                      "UPDATE quote_actions SET session_id='changed'", "DELETE FROM quote_actions"):
        with pytest.raises(sqlite3.DatabaseError):
            with connect_saas(engine.settings) as db:
                db.execute(statement)
    assert client.patch(f"/quotes/{quote['quote_id']}", json={"snapshot": {}}).status_code == 405
    assert client.get(f"/quotes/{quote['quote_id']}").json()["snapshot"] == quote["snapshot"]


def test_legacy_schema_migrates_without_editing_original_snapshot_or_other_databases(tmp_path):
    legacy = tmp_path / "legacy-private.sqlite3"
    unrelated = tmp_path / "unrelated-sentinel.db"
    unrelated.write_bytes(b"untouched original library placeholder")
    settings = Settings(_env_file=None, saas_database_url=f"sqlite:///{legacy}")
    # Obtain a valid historical snapshot exclusively from the real domain
    # services, then construct the former physical schema in another file.
    staging = settings.model_copy(update={"saas_database_url": f"sqlite:///{tmp_path / 'seed.sqlite3'}"})
    init_saas_db(staging)
    source = Orchestrator(staging)
    sid, _, proposal = asyncio.run(seed(source))
    quote = asyncio.run(source.submit_quote(sid, proposal.action_ref)).quote
    raw = proposal.model_dump_json()
    with sqlite3.connect(legacy) as db:
        db.executescript("""CREATE TABLE conversations (id TEXT PRIMARY KEY, title TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
            CREATE TABLE conversation_messages (id INTEGER PRIMARY KEY AUTOINCREMENT,
            conversation_id TEXT NOT NULL, role TEXT NOT NULL, content TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(conversation_id) REFERENCES conversations(id) ON DELETE CASCADE);
            CREATE TABLE quotes (quote_id TEXT PRIMARY KEY NOT NULL, saved_at TEXT NOT NULL,
            snapshot_version INTEGER NOT NULL, snapshot_json TEXT NOT NULL, snapshot_sha256 TEXT NOT NULL);
            CREATE TABLE quote_actions (action_ref TEXT PRIMARY KEY NOT NULL, session_id TEXT NOT NULL,
            proposal_id TEXT NOT NULL, quote_id TEXT NOT NULL UNIQUE REFERENCES quotes(quote_id));
            CREATE TRIGGER quotes_no_update BEFORE UPDATE ON quotes
            BEGIN SELECT RAISE(ABORT, 'Quotes are immutable'); END;
            CREATE TRIGGER quotes_no_delete BEFORE DELETE ON quotes
            BEGIN SELECT RAISE(ABORT, 'Quotes are immutable'); END;
            CREATE TRIGGER quote_actions_no_update BEFORE UPDATE ON quote_actions
            BEGIN SELECT RAISE(ABORT, 'Quote actions are immutable'); END;
            CREATE TRIGGER quote_actions_no_delete BEFORE DELETE ON quote_actions
            BEGIN SELECT RAISE(ABORT, 'Quote actions are immutable'); END;""")
        with connect_saas(staging) as seeded:
            stored = seeded.execute("SELECT * FROM quotes").fetchone()
            db.execute("INSERT INTO quotes VALUES (?,?,?,?,?)", tuple(stored))
            db.execute("INSERT INTO quote_actions VALUES (?,?,?,?)",
                       (proposal.action_ref, sid, proposal.proposal_id, quote.quote_id))
        db.execute("INSERT INTO conversations (id,title) VALUES (?,?)", (sid, "original historical title"))
        db.execute("INSERT INTO conversation_messages (conversation_id,role,content) VALUES (?,?,?)",
                   (sid, "user", "original historical user text"))
    original_unrelated = unrelated.read_bytes()
    init_saas_db(settings)
    init_saas_db(settings)
    restarted = Orchestrator(settings)
    assert restarted.quotes.get(quote.quote_id).snapshot.model_dump_json() == raw
    assert restarted.quotes.find_action(sid, proposal.action_ref).quote_id == quote.quote_id
    assert restarted.conversations.get_messages(sid)[0].content == "original historical user text"
    assert unrelated.read_bytes() == original_unrelated


def test_source_status_changes_without_mutating_saved_snapshot(isolated_app):
    client, engine = isolated_app
    sid, _, _, quote = saved(client, engine)
    assert client.get(f"/quotes/{quote['quote_id']}").json()["source_conversation_status"] == "active"
    completed(client, "trash", "chat", sid)
    read = client.get(f"/quotes/{quote['quote_id']}").json()
    assert read["source_conversation_status"] == "trash" and read["snapshot"] == quote["snapshot"]
    completed(client, "purge", "chat", sid)
    read = client.get(f"/quotes/{quote['quote_id']}").json()
    assert read["source_conversation_status"] == "permanently_deleted"
    assert read["snapshot"] == quote["snapshot"]


def test_empty_trash_preserves_normal_objects_and_reports_actual_count(isolated_app):
    client, engine = isolated_app
    sid, _, _, quote = saved(client, engine)
    other_sid, _, _, other_quote = saved(client, engine)
    completed(client, "trash", "chat", sid)
    completed(client, "trash", "quote", quote["quote_id"])
    result = completed(client, "empty_trash")
    assert result["completed_count"] == 2 and result["remaining_count"] == 0
    assert client.get("/data-management/trash").json()["items"] == []
    assert {r["id"] for r in client.get("/sessions").json()["sessions"]} == {other_sid}
    assert {q["quote_id"] for q in client.get("/quotes").json()["quotes"]} == {other_quote["quote_id"]}
    assert table_counts(engine)["quotes"] == 1


def test_pending_creation_permission_does_not_return_after_chat_restore(isolated_app):
    client, engine = isolated_app
    sid, _, pending = asyncio.run(seed(engine))
    completed(client, "trash", "chat", sid)
    completed(client, "restore", "chat", sid)
    result = client.post(f"/sessions/{sid}/quotes", json={"action_ref": pending.action_ref},
                         headers=headers(client)).json()
    assert result["status"] == "not_saved" and result["quote"] is None
    assert table_counts(engine)["quotes"] == 0


def test_unavailable_data_location_has_no_fake_default_path_or_empty_counts(isolated_app, monkeypatch):
    client, engine = isolated_app
    def unavailable():
        raise OSError("controlled isolated metadata outage")
    monkeypatch.setattr(engine.data, "info", unavailable)
    response = client.get("/data-management")
    assert response.status_code == 503
    assert "暂时无法核实" in response.json()["detail"]["message"]
    assert "database_path" not in response.text and "counts" not in response.text


def test_empty_trash_fault_rolls_back_all_items_and_never_claims_completion(isolated_app, monkeypatch):
    client, engine = isolated_app
    sid, _, _, quote = saved(client, engine)
    completed(client, "trash", "chat", sid)
    completed(client, "trash", "quote", quote["quote_id"])
    before = table_counts(engine)
    def fail_before_commit(db, result):
        assert db.in_transaction
        assert result["completed_count"] == 2
        assert db.execute("SELECT COUNT(*) FROM quotes").fetchone()[0] == 0
        raise OSError("controlled failure after both deletes before commit")
    monkeypatch.setattr(engine.data, "_before_commit", fail_before_commit)
    response, body = manage(client, "empty_trash")
    assert response.status_code == 503 and response.json()["status"] == "unknown"
    assert table_counts(engine) == before
    assert client.get(f"/data-management/actions/{body['action_ref']}").status_code == 404
    assert len(client.get("/data-management/trash").json()["items"]) == 2


def test_real_commit_lost_receipt_reconciles_original_action_and_clears_business(isolated_app, monkeypatch):
    client, engine = isolated_app
    sid, _, pending = asyncio.run(seed(engine))
    def commit_then_lose(db):
        db.commit()
        raise OSError("controlled management receipt loss after real commit")
    monkeypatch.setattr(engine.data, "_commit", commit_then_lose)
    response, body = manage(client, "clear")
    assert response.status_code in (200, 503)
    if response.status_code == 503:
        assert response.json()["status"] == "unknown"
    checked = client.get(f"/data-management/actions/{body['action_ref']}")
    assert checked.status_code == 200 and checked.json()["status"] == "completed"
    assert all(count == 0 for count in table_counts(engine).values())
    assert engine.work.peek(sid) is None
    old = client.post(f"/sessions/{sid}/quotes", json={"action_ref": pending.action_ref},
                      headers=headers(client)).json()
    assert old["status"] == "not_saved" and old["quote"] is None
    assert "从未保存" in old["message"]


def test_related_delete_waits_for_actual_save_transaction_then_requires_fresh_count(isolated_app, monkeypatch):
    client, engine = isolated_app
    sid, _, pending = asyncio.run(seed(engine))
    entered, release = threading.Event(), threading.Event()
    def wait_inside_transaction(db, proposal):
        assert db.in_transaction
        entered.set()
        assert release.wait(10), "isolated save barrier was not released"
    monkeypatch.setattr(engine.quotes, "_before_commit", wait_inside_transaction)
    request_scope = headers(client)
    before = info(client)
    with ThreadPoolExecutor(max_workers=2) as pool:
        saving = pool.submit(client.post, f"/sessions/{sid}/quotes",
            json={"action_ref": pending.action_ref}, headers=request_scope)
        try:
            assert entered.wait(5)
            deleting = pool.submit(client.post, "/data-management/actions", json={
                "action_ref": uuid4().hex, "operation": "trash", "object_type": "chat",
                "object_id": sid, "expected_revision": before["revision"]}, headers=request_scope)
            assert not deleting.done()
        finally:
            release.set()
        saved_response = saving.result(timeout=5)
        deleted_response = deleting.result(timeout=5)
    assert saved_response.status_code == 200 and saved_response.json()["status"] == "saved"
    assert deleted_response.status_code == 200 and deleted_response.json()["status"] == "not_completed"
    assert table_counts(engine)["quotes"] == 1
    completed(client, "trash", "chat", sid)
    assert client.get(f"/quotes/{saved_response.json()['quote']['quote_id']}").status_code == 200


def test_unrelated_idle_chat_delete_does_not_wait_for_another_chat_model(isolated_app, monkeypatch):
    client, engine = isolated_app
    busy_sid, _, _, _ = saved(client, engine)
    idle_sid, _, _, _ = saved(client, engine)
    entered, release = threading.Event(), threading.Event()
    class PausedModel:
        def __init__(self, settings):
            pass
        async def chat(self, **kwargs):
            entered.set()
            await asyncio.to_thread(release.wait, 10)
            raise RuntimeError("controlled model unavailable after released barrier")
    monkeypatch.setattr("app.agents.orchestrator.DeepSeekClient", PausedModel)
    scope = headers(client, busy_sid)
    with ThreadPoolExecutor(max_workers=1) as pool:
        chat = pool.submit(client.post, "/chat", json={"session_id": busy_sid, "message": "忙碌对话"}, headers=scope)
        try:
            assert entered.wait(5)
            result = completed(client, "trash", "chat", idle_sid)
            assert result["status"] == "completed" and not chat.done()
        finally:
            release.set()
        assert chat.result(timeout=5).status_code == 200
    assert client.get(f"/sessions/{busy_sid}").status_code == 200
    assert client.get(f"/sessions/{idle_sid}").status_code == 410


def fake_request(epoch, location):
    return Request({"type": "http", "method": "POST", "path": "/data-management/actions",
        "headers": [(b"x-data-epoch", epoch.encode()), (b"x-data-location", location.encode())]})


def test_cancelled_clear_waiter_keeps_gate_until_sqlite_and_business_cleanup_finish(isolated_app, monkeypatch):
    client, engine = isolated_app
    async def scenario():
        sid, _, pending = await seed(engine)
        state = engine.data.info()
        entered, release = asyncio.Event(), threading.Event()
        loop = asyncio.get_running_loop()
        def wait_before_commit(db, result):
            assert db.in_transaction
            loop.call_soon_threadsafe(entered.set)
            assert release.wait(10), "isolated clear barrier was not released"
        monkeypatch.setattr(engine.data, "_before_commit", wait_before_commit)
        action_ref = uuid4().hex
        task = asyncio.create_task(main.manage_data(main.DataActionRequest(action_ref=action_ref,
            operation="clear", expected_revision=state["revision"]), fake_request(state["epoch"], state["location_id"])))
        try:
            await asyncio.wait_for(entered.wait(), 5)
            task.cancel()
            await asyncio.sleep(0)
            assert engine.order.clearing and engine.work.peek(sid) is not None
            task.cancel()
            await asyncio.sleep(0)
            assert engine.order.clearing
            async def reject_new_work():
                async with engine.order.ordinary():
                    pytest.fail("new work entered while clear was active")
            with pytest.raises(RuntimeError, match="record_clear_in_progress"):
                await reject_new_work()
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not engine.order.clearing
        assert engine.work.peek(sid) is None
        assert engine.data.resolve(action_ref)["status"] == "completed"
        assert table_counts(engine)["quotes"] == 0 and table_counts(engine)["conversations"] == 0
    asyncio.run(scenario())


def test_queued_chat_rechecks_deleted_state_after_its_session_lock_wait(isolated_app, monkeypatch):
    client, engine = isolated_app
    async def scenario():
        sid, runtime, _ = await seed(engine)
        state = engine.data.info()
        entered, release = asyncio.Event(), threading.Event()
        loop = asyncio.get_running_loop()
        def wait_before_commit(db, result):
            loop.call_soon_threadsafe(entered.set)
            assert release.wait(10), "isolated delete barrier was not released"
        monkeypatch.setattr(engine.data, "_before_commit", wait_before_commit)
        deletion = asyncio.create_task(main.manage_data(main.DataActionRequest(action_ref=uuid4().hex,
            operation="trash", object_type="chat", object_id=sid, expected_revision=state["revision"]),
            fake_request(state["epoch"], state["location_id"])))
        try:
            await asyncio.wait_for(entered.wait(), 5)
            assert runtime.lock.locked()
            # Uncommitted delete is invisible on the preliminary read. The
            # recheck after the same runtime lock must catch committed deletion.
            chat = asyncio.create_task(engine.chat(ChatRequest(session_id=sid, session_version=0,
                                                               message="late stale request")))
            await asyncio.sleep(0)
            assert not chat.done()
        finally:
            release.set()
        assert (await deletion)["status"] == "completed"
        with pytest.raises((ValueError, RuntimeError), match="conversation_in_trash"):
            await chat
        assert engine.conversations.status(sid)["status"] == "trash"
    asyncio.run(scenario())


def test_queued_old_save_cannot_regain_creation_permission_after_chat_restore(isolated_app, monkeypatch):
    client, engine = isolated_app
    async def scenario():
        sid, runtime, pending = await seed(engine)
        before = engine.data.info()
        deleting, save_checked = asyncio.Event(), asyncio.Event()
        release_delete, release_save = threading.Event(), asyncio.Event()
        loop = asyncio.get_running_loop()
        def pause_delete(db, result):
            if result["operation"] == "trash":
                loop.call_soon_threadsafe(deleting.set)
                assert release_delete.wait(10)
        monkeypatch.setattr(engine.data, "_before_commit", pause_delete)
        original = engine._persisted_quote_result
        async def pause_save_read(session_id, action_ref):
            save_checked.set()
            await release_save.wait()
            return await original(session_id, action_ref)
        monkeypatch.setattr(engine, "_persisted_quote_result", pause_save_read)
        delete = asyncio.create_task(main.manage_data(main.DataActionRequest(action_ref=uuid4().hex,
            operation="trash", object_type="chat", object_id=sid,
            expected_revision=before["revision"]), fake_request(before["epoch"], before["location_id"])))
        try:
            await asyncio.wait_for(deleting.wait(), 5)
            assert runtime.lock.locked()
            # Capture the old runtime and queue behind the deleting operation.
            save = asyncio.create_task(engine.submit_quote(sid, pending.action_ref))
            await asyncio.sleep(0)
            assert not save.done()
        finally:
            release_delete.set()
        assert (await delete)["status"] == "completed"
        await asyncio.wait_for(save_checked.wait(), 5)
        current = engine.data.info()
        restored = await main.manage_data(main.DataActionRequest(action_ref=uuid4().hex,
            operation="restore", object_type="chat", object_id=sid,
            expected_revision=current["revision"]), fake_request(current["epoch"], current["location_id"]))
        assert restored["status"] == "completed"
        release_save.set()
        result = await save
        assert result.status == "not_saved" and result.quote is None
        assert table_counts(engine)["quotes"] == 0
    asyncio.run(scenario())


def test_clear_original_action_read_failure_before_execution_preserves_business(isolated_app, monkeypatch):
    client, engine = isolated_app
    sid, runtime, pending = asyncio.run(seed(engine))
    original_state = info(client)
    original_counts = table_counts(engine)
    original_resolve = engine.data.resolve
    attempted = []

    def unavailable(*args):
        raise OSError("controlled original action read outage before clear execution")

    def unexpected_perform(*args, **kwargs):
        attempted.append((args, kwargs))
        pytest.fail("Clear must not execute when its original action cannot be checked")

    monkeypatch.setattr(engine.data, "resolve", unavailable)
    with monkeypatch.context() as execution_check:
        execution_check.setattr(engine.data, "perform", unexpected_perform)
        response, body = manage(client, "clear")

    assert response.status_code == 503 and response.json()["status"] == "unknown"
    assert "结果待核对" in response.json()["message"]
    assert "当前业务状态已结束" not in response.json()["message"]
    assert response.json()["completed_count"] is None
    assert response.json()["remaining_count"] is None
    assert attempted == []
    assert table_counts(engine) == original_counts
    assert info(client) == original_state
    assert not engine.order.clearing
    assert engine.work.peek(sid) is runtime
    assert runtime.proposal is pending

    monkeypatch.setattr(engine.data, "resolve", original_resolve)
    assert client.get(f"/data-management/actions/{body['action_ref']}").status_code == 404
    # No clear ran, so the still-current proposal remains usable. This also
    # verifies that merely reporting an unknown management result did not revoke it.
    saved_result = client.post(f"/sessions/{sid}/quotes",
        json={"action_ref": pending.action_ref}, headers=headers(client))
    assert saved_result.status_code == 200, saved_result.text
    assert saved_result.json()["status"] == "saved"
    assert table_counts(engine)["quotes"] == 1


def test_committed_clear_and_unavailable_original_read_stay_unknown_then_reconcile(isolated_app, monkeypatch):
    client, engine = isolated_app
    sid, runtime, pending = asyncio.run(seed(engine))
    original_epoch = info(client)["epoch"]
    original_resolve = engine.data.resolve
    def unavailable(*args):
        raise OSError("controlled management read outage after real clear commit")
    def commit_then_read_outage(db):
        db.commit()
        monkeypatch.setattr(engine.data, "resolve", unavailable)
        raise OSError("controlled clear receipt loss")
    monkeypatch.setattr(engine.data, "_commit", commit_then_read_outage)
    response, body = manage(client, "clear")
    assert response.status_code == 503 and response.json()["status"] == "unknown"
    assert "当前业务状态已结束" in response.json()["message"]
    assert "存档清理结果待核对" in response.json()["message"]
    assert response.json()["completed_count"] is None
    assert response.json()["remaining_count"] is None
    assert client.get(f"/data-management/actions/{body['action_ref']}").status_code == 503
    assert all(count == 0 for count in table_counts(engine).values())
    assert info(client)["epoch"] != original_epoch
    # Unknown persistent result must still end old process creation authority.
    assert engine.work.peek(sid) is None
    assert runtime.proposal is None
    monkeypatch.setattr(engine.data, "resolve", original_resolve)
    checked = client.get(f"/data-management/actions/{body['action_ref']}")
    assert checked.status_code == 200 and checked.json()["status"] == "completed"
    assert checked.json()["action_ref"] == body["action_ref"]
    assert table_counts(engine)["quotes"] == 0


def test_copied_database_identity_does_not_authorize_old_location_requests(isolated_app, monkeypatch, tmp_path):
    client, original = isolated_app
    sid, _, proposal, quote = saved(client, original)
    old = info(client)
    old_scope = headers(client, sid)
    copied_path = tmp_path / "another-private-location.sqlite3"
    with sqlite3.connect(saas_database_path(original.settings)) as source:
        with sqlite3.connect(copied_path) as destination:
            source.backup(destination)
    copied_settings = original.settings.model_copy(update={"saas_database_url": f"sqlite:///{copied_path}"})
    init_saas_db(copied_settings)
    copied = Orchestrator(copied_settings)
    monkeypatch.setattr(main, "settings", copied_settings)
    monkeypatch.setattr(main, "orchestrator", copied)
    monkeypatch.setattr(main, "conversations", copied.conversations)
    current = info(client)
    assert current["dataset_id"] == old["dataset_id"] and current["epoch"] == old["epoch"]
    assert current["database_path"] != old["database_path"]
    assert current["location_id"] != old["location_id"]
    for endpoint, body in (("/chat", {"session_id": sid, "message": "old location body"}),
                           (f"/sessions/{sid}/quotes", {"action_ref": proposal.action_ref}),
                           ("/data-management/actions", {"action_ref": uuid4().hex, "operation": "clear",
                            "object_type": None, "object_id": None, "expected_revision": current["revision"]})):
        rejected = client.post(endpoint, json=body, headers=old_scope)
        assert rejected.status_code == 409 and rejected.json()["detail"]["code"] == "data_location_changed"
    assert table_counts(copied)["quotes"] == 1 and table_counts(original)["quotes"] == 1
    completed(client, "clear")
    assert table_counts(copied)["quotes"] == 0
    assert table_counts(original)["quotes"] == 1
    assert original.quotes.get(quote["quote_id"]).snapshot.model_dump(mode="json") == quote["snapshot"]


def test_cross_chat_quote_read_drains_before_purge_and_unrelated_quote_can_delete(isolated_app, monkeypatch):
    client, engine = isolated_app
    _, _, _, target = saved(client, engine)
    _, _, _, unrelated = saved(client, engine)
    reader_sid = engine.conversations.ensure_conversation(None, "separate historical query chat")
    entered, release = threading.Event(), threading.Event()
    class PausedQueryModel:
        review_calls = []
        def __init__(self, settings):
            self.step = 0
        async def chat(self, **kwargs):
            if is_review_request(kwargs):
                return answer_review(type(self), kwargs)
            self.step += 1
            if self.step == 1:
                return completion(call("update_requirements", {"intent": "quote_query"}))
            if self.step == 2:
                return completion(call("query_quote", {"quote_id": target["quote_id"]}))
            entered.set()
            await asyncio.to_thread(release.wait, 10)
            return completion()
    monkeypatch.setattr("app.agents.orchestrator.DeepSeekClient", PausedQueryModel)
    scope = headers(client, reader_sid)
    with ThreadPoolExecutor(max_workers=2) as pool:
        query = pool.submit(client.post, "/chat", json={"session_id": reader_sid,
            "message": "查询草案 " + target["quote_id"]}, headers=scope)
        try:
            assert entered.wait(5), "controlled model did not pause after real Quote read"
            assert engine.order._quote_readers.get(target["quote_id"]) == 1
            completed(client, "trash", "quote", unrelated["quote_id"])
            assert not query.done()
            state = info(client)
            body = {"action_ref": uuid4().hex, "operation": "trash", "object_type": "quote",
                    "object_id": target["quote_id"], "expected_revision": state["revision"]}
            deleting = pool.submit(client.post, "/data-management/actions", json=body,
                                   headers={"X-Data-Epoch": state["epoch"], "X-Data-Location": state["location_id"]})
            assert not deleting.done()
        finally:
            release.set()
        response = query.result(timeout=5)
        delayed_delete = deleting.result(timeout=5)
    assert response.status_code == 200
    assert response.json()["business"]["queried_quotes"][0]["quote_id"] == target["quote_id"]
    assert delayed_delete.status_code == 200 and delayed_delete.json()["status"] == "not_completed"
    assert engine.order._quote_readers == {}
    completed(client, "trash", "quote", target["quote_id"])
    completed(client, "purge", "quote", target["quote_id"])
    history = engine.conversations.get_messages(reader_sid, limit=None)
    assert target["quote_id"] in history[-1].content
    # This old plain chat remains a deliberately disclosed independent copy;
    # no later reply may append the removed snapshot after purge completed.
    assert client.get(f"/quotes/{target['quote_id']}").status_code == 410


def test_bulk_quote_management_allows_existing_reader_to_finish_second_quote_without_deadlock(isolated_app):
    _, engine = isolated_app
    async def scenario():
        first, second = "Q-" + "1" * 32, "Q-" + "2" * 32
        entered, release = asyncio.Event(), asyncio.Event()
        await engine.order.begin_quote_read(first)
        async def mutate():
            async with engine.order.manage_quotes((first, second)):
                entered.set()
                await release.wait()
        management = asyncio.create_task(mutate())
        await asyncio.sleep(0)
        assert first in engine.order._quote_waiting and not entered.is_set()
        # A turn that already holds one read can extend to the next saved draft
        # while a bulk operation waits for its eventual historical write.
        await asyncio.wait_for(engine.order.begin_quote_read(second, continuing=True), 2)
        await engine.order.end_quote_reads((first, second))
        await asyncio.wait_for(entered.wait(), 2)
        blocked = asyncio.create_task(engine.order.begin_quote_read(first))
        await asyncio.sleep(0)
        assert not blocked.done()
        release.set()
        await management
        await asyncio.wait_for(blocked, 2)
        await engine.order.end_quote_reads((first,))
        assert engine.order._quote_readers == {} and engine.order._quote_mutating == set()
    asyncio.run(scenario())


@pytest.mark.parametrize("scope_kind", ["missing", "epoch_only", "location_only"])
def test_fresh_instance_rejects_unverified_old_browser_mutation_without_new_records(isolated_app, scope_kind):
    client, engine = isolated_app
    state = info(client)
    scope = {} if scope_kind == "missing" else {
        "X-Data-Epoch" if scope_kind == "epoch_only" else "X-Data-Location":
        state["epoch"] if scope_kind == "epoch_only" else state["location_id"]}
    for endpoint, body in (("/chat", {"message": "unverified obsolete browser body"}),
        ("/sessions/old-session/quotes", {"action_ref": "0" * 32}),
        ("/data-management/actions", {"action_ref": uuid4().hex, "operation": "clear",
         "object_type": None, "object_id": None, "expected_revision": state["revision"]})):
        response = client.post(endpoint, json=body, headers=scope)
        assert response.status_code == 409, response.text
    assert all(value == 0 for value in table_counts(engine).values())
    assert info(client)["epoch"] == state["epoch"]


def test_new_save_rollback_after_clear_remains_definitive_while_old_fact_is_only_missing(isolated_app, monkeypatch):
    client, engine = isolated_app
    old_sid, _, old_action, _ = saved(client, engine)
    completed(client, "clear")
    new_sid, _, new_action = asyncio.run(seed(engine))
    def rollback_new_quote(db, proposal):
        assert proposal is new_action
        assert db.in_transaction and db.execute("SELECT COUNT(*) FROM quotes").fetchone()[0] == 1
        raise OSError("controlled new-generation Quote rollback")
    monkeypatch.setattr(engine.quotes, "_before_commit", rollback_new_quote)
    failed = client.post(f"/sessions/{new_sid}/quotes", json={"action_ref": new_action.action_ref},
                         headers=headers(client))
    assert failed.status_code == 200
    assert failed.json()["status"] == "not_saved"
    assert failed.json()["code"] == "quote_transaction_rolled_back"
    assert "事务已回滚，明确未保存" in failed.json()["message"]
    assert table_counts(engine)["quotes"] == 0
    old = client.post(f"/sessions/{old_sid}/quote-actions/resolve",
        json={"action_ref": old_action.action_ref}, headers=headers(client))
    assert old.status_code == 200 and old.json()["code"] == "current_record_not_found"
    assert "不能据此判断它从未保存" in old.json()["message"]


def test_cancelled_query_keeps_quote_lease_until_actual_read_worker_finishes(isolated_app, monkeypatch):
    client, engine = isolated_app
    _, _, _, quote = saved(client, engine)
    async def scenario():
        sid = engine.conversations.ensure_conversation(None, "cancelled independent Quote reader")
        state = engine.data.info()
        entered, release = asyncio.Event(), threading.Event()
        loop = asyncio.get_running_loop()
        original_get = engine.quotes.get
        def pause_actual_read(quote_id):
            result = original_get(quote_id)
            loop.call_soon_threadsafe(entered.set)
            assert release.wait(10), "isolated Quote read barrier was not released"
            return result
        monkeypatch.setattr(engine.quotes, "get", pause_actual_read)
        class ReadModel:
            review_calls = []
            def __init__(self, settings):
                self.step = 0
            async def chat(self, **kwargs):
                if is_review_request(kwargs):
                    return answer_review(type(self), kwargs)
                self.step += 1
                if self.step == 1:
                    return completion(call("update_requirements", {"intent": "quote_query"}))
                return completion(call("query_quote", {"quote_id": quote["quote_id"]}))
        monkeypatch.setattr("app.agents.orchestrator.DeepSeekClient", ReadModel)
        query = asyncio.create_task(main.chat(ChatRequest(session_id=sid, session_version=0,
            message="查询 " + quote["quote_id"]), fake_request(state["epoch"], state["location_id"])))
        try:
            await asyncio.wait_for(entered.wait(), 5)
            query.cancel()
            await asyncio.sleep(0)
            query.cancel()
            await asyncio.sleep(0)
            assert engine.order._quote_readers.get(quote["quote_id"]) == 1
            assert engine.work.peek(sid).lock.locked()
            deletion = asyncio.create_task(main.manage_data(main.DataActionRequest(action_ref=uuid4().hex,
                operation="trash", object_type="quote", object_id=quote["quote_id"],
                expected_revision=state["revision"]), fake_request(state["epoch"], state["location_id"])))
            await asyncio.sleep(0)
            assert not deletion.done()
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await query
        result = await asyncio.wait_for(deletion, 5)
        assert result["status"] == "completed"
        assert engine.order._quote_readers == {}
    asyncio.run(scenario())

