"""Scheduling/error tests use model substitutes and real domain services.

These do not establish normal language understanding, real model tool choice,
or knowledge explanation. Those require separate live-model verification.
All conversation rows are confined to temporary databases.
"""

import asyncio
from copy import deepcopy
import json
import subprocess
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.agents import orchestrator as orchestrator_module
from app.agents.orchestrator import Orchestrator
from app.config import Settings
from app.db.session import init_saas_db
from app.schemas.chat import ChatRequest
from app.tools.business import BusinessToolRegistry
from controlled_review import answer_review, is_review_request, review_completion, review_from_candidate


def tool(name, arguments, identity=None):
    raw = arguments if isinstance(arguments, str) else json.dumps(arguments, ensure_ascii=False)
    return SimpleNamespace(id=uuid4().hex if identity is None else identity, function=SimpleNamespace(name=name, arguments=raw))


def completion(*calls, content="本轮结束", finish_reason="stop"):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content, tool_calls=list(calls)),
                                 finish_reason=finish_reason)],
        model="controlled-orchestrator-model", usage=None,
    )


def scripted(monkeypatch, outputs, *, review_candidates=(), review_outputs=()):
    controlled_candidates, controlled_outputs = list(review_candidates), list(review_outputs)
    class ScriptedClient:
        captured = []
        remaining = list(outputs)
        review_calls = []
        review_candidates = controlled_candidates
        review_outputs = controlled_outputs

        def __init__(self, settings):
            self.settings = settings

        async def chat(self, **kwargs):
            if is_review_request(kwargs):
                return answer_review(type(self), kwargs)
            type(self).captured.append(deepcopy(kwargs))
            assert type(self).remaining, "Unexpected extra model invocation"
            response = type(self).remaining.pop(0)
            if isinstance(response, Exception):
                raise response
            return response

    monkeypatch.setattr(orchestrator_module, "DeepSeekClient", ScriptedClient)
    return ScriptedClient


@pytest.fixture
def engine(tmp_path):
    settings = Settings(deepseek_api_key="test", deepseek_default_max_completion_tokens=512,
        saas_database_url=f"sqlite:///{tmp_path / 'orchestrator.db'}")
    init_saas_db(settings)
    return Orchestrator(settings)


def error_codes(response):
    return {error["code"] for error in response.business.errors}


def assert_stored_exactly(engine, response, user_message):
    history = engine.conversations.get_messages(response.session_id)
    assert history[-2].role == "user" and history[-2].content == user_message
    assert history[-1].role == "assistant" and history[-1].content == response.message


def test_unit_price_detour_keeps_pending_task_without_asking_it_in_rate_reply(engine, monkeypatch):
    message = "必须有产品API，帮我选型并算金额"
    client = scripted(monkeypatch, [
        completion(tool("update_requirements", {"intent": "selection", "pricing_quote": "算金额",
            "add": [{"statement": "需要产品API", "user_quote": "产品API", "feature_id": "api_access"}]})),
        completion(tool("match_requirements", {})),
        completion(tool("update_requirements", {"intent": "unit_price"})),
        completion(tool("query_catalog", {"plan_id": "team"})), completion(),
        completion(tool("update_requirements", {"intent": "selection", "parameters": [
            {"name": "paid_seats", "value": 80, "user_quote": "80个付费席位"},
            {"name": "months", "value": 12, "user_quote": "一年"}]})),
        completion(tool("match_requirements", {}),
            tool("calculate_price", {"plan_id": "team", "paid_seats": 80, "months": 12}, "price")),
        completion(),
    ])
    async def run():
        first = await engine.chat(ChatRequest(message=message))
        assert set(first.business.pending) == {"paid_seats", "months"}
        assert first.status == "needs_input" and len(client.captured) == 2
        assert first.business.requirements.selected_plan_id is None
        assert first.business.unit_price.plan_id == "team" and first.business.unit_price.unit_price_minor == 8000
        assert [r["name"] for r in first.tool_results] == ["update_requirements", "match_requirements"]
        response = await engine.chat(ChatRequest(message="Team单价多少", session_id=first.session_id))
        assert response.business.questions == [] and response.business.pending == []
        assert response.business.unit_price.unit_price_minor == 8000
        assert set(engine.work.get(first.session_id).state.pending) == {"paid_seats", "months"}
        assert engine.work.get(first.session_id).state.active_intent == "selection"
        assert "产品 API" in json.dumps(client.captured[-1]["messages"], ensure_ascii=False)
        assert_stored_exactly(engine, response, "Team单价多少")
        resumed = await engine.chat(ChatRequest(message="明确购买80个付费席位，订阅一年，继续计价", session_id=first.session_id))
        assert resumed.status == "completed" and resumed.business.pricing.base_price_minor == 7680000
        assert {i.feature_id for i in resumed.business.requirements.items} == {"api_access"}
        assert response.message in [m["content"] for m in client.captured[5]["messages"] if m["role"] == "assistant"]
        assert_stored_exactly(engine, resumed, "明确购买80个付费席位，订阅一年，继续计价")
    asyncio.run(run())


def test_later_empty_candidate_does_not_erase_explicit_deletion_from_final_text(engine, monkeypatch):
    session = engine.conversations.ensure_conversation(None, "原需求")
    state = engine.work.get(session).state
    state.apply_patch({"intent": "selection", "add": [
        {"statement": "任务与项目协作", "user_quote": "项目协作", "feature_id": "project_collaboration"},
        {"statement": "产品API", "user_quote": "产品API", "feature_id": "api_access"}]}, "项目协作和产品API必须有")
    target = next(i for i in state.requirements.items if i.feature_id == "api_access")
    scripted(monkeypatch, [
        completion(tool("update_requirements", {"intent": "selection", "remove": [
            {"requirement_id": target.requirement_id, "user_quote": "取消产品API"}]})),
        completion(tool("update_requirements", {"intent": "selection"}, "second-update")),
        completion(tool("match_requirements", {})), completion(),
    ])
    response = asyncio.run(engine.chat(ChatRequest(message="取消产品API，其他要求不变", session_id=session)))
    assert response.business.match.lowest_plan_id == "basic"
    assert {i.feature_id for i in response.business.requirements.items} == {"project_collaboration"}
    assert "已撤回硬需求：必须支持 产品 API" in response.business.changes
    assert "已撤回硬需求：" + target.statement in response.message
    assert_stored_exactly(engine, response, "取消产品API，其他要求不变")


