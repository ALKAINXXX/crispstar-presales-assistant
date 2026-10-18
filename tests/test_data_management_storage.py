"""Data-management isolated SQLite evidence. No fixture opens a configured user database."""

import asyncio
import sqlite3
import shutil
from uuid import uuid4

import pytest

from app.agents.orchestrator import Orchestrator
from app.config import Settings
from app.db.session import connect_saas, init_saas_db, saas_database_path
from app.services.conversation_store import ConversationStore
from app.services.data_management import DataManagementError, DataManagementStore
from app.services.quotes import QuoteReadError, QuoteStore, QuoteWriteError
from app.schemas.quote import quote_facts
from app.services.operation_order import OperationOrder
from test_proposals import seed


@pytest.fixture
def records(tmp_path):
    settings = Settings(_env_file=None, deepseek_api_key="controlled",
        saas_database_url=f"sqlite:///{tmp_path / 'management-isolated.sqlite3'}")
    init_saas_db(settings)
    engine = Orchestrator(settings)
    sid, _, proposal = asyncio.run(seed(engine))
    engine.conversations.add_turn(sid, "原始需求与自述", "当时保存的摘要与编号")
    quote = engine.quotes._create(proposal)
    yield settings, sid, proposal, quote


def perform(store, operation, object_type=None, object_id=None, *, ref=None):
    info = store.info()
    return store.perform(ref or uuid4().hex, operation, info["epoch"], info["revision"], object_type, object_id)


def test_independent_trash_restore_keeps_exact_snapshot_identity_and_messages(records):
    settings, sid, proposal, quote = records
    chats, quotes, management = ConversationStore(settings), QuoteStore(settings), DataManagementStore(settings)
    original_messages = chats.get_messages(sid, limit=None)
    with connect_saas(settings) as db:
        original_snapshot = dict(db.execute("SELECT * FROM quotes").fetchone())
    perform(management, "trash", "chat", sid)
    assert chats.list_conversations() == []
    assert quotes.get(quote.quote_id).source_conversation_status == "trash"
    assert quote_facts(quotes.get(quote.quote_id)).source_conversation_status == "trash"
    assert quotes.list_quotes()[0]["quote_id"] == quote.quote_id
    with pytest.raises(ValueError, match="conversation_in_trash"):
        chats.get_messages(sid)
    perform(management, "trash", "quote", quote.quote_id)
    assert {item["object_type"] for item in management.list_trash()} == {"chat", "quote"}
    assert quotes.list_quotes() == []
    with pytest.raises(QuoteReadError, match="quote_in_trash"):
        quotes.get(quote.quote_id)
    assert quotes.action_status(sid, proposal.action_ref)["current_status"] == "trash"
    # A process restart rebuilds no business authority and preserves both trash rows.
    init_saas_db(settings)
    restarted = DataManagementStore(settings)
    assert len(restarted.list_trash()) == 2
    perform(restarted, "restore", "quote", quote.quote_id)
    restored = quotes.get(quote.quote_id)
    assert restored.quote_id == quote.quote_id and restored.saved_at == quote.saved_at
    assert restored.snapshot == quote.snapshot and restored.source_conversation_status == "trash"
    assert chats.list_conversations() == []  # restoring one kind does not restore the other
    perform(restarted, "restore", "chat", sid)
    assert chats.get_messages(sid, limit=None) == original_messages
    assert quotes.get(quote.quote_id) == quote
    with connect_saas(settings) as db:
        assert dict(db.execute("SELECT * FROM quotes").fetchone()) == original_snapshot
    with pytest.raises(ValueError, match="conversation_version_changed"):
        chats.assert_active(sid)
    with pytest.raises(ValueError, match="conversation_version_changed"):
        chats.add_turn(sid, "迟到需求", "迟到回复", expected_version=0)
    assert chats.get_messages(sid, limit=None) == original_messages


