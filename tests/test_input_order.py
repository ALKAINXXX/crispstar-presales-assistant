"""Input-review wait/cancel ordering with controlled provider barriers.

Actual verifier, production route gate, session lock, Quote read leases and
SQLite management execute in synthetic tmp_path stores. No upstream is called.
"""

import asyncio
from uuid import uuid4

from fastapi import HTTPException
import pytest
from starlette.requests import Request

import app.main as main
from app.llm.semantic_verifier import SemanticVerifier
from app.schemas.chat import ChatRequest
from app.schemas.quote import quote_facts
from test_input_orchestrator import call, completion, engine, script
from test_proposals import seed


def scope(engine):
    info = engine.data.info()
    return Request({"type": "http", "method": "POST", "path": "/chat", "headers": [
        (b"x-data-epoch", info["epoch"].encode()), (b"x-data-location", info["location_id"].encode())]})


def bind(monkeypatch, engine):
    monkeypatch.setattr(main, "settings", engine.settings)
    monkeypatch.setattr(main, "orchestrator", engine)
    monkeypatch.setattr(main, "conversations", engine.conversations)


async def manage(engine, operation, *, kind=None, identity=None):
    info = engine.data.info()
    return await main.manage_data(main.DataActionRequest(action_ref=uuid4().hex,
        operation=operation, object_type=kind, object_id=identity,
        expected_revision=info["revision"]), scope(engine))


@pytest.mark.parametrize("ending", ["timeout", "cancel"])
def test_inflight_review_keeps_existing_session_and_ordinary_gate_until_actual_dispatch_ends(engine, monkeypatch, ending):
    bind(monkeypatch, engine)
    monkeypatch.setattr(SemanticVerifier, "TIMEOUT_SECONDS", 0.01 if ending == "timeout" else 5)
    async def scenario():
        sid, runtime, old = await seed(engine)
        idle = engine.conversations.ensure_conversation(None, "unrelated synthetic idle chat")
        entered, release = asyncio.Event(), asyncio.Event()
        before = runtime.state.current_task(recheck=False)
        async def delayed_review():
            entered.set()
            await release.wait()
            return completion(call("adjudicate_turn", {"intent": "general", "coverage_complete": True}))
        provider = script(monkeypatch, [completion(call("update_requirements", {"intent": "general"})), delayed_review])
        chat = asyncio.create_task(main.chat(ChatRequest(session_id=sid,
            message="付费席位改为50，其余不变"), scope(engine)))
        clear = None
        try:
            await asyncio.wait_for(entered.wait(), 2)
            assert runtime.lock.locked() and engine.order._active == 1
            assert runtime.proposal is None
            # The review holds only the busy task; ordinary management for an
            # idle unrelated object remains available before a global clear.
            idle_deleted = await asyncio.wait_for(manage(engine, "trash", kind="chat", identity=idle), 2)
            assert idle_deleted["status"] == "completed" and not chat.done()
            if ending == "timeout":
                await asyncio.sleep(0.03)
            else:
                chat.cancel()
                await asyncio.sleep(0)
                chat.cancel()
                await asyncio.sleep(0)
            assert not chat.done() and runtime.lock.locked() and engine.order._active == 1
            clear = asyncio.create_task(manage(engine, "clear"))
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            assert engine.order.clearing and not clear.done()
            with pytest.raises(HTTPException) as blocked:
                await main.chat(ChatRequest(message="late page request"), scope(engine))
            assert blocked.value.status_code == 409
            assert runtime.state.current_task(recheck=False)["requirements"] == before["requirements"]
        finally:
            release.set()
        if ending == "cancel":
            with pytest.raises(asyncio.CancelledError):
                await chat
        else:
            result = await asyncio.wait_for(chat, 2)
            assert result.business.semantic_review["error_code"] == "semantic_review_timeout"
            assert result.business.current_task.pricing == old.pricing
            assert result.business.current_task.result_turn_id == before["result_turn_id"]
        assert clear is not None
        await asyncio.wait_for(clear, 2)
        assert not engine.order.clearing and engine.order._active == 0
        assert len([c for c in provider.captured if c.get("max_retries") == 0]) == 1
    asyncio.run(scenario())


def test_related_chat_management_waits_for_review_and_new_facts_never_commit_after_review_failure(engine, monkeypatch):
    bind(monkeypatch, engine)
    async def scenario():
        sid, runtime, _ = await seed(engine)
        entered, release = asyncio.Event(), asyncio.Event()
        async def failed_review():
            entered.set()
            await release.wait()
            raise RuntimeError("controlled late provider failure")
        script(monkeypatch, [completion(call("update_requirements", {"intent": "selection"})), failed_review])
        chat = asyncio.create_task(main.chat(ChatRequest(session_id=sid, message="改为50席"), scope(engine)))
        deletion = None
        try:
            await asyncio.wait_for(entered.wait(), 2)
            deletion = asyncio.create_task(manage(engine, "trash", kind="chat", identity=sid))
            await asyncio.sleep(0)
            assert not deletion.done() and runtime.lock.locked()
            assert next(p.value for p in runtime.state.requirements.parameters if p.name == "paid_seats") == 80
        finally:
            release.set()
        result = await asyncio.wait_for(chat, 2)
        assert result.business.semantic_review["error_code"] == "semantic_review_unavailable"
        # The accepted turn increments archive revision. Management may report
        # a known revision conflict, rather than delete against a stale view.
        deleted = await asyncio.wait_for(deletion, 2)
        assert deleted["status"] in {"completed", "not_completed"}
        assert engine.order._active == 0 and not runtime.lock.locked()
    asyncio.run(scenario())