def test_history_transaction_failure_preserves_results_and_does_not_commit_half_turn(engine, monkeypatch):
    from app.db.session import connect_saas
    session = engine.conversations.ensure_conversation(None, "Team单价")
    with connect_saas(engine.settings) as db:
        db.execute("""CREATE TRIGGER fail_assistant BEFORE INSERT ON conversation_messages
                      WHEN NEW.role = 'assistant' BEGIN SELECT RAISE(ABORT, 'injected write failure'); END""")
    scripted(monkeypatch, [completion(tool("update_requirements", {"intent": "unit_price"})),
                          completion(tool("query_catalog", {"plan_id": "team"})), completion()])
    response = asyncio.run(engine.chat(ChatRequest(message="Team单价", session_id=session)))
    assert response.status == "incomplete" and error_codes(response) == {"history_write_unconfirmed"}
    assert response.business.unit_price.unit_price_minor == 8000 and "80.00" in response.message
    assert [t["name"] for t in response.tool_results] == ["update_requirements", "query_catalog"]
    assert all(t["result"]["ok"] for t in response.tool_results)
    assert engine.conversations.get_messages(session) == []


@pytest.mark.parametrize("name,arguments,code", [
    ("query_catalog", '{"plan_id":', "invalid_arguments"),
    ("create_quote", {}, "unknown_tool"),
    ("save_memory", {"content": "secret"}, "unknown_tool"),
    ("query_catalog", {"plan_id": True}, "invalid_arguments"),
    ("calculate_price", {"plan_id": "team", "paid_seats": 0, "months": 12}, "invalid_arguments"),
])
def test_bad_calls_stop_and_keep_actual_error_record(engine, monkeypatch, name, arguments, code):
    client = scripted(monkeypatch, [completion(tool("update_requirements", {"intent": "general"}, "normalize"), tool(name, arguments))])
    response = asyncio.run(engine.chat(ChatRequest(message="核对这项只读请求")))
    assert response.status == "incomplete"
    assert error_codes(response) == {code}
    assert len(client.captured) == 1
    assert len(response.tool_results) == 2 and len(client.review_calls) == 1
    assert response.tool_results[-1]["name"] == name
    assert response.tool_results[-1]["result"]["ok"] is False
    assert response.business.match is None and response.business.pricing is None
    assert_stored_exactly(engine, response, "核对这项只读请求")


def test_initial_model_failure_is_nonempty_and_persisted(engine, monkeypatch):
    scripted(monkeypatch, [RuntimeError("fake provider failure containing PRIVATE_KEY_SENTINEL")])
    response = asyncio.run(engine.chat(ChatRequest(message="Team单价")))
    assert response.status == "incomplete" and response.message.strip()
    assert error_codes(response) == {"model_unavailable"}
    assert response.tool_results == []
    assert "PRIVATE_KEY_SENTINEL" not in response.message
    assert_stored_exactly(engine, response, "Team单价")


def test_model_failure_after_real_rate_keeps_rate_and_record(engine, monkeypatch):
    scripted(monkeypatch, [completion(tool("update_requirements", {"intent": "unit_price"}, "normalize"),
        tool("query_catalog", {"plan_id": "team"})), RuntimeError("failed")])
    response = asyncio.run(engine.chat(ChatRequest(message="Team每席多少钱")))
    assert response.status == "incomplete"
    assert error_codes(response) == {"model_unavailable"}
    assert response.business.unit_price.unit_price_minor == 8000
    assert "80.00 元／席／月" in response.message
    assert response.tool_results[-1]["result"]["data"]["unit_price_minor"] == 8000
    assert_stored_exactly(engine, response, "Team每席多少钱")


def test_retrieval_timeout_propagates_actual_failure_observation(engine, monkeypatch):
    # Only the native worker is substituted. KnowledgeService still issues its
    # own failure observation; Registry and the response use that exact record.
    session = engine.conversations.ensure_conversation(None, "timeout")
    state = engine.work.get(session).state

    def worker_timeout(request):
        raise subprocess.TimeoutExpired("controlled-native-worker", 0.01)

    monkeypatch.setattr(state.knowledge, "_execute", worker_timeout)
    client = scripted(monkeypatch, [completion(tool("update_requirements", {"intent": "knowledge"}, "normalize"),
        tool("search_knowledge", {"query": "SSO使用前提"}))])
    response = asyncio.run(engine.chat(ChatRequest(message="SSO使用前提", session_id=session)))
    assert response.status == "incomplete"
    assert error_codes(response) == {"retrieval_unavailable"}
    # Current scope contract reports its own incompleteness; the real retrieval
    # timeout remains authoritative instead of a duplicate intent-wide error.
    assert response.business.consultation_coverage.status == "incomplete"
    assert not response.business.consultation_coverage.scope_complete
    assert len(client.captured) == 1
    observation = response.business.retrieval_evidence[0]
    assert observation.status == "unavailable" and observation.reason_codes == ("retrieval_timeout",)
    assert observation == state.knowledge.evidence(observation.retrieval_id)
    assert response.tool_results[-1]["result"]["ok"] is True
    assert response.tool_results[-1]["result"]["data"]["retrieval_id"] == observation.retrieval_id
    assert "资料查询失败" in response.message and "单价" in response.message
    assert response.business.match is None and response.business.pricing is None
    assert_stored_exactly(engine, response, "SSO使用前提")


@pytest.mark.parametrize("body", ["", "   ", None])
def test_empty_model_body_is_not_success(engine, monkeypatch, body):
    scripted(monkeypatch, [completion(content=body)])
    response = asyncio.run(engine.chat(ChatRequest(message="请查询")))
    assert response.status == "incomplete" and response.message.strip()
    assert error_codes(response) == {"empty_model_response", "semantic_review_required"}


@pytest.mark.parametrize("response", [SimpleNamespace(choices=[]), SimpleNamespace(choices=[None])])
def test_malformed_model_envelope_stops_without_blank_success(engine, monkeypatch, response):
    scripted(monkeypatch, [response])
    result = asyncio.run(engine.chat(ChatRequest(message="查询")))
    assert result.status == "incomplete" and result.message.strip()
    assert error_codes(result) == {"bad_model_response"}