def test_permanent_delete_removes_body_retains_minimal_fact_and_cannot_recreate(records):
    settings, sid, proposal, quote = records
    chats, quotes, management = ConversationStore(settings), QuoteStore(settings), DataManagementStore(settings)
    perform(management, "trash", "chat", sid)
    perform(management, "purge", "chat", sid)
    assert quotes.get(quote.quote_id).source_conversation_status == "permanently_deleted"
    assert quotes.get(quote.quote_id).snapshot == quote.snapshot
    perform(management, "trash", "quote", quote.quote_id)
    perform(management, "purge", "quote", quote.quote_id)
    assert management.list_trash() == []
    with pytest.raises(QuoteReadError, match="quote_permanently_deleted"):
        quotes.get(quote.quote_id)
    assert quotes.action_status(sid, proposal.action_ref) == {
        "quote_id": quote.quote_id, "saved_at": quote.saved_at, "current_status": "permanently_deleted"}
    with pytest.raises(QuoteWriteError):
        quotes._create(proposal)
    with connect_saas(settings) as db:
        assert db.execute("SELECT COUNT(*) FROM quotes").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM conversation_messages").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM conversations").fetchone()[0] == 0
        assert set(dict(db.execute("SELECT * FROM quote_actions").fetchone())) == {
            "action_ref", "session_id", "proposal_id", "quote_id"}
        state = dict(db.execute("SELECT * FROM quote_states").fetchone())
        assert set(state) == {"quote_id", "saved_at", "deleted_at", "permanently_deleted_at", "version"}
        assert state["permanently_deleted_at"] and not state["deleted_at"]
        # Inspect all retained application values; no demand or amount copy survives.
        retained = repr([tuple(row) for table in ("quote_states", "quote_actions", "conversation_tombstones", "management_actions")
                         for row in db.execute(f"SELECT * FROM {table}")])
        assert "原始需求与自述" not in retained and "API必须有" not in retained
        assert "11520000" not in retained and "9600000" not in retained
    with pytest.raises(ValueError, match="conversation_not_found"):
        chats.ensure_conversation(sid, "旧会话请求")


def test_clear_current_dataset_erases_old_history_and_retains_only_new_receipt(records, tmp_path):
    settings, sid, proposal, quote = records
    management, chats, quotes = DataManagementStore(settings), ConversationStore(settings), QuoteStore(settings)
    other_path = tmp_path / "excluded-export.txt"
    other_path.write_text("outside-current-database", encoding="utf-8")
    other_db = tmp_path / "other-location.sqlite3"
    other_db.write_bytes(b"untouched-other-location")
    trash_receipt = perform(management, "trash", "quote", quote.quote_id)
    old_info = management.info()
    receipt = perform(management, "clear", ref="clear-original-action")
    assert receipt["status"] == "completed" and receipt["completed_count"] == 2
    assert receipt["epoch"] != old_info["epoch"]
    assert management.info()["epoch_requires_scope"]
    assert not any(management.info()["counts"].values())
    assert management.resolve(trash_receipt["action_ref"])["status"] == "not_found"
    assert quotes.action_status(sid, proposal.action_ref) is None
    with pytest.raises(QuoteReadError, match="quote_not_found"):
        quotes.get(quote.quote_id)
    with pytest.raises(DataManagementError, match="dataset_epoch_changed"):
        management.validate_scope(old_info["epoch"])
    with pytest.raises(DataManagementError, match="dataset_epoch_changed"):
        management.validate_scope(None)
    with pytest.raises(ValueError, match="dataset_epoch_changed"):
        chats.ensure_conversation(None, "旧页面第一条", expected_epoch=old_info["epoch"])
    with pytest.raises(ValueError, match="conversation_not_found"):
        chats.ensure_conversation(sid, "旧会话")
    init_saas_db(settings)
    restarted = DataManagementStore(settings)
    assert restarted.resolve("clear-original-action") == receipt
    assert restarted.info()["last_clear"] == receipt
    assert not any(restarted.info()["counts"].values())
    with connect_saas(settings) as db:
        for table in ("conversations", "conversation_messages", "quotes", "quote_actions", "quote_states", "conversation_tombstones"):
            assert db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
        assert [row["action_ref"] for row in db.execute("SELECT * FROM management_actions")] == ["clear-original-action"]
    assert other_path.read_text(encoding="utf-8") == "outside-current-database"
    assert other_db.read_bytes() == b"untouched-other-location"


