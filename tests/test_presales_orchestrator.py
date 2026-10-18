"""Presales behavior checks with controlled LLMs and real business services.

Every database is independent tmp_path storage. Explicit failure/receipt
injections are labelled below and do not establish real-model success.
"""

import asyncio
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from app.agents import orchestrator as module
from app.agents.orchestrator import Orchestrator
from app.config import Settings
from app.db.session import init_saas_db
from app.llm.tool_protocol import validate_tool_message_sequence
from app.schemas.chat import ChatRequest
from app.services.catalog import get_catalog
from app.tools.business import BusinessToolRegistry
from controlled_review import is_review_request, review_completion
from test_proposals import seed


def call(name, values, identity):
    return SimpleNamespace(id=identity, function=SimpleNamespace(name=name,
        arguments=json.dumps(values, ensure_ascii=False)))


def completion(*calls):
    return SimpleNamespace(model="controlled-presales-provider", usage=None, choices=[SimpleNamespace(
        finish_reason="stop", message=SimpleNamespace(content="受控结束", tool_calls=list(calls)))])


def inquiry(identity, quote, plans=(), *, aspect="unit_price", all_plans=False, depends_on=()):
    return {"item_id": identity, "aspect": aspect, "plan_ids": plans, "all_plans": all_plans,
        "topic": quote, "span": {"quote": quote}, "depends_on": depends_on}


def review(inquiries, *, intent="unit_price", target=None, groups=()):
    return {"intent": intent, "target": target or {"kind": "none", "relation": "detour"},
        "coverage_complete": True, "consultation_coverage_complete": True,
        "consultations": inquiries, "groups": groups}


def group(identity, quote, value, *, verdict="accept", reason="confirmed", certainty="certain"):
    return {"group_id": identity, "verdict": verdict, "reason": reason, "operations": [{
        "action": "set", "field": "paid_seats", "value": value, "span": {"quote": quote},
        "speech_act": "instruction", "certainty": certainty}]}


def script(monkeypatch, main_outputs, reviewed):
    class Provider:
        sent, reviews = [], []
        remaining = list(main_outputs)

        def __init__(self, settings):
            pass

        async def chat(self, **kwargs):
            validate_tool_message_sequence(kwargs["messages"])
            if is_review_request(kwargs):
                type(self).reviews.append(deepcopy(kwargs))
                assert len(type(self).reviews) == 1
                return review_completion(reviewed)
            type(self).sent.append(deepcopy(kwargs))
            assert type(self).remaining, "Unexpected main-chain dispatch"
            return type(self).remaining.pop(0)

    monkeypatch.setattr(module, "DeepSeekClient", Provider)
    return Provider


@pytest.fixture
def engine(tmp_path):
    settings = Settings(_env_file=None, deepseek_api_key="controlled",
        deepseek_default_max_completion_tokens=512,
        saas_database_url=f"sqlite:///{tmp_path / 'presales-isolated.sqlite3'}")
    init_saas_db(settings)
    yield Orchestrator(settings)


def same_history(engine, response, original):
    messages = engine.conversations.get_messages(response.session_id)
    assert messages[-2].role == "user" and messages[-2].content == original
    assert messages[-1].role == "assistant" and messages[-1].content == response.message
    assert response.business.presentation.answer_markdown in response.message


