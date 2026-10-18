"""Controlled protocol/failure checks, without any real model dispatch."""

import asyncio
from copy import deepcopy
import json
import threading
from types import SimpleNamespace

import httpx
import pytest
from openai import APIStatusError

from app.agents import orchestrator as orchestrator_module
from app.agents.orchestrator import Orchestrator
from app.config import Settings
from app.db.session import init_saas_db
from app.llm.semantic_verifier import SemanticVerifier
from app.llm.tool_protocol import ModelRequestInvalid, ToolMessageProtocolError, validate_tool_message_sequence
from app.schemas.chat import ChatRequest
from app.tools.business import BusinessToolRegistry
from controlled_review import answer_review, is_review_request


def call(name, arguments, identity):
    return SimpleNamespace(id=identity, function=SimpleNamespace(name=name,
        arguments=json.dumps(arguments, ensure_ascii=False)))


def completion(*calls):
    return SimpleNamespace(model="controlled-protocol-provider", usage=None, choices=[SimpleNamespace(
        finish_reason="stop", message=SimpleNamespace(content="本轮结束", tool_calls=list(calls)))])


@pytest.fixture
def engine(tmp_path):
    settings = Settings(deepseek_api_key="test", deepseek_default_max_completion_tokens=512,
        saas_database_url=f"sqlite:///{tmp_path / 'protocol.db'}")
    init_saas_db(settings)
    return Orchestrator(settings)


def script(monkeypatch, outputs):
    class Provider:
        sent, live_messages, review_calls = [], [], []
        remaining = list(outputs)

        def __init__(self, settings):
            pass

        async def chat(self, **kwargs):
            if is_review_request(kwargs):
                return answer_review(type(self), kwargs)
            validate_tool_message_sequence(kwargs["messages"])
            type(self).sent.append(deepcopy(kwargs["messages"]))
            type(self).live_messages.append(kwargs["messages"])
            assert type(self).remaining, "Unexpected extra model invocation"
            result = type(self).remaining.pop(0)
            if isinstance(result, Exception):
                raise result
            return result

    monkeypatch.setattr(orchestrator_module, "DeepSeekClient", Provider)
    return Provider


def receipts(messages):
    validate_tool_message_sequence(messages)
    return {item["tool_call_id"]: json.loads(item["content"])
        for item in messages if item["role"] == "tool"}


@pytest.mark.parametrize("following", ["none", "search", "catalogs"])
def test_update_batch_receipts_are_contiguous_before_reminder(engine, monkeypatch, following):
    execute = BusinessToolRegistry.execute
    executed = []

    async def controlled_search(registry, name, arguments):
        executed.append(name)
        if name == "search_knowledge":
            return {"ok": True, "data": {"retrieval_id": "controlled-empty", "chain_id": "controlled",
                "query": "SSO前提", "retrieved_at": "controlled-time", "options": {}, "status": "empty"}}
        return await execute(registry, name, arguments)

    monkeypatch.setattr(BusinessToolRegistry, "execute", controlled_search)
    intent = "knowledge" if following == "search" else "unit_price"
    calls = [call("update_requirements", {"intent": intent}, "update")]
    if following == "search":
        calls.append(call("search_knowledge", {"query": "SSO前提"}, "search"))
    elif following == "catalogs":
        calls.extend([call("query_catalog", {"plan_id": "team"}, "team"),
            call("query_catalog", {"plan_id": "basic"}, "basic")])
    provider = script(monkeypatch, [completion(*calls), completion(), completion()])
    asyncio.run(engine.chat(ChatRequest(message="Team和Basic单价；SSO前提")))
    assert len(provider.sent) >= 2
    messages = provider.sent[1]
    batch_index = next(index for index, item in enumerate(messages) if item.get("tool_calls"))
    assert [item["role"] for item in messages[batch_index + 1:batch_index + 1 + len(calls)]] == ["tool"] * len(calls)
    assert [item["tool_call_id"] for item in messages[batch_index + 1:batch_index + 1 + len(calls)]] == [item.id for item in calls]
    assert messages[batch_index + 1 + len(calls)]["role"] == "system"
    assert len(provider.review_calls) == 1
    if following == "catalogs":
        assert executed == ["query_catalog", "query_catalog"]