def test_model_output_limit_is_reported_even_with_body(engine, monkeypatch):
    scripted(monkeypatch, [completion(content="残缺正文", finish_reason="length")])
    response = asyncio.run(engine.chat(ChatRequest(message="查询")))
    assert response.status == "incomplete"
    assert error_codes(response) == {"model_output_exhausted", "semantic_review_required"}


def test_model_timeout_has_finite_wait(engine, monkeypatch):
    class NeverFinishes:
        def __init__(self, settings):
            pass

        async def chat(self, **kwargs):
            await asyncio.Event().wait()

    monkeypatch.setattr(orchestrator_module, "DeepSeekClient", NeverFinishes)
    engine.MODEL_TIMEOUT_SECONDS = 0.01
    response = asyncio.run(engine.chat(ChatRequest(message="查询")))
    assert response.status == "incomplete"
    assert error_codes(response) == {"model_unavailable"}


def test_five_batches_do_not_execute_sixth_proposed_batch(engine, monkeypatch):
    client = scripted(monkeypatch, [completion(tool("update_requirements", {"intent": "unit_price"}, "normalize"),
        tool("query_catalog", {"plan_id": "team"}, "rate-1")),
        *(completion(tool("query_catalog", {"plan_id": "team"}, f"rate-{i}")) for i in range(2, 7))])
    response = asyncio.run(engine.chat(ChatRequest(message="Team单价")))
    assert response.status == "incomplete"
    assert error_codes(response) == {"tool_batches_exhausted"}
    assert len(client.captured) == 6
    assert len(response.tool_results) == 6 and len(client.review_calls) == 1
    assert [r["batch"] for r in response.tool_results] == [1, 1, 2, 3, 4, 5]
    assert [r["tool_call_id"] for r in response.tool_results] == ["normalize", *[f"rate-{i}" for i in range(1, 6)]]
    assert response.business.unit_price.unit_price_minor == 8000


def test_batch_over_four_is_rejected_before_any_domain_execution(engine, monkeypatch):
    client = scripted(monkeypatch, [completion(*(tool("query_catalog", {"plan_id": "team"}, str(i))
                                               for i in range(5)))])
    response = asyncio.run(engine.chat(ChatRequest(message="Team单价")))
    assert len(client.captured) == 1
    assert response.tool_results == [] and response.business.unit_price is None
    assert error_codes(response) == {"tool_batch_too_large"}


def test_duplicate_tool_call_ids_rejected_before_execution(engine, monkeypatch):
    scripted(monkeypatch, [completion(tool("query_catalog", {"plan_id": "team"}, "duplicate"),
                                      tool("query_catalog", {"plan_id": "basic"}, "duplicate"))])
    response = asyncio.run(engine.chat(ChatRequest(message="比较单价")))
    assert response.status == "incomplete" and response.tool_results == []
    assert error_codes(response) == {"model_request_invalid"}
    assert response.business.errors[0]["reason"] == "tool_call_identity_duplicate"


def test_arguments_budget_does_not_execute_large_call(engine, monkeypatch):
    raw = '{"plan_id":"team"}' + " " * engine.MAX_ARGUMENT_CHARS
    scripted(monkeypatch, [completion(tool("query_catalog", raw))])
    response = asyncio.run(engine.chat(ChatRequest(message="Team单价")))
    assert response.status == "incomplete"
    assert response.business.unit_price is None
    assert response.tool_results[0]["result"]["error"]["code"] == "arguments_budget"


def test_result_budget_keeps_already_acquired_real_domain_result(engine, monkeypatch):
    execute = BusinessToolRegistry.execute
    async def cutoff_after_rate(registry, name, arguments):
        value = await execute(registry, name, arguments)
        if name == "query_catalog":
            engine.MAX_RESULT_CHARS = 100
        return value
    monkeypatch.setattr(BusinessToolRegistry, "execute", cutoff_after_rate)
    client = scripted(monkeypatch, [completion(tool("update_requirements", {"intent": "unit_price"}, "normalize"),
        tool("query_catalog", {"plan_id": "team"}))])
    response = asyncio.run(engine.chat(ChatRequest(message="Team单价")))
    assert len(client.captured) == 1
    assert error_codes(response) == {"tool_result_budget"}
    assert response.tool_results[-1]["result"]["ok"] is True
    assert response.business.unit_price.unit_price_minor == 8000
    assert "80.00 元／席／月" in response.message


def _initial_payload(engine, message):
    session = engine.conversations.ensure_conversation(None, message)
    state = engine.work.get(session).state
    messages = engine.prompt_builder.build_messages(user_message=message,
        history=[], business_state=state.prompt_state())
    schemas = BusinessToolRegistry(engine.settings, state, message).schemas()
    payload = json.dumps({"messages": messages, "tools": schemas}, ensure_ascii=False)
    return session, payload


def test_schema_and_output_reserve_count_in_initial_context_budget(engine, monkeypatch):
    session, payload = _initial_payload(engine, "Team单价")
    monkeypatch.setattr(orchestrator_module, "count_text_tokens", len)
    message_only = len(json.dumps({"messages": json.loads(payload)["messages"], "tools": []}, ensure_ascii=False))
    engine.settings.deepseek_max_context_tokens = message_only + engine.settings.deepseek_default_max_completion_tokens + 256
    client = scripted(monkeypatch, [])
    response = asyncio.run(engine.chat(ChatRequest(message="Team单价", session_id=session)))
    assert client.captured == [] and response.tool_results == []
    assert error_codes(response) == {"invalid_or_budget"}


@pytest.mark.parametrize("component", ["tool_call", "arguments", "result"])
def test_appended_complete_tool_context_rechecks_budget(engine, monkeypatch, component):
    session, payload = _initial_payload(engine, "Team单价")
    monkeypatch.setattr(orchestrator_module, "count_text_tokens", len)
    engine.settings.deepseek_max_context_tokens = len(payload) + engine.settings.deepseek_default_max_completion_tokens + 256 + 1000
    call = tool("query_catalog", {} if component == "result" else {"plan_id": "team"})
    body = "x" * 3000 if component == "tool_call" else "工具调用"
    if component == "arguments":
        call.function.arguments += " " * 3000
    client = scripted(monkeypatch, [completion(tool("update_requirements", {"intent": "unit_price"}, "normalize"), call, content=body)])
    response = asyncio.run(engine.chat(ChatRequest(message="Team单价", session_id=session)))
    assert len(client.captured) == 1 and len(response.tool_results) == 2
    assert response.tool_results[-1]["result"]["ok"] is True
    assert error_codes(response) == {"invalid_or_budget"}
    if component == "result":
        assert response.business.catalog is not None
    else:
        assert response.business.unit_price is not None