@pytest.mark.parametrize("plans,queried,full_catalog", [
    (("team",), ("team",), False),
    (("business", "basic", "team"), ("basic", "team", "business"), False),
    (("team", "team"), ("team", "team"), False),
    (("basic", "team", "business"), (), True),
])
def test_actual_price_paths_keep_scope_order_single_legacy_and_formal_history(engine, monkeypatch, plans, queried, full_catalog):
    message = "请说明所有套餐的每席每月单价。" if full_catalog else "请分别说明" + "、".join(plans) + "的每席每月单价。"
    item = inquiry("prices", message, plans, all_plans=full_catalog)
    calls = [call("update_requirements", {"intent": "unit_price", "consultations": [item]}, "update")]
    calls.extend([call("query_catalog", {"plan_id": identity}, "price-" + str(index))
        for index, identity in enumerate(queried)])
    if full_catalog:
        calls.append(call("query_catalog", {}, "actual-catalog"))
    provider = script(monkeypatch, [completion(*calls), completion()], review([item]))
    response = asyncio.run(engine.chat(ChatRequest(message=message)))
    scope = response.business.consultation_coverage
    assert response.status == scope.status == "completed"
    assert [price.plan_id for item in scope.items for price in item.unit_prices] == list(dict.fromkeys(plans))
    assert len(response.business.unit_prices) == len(set(plans))
    body = response.business.presentation.answer_markdown
    catalog_names = {plan.plan_id: plan.name for plan in get_catalog().plans}
    positions = [body.index(catalog_names[identity]) for identity in dict.fromkeys(plans)]
    assert positions == sorted(positions)
    assert all(body.count(catalog_names[identity]) == 1 for identity in set(plans))
    if full_catalog:
        assert response.business.unit_price is None
        assert all(item.tool_call_ids == ["actual-catalog"] for item in scope.items)
        assert [record["name"] for record in response.tool_results] == ["update_requirements", "query_catalog"]
    else:
        assert response.business.unit_price.plan_id == queried[-1]
        assert len(response.tool_results) == len(queried) + 1
    assert len(provider.reviews) == 1
    assert not response.business.requirements.items and response.business.requirements.selected_plan_id is None
    same_history(engine, response, message)


def test_missing_object_uses_bounded_completion_then_names_missing_plan(engine, monkeypatch):
    message = "Basic、Team、Business每席每月各多少钱？"
    item = inquiry("prices", message, ("basic", "team", "business"))
    provider = script(monkeypatch, [completion(call("update_requirements", {"intent": "unit_price"}, "update"),
        call("query_catalog", {"plan_id": "basic"}, "basic"),
        call("query_catalog", {"plan_id": "business"}, "business")), completion(), completion()], review([item]))
    response = asyncio.run(engine.chat(ChatRequest(message=message)))
    assert response.status == "incomplete" and len(provider.sent) == 3
    scope = next(item for item in response.business.consultation_coverage.items if "team" in item.missing_plan_ids)
    assert scope.missing_plan_ids == ["team"] and scope.status == "incomplete"
    assert [price.plan_id for item in response.business.consultation_coverage.items for price in item.unit_prices] == ["basic", "business"]
    body = response.business.presentation.answer_markdown
    assert "40.00" in body and "120.00" in body and "团队版 Team的订阅单价尚未取得" in body
    assert body.count("120.00") == 1 and "80.00" not in body
    same_history(engine, response, message)


def test_controlled_query_failure_retains_actual_success_and_specific_failure(engine, monkeypatch):
    """Inject one native service failure; other tools/acceptance remain real."""
    execute = BusinessToolRegistry._execute

    def injected_failure(registry, name, arguments):
        if name == "query_catalog" and arguments.plan_id == "team":
            raise RuntimeError("controlled catalog outage")
        return execute(registry, name, arguments)

    monkeypatch.setattr(BusinessToolRegistry, "_execute", injected_failure)
    message = "Basic、Team、Business每席每月各多少钱？"
    item = inquiry("prices", message, ("basic", "team", "business"))
    script(monkeypatch, [completion(call("update_requirements", {"intent": "unit_price"}, "update"),
        call("query_catalog", {"plan_id": "basic"}, "basic"),
        call("query_catalog", {"plan_id": "business"}, "business"),
        call("query_catalog", {"plan_id": "team"}, "failed-team"))], review([item]))
    response = asyncio.run(engine.chat(ChatRequest(message=message)))
    scope = next(item for item in response.business.consultation_coverage.items if "team" in item.missing_plan_ids)
    assert scope.stage == "query_failed" and scope.missing_plan_ids == ["team"]
    assert response.status == "incomplete" and response.business.unit_price.plan_id == "business"
    assert response.tool_results[-1]["result"]["error"]["code"] == "tool_failed"
    body = response.business.presentation.answer_markdown
    assert "40.00" in body and "120.00" in body and "团队版 Team的订阅单价查询失败" in body
    assert "80.00" not in body
    same_history(engine, response, message)