def test_failed_precondition_closes_dependents_without_executing_them(engine, monkeypatch):
    executed = []
    monkeypatch.setattr(BusinessToolRegistry, "execute", lambda *args: executed.append(args))
    provider = script(monkeypatch, [completion(call("query_catalog", {"plan_id": "team"}, "early"),
        call("query_catalog", {"plan_id": "basic"}, "dependent"))])
    response = asyncio.run(engine.chat(ChatRequest(message="查询两档单价")))
    actual = receipts(provider.live_messages[0])
    assert actual["early"]["error"]["code"] == "semantic_review_required"
    assert actual["dependent"]["error"]["code"] == "dependency_not_executed"
    assert executed == [] and len(response.tool_results) == 1 and len(provider.sent) == 1


def test_result_budget_closes_success_and_unexecuted_remainder(engine, monkeypatch):
    execute = BusinessToolRegistry.execute
    executed = []

    async def rate_with_small_receipt_budget(registry, name, arguments):
        executed.append(name)
        result = await execute(registry, name, arguments)
        if name == "query_catalog":
            engine.MAX_RESULT_CHARS = 100
        return result

    monkeypatch.setattr(BusinessToolRegistry, "execute", rate_with_small_receipt_budget)
    provider = script(monkeypatch, [completion(call("update_requirements", {"intent": "unit_price"}, "update"),
        call("query_catalog", {"plan_id": "team"}, "team"), call("query_catalog", {"plan_id": "basic"}, "basic"))])
    response = asyncio.run(engine.chat(ChatRequest(message="Team和Basic单价")))
    actual = receipts(provider.live_messages[0])
    assert actual["team"]["ok"] is True and actual["team"]["result_omitted"] is True
    assert actual["basic"]["error"]["code"] == "dependency_not_executed"
    assert response.tool_results[-1]["result"]["data"]["unit_price_minor"] == 8000
    assert executed == ["query_catalog"] and len(provider.sent) == 1


def test_tool_exception_closes_failed_call_and_dependents(engine, monkeypatch):
    executed = []

    async def exploded(registry, name, arguments):
        executed.append(name)
        raise RuntimeError("PRIVATE_KEY_SENTINEL")

    monkeypatch.setattr(BusinessToolRegistry, "execute", exploded)
    provider = script(monkeypatch, [completion(call("update_requirements", {"intent": "unit_price"}, "update"),
        call("query_catalog", {"plan_id": "team"}, "team"), call("query_catalog", {"plan_id": "basic"}, "basic"))])
    response = asyncio.run(engine.chat(ChatRequest(message="Team和Basic单价")))
    actual = receipts(provider.live_messages[0])
    assert actual["team"]["error"]["code"] == "tool_failed"
    assert actual["basic"]["error"]["code"] == "dependency_not_executed"
    assert executed == ["query_catalog"] and len(provider.sent) == 1
    assert "PRIVATE_KEY_SENTINEL" not in response.message


def test_review_exception_still_closes_the_original_batch(engine, monkeypatch):
    async def exploded(*args):
        raise RuntimeError("PRIVATE_KEY_SENTINEL")

    monkeypatch.setattr(SemanticVerifier, "verify", exploded)
    provider = script(monkeypatch, [completion(call("update_requirements", {"intent": "unit_price"}, "update"),
        call("query_catalog", {"plan_id": "team"}, "team"))])
    response = asyncio.run(engine.chat(ChatRequest(message="Team单价")))
    actual = receipts(provider.live_messages[0])
    assert actual["update"]["error"]["code"] == "tool_failed"
    assert actual["team"]["error"]["code"] == "dependency_not_executed"
    assert "PRIVATE_KEY_SENTINEL" not in response.message and len(provider.sent) == 1


def test_missing_user_parameter_closes_proposed_dependent_calculation(engine, monkeypatch):
    provider = script(monkeypatch, [completion(call("update_requirements", {"intent": "price",
        "selected_plan_id": "team", "selection_quote": "Team"}, "update"),
        call("calculate_price", {"plan_id": "team", "paid_seats": 50, "months": 6}, "guessed"))])
    response = asyncio.run(engine.chat(ChatRequest(message="Team独立计价")))
    actual = receipts(provider.live_messages[0])
    assert actual["update"]["ok"] is True
    assert actual["guessed"]["error"]["code"] == "dependency_not_executed"
    assert response.business.pricing is None and len(response.tool_results) == 1