@pytest.mark.parametrize("cancel", [False, True])
def test_quote_read_lease_covers_later_model_and_formal_history_until_entire_turn_ends(engine, monkeypatch, cancel):
    bind(monkeypatch, engine)
    async def scenario():
        source, _, proposal = await seed(engine)
        quote = (await engine.submit_quote(source, proposal.action_ref)).quote
        reader = engine.conversations.ensure_conversation(None, "synthetic independent Quote reader")
        entered, release = asyncio.Event(), asyncio.Event()
        async def delayed_final():
            entered.set()
            # A controlled provider can finish its actual request after the
            # outer browser waiter is cancelled. wait_for must drain it.
            pending = asyncio.create_task(release.wait())
            try:
                await asyncio.shield(pending)
            except asyncio.CancelledError:
                while not pending.done():
                    try:
                        await asyncio.shield(pending)
                    except asyncio.CancelledError:
                        continue
            return completion()
        review = {"intent": "quote_query", "coverage_complete": True,
            "quote_id": quote.quote_id, "groups": []}
        script(monkeypatch, [completion(call("update_requirements", {"intent": "quote_query"})),
            completion(call("adjudicate_turn", review)), completion(call("query_quote", {"quote_id": quote.quote_id})), delayed_final])
        original = engine.conversations.add_turn
        writes = []
        def record_under_lease(*args):
            writes.append(engine.order._quote_readers.get(quote.quote_id))
            return original(*args)
        monkeypatch.setattr(engine.conversations, "add_turn", record_under_lease)
        chat = asyncio.create_task(main.chat(ChatRequest(session_id=reader,
            message="按编号查草案 " + quote.quote_id), scope(engine)))
        deleting = None
        try:
            await asyncio.wait_for(entered.wait(), 2)
            assert engine.order._quote_readers == {quote.quote_id: 1}
            if cancel:
                chat.cancel()
                await asyncio.sleep(0)
                chat.cancel()
                await asyncio.sleep(0)
            assert not chat.done() and engine.work.get(reader).lock.locked()
            deleting = asyncio.create_task(manage(engine, "trash", kind="quote", identity=quote.quote_id))
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            assert not deleting.done() and engine.order._quote_readers == {quote.quote_id: 1}
        finally:
            release.set()
        if cancel:
            with pytest.raises(asyncio.CancelledError):
                await chat
            assert writes == []
        else:
            result = await asyncio.wait_for(chat, 2)
            assert result.business.queried_quotes[0] == quote_facts(quote)
            assert writes == [1]
        await asyncio.wait_for(deleting, 2)
        assert engine.order._quote_readers == {} and engine.order._active == 0
    asyncio.run(scenario())


def test_bulk_quote_waiter_allows_existing_turn_second_read_without_deadlock(engine, monkeypatch):
    bind(monkeypatch, engine)
    async def scenario():
        ids = []
        for _ in range(2):
            sid, _, proposal = await seed(engine)
            ids.append((await engine.submit_quote(sid, proposal.action_ref)).quote.quote_id)
        reader = engine.conversations.ensure_conversation(None, "multiple synthetic draft lookup")
        entered, release = asyncio.Event(), asyncio.Event()
        async def delayed_second_read():
            entered.set()
            await release.wait()
            return completion(call("query_quote", {"quote_id": ids[1]}))
        review = {"intent": "quote_query", "coverage_complete": True, "quote_ids": ids}
        script(monkeypatch, [completion(call("update_requirements", {"intent": "quote_query"})),
            completion(call("adjudicate_turn", review)), completion(call("query_quote", {"quote_id": ids[0]})),
            delayed_second_read, completion()])
        chat = asyncio.create_task(main.chat(ChatRequest(session_id=reader,
            message="查询草案 " + " 和 ".join(ids)), scope(engine)))
        mutated = asyncio.Event()
        async def bulk():
            async with engine.order.management_lock:
                async with engine.order.manage_quotes(ids):
                    assert engine.order._quote_readers == {}
                    mutated.set()
        management = None
        try:
            await asyncio.wait_for(entered.wait(), 2)
            management = asyncio.create_task(bulk())
            await asyncio.sleep(0)
            assert set(ids) <= engine.order._quote_waiting and not mutated.is_set()
        finally:
            release.set()
        result = await asyncio.wait_for(chat, 2)
        assert [q.quote_id for q in result.business.queried_quotes] == ids
        await asyncio.wait_for(management, 2)
        assert mutated.is_set() and engine.order._quote_readers == {}
    asyncio.run(scenario())