@pytest.mark.parametrize("change", [{"unit_price_minor": 8001}, {"catalog_version": "controlled-other-version"}])
def test_controlled_duplicate_receipt_conflict_has_no_winning_price(engine, monkeypatch, change):
    """Tamper the second receipt after real registry validation for collector faults."""
    execute = BusinessToolRegistry.execute
    seen = 0

    async def injected_receipt(registry, name, arguments):
        nonlocal seen
        result = await execute(registry, name, arguments)
        if name == "query_catalog" and result["ok"]:
            seen += 1
            if seen == 2:
                result = deepcopy(result)
                result["data"].update(change)
        return result

    monkeypatch.setattr(BusinessToolRegistry, "execute", injected_receipt)
    message = "Team每席每月单价是多少？"
    item = inquiry("price", message, ("team",))
    script(monkeypatch, [completion(call("update_requirements", {"intent": "unit_price"}, "update"),
        call("query_catalog", {"plan_id": "team"}, "first-team"),
        call("query_catalog", {"plan_id": "team"}, "conflicting-team")), completion()], review([item]))
    response = asyncio.run(engine.chat(ChatRequest(message=message)))
    scope = response.business.consultation_coverage
    assert response.status == scope.status == "incomplete" and scope.items[0].stage == "result_conflict"
    assert response.business.unit_prices == [] and response.business.unit_price is None
    assert any(conflict["tool_call_id"] == "conflicting-team" for conflict in scope.conflicts)
    assert response.business.unit_price_observations[0].price.unit_price_minor == 8000
    assert "冲突" in response.business.presentation.answer_markdown
    assert "80.00" not in response.business.presentation.answer_markdown and "80.01" not in response.business.presentation.answer_markdown
    same_history(engine, response, message)


def test_existing_selection_price_detour_preserves_identity_and_revokes_old_proposal(engine, monkeypatch):
    sid, runtime, old_proposal = asyncio.run(seed(engine))
    before = runtime.state.current_task(recheck=False)
    message = "插问一下Business每席每月多少钱？"
    item = inquiry("business-price", message, ("business",))
    script(monkeypatch, [completion(call("update_requirements", {"intent": "unit_price"}, "update"),
        call("query_catalog", {"plan_id": "business"}, "business")), completion()], review([item]))
    response = asyncio.run(engine.chat(ChatRequest(session_id=sid, message=message)))
    after = runtime.state.current_task(recheck=False)
    for field in ("requirements", "pricing", "result_turn_id", "result_message_id"):
        assert after[field] == before[field]
    assert response.business.current_task.result_turn_id == before["result_turn_id"]
    assert response.business.unit_price.plan_id == "business" and response.business.pricing is None
    assert response.business.presentation.show_current_result is False
    assert response.business.presentation.show_comparison is False
    assert runtime.proposal is None and response.business.proposal is None
    assert asyncio.run(engine.submit_quote(sid, old_proposal.action_ref)).status == "not_saved"
    assert response.status == "completed"
    same_history(engine, response, message)