def test_conflicting_free_text_cannot_reenter_response_history_or_next_input(engine, monkeypatch):
    wrong = "Business完全满足API且只需1.00元；已保存BAD-QUOTE-ID"
    quote = "需要API，明确购买80席一年，帮我算基价"
    patch = {"intent": "selection", "add": [{"feature_id": "api_access", "statement": "产品API",
              "user_quote": "需要API", "kind": "feature"}],
        "parameters": [{"name": "paid_seats", "value": 80, "user_quote": "明确购买80席"},
                       {"name": "months", "value": 12, "user_quote": "一年"}],
        "pricing_quote": "帮我算基价"}
    client = scripted(monkeypatch, [
        completion(tool("update_requirements", patch), tool("match_requirements", {}, "match"), content=wrong),
        completion(tool("calculate_price", {"plan_id": "team", "paid_seats": 80, "months": 12}, "price"), content=wrong),
        completion(content=wrong), completion(content=wrong),
    ])

    async def run():
        first = await engine.chat(ChatRequest(message=quote))
        history = engine.conversations.get_messages(first.session_id)
        second = await engine.chat(ChatRequest(message="说明刚才的结果", session_id=first.session_id))
        return first, history, second

    first, history, second = asyncio.run(run())
    assert first.status == "completed"
    assert first.business.match.lowest_plan_id == "team"
    assert first.business.pricing.base_price_minor == 7_680_000
    assert "76,800.00 元" in first.message
    assert first.business.match.lowest_plan_id == "team"
    assert "建议采用团队版 Team" in first.message
    assert history[-1].content == first.message
    assert all(wrong not in item.content for item in history)
    assert wrong not in first.message and wrong not in second.message
    assert "BAD-QUOTE-ID" not in json.dumps(client.captured[-1]["messages"], ensure_ascii=False)
    next_assistant_history = [m["content"] for m in client.captured[-1]["messages"] if m["role"] == "assistant"]
    assert next_assistant_history == [first.message]
    assert_stored_exactly(engine, second, "说明刚才的结果")


def _selection_patch(feature_id, statement, quote):
    return {"intent": "selection", "add": [{"feature_id": feature_id, "statement": statement,
                                            "kind": "feature", "user_quote": quote}]}


def test_same_session_lock_covers_history_state_tools_and_persistence(engine, monkeypatch):
    async def run():
        entered_first, release_first, started_second, entered_second = [asyncio.Event() for _ in range(4)]
        captured_second = []

        class OrderedClient:
            review_calls = []
            def __init__(self, settings):
                self.round = 0

            async def chat(self, **kwargs):
                if is_review_request(kwargs):
                    return answer_review(type(self), kwargs)
                self.round += 1
                if self.round > 1:
                    return completion()
                current = [m["content"] for m in kwargs["messages"] if m["role"] == "user"][-1]
                if current == "需要项目协作":
                    entered_first.set()
                    await release_first.wait()
                    patch = _selection_patch("project_collaboration", "任务与项目协作", current)
                else:
                    entered_second.set()
                    captured_second.append(deepcopy(kwargs["messages"]))
                    patch = _selection_patch("api_access", "产品API", current)
                return completion(tool("update_requirements", patch), tool("match_requirements", {}, "match"))

        monkeypatch.setattr(orchestrator_module, "DeepSeekClient", OrderedClient)
        session = engine.conversations.ensure_conversation(None, "ordered")
        first_task = asyncio.create_task(engine.chat(ChatRequest(message="需要项目协作", session_id=session)))
        await asyncio.wait_for(entered_first.wait(), 1)

        async def second_turn():
            started_second.set()
            return await engine.chat(ChatRequest(message="增加API", session_id=session))

        second_task = asyncio.create_task(second_turn())
        await started_second.wait()
        await asyncio.sleep(0)
        assert not entered_second.is_set() and not second_task.done()
        release_first.set()
        first, second = await asyncio.wait_for(asyncio.gather(first_task, second_task), 3)
        return first, second, captured_second[0]

    first, second, second_messages = asyncio.run(run())
    assert first.business.match.lowest_plan_id == "basic"
    assert second.business.match.lowest_plan_id == "team"
    assert {i.feature_id for i in second.business.requirements.items} == {"project_collaboration", "api_access"}
    assert [m["content"] for m in second_messages if m["role"] == "assistant"] == [first.message]
    history = engine.conversations.get_messages(first.session_id)
    assert [m.content for m in history] == ["需要项目协作", first.message, "增加API", second.message]


def test_different_sessions_run_in_parallel_and_keep_separate_state(engine, monkeypatch):
    async def run():
        entered = {"需要项目协作": asyncio.Event(), "需要API": asyncio.Event()}
        release = asyncio.Event()

        class ParallelClient:
            review_calls = []
            def __init__(self, settings):
                self.round = 0

            async def chat(self, **kwargs):
                if is_review_request(kwargs):
                    return answer_review(type(self), kwargs)
                self.round += 1
                if self.round > 1:
                    return completion()
                current = [m["content"] for m in kwargs["messages"] if m["role"] == "user"][-1]
                entered[current].set()
                await release.wait()
                feature, statement = (("project_collaboration", "任务与项目协作") if current == "需要项目协作"
                                      else ("api_access", "产品API"))
                return completion(tool("update_requirements", _selection_patch(feature, statement, current)),
                                  tool("match_requirements", {}, "match"))

        monkeypatch.setattr(orchestrator_module, "DeepSeekClient", ParallelClient)
        sessions = [engine.conversations.ensure_conversation(None, text) for text in entered]
        tasks = [asyncio.create_task(engine.chat(ChatRequest(message=text, session_id=session)))
                 for text, session in zip(entered, sessions)]
        try:
            await asyncio.wait_for(asyncio.gather(*(event.wait() for event in entered.values())), 1)
        finally:
            release.set()
        return await asyncio.wait_for(asyncio.gather(*tasks), 3)

    first, second = asyncio.run(run())
    assert first.session_id != second.session_id
    assert first.business.requirements.requirement_id != second.business.requirements.requirement_id
    assert {i.feature_id for i in first.business.requirements.items} == {"project_collaboration"}
    assert {i.feature_id for i in second.business.requirements.items} == {"api_access"}
    assert first.business.match.lowest_plan_id == "basic" and second.business.match.lowest_plan_id == "team"