def test_persisted_original_action_results_and_confirmation_revision(records):
    settings, sid, _, _ = records
    management, chats = DataManagementStore(settings), ConversationStore(settings)
    stale = management.info()
    chats.add_turn(sid, "新增内容", "新增回执")
    rejected = management.perform("stale-clear", "clear", stale["epoch"], stale["revision"])
    assert rejected["status"] == "not_completed" and rejected["code"] == "data_revision_changed"
    assert management.resolve("stale-clear") == rejected
    assert management.info()["counts"]["conversations"] == 1
    receipt = perform(management, "trash", "chat", sid, ref="single-original-ref")
    perform(management, "restore", "chat", sid)
    # Repeating a historical deletion action must not delete the restored object again.
    info = management.info()
    assert management.perform("single-original-ref", "trash", info["epoch"], info["revision"], "chat", sid) == receipt
    assert chats.status(sid)["status"] == "active"
    with pytest.raises(DataManagementError, match="management_action_mismatch"):
        management.perform("single-original-ref", "restore", info["epoch"], info["revision"], "chat", sid)


def test_transaction_failure_rolls_back_all_trash_and_lost_commit_receipt_can_resolve(records, monkeypatch):
    settings, sid, _, quote = records
    management = DataManagementStore(settings)
    perform(management, "trash", "chat", sid)
    perform(management, "trash", "quote", quote.quote_id)
    def before_commit(db, result):
        assert db.in_transaction and result["completed_count"] == 2
        raise OSError("controlled precommit failure")
    monkeypatch.setattr(management, "_before_commit", before_commit)
    with pytest.raises(OSError):
        perform(management, "empty_trash", ref="rollback-ref")
    assert len(management.list_trash()) == 2
    assert management.resolve("rollback-ref")["status"] == "not_found"
    monkeypatch.setattr(management, "_before_commit", lambda db, result: None)
    def after_commit(db):
        db.commit()
        raise OSError("controlled lost completion response")
    monkeypatch.setattr(management, "_commit", after_commit)
    with pytest.raises(OSError):
        perform(management, "empty_trash", ref="committed-ref")
    assert management.list_trash() == []
    assert management.resolve("committed-ref")["status"] == "completed"
    assert management.resolve("committed-ref")["completed_count"] == 2


def test_migrate_isolated_legacy_schema_preserves_raw_snapshot_and_immutability(records, tmp_path):
    settings, sid, proposal, quote = records
    with connect_saas(settings) as db:
        chat = tuple(db.execute("SELECT id,title,created_at,updated_at FROM conversations").fetchone())
        messages = [tuple(row) for row in db.execute("SELECT * FROM conversation_messages")]
        frozen = tuple(db.execute("SELECT * FROM quotes").fetchone())
        action = tuple(db.execute("SELECT * FROM quote_actions").fetchone())
    path = tmp_path / "legacy-schema-isolated.sqlite3"
    with sqlite3.connect(path) as legacy:
        legacy.executescript("""
            CREATE TABLE conversations(id TEXT PRIMARY KEY,title TEXT NOT NULL,created_at TEXT,updated_at TEXT);
            CREATE TABLE conversation_messages(id INTEGER PRIMARY KEY AUTOINCREMENT,conversation_id TEXT NOT NULL,
                role TEXT NOT NULL,content TEXT NOT NULL,created_at TEXT,
                FOREIGN KEY(conversation_id) REFERENCES conversations(id) ON DELETE CASCADE);
            CREATE TABLE quotes(quote_id TEXT PRIMARY KEY,saved_at TEXT NOT NULL,snapshot_version INTEGER,
                snapshot_json TEXT NOT NULL,snapshot_sha256 TEXT NOT NULL);
            CREATE TABLE quote_actions(action_ref TEXT PRIMARY KEY,session_id TEXT NOT NULL,proposal_id TEXT NOT NULL,
                quote_id TEXT NOT NULL UNIQUE REFERENCES quotes(quote_id));
        """)
        legacy.execute("INSERT INTO conversations VALUES (?,?,?,?)", chat)
        legacy.executemany("INSERT INTO conversation_messages VALUES (?,?,?,?,?)", messages)
        legacy.execute("INSERT INTO quotes VALUES (?,?,?,?,?)", frozen)
        legacy.execute("INSERT INTO quote_actions VALUES (?,?,?,?)", action)
        for table in ("quotes", "quote_actions"):
            for operation in ("update", "delete"):
                legacy.execute(f"CREATE TRIGGER {table}_no_{operation} BEFORE {operation.upper()} ON {table} BEGIN SELECT RAISE(ABORT,'immutable'); END")
    migrated = settings.model_copy(update={"saas_database_url": f"sqlite:///{path}"})
    init_saas_db(migrated)
    meta = DataManagementStore(migrated).info()
    init_saas_db(migrated)
    assert DataManagementStore(migrated).info() == meta
    assert meta["database_path"] == str(path.resolve())
    assert QuoteStore(migrated).get(quote.quote_id) == quote
    assert QuoteStore(migrated).find_action(sid, proposal.action_ref) == quote
    with connect_saas(migrated) as db:
        assert tuple(db.execute("SELECT * FROM quotes").fetchone()) == frozen
        assert [tuple(row) for row in db.execute("SELECT * FROM conversation_messages")] == messages
        assert tuple(db.execute("SELECT * FROM quote_actions").fetchone()) == action
        assert not db.execute("PRAGMA foreign_key_check").fetchall()
    for sql in ("UPDATE quotes SET snapshot_json='changed'", "DELETE FROM quotes", "DELETE FROM quote_actions",
                "UPDATE quote_actions SET session_id='changed'"):
        with pytest.raises(sqlite3.IntegrityError):
            with connect_saas(migrated) as db:
                db.execute(sql)
    assert DataManagementStore(migrated).info()["counts"]["quotes"] == 1