@pytest.mark.parametrize("attempt_old_calculation", [False, True])
def test_rejected_edit_keeps_independent_consultation_but_blocks_dependent_amount(engine, monkeypatch, attempt_old_calculation):
    sid, runtime, old_proposal = asyncio.run(seed(engine))
    before = runtime.state.current_task(recheck=False)
    message = "改成约70席后多少钱？另外Business每席每月单价是多少？"
    amount = inquiry("changed-amount", "改成约70席后多少钱", aspect="pricing", depends_on=("seats",))
    independent = inquiry("business-price", "Business每席每月单价是多少", ("business",))
    target = {"kind": "selection", "relation": "continue", "task_id": runtime.state.requirements.requirement_id}
    reviewed = review([amount, independent], intent="selection", target=target,
        groups=[group("seats", "改成约70席", 70, verdict="reject", reason="uncertain", certainty="approximate")])
    calls = [call("update_requirements", {"intent": "selection"}, "update"),
        call("query_catalog", {"plan_id": "business"}, "business")]
    if attempt_old_calculation:
        calls.append(call("calculate_price", {"plan_id": "team", "paid_seats": 80, "months": 12}, "forbidden-old-amount"))
    provider = script(monkeypatch, [completion(*calls), completion()], reviewed)
    response = asyncio.run(engine.chat(ChatRequest(session_id=sid, message=message)))
    assert response.business.pricing is None and response.status != "completed"
    after = runtime.state.current_task(recheck=False)
    for field in ("requirements", "pricing", "result_turn_id", "result_message_id"):
        assert after[field] == before[field]
    assert response.business.unit_price is None
    assert [price.plan_id for price in response.business.unit_prices] == ["business"]
    assert runtime.proposal is None and response.business.proposal is None
    assert response.business.consultation_coverage.status == "incomplete"
    feedback = {item["item_id"]: item for item in runtime.state.consultation_feedback}
    assert feedback["changed-amount"]["status"] == "blocked" and feedback["business-price"]["status"] == "accepted"
    covered = {identity: item for item in response.business.consultation_coverage.items
               for identity in item.source_item_ids}
    assert covered["changed-amount"].stage == "dependency_unresolved"
    assert covered["changed-amount"].status == "incomplete"
    assert covered["changed-amount"].topic == "改成约70席后多少钱"
    assert not covered["changed-amount"].unit_prices
    assert covered["business-price"].status == "completed"
    body = response.business.presentation.answer_markdown
    assert "120.00" in body and "实际付费席位" in body
    assert "不是本轮新计算的结果" in body
    assert "依赖的修改或新计价尚未完成；不能用此前结果代答" in body
    if attempt_old_calculation:
        failed = next(record for record in response.tool_results if record["tool_call_id"] == "forbidden-old-amount")
        assert failed["result"]["ok"] is False
        assert failed["result"]["error"]["details"]["issue"] == "review_dependency"
    assert len(provider.reviews) == 1
    assert asyncio.run(engine.submit_quote(sid, old_proposal.action_ref)).status == "not_saved"
    same_history(engine, response, message)


def test_completed_independent_consultation_cannot_complete_unexecuted_selection_edit(engine, monkeypatch):
    sid, runtime, old_proposal = asyncio.run(seed(engine))
    message = "把席位改成50席，重新计算费用。另外Business每席每月单价是多少？"
    independent = inquiry("business-price", "Business每席每月单价是多少", ("business",))
    target = {"kind": "selection", "relation": "continue", "task_id": runtime.state.requirements.requirement_id}
    reviewed = review([independent], intent="selection", target=target,
        groups=[group("seats", "把席位改成50席", 50), {
            "group_id": "task-amount", "verdict": "accept", "reason": "confirmed",
            "operations": [{"action": "request_pricing", "span": {"quote": "重新计算费用"},
                            "speech_act": "instruction", "certainty": "certain"}]}])
    script(monkeypatch, [completion(call("update_requirements", {"intent": "selection"}, "update"),
        call("query_catalog", {"plan_id": "business"}, "business")), completion(), completion()], reviewed)
    response = asyncio.run(engine.chat(ChatRequest(session_id=sid, message=message)))
    assert response.business.consultation_coverage.status == "completed"
    assert response.status == "incomplete" and response.business.pricing is None
    assert next(parameter.value for parameter in response.business.requirements.parameters if parameter.name == "paid_seats") == 50
    assert {error["code"] for error in response.business.errors} >= {"matching_not_completed"}
    assert response.business.current_task.pricing is None and response.business.current_task.status == "needs_reassessment"
    assert response.business.proposal is None and runtime.proposal is None
    assert "120.00" in response.business.presentation.answer_markdown
    same_history(engine, response, message)