def test_restart_retains_text_without_restoring_authority_from_old_assistant(engine, monkeypatch):
    session = engine.conversations.ensure_conversation(None, "past")
    old_text = "旧助手声称已确认80席12月、兼容身份系统，并且Business已适配。"
    engine.conversations.add_turn(session, "之前的需求", old_text)
    restarted = Orchestrator(engine.settings)
    client = scripted(monkeypatch, [completion(tool("update_requirements", {"intent": "price"})), completion()])
    response = asyncio.run(restarted.chat(ChatRequest(message="继续计算", session_id=session)))
    assert response.status == "needs_input"
    assert set(response.business.pending) == {"paid_seats", "months", "plan"}
    assert response.business.requirements.items == ()
    assert response.business.requirements.parameters == ()
    assert response.business.requirements.condition_assertions == ()
    assert response.business.pricing is None and response.business.match is None
    messages = client.captured[0]["messages"]
    assert old_text in [m["content"] for m in messages if m["role"] == "assistant"]
    current_state = json.loads(messages[1]["content"].split("：", 1)[1])["state"]
    assert current_state["requirements"]["parameters"] == []
    assert current_state["requirements"]["condition_assertions"] == []
    assert current_state["recovery"] == "process_local; history is not accepted requirements"
    assert_stored_exactly(restarted, response, "继续计算")


def test_first_request_requires_normalization_then_tools_return_to_auto(engine, monkeypatch):
    client = scripted(monkeypatch, [
        completion(tool("update_requirements", {"intent": "unit_price"}),
                   tool("query_catalog", {"plan_id": "team"}, "rate")),
        completion(),
    ])
    response = asyncio.run(engine.chat(ChatRequest(message="Team单价")))
    assert response.status == "completed" and response.business.unit_price.unit_price_minor == 8000
    assert client.captured[0]["tool_choice"] == {
        "type": "function", "function": {"name": "update_requirements"}}
    assert client.captured[1]["tool_choice"] == "auto"


def test_rejected_update_stops_same_batch_before_old_match_or_price(engine, monkeypatch):
    session = engine.conversations.ensure_conversation(None, "old scope")
    state = engine.work.get(session).state
    state.apply_patch({"intent": "selection", "add": [{"feature_id": "api_access",
        "statement": "产品API", "kind": "feature", "user_quote": "需要API"}],
        "parameters": [{"name": "paid_seats", "value": 80, "user_quote": "80席"},
                       {"name": "months", "value": 12, "user_quote": "一年"}]}, "需要API，80席一年")
    state.match()
    old = state.requirements
    client = scripted(monkeypatch, [completion(
        tool("update_requirements", {"intent": "selection", "parameters": [
            {"name": "paid_seats", "value": 80, "user_quote": "约80人"}]}),
        tool("match_requirements", {}, "should-not-match"),
        tool("calculate_price", {"plan_id": "team", "paid_seats": 80, "months": 12}, "should-not-price"),
    )])
    response = asyncio.run(engine.chat(ChatRequest(message="约80人", session_id=session)))
    assert response.status == "needs_input"
    assert len(client.captured) == 1
    assert [record["name"] for record in response.tool_results] == ["update_requirements"]
    assert response.business.operation_feedback[0]["issue"] == "uncertain"
    assert response.business.operation_feedback[0]["status"] == "rejected"
    assert state.requirements == old
    assert response.business.match is None and response.business.pricing is None


@pytest.mark.parametrize("include_price_call", [False, True])
def test_independent_price_missing_months_stops_before_guessed_model_calculation(engine, monkeypatch, include_price_call):
    calls = [tool("update_requirements", {"intent": "price", "selected_plan_id": "business",
        "selection_quote": "Business", "pricing_quote": "算基价", "parameters": [
            {"name": "paid_seats", "value": 80, "user_quote": "80席"}]})]
    if include_price_call:
        calls.append(tool("calculate_price", {"plan_id": "business", "paid_seats": 80, "months": 12}, "not-executed"))
    client = scripted(monkeypatch, [completion(*calls)])
    response = asyncio.run(engine.chat(ChatRequest(message="Business 80席，算基价")))
    assert response.status == "needs_input" and response.business.pending == ["months"]
    assert response.business.pricing is None and not response.business.errors
    assert len(client.captured) == 1
    assert [r["name"] for r in response.tool_results] == ["update_requirements"]
    assert "订阅" in response.message and "80 个付费席位" in response.message
    assert_stored_exactly(engine, response, "Business 80席，算基价")


def test_one_missing_result_reminder_can_obtain_real_matching(engine, monkeypatch):
    patch = _selection_patch("api_access", "产品API", "需要API")
    client = scripted(monkeypatch, [completion(tool("update_requirements", patch)),
        completion(content="已经选好了"), completion(tool("match_requirements", {}, "repaired-match")),
        completion()])
    response = asyncio.run(engine.chat(ChatRequest(message="需要API")))
    assert response.status == "completed" and response.business.match.lowest_plan_id == "team"
    assert [record["name"] for record in response.tool_results] == ["update_requirements", "match_requirements"]
    assert [record["batch"] for record in response.tool_results] == [1, 2]
    reminders = [message for message in client.captured[2]["messages"]
                 if message["role"] == "system" and "唯一一次补全提醒" in message["content"]]
    assert len(reminders) == 1 and "match_requirements" in reminders[0]["content"]
    assert "已经选好了" not in response.message


def test_second_early_stop_does_not_repeat_reminder_or_claim_matching(engine, monkeypatch):
    client = scripted(monkeypatch, [completion(tool("update_requirements",
        _selection_patch("api_access", "产品API", "需要API"))),
        completion(content="声称选好了"), completion(content="再次声称选好了")])
    response = asyncio.run(engine.chat(ChatRequest(message="需要API")))
    assert response.status == "incomplete"
    assert error_codes(response) == {"matching_not_completed"}
    assert len(client.captured) == 3 and len(response.tool_results) == 1
    assert sum(message["role"] == "system" and "唯一一次补全提醒" in message["content"]
               for message in client.captured[-1]["messages"]) == 1
    assert response.business.match is None and response.business.pricing is None
    assert "声称选好了" not in response.message