def test_cancellation_drains_worker_before_closure_and_never_executes_remainder(engine, monkeypatch):
    execute = BusinessToolRegistry._execute
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    executed = []

    def delayed(registry, name, arguments):
        executed.append(name)
        if name == "query_catalog":
            entered.set()
            assert release.wait(3)
        try:
            return execute(registry, name, arguments)
        finally:
            finished.set()

    monkeypatch.setattr(BusinessToolRegistry, "_execute", delayed)
    provider = script(monkeypatch, [completion(call("update_requirements", {"intent": "unit_price"}, "update"),
        call("query_catalog", {"plan_id": "team"}, "team"), call("query_catalog", {"plan_id": "basic"}, "basic"))])

    async def scenario():
        sid = engine.conversations.ensure_conversation(None, "cancellation fixture")
        runtime = engine.work.get(sid)
        task = asyncio.create_task(engine.chat(ChatRequest(session_id=sid, message="Team和Basic单价")))
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            await asyncio.sleep(0)
            assert runtime.lock.locked() and not task.done() and not finished.is_set()
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert finished.is_set() and not runtime.lock.locked()
        actual = receipts(provider.live_messages[0])
        assert actual["team"]["error"]["code"] == "tool_cancelled"
        assert actual["basic"]["error"]["code"] == "dependency_not_executed"
        assert executed == ["query_catalog"] and len(provider.sent) == 1
        assert engine.conversations.get_messages(sid) == []

    asyncio.run(scenario())


@pytest.mark.parametrize("defect,reason", [
    ("interrupted", "tool_batch_interrupted"), ("missing", "tool_batch_unclosed"),
    ("duplicate", "tool_receipt_identity_invalid"), ("foreign", "tool_receipt_identity_invalid"),
])
def test_local_validator_rejects_bad_batches(defect, reason):
    calls = [{"id": identity, "type": "function", "function": {"name": "query_catalog", "arguments": "{}"}}
        for identity in ("one", "two")]
    messages = [{"role": "assistant", "tool_calls": calls},
        {"role": "tool", "tool_call_id": "one", "content": "{}"}]
    if defect == "interrupted":
        messages.append({"role": "system", "content": "reminder"})
    elif defect in {"duplicate", "foreign"}:
        messages.append({"role": "tool", "tool_call_id": "one" if defect == "duplicate" else "foreign", "content": "{}"})
    with pytest.raises(ToolMessageProtocolError) as failure:
        validate_tool_message_sequence(messages)
    assert failure.value.reason == reason


def test_request_construction_failure_is_not_service_unavailable(engine, monkeypatch):
    provider = script(monkeypatch, [ModelRequestInvalid("upstream_bad_request")])
    response = asyncio.run(engine.chat(ChatRequest(message="Team单价")))
    assert [error["code"] for error in response.business.errors] == ["model_request_invalid"]
    assert response.business.errors[0]["reason"] == "upstream_bad_request"
    assert response.status == "incomplete" and len(provider.sent) == 1


def test_main_chain_checks_local_structure_before_any_dispatch(engine, monkeypatch):
    build = engine.prompt_builder.build_messages

    def corrupted(**kwargs):
        return build(**kwargs) + [{"role": "tool", "tool_call_id": "unowned", "content": "{}"}]

    monkeypatch.setattr(engine.prompt_builder, "build_messages", corrupted)
    provider = script(monkeypatch, [])
    response = asyncio.run(engine.chat(ChatRequest(message="Team单价")))
    assert provider.sent == [] and provider.review_calls == []
    assert [error["code"] for error in response.business.errors] == ["model_request_invalid"]
    assert response.business.errors[0]["reason"] == "tool_receipt_identity_invalid"


