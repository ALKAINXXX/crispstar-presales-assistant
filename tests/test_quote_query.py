"""Quote-query read-only model boundary with controlled model, real isolated SQLite.

These checks do not call the configured upstream or prove language quality.
"""
import asyncio
import json

import pytest
from zoneinfo import ZoneInfoNotFoundError

from app.agents.orchestrator import Orchestrator
from app.schemas.chat import ChatRequest
from app.schemas.quote import quote_facts
from app.services.chat_render import _quote_saved_time
from app.services.quotes import QuoteReadError
from app.tools.business import BusinessToolRegistry
from test_proposals import call, completion, scripted, seed
from test_quotes import counts, engine


def test_query_time_is_exact_beijing_time_without_adding_tzdata(monkeypatch):
    def no_iana_data(key):
        raise ZoneInfoNotFoundError(key)
    monkeypatch.setattr("app.services.chat_render.ZoneInfo", no_iana_data)
    assert _quote_saved_time("2026-10-04T20:14:19.084189+00:00") == "2026-10-05 04:14:19（北京时间）"


def test_query_reads_same_persisted_facts_and_never_exposes_action_authority(engine, monkeypatch):
    async def run():
        sid, runtime, proposal = await seed(engine, monkeypatch, sso=True)
        quote = (await engine.submit_quote(sid, proposal.action_ref)).quote
        before = runtime.state.current_task()
        client = scripted(monkeypatch, [
            completion(call("update_requirements", {"intent": "quote_query"})),
            completion(call("query_quote", {"quote_id": quote.quote_id})), completion(),
        ])
        response = await engine.chat(ChatRequest(message="请查回草案 " + quote.quote_id, session_id=sid))
        facts = quote_facts(quote)
        assert response.status == "completed"
        assert response.business.queried_quotes == [facts]
        assert response.business.proposal is None and runtime.proposal is None
        assert runtime.state.current_task() == before
        assert counts(engine) == (1, 1)
        assert [record["name"] for record in response.tool_results] == ["update_requirements", "query_quote"]
        assert quote.quote_id in response.message and "115,200.00" in response.message
        assert _quote_saved_time(quote.saved_at) in response.message and proposal.conditions[0].user_quote in response.message
        visible = response.business.presentation.answer_markdown
        for fact in ("Business", "最低满足档", "所选套餐", "80 个付费席位", "12 个月",
                     "120.00 元／席／月", "115,200.00 元", proposal.conditions[0].statement,
                     proposal.conditions[0].user_quote, "北京时间"):
            assert fact in visible
        assert response.message.count(quote.quote_id) == 1
        assert response.message.count(_quote_saved_time(quote.saved_at)) == 1
        assert engine.conversations.get_messages(sid)[-1].content == response.message
        model_history = json.dumps(client.captured, ensure_ascii=False)
        assert proposal.action_ref not in model_history
        assert "action_ref" not in client.captured[-1]["messages"][-1]["content"]
        raw = response.business.queried_quotes[0].model_dump(mode="json")
        for field in ("action_ref", "session_id", "view_id", "proposal_id", "turn_id",
                      "interaction_revision", "status", "save_available", "notice"):
            assert field not in raw["snapshot"]
        assert raw["snapshot"]["adopted_sources"] == quote.snapshot.model_dump(mode="json")["adopted_sources"]
    asyncio.run(run())


def test_query_does_not_revalidate_current_match_or_replace_original_evidence(engine, monkeypatch):
    async def run():
        sid, runtime, proposal = await seed(engine, monkeypatch, sso=True)
        quote = (await engine.submit_quote(sid, proposal.action_ref)).quote
        before = runtime.state.current_task(recheck=False)
        def forbidden(*args, **kwargs):
            pytest.fail("Historical Quote lookup called current matching or retrieval")
        monkeypatch.setattr("app.services.work_state.match_requirements", forbidden)
        monkeypatch.setattr(runtime.state.knowledge, "retrieve", forbidden)
        scripted(monkeypatch, [completion(call("update_requirements", {"intent": "quote_query"})),
            completion(call("query_quote", {"quote_id": quote.quote_id})), completion()])
        response = await engine.chat(ChatRequest(message="查询 " + quote.quote_id, session_id=sid))
        assert response.status == "completed"
        assert runtime.state.current_task(recheck=False) == before
        assert response.business.current_task.pricing == proposal.pricing
    asyncio.run(run())


def test_rejected_extra_matching_in_query_does_not_erase_current_selection(engine, monkeypatch):
    async def run():
        sid, runtime, proposal = await seed(engine)
        quote = (await engine.submit_quote(sid, proposal.action_ref)).quote
        before = runtime.state.current_task(recheck=False)
        scripted(monkeypatch, [completion(call("update_requirements", {"intent": "quote_query"})),
            completion(call("query_quote", {"quote_id": quote.quote_id}), call("match_requirements", {}, "extra-match"))])
        response = await engine.chat(ChatRequest(message="查询 " + quote.quote_id, session_id=sid))
        assert response.status == "incomplete"
        assert {e["code"] for e in response.business.errors} == {"quote_query_read_only"}
        assert response.business.queried_quotes == [quote_facts(quote)]
        assert runtime.state.current_task(recheck=False) == before
        assert counts(engine) == (1, 1)
    asyncio.run(run())