def test_reminder_never_grants_a_sixth_execution_batch(engine, monkeypatch):
    client = scripted(monkeypatch, [completion(tool("update_requirements",
        _selection_patch("api_access", "产品API", "需要API"))),
        completion(content="提前结束"),
        *(completion(tool("query_catalog", {"plan_id": "team"}, f"extra-rate-{i}")) for i in range(4)),
        completion(tool("match_requirements", {}, "forbidden-sixth-batch")),
    ])
    response = asyncio.run(engine.chat(ChatRequest(message="需要API")))
    assert response.status == "incomplete"
    assert error_codes(response) == {"tool_batches_exhausted", "matching_not_completed"}
    assert len(client.captured) == 7 and len(response.tool_results) == 5
    assert [record["batch"] for record in response.tool_results] == [1, 2, 3, 4, 5]
    assert all(record["tool_call_id"] != "forbidden-sixth-batch" for record in response.tool_results)
    assert response.business.unit_price.unit_price_minor == 8000
    assert response.business.match is None


def test_budget_rechecked_after_the_single_reminder_preserves_accepted_state(engine, monkeypatch):
    patch = _selection_patch("api_access", "产品API", "需要API")
    session, _ = _initial_payload(engine, "需要API")

    def token_count(serialized):
        # Substitute only the estimator to place the cutoff exactly between
        # the premature finish and the next request containing its reminder.
        return engine.settings.deepseek_max_context_tokens if "唯一一次补全提醒" in serialized else 1

    monkeypatch.setattr(orchestrator_module, "count_text_tokens", token_count)
    client = scripted(monkeypatch, [completion(tool("update_requirements", patch)), completion()])
    response = asyncio.run(engine.chat(ChatRequest(message="需要API", session_id=session)))
    assert response.status == "incomplete"
    assert error_codes(response) == {"invalid_or_budget", "matching_not_completed"}
    assert len(client.captured) == 2 and len(response.tool_results) == 1
    assert response.business.requirements.items[0].feature_id == "api_access"
    assert response.business.match is None


def _joint_requirement_reference_candidates():
    message = "任务项目协作和数据导出必须有，还需要产品API，帮我推荐套餐。"
    candidate = {"intent": "selection", "add": [
        {"statement": "任务项目协作必须有", "feature_id": "project_collaboration",
         "user_quote": "任务项目协作必须有"},
        {"statement": "数据导出必须有", "feature_id": "data_export", "user_quote": "数据导出必须有"},
        {"statement": "产品API必须有", "feature_id": "api_access", "user_quote": "产品API"}]}
    corrected = deepcopy(candidate)
    corrected["add"][0]["user_quote"] = "任务项目协作和数据导出必须有"
    return message, candidate, corrected


def _spy_registry_execution(monkeypatch):
    executed = []
    original = BusinessToolRegistry.execute

    async def tracked(registry, name, arguments):
        executed.append(name)
        return await original(registry, name, arguments)

    monkeypatch.setattr(BusinessToolRegistry, "execute", tracked)
    return executed


def _assert_complete_tool_protocol(messages):
    """Protocol replies include skipped proposals, not fabricated executions."""
    for index, message in enumerate(messages):
        calls = message.get("tool_calls", []) if message["role"] == "assistant" else []
        if not calls:
            continue
        end = next((i for i in range(index + 1, len(messages)) if messages[i]["role"] == "assistant"), len(messages))
        replies = [item for item in messages[index + 1:end] if item["role"] == "tool"]
        assert [item["tool_call_id"] for item in replies] == [call["id"] for call in calls]
        assert all(type(json.loads(item["content"])) is dict for item in replies)


def _atomic_review(message, candidate, *, reason=None):
    """Explicit controlled group, still checked by actual WorkState."""
    review = review_from_candidate({"original_message": message, "first_candidate": candidate,
        "current_tasks": {}, "identities": {}})
    operations = [op for group in review["groups"] for op in group["operations"]]
    review["groups"] = [{"group_id": "atomic-controlled", "verdict": "reject" if reason else "accept",
        "reason": reason or "confirmed", "operations": operations}]
    return review


def test_joint_natural_requirements_repair_only_misquoted_reference_and_keep_history_same_source(engine, monkeypatch):
    message, candidate, corrected = _joint_requirement_reference_candidates()
    client = scripted(monkeypatch, [
        completion(tool("update_requirements", candidate, "misquoted-update")),
        completion(tool("match_requirements", {}, "real-match")), completion(),
        completion(tool("update_requirements", {"intent": "unit_price"}, "rate-update"),
            tool("query_catalog", {"plan_id": "team"}, "real-rate")), completion(),
    ], review_candidates=[corrected])
    async def run():
        response = await engine.chat(ChatRequest(message=message))
        assert response.status == "completed" and response.business.errors == []
        assert response.business.match.lowest_plan_id == response.business.match.selected_plan_id == "team"
        assert {item.feature_id for item in response.business.requirements.items} == {
            "project_collaboration", "data_export", "api_access"}
        assert response.business.requirements.items[0].user_quote == corrected["add"][0]["user_quote"]
        assert response.business.requirements.items[0].statement == "必须支持 任务与项目协作"
        assert [record["name"] for record in response.tool_results] == ["update_requirements", "match_requirements"]
        assert response.tool_results[0]["arguments"] == json.dumps(candidate, ensure_ascii=False)
        assert response.tool_results[0]["result"]["ok"]
        assert len(client.review_calls) == 1 and response.business.semantic_review["dispatched"] == 1
        assert "未能正确接收" not in response.message and "尚未更新" not in response.message
        assert_stored_exactly(engine, response, message)
        _assert_complete_tool_protocol(client.captured[1]["messages"])
        next_request = len(client.captured)
        followup = await engine.chat(ChatRequest(message="Team单价多少", session_id=response.session_id))
        ordinary = [item["content"] for item in client.captured[next_request]["messages"] if item["role"] == "assistant"]
        assert response.message in ordinary
        assert not any(candidate["add"][0]["user_quote"] in item for item in ordinary)
        assert followup.business.unit_price.unit_price_minor == 8000 and len(client.review_calls) == 2
        assert_stored_exactly(engine, followup, "Team单价多少")
    asyncio.run(run())