def test_location_identity_uses_actual_path_even_for_copied_dataset(records, tmp_path):
    settings, _, _, _ = records
    original = DataManagementStore(settings).info()
    copied_path = tmp_path / "another-isolated-location.sqlite3"
    shutil.copyfile(saas_database_path(settings), copied_path)
    copied_settings = settings.model_copy(update={"saas_database_url": f"sqlite:///{copied_path}"})
    copied = DataManagementStore(copied_settings).info()
    assert copied["dataset_id"] == original["dataset_id"]
    assert copied["epoch"] == original["epoch"]
    assert copied["location_id"] != original["location_id"]
    assert copied["database_path"] == str(copied_path.resolve())
    perform(DataManagementStore(copied_settings), "clear")
    assert DataManagementStore(settings).info()["counts"] == original["counts"]
    assert DataManagementStore(settings).info()["epoch"] == original["epoch"]


def test_bulk_quote_management_waits_for_actual_read_turn_and_allows_its_remaining_reads():
    async def scenario():
        order = OperationOrder()
        await order.begin_quote_read("first-draft")
        writer_started, mutating, release_mutation = asyncio.Event(), asyncio.Event(), asyncio.Event()
        async def writer():
            writer_started.set()
            async with order.manage_quotes(["first-draft", "second-draft"]):
                mutating.set()
                await release_mutation.wait()
        write_task = asyncio.create_task(writer())
        await writer_started.wait()
        assert not mutating.is_set()
        fresh_read = asyncio.create_task(order.begin_quote_read("second-draft"))
        await asyncio.sleep(0)
        assert not fresh_read.done()  # a new read cannot pass the waiting deletion
        # The existing turn can finish reading several drafts rather than
        # deadlocking while management waits for that same turn to finish.
        await asyncio.wait_for(order.begin_quote_read("second-draft", continuing=True), 1)
        assert not mutating.is_set()
        await order.end_quote_reads({"first-draft", "second-draft"})
        await asyncio.wait_for(mutating.wait(), 1)
        assert not fresh_read.done()  # mutation retains its barrier until real completion
        release_mutation.set()
        await asyncio.wait_for(write_task, 1)
        await asyncio.wait_for(fresh_read, 1)
        await order.end_quote_reads({"second-draft"})
    asyncio.run(scenario())


def test_cancelled_quote_writer_waiter_does_not_leave_an_unreleasable_barrier():
    async def scenario():
        order = OperationOrder()
        await order.begin_quote_read("draft")
        started = asyncio.Event()
        async def writer():
            started.set()
            async with order.manage_quotes(["draft"]):
                pytest.fail("writer entered before original read finished")
        task = asyncio.create_task(writer())
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        # The old reader remains live; cancelling its waiting writer still
        # releases the writer marker so a separate valid reader may proceed.
        await asyncio.wait_for(order.begin_quote_read("draft"), 1)
        await order.end_quote_reads({"draft"})
        await order.end_quote_reads({"draft"})
        async with order.manage_quotes(["draft"]):
            pass
    asyncio.run(scenario())