@pytest.mark.parametrize("second_plan", ["team", "basic"])
def test_reused_call_identity_in_later_batch_is_rejected_before_any_reexecution(engine, monkeypatch, second_plan):
    execute = BusinessToolRegistry.execute
    executed = []

    async def tracked(registry, name, arguments):
        executed.append((name, json.loads(arguments)))
        return await execute(registry, name, arguments)

    monkeypatch.setattr(BusinessToolRegistry, "execute", tracked)
    provider = script(monkeypatch, [completion(call("update_requirements", {"intent": "unit_price"}, "update"),
        call("query_catalog", {"plan_id": "team"}, "already-executed")),
        completion(call("query_catalog", {"plan_id": second_plan}, "already-executed"))])
    response = asyncio.run(engine.chat(ChatRequest(message="Team和Basic的单价")))
    assert executed == [("query_catalog", {"plan_id": "team"})]
    assert len(response.tool_results) == 2 and response.business.unit_price.plan_id == "team"
    assert response.business.unit_price.unit_price_minor == 8000
    assert response.status == "incomplete" and len(provider.sent) == 2
    failure = next(error for error in response.business.errors if error["code"] == "model_request_invalid")
    assert failure["reason"] == "tool_call_identity_reused"
    actual = receipts(provider.live_messages[0])
    assert list(actual) == ["update", "already-executed"]


def test_validator_rejects_two_fully_closed_batches_with_same_call_identity():
    batch = [{"role": "assistant", "tool_calls": [{"id": "same", "type": "function",
        "function": {"name": "query_catalog", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "same", "content": "{}"}]
    with pytest.raises(ToolMessageProtocolError) as failure:
        validate_tool_message_sequence(batch + batch)
    assert failure.value.reason == "tool_call_identity_reused"


@pytest.mark.parametrize("invalid", [SimpleNamespace(id="bad-shape"),
    SimpleNamespace(id="bad-shape", function=SimpleNamespace(name="query_catalog", arguments=None))])
def test_incoming_model_call_shape_failure_is_request_fault_before_execution(engine, monkeypatch, invalid):
    provider = script(monkeypatch, [completion(invalid)])
    response = asyncio.run(engine.chat(ChatRequest(message="Team单价")))
    assert response.tool_results == [] and response.status == "incomplete"
    assert [error["code"] for error in response.business.errors] == ["model_request_invalid"]
    assert response.business.errors[0]["reason"] == "tool_call_shape_invalid"
    assert len(provider.sent) == 1


def test_previous_turn_text_history_does_not_reserve_new_turn_call_ids(engine, monkeypatch):
    def batch():
        return completion(call("update_requirements", {"intent": "unit_price"}, "update"),
            call("query_catalog", {"plan_id": "team"}, "rate"))

    provider = script(monkeypatch, [batch(), completion(), batch(), completion()])

    async def scenario():
        first = await engine.chat(ChatRequest(message="Team单价"))
        second = await engine.chat(ChatRequest(session_id=first.session_id, message="再问Team单价"))
        assert first.business.unit_price.unit_price_minor == second.business.unit_price.unit_price_minor == 8000
        assert all(error["code"] != "model_request_invalid" for error in second.business.errors)
        assert len(second.tool_results) == 2

    asyncio.run(scenario())
    assert len(provider.review_calls) == 2


def test_sdk_rejects_local_bad_protocol_before_dispatch_and_classifies_http_400(engine, monkeypatch):
    from app.llm import deepseek_client
    sent = []

    class SDK:
        def __init__(self, **kwargs):
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

        async def create(self, **kwargs):
            sent.append(kwargs)
            raise APIStatusError("PRIVATE_KEY_SENTINEL", response=httpx.Response(400,
                request=httpx.Request("POST", "https://example.invalid")), body=None)

    monkeypatch.setattr(deepseek_client, "AsyncOpenAI", SDK)

    async def scenario():
        client = deepseek_client.DeepSeekClient(engine.settings)
        with pytest.raises(ToolMessageProtocolError):
            await client.chat(messages=[{"role": "tool", "tool_call_id": "foreign", "content": "{}"}])
        assert sent == []
        with pytest.raises(ModelRequestInvalid) as failure:
            await client.chat(messages=[{"role": "user", "content": "Team单价"}])
        assert failure.value.reason == "upstream_bad_request"
        assert "PRIVATE_KEY_SENTINEL" not in failure.value.public_message
        assert len(sent) == 1

    asyncio.run(scenario())