def test_reference_repair_is_attempted_once_when_review_reference_is_still_missing(engine, monkeypatch):
    message, candidate, _ = _joint_requirement_reference_candidates()
    invalid = _atomic_review(message, candidate)
    client = scripted(monkeypatch, [completion(tool("update_requirements", candidate, "first-bad"))],
        review_outputs=[review_completion(invalid)])
    response = asyncio.run(engine.chat(ChatRequest(message=message)))
    assert len(client.captured) == len(client.review_calls) == 1
    assert response.business.semantic_review["dispatched"] == 1
    assert len(response.tool_results) == 1
    assert response.business.operation_feedback[0]["issue"] == "reference_missing"
    assert response.business.requirements.items == () and response.business.proposal is None
    assert response.business.match is None and response.business.pricing is None
    assert_stored_exactly(engine, response, message)


def test_failed_review_batch_skips_dependents_without_a_second_correction(engine, monkeypatch):
    message, candidate, _ = _joint_requirement_reference_candidates()
    executed = _spy_registry_execution(monkeypatch)
    client = scripted(monkeypatch, [completion(tool("update_requirements", candidate, "bad"),
        tool("match_requirements", {}, "blocked-match"),
        tool("calculate_price", {"plan_id": "team", "paid_seats": 80, "months": 12}, "blocked-price"))],
        review_outputs=[review_completion({"intent": "selection"})])
    response = asyncio.run(engine.chat(ChatRequest(message=message)))
    assert response.status == "incomplete" and error_codes(response) == {"semantic_review_invalid"}
    assert executed == [] and len(client.captured) == len(client.review_calls) == 1
    assert [record["tool_call_id"] for record in response.tool_results] == ["bad"]
    assert not response.tool_results[0]["result"]["ok"]
    assert response.business.requirements.items == ()
    assert response.business.match is None and response.business.pricing is None and response.business.proposal is None


@pytest.mark.parametrize("change", ["feature-identity", "selected-plan", "parameter-value", "condition-polarity"])
def test_reference_review_cannot_change_nonreference_candidate_facts(engine, monkeypatch, change):
    # These are concrete fact disagreements, rather than the removed legacy
    # requirement that a *second update* must byte-match the first candidate.
    if change == "feature-identity":
        message = "需要API"
        candidate = _selection_patch("api_access", "产品API", message)
        corrected = _selection_patch("data_export", "数据导出", message)
    elif change == "selected-plan":
        message = "主动选择Team"
        candidate = {"intent": "selection", "selected_plan_id": "team", "selection_quote": message}
        corrected = {**candidate, "selected_plan_id": "business"}
    elif change == "parameter-value":
        message = "明确购买80席"
        candidate = {"intent": "selection", "parameters": [{"name": "paid_seats", "value": 80, "user_quote": message}]}
        corrected = deepcopy(candidate)
        corrected["parameters"][0]["value"] = 70
    else:
        message = "SSO必须有，我们目前具备兼容身份提供方"
        candidate = {"intent": "selection", "add": [{"statement": "SSO必须有", "feature_id": "saml_sso", "user_quote": "SSO必须有"}],
            "conditions": [{"condition_id": "saml_sso_compatible_idp", "value": "yes", "user_quote": "我们目前具备兼容身份提供方"}]}
        corrected = deepcopy(candidate)
        corrected["conditions"][0]["value"] = "no"
    executed = _spy_registry_execution(monkeypatch)
    client = scripted(monkeypatch, [completion(tool("update_requirements", candidate, "original"))],
        review_outputs=[review_completion(_atomic_review(message, corrected))])
    response = asyncio.run(engine.chat(ChatRequest(message=message)))
    assert len(client.captured) == len(client.review_calls) == 1 and executed == []
    assert response.business.operation_feedback[0]["status"] == "rejected"
    assert response.business.operation_feedback[0]["issue"] == "candidate_disagreement"
    assert response.business.requirements.items == () and response.business.requirements.parameters == ()
    assert response.business.requirements.condition_assertions == () and response.business.requirements.selected_plan_id is None
    assert response.business.match is None and response.business.pricing is None and response.business.proposal is None


def test_reference_review_cannot_execute_a_batch_with_adjudication_and_dependent_tools(engine, monkeypatch):
    message, candidate, corrected = _joint_requirement_reference_candidates()
    executed = _spy_registry_execution(monkeypatch)
    unsafe = completion(tool("adjudicate_turn", _atomic_review(message, corrected), "unsafe-review"),
        tool("match_requirements", {}, "unpermitted-dependent"))
    client = scripted(monkeypatch, [completion(tool("update_requirements", candidate, "first"))], review_outputs=[unsafe])
    response = asyncio.run(engine.chat(ChatRequest(message=message)))
    assert len(client.captured) == len(client.review_calls) == 1 and executed == []
    assert error_codes(response) == {"semantic_review_invalid"}
    assert response.business.requirements.items == () and response.business.match is None
    assert [record["tool_call_id"] for record in response.tool_results] == ["first"]


@pytest.mark.parametrize("first_reference_failure", [False, True])
@pytest.mark.parametrize("danger,issue", [("approximate-seats", "uncertain"),
    ("future-idp", "future"), ("wrong-deletion", "unknown_identity")])
def test_reference_review_never_relaxes_quantity_condition_or_deletion_guards(engine, monkeypatch, danger, issue, first_reference_failure):
    session = engine.conversations.ensure_conversation(None, "prior accepted scope")
    state = engine.work.get(session).state
    state.apply_patch({"intent": "selection", "add": [{"statement": "产品API", "feature_id": "api_access", "user_quote": "需要API"}],
        "parameters": [{"name": "paid_seats", "value": 80, "user_quote": "80席"}, {"name": "months", "value": 12, "user_quote": "一年"}]}, "需要API，80席一年")
    previous = state.requirements
    if danger == "approximate-seats":
        message = "改为约80席，其他不变"
        patch = {"intent": "selection", "parameters": [{"name": "paid_seats", "value": 80, "user_quote": "80席"}]}
        bucket = "parameters"
    elif danger == "future-idp":
        message = "以后具备兼容 SAML 身份提供方"
        patch = {"intent": "selection", "conditions": [{"condition_id": "saml_sso_compatible_idp", "value": "yes", "user_quote": "兼容 SAML 身份提供方"}]}
        bucket = "conditions"
    else:
        message = "删除数据导出，产品API保留"
        patch = {"intent": "selection", "remove": [{"requirement_id": previous.items[0].requirement_id, "user_quote": "删除数据导出"}]}
        bucket = "remove"
    original = deepcopy(patch)
    if first_reference_failure:
        original[bucket][0]["user_quote"] = "模型改写了不在输入中的原话"
    client = scripted(monkeypatch, [completion(tool("update_requirements", original, "first"))], review_candidates=[patch])
    response = asyncio.run(engine.chat(ChatRequest(message=message, session_id=session)))
    assert len(client.captured) == len(client.review_calls) == 1
    assert state.requirements == previous
    assert response.business.operation_feedback[0]["issue"] == issue
    assert response.business.operation_feedback[0]["status"] == "rejected"
    assert response.business.match is None and response.business.pricing is None and response.business.proposal is None
    assert_stored_exactly(engine, response, message)