@pytest.mark.parametrize("extra", [
    {"parameters": [{"name": "paid_seats", "value": 50, "user_quote": "50席"}]},
    {"selected_plan_id": "business", "selection_quote": "Business"},
    {"context_quotes": ["查询草案"]},
    {"add": [{"statement": "API", "feature_id": "api_access", "user_quote": "API"}]},
])
def test_historical_query_classifier_rejects_all_modifying_fields(engine, extra):
    async def run():
        sid, runtime, proposal = await seed(engine)
        registry = BusinessToolRegistry(engine.settings, runtime.state, "查询草案 API Business 50席")
        before = runtime.state.current_task()
        result = await registry.execute("update_requirements", {"intent": "quote_query", **extra})
        assert result["ok"] is False and result["error"]["code"] == "invalid_arguments"
        assert runtime.state.current_task() == before
        assert runtime.proposal == proposal and counts(engine) == (0, 0)
    asyncio.run(run())


@pytest.mark.parametrize("quote_id,code", [("bad", "quote_query_scope_mismatch"), ("Q-" + "0" * 32, "quote_not_found")])
def test_model_lookup_bad_or_absent_number_has_same_read_error_and_never_creates(engine, monkeypatch, quote_id, code):
    scripted(monkeypatch, [completion(call("update_requirements", {"intent": "quote_query"})),
        completion(call("query_quote", {"quote_id": quote_id}))])
    response = asyncio.run(engine.chat(ChatRequest(message="查询草案 " + quote_id)))
    assert response.status == "incomplete"
    assert [e["code"] for e in response.business.errors] == [code]
    assert response.business.queried_quotes == [] and response.business.proposal is None
    assert counts(engine) == (0, 0)


def test_direct_lookup_invalid_number_keeps_original_read_validation(engine):
    # A malformed natural-language identifier cannot become a reviewed lookup
    # target. The direct read boundary still validates its original error.
    async def run():
        sid = engine.conversations.ensure_conversation(None, "隔离坏编号校验")
        runtime = engine.work.get(sid)
        registry = BusinessToolRegistry(engine.settings, runtime.state, "", quotes=engine.quotes)
        result = await registry.execute("query_quote", {"quote_id": "bad"})
        assert result["ok"] is False
        assert result["error"]["code"] == "invalid_quote_id"
        assert counts(engine) == (0, 0)
    asyncio.run(run())


def test_lookup_read_failure_has_no_false_not_found_or_saved_fact(engine, monkeypatch):
    def fail(*args, **kwargs):
        raise QuoteReadError()
    monkeypatch.setattr(engine.quotes, "get", fail)
    scripted(monkeypatch, [completion(call("update_requirements", {"intent": "quote_query"})),
        completion(call("query_quote", {"quote_id": "Q-" + "A" * 32}))])
    response = asyncio.run(engine.chat(ChatRequest(message="查询草案 Q-" + "A" * 32)))
    assert [e["code"] for e in response.business.errors] == ["quote_read_unavailable"]
    assert response.business.queried_quotes == [] and "没有找到" not in response.message
    assert counts(engine) == (0, 0)


def test_legacy_snapshot_keeps_historical_false_notice_and_remains_queryable_after_restart(engine, monkeypatch):
    async def run():
        sid, runtime, proposal = await seed(engine, monkeypatch, sso=True)
        old_notice = "方案已准备，尚未保存；确认并保存草案功能尚未开放。"
        old = proposal.model_copy(update={"save_available": False, "notice": old_notice})
        runtime.proposal = old
        quote = (await engine.submit_quote(sid, old.action_ref)).quote
        assert quote.snapshot.save_available is False and quote.snapshot.notice == old_notice
        restarted = Orchestrator(engine.settings)
        assert restarted.quotes.get(quote.quote_id) == quote
        scripted(monkeypatch, [completion(call("update_requirements", {"intent": "quote_query"})),
            completion(call("query_quote", {"quote_id": quote.quote_id})), completion()])
        response = await restarted.chat(ChatRequest(message="查询草案 " + quote.quote_id))
        assert response.business.queried_quotes == [quote_facts(quote)]
        assert "尚未保存" not in response.message and "尚未开放" not in response.message
        assert "已保存" in response.message and counts(engine) == (1, 1)
    asyncio.run(run())


def test_new_message_after_readonly_query_can_modify_and_prepare_new_object(engine, monkeypatch):
    async def run():
        sid, runtime, proposal = await seed(engine)
        quote = (await engine.submit_quote(sid, proposal.action_ref)).quote
        scripted(monkeypatch, [completion(call("update_requirements", {"intent": "quote_query"})),
            completion(call("query_quote", {"quote_id": quote.quote_id})), completion(),
            completion(call("update_requirements", {"intent": "selection", "parameters": [
                {"name": "paid_seats", "value": 50, "user_quote": "改为50席"}]})),
            completion(call("match_requirements", {}), call("calculate_price", {
                "plan_id": "team", "paid_seats": 50, "months": 12}, "price")), completion()])
        await engine.chat(ChatRequest(message="查询 " + quote.quote_id, session_id=sid))
        response = await engine.chat(ChatRequest(message="改为50席，其他不变，继续原选型计价", session_id=sid))
        assert response.status == "completed"
        assert response.business.proposal.save_available is True
        assert response.business.proposal.pricing.base_price_minor == 4_800_000
        assert engine.quotes.get(quote.quote_id).snapshot == proposal
        assert counts(engine) == (1, 1)
    asyncio.run(run())