@pytest.mark.parametrize("prior_success", ["accepted-update", "same-batch-rate"])
def test_reference_review_is_not_repeated_after_an_accepted_update_or_other_success(engine, monkeypatch, prior_success):
    message, candidate, _ = _joint_requirement_reference_candidates()
    initial = {"intent": "selection"} if prior_success == "accepted-update" else {"intent": "unit_price"}
    calls = [tool("update_requirements", initial, "accepted")]
    if prior_success == "accepted-update":
        initial.update(_selection_patch("api_access", "产品API", "产品API"))
        calls[0] = tool("update_requirements", initial, "accepted")
    else:
        calls.append(tool("query_catalog", {"plan_id": "team"}, "accepted-rate"))
    calls.append(tool("update_requirements", candidate, "not-retriable"))
    client = scripted(monkeypatch, [completion(*calls)])
    response = asyncio.run(engine.chat(ChatRequest(message=message)))
    assert len(client.captured) == len(client.review_calls) == 1
    assert response.tool_results[-1]["result"]["error"]["details"]["issue"] == "unreviewed_mutation"
    if prior_success == "accepted-update":
        assert {item.feature_id for item in response.business.requirements.items} == {"api_access"}
    else:
        assert response.business.unit_price.unit_price_minor == 8000
    assert response.business.proposal is None
    assert_stored_exactly(engine, response, message)


def test_model_failure_after_reference_review_preserves_real_rate_without_exception_echo(engine, monkeypatch):
    candidate = {"intent": "unit_price", "context_quotes": ["Team单价多少钱"]}
    corrected = {"intent": "unit_price", "context_quotes": ["Team 每席每月多少钱"]}
    message = "Team 每席每月多少钱？"
    client = scripted(monkeypatch, [completion(tool("update_requirements", candidate, "wrong")),
        completion(tool("query_catalog", {"plan_id": "team"}, "actual-rate")),
        RuntimeError("quota failure containing PRIVATE_KEY_SENTINEL")], review_candidates=[corrected])
    response = asyncio.run(engine.chat(ChatRequest(message=message)))
    assert len(client.captured) == 3 and len(client.review_calls) == 1 and response.status == "incomplete"
    assert error_codes(response) == {"model_unavailable"}
    assert response.business.unit_price.unit_price_minor == 8000 and "80.00" in response.message
    assert "PRIVATE_KEY_SENTINEL" not in response.message
    assert response.tool_results[0]["result"]["ok"]
    assert_stored_exactly(engine, response, message)


def test_truncated_unparseable_reference_review_never_accepts_missing_candidate(engine, monkeypatch):
    message, candidate, _ = _joint_requirement_reference_candidates()
    broken = review_completion({"intent": "selection"}, finish_reason="length")
    client = scripted(monkeypatch, [completion(tool("update_requirements", candidate))], review_outputs=[broken])
    response = asyncio.run(engine.chat(ChatRequest(message=message)))
    assert len(client.captured) == len(client.review_calls) == 1
    assert error_codes(response) == {"semantic_review_output_exhausted"}
    assert response.business.semantic_review["finish_reason"] == "length"
    assert "独立核对输出达到本次额度" in response.message
    assert response.status == "incomplete" and response.business.requirements.items == ()
    assert response.business.match is None and response.business.proposal is None


def test_context_budget_is_rechecked_before_reference_review(engine, monkeypatch):
    message, candidate, _ = _joint_requirement_reference_candidates()
    from app.llm import token_counter
    def review_cutoff(serialized):
        try:
            value = json.loads(serialized)
        except (TypeError, ValueError):
            return 1
        if not isinstance(value, dict):
            return 1
        if any(item.get("function", {}).get("name") == "adjudicate_turn" for item in value.get("tools", [])):
            return engine.settings.deepseek_max_context_tokens
        return 1
    monkeypatch.setattr(token_counter, "count_text_tokens", review_cutoff)
    client = scripted(monkeypatch, [completion(tool("update_requirements", candidate))])
    response = asyncio.run(engine.chat(ChatRequest(message=message)))
    assert len(client.captured) == 1 and client.review_calls == []
    assert error_codes(response) == {"semantic_review_invalid"}
    assert response.business.semantic_review["dispatched"] == 0
    assert response.business.requirements.items == () and response.business.match is None


def test_reference_review_keeps_original_batch_budget_even_with_missing_result_reminder(engine, monkeypatch):
    message, candidate, corrected = _joint_requirement_reference_candidates()
    executed = _spy_registry_execution(monkeypatch)
    client = scripted(monkeypatch, [completion(tool("update_requirements", candidate, "reviewed-batch")),
        completion(content="尚未实际匹配"),
        *(completion(tool("query_catalog", {"plan_id": "team"}, f"rate-batch-{index}")) for index in range(5))],
        review_candidates=[corrected])
    response = asyncio.run(engine.chat(ChatRequest(message=message)))
    assert len(client.captured) == 7 and len(client.review_calls) == 1 and executed == ["query_catalog"] * 4
    assert error_codes(response) == {"tool_batches_exhausted", "matching_not_completed"}
    assert [record["batch"] for record in response.tool_results] == [1, 2, 3, 4, 5]
    assert all(record["tool_call_id"] != "rate-batch-4" for record in response.tool_results)
    assert response.business.unit_price.unit_price_minor == 8000 and response.business.match is None
