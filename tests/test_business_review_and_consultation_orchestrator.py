"""Production business-review and consultation checks with an explicitly controlled provider.

SemanticVerifier parsing, WorkState acceptance, real catalog/matching/pricing,
response assembly and SQLite history are enabled; no real-model claim is made.
"""

import asyncio
from copy import deepcopy
import json

import pytest

from app.schemas.chat import ChatRequest
from app.agents import orchestrator as module
from app.llm.tool_protocol import validate_tool_message_sequence
from app.services.catalog import get_catalog
from controlled_review import is_review_request, review_completion
from test_presales_orchestrator import (call, completion, engine, group,
                                        review, same_history, script)
from test_proposals import seed, worker


@pytest.mark.parametrize("intent", ["selection", "price"])
@pytest.mark.parametrize("missing_contract", [False, True])
def test_production_review_contract_preserves_real_business_execution_but_not_false_completion(
        engine, monkeypatch, intent, missing_contract):
    if intent == "selection":
        sid, runtime, _ = asyncio.run(seed(engine))
        modification = "把席位改为40席并重新计算"
        target = {"kind": "selection", "relation": "continue",
                  "task_id": runtime.state.requirements.requirement_id}
        groups = [group("seats", "把席位改为40席", 40)]
        candidate = {"intent": intent, "parameters": [
            {"name": "paid_seats", "value": 40, "user_quote": "把席位改为40席"}]}
        business_calls = [call("match_requirements", {}, "actual-match"),
            call("calculate_price", {"plan_id": "team", "paid_seats": 40, "months": 12}, "actual-price")]
        expected_amount = 3840000
    else:
        sid = None
        modification = "独立计算Team，实际购买40席，订阅6个月"
        target = {"kind": "price", "relation": "independent", "span": {"quote": "独立计算Team"}}
        groups = [group("seats", "实际购买40席", 40), {
            "group_id": "months", "verdict": "accept", "operations": [{
                "action": "set", "field": "months", "value": 6,
                "span": {"quote": "订阅6个月"}, "speech_act": "assertion"}]}, {
            "group_id": "plan", "verdict": "accept", "operations": [{
                "action": "select_plan", "value": "team", "span": {"quote": "独立计算Team"},
                "speech_act": "instruction"}]}]
        candidate = {"intent": intent, "selected_plan_id": "team", "selection_quote": "独立计算Team",
            "parameters": [{"name": "paid_seats", "value": 40, "user_quote": "实际购买40席"},
                           {"name": "months", "value": 6, "user_quote": "订阅6个月"}]}
        business_calls = [call("calculate_price", {"plan_id": "team", "paid_seats": 40, "months": 6}, "actual-price")]
        expected_amount = 1920000
    message = modification + ("；另外Business的SSO需要什么前提？" if missing_contract else "。")
    raw_review = deepcopy(review([], intent=intent, target=target, groups=groups))
    if missing_contract:
        del raw_review["consultations"]
        del raw_review["consultation_coverage_complete"]
    provider = script(monkeypatch, [completion(call("update_requirements", candidate, "candidate"),
        *business_calls), completion()], raw_review)
    response = asyncio.run(engine.chat(ChatRequest(session_id=sid, message=message)))
    assert len(provider.reviews) == 1
    assert response.business.intent == intent
    assert response.business.pricing.base_price_minor == expected_amount
    assert {p.name: p.value for p in response.business.requirements.parameters}["paid_seats"] == 40
    assert all(record["result"]["ok"] for record in response.tool_results)
    coverage = response.business.consultation_coverage
    if missing_contract:
        assert response.status == coverage.status == "incomplete"
        assert coverage.scope_complete is False
        assert all(item.stage == "extraction_incomplete" for item in coverage.items)
        assert any(error["code"] == "consultation_review_incomplete" for error in response.business.errors)
        assert "咨询部分尚未完成" in response.business.presentation.answer_markdown
        assert "请明确需要修改" not in response.message
        assert "Business的 SAML SSO 使用前提" not in response.message
    else:
        assert response.status == coverage.status == "completed"
        assert coverage.scope_complete is True and not response.business.errors
    same_history(engine, response, message)


def mixed_sso_provider(monkeypatch, first_calls, raw_review, source):
    """Controlled LLM; explanation consumes the real issued search identity."""
    class Provider:
        main_calls, reviews, retrieval_id = [], [], None

        def __init__(self, settings):
            pass

        async def chat(self, **kwargs):
            validate_tool_message_sequence(kwargs["messages"])
            if is_review_request(kwargs):
                type(self).reviews.append(deepcopy(kwargs))
                assert len(type(self).reviews) == 1
                return review_completion(raw_review)
            type(self).main_calls.append(deepcopy(kwargs))
            if len(type(self).main_calls) == 1:
                return completion(*first_calls)
            if len(type(self).main_calls) == 2:
                actual = next(json.loads(message["content"])["data"]
                    for message in reversed(kwargs["messages"])
                    if message["role"] == "tool" and "retrieval_id" in
                        (json.loads(message["content"]).get("data") or {}))
                assert actual["status"] == "retrieved"
                assert actual["hits"][0]["source"]["source_id"] == source.source_id
                type(self).retrieval_id = actual["retrieval_id"]
                return completion(call("explain_knowledge", {"retrieval_id": actual["retrieval_id"],
                    "answerability": "supported", "consultation_item_ids": ["business-rule"],
                    "citations": [{"source_id": source.source_id,
                        "quote": get_catalog().conditions[0].statement}]}, "actual-rule-explanation"))
            assert len(type(self).main_calls) == 3
            return completion()

    monkeypatch.setattr(module, "DeepSeekClient", Provider)
    return Provider


def mixed_scope(seat_quote):
    return [{"item_id": "changed-amount", "aspect": "pricing", "plan_ids": [],
        "topic": seat_quote + "后算当前费用", "span": {"quote": seat_quote + "后算当前费用"},
        "depends_on": ["seats"]},
        {"item_id": "business-rule", "aspect": "prerequisite", "plan_ids": ["business"],
        "feature_id": "saml_sso", "topic": "Business的SSO有什么前提",
        "span": {"quote": "Business的SSO有什么前提"}, "depends_on": []}]


def assert_real_sso_binding(runtime, response, provider, source):
    scope = {identity: item for item in response.business.consultation_coverage.items
             for identity in item.source_item_ids}
    rule = scope["business-rule"]
    assert rule.status == "completed" and rule.plan_ids == ["business"]
    assert [prerequisite.plan_id for prerequisite in rule.prerequisites] == ["business"]
    assert not rule.support
    prerequisite = rule.prerequisites[0]
    assert prerequisite.statement == prerequisite.quote == get_catalog().conditions[0].statement
    assert prerequisite.source_id == source.source_id and prerequisite.retrieval_id == provider.retrieval_id
    actual = runtime.state.knowledge.evidence(provider.retrieval_id)
    assert actual.chain_id == runtime.state.knowledge.chain_id
    assert source == actual.hits[0].source
    search_record = next(record for record in response.tool_results if record["name"] == "search_knowledge")
    assert json.loads(search_record["arguments"])["consultation_item_ids"] == ["business-rule"]
    explanation_record = next(record for record in response.tool_results if record["name"] == "explain_knowledge")
    assert explanation_record["result"]["data"]["consultation_item_ids"] == ["business-rule"]
    assert explanation_record["display_arguments"]["citations"][0]["quote"] == prerequisite.quote
    assert rule.retrieval_ids == [actual.retrieval_id] and rule.source_ids == [source.source_id]
    assert not response.business.requirements.condition_assertions
    assert {item.feature_id for item in response.business.requirements.items} == {"api_access"}
    assert response.business.requirements.selected_plan_id is None
    assert "企业版 Business的 SAML SSO 使用前提" in response.business.presentation.answer_markdown
    assert "团队版 Team的 SAML SSO 使用前提" not in response.message
    assert "04_saml_sso.md" in response.message
    assert len(provider.reviews) == 1
    assert response.business.semantic_review["consultation_contract_missing"] is False
    return scope


def test_legal_selection_edit_and_independent_sso_keep_correct_new_amount_and_rule_scope(engine, monkeypatch):
    """Controlled fixed-index recall, with real parser/services/history enabled."""
    sid, runtime, old_proposal = asyncio.run(seed(engine))
    before = runtime.state.current_task(recheck=False)
    source = worker(monkeypatch, runtime.state)
    seat_quote = "把席位改成50席"
    message = seat_quote + "后算当前费用；另外Business的SSO有什么前提？"
    raw_review = review(mixed_scope(seat_quote), intent="selection", target={"kind": "selection",
        "relation": "continue", "task_id": runtime.state.requirements.requirement_id}, groups=[
            group("seats", seat_quote, 50)])
    provider = mixed_sso_provider(monkeypatch, [call("update_requirements", {"intent": "selection",
        "parameters": [{"name": "paid_seats", "value": 50, "user_quote": seat_quote}]}, "candidate"),
        call("match_requirements", {}, "actual-new-match"),
        call("calculate_price", {"plan_id": "team", "paid_seats": 50, "months": 12}, "actual-new-price"),
        call("search_knowledge", {"query": "Business SAML SSO 使用前提",
            "consultation_item_ids": ["business-rule"]}, "actual-rule-search")], raw_review, source)
    response = asyncio.run(engine.chat(ChatRequest(session_id=sid, message=message)))
    scope = assert_real_sso_binding(runtime, response, provider, source)
    assert response.status == response.business.consultation_coverage.status == "completed"
    assert response.business.consultation_coverage.scope_complete is True
    assert scope["changed-amount"].status == "completed" and scope["changed-amount"].stage == "answered"
    assert all(record["result"]["ok"] for record in response.tool_results)
    assert response.business.pricing.plan_id == "team" and response.business.pricing.base_price_minor == 4800000
    assert {parameter.name: parameter.value for parameter in response.business.requirements.parameters} == {
        "paid_seats": 50, "months": 12}
    current = runtime.state.current_task(recheck=False)
    assert current["requirements"]["requirement_id"] == before["requirements"]["requirement_id"]
    assert current["requirements"]["revision"] > before["requirements"]["revision"]
    assert current["result_turn_id"] == response.business.turn_id != before["result_turn_id"]
    assert current["result_message_id"] == response.message_id
    assert current["pricing"]["base_price_minor"] == 4800000
    assert "48,000.00" in response.business.presentation.answer_markdown
    assert "76,800.00" not in response.business.presentation.answer_markdown
    if response.business.proposal:
        assert response.business.proposal.selected_plan_id == "team"
        assert not response.business.proposal.adopted_sources
    assert asyncio.run(engine.submit_quote(sid, old_proposal.action_ref)).status == "not_saved"
    same_history(engine, response, message)


def test_rejected_approximate_edit_keeps_old_identity_while_independent_sso_completes(engine, monkeypatch):
    """Controlled fixed-index recall never bypasses the rejected edit dependency."""
    sid, runtime, old_proposal = asyncio.run(seed(engine))
    before = runtime.state.current_task(recheck=False)
    source = worker(monkeypatch, runtime.state)
    seat_quote = "把席位改成约70席"
    message = seat_quote + "后算当前费用；另外Business的SSO有什么前提？"
    raw_review = review(mixed_scope(seat_quote), intent="selection", target={"kind": "selection",
        "relation": "continue", "task_id": runtime.state.requirements.requirement_id}, groups=[
            group("seats", seat_quote, 70, verdict="reject", reason="uncertain", certainty="approximate")])
    provider = mixed_sso_provider(monkeypatch, [call("update_requirements", {"intent": "selection",
        "parameters": [{"name": "paid_seats", "value": 70, "user_quote": seat_quote}]}, "candidate"),
        call("search_knowledge", {"query": "Business SAML SSO 使用前提",
            "consultation_item_ids": ["business-rule"]}, "actual-rule-search")], raw_review, source)
    response = asyncio.run(engine.chat(ChatRequest(session_id=sid, message=message)))
    scope = assert_real_sso_binding(runtime, response, provider, source)
    assert scope["changed-amount"].status == "incomplete"
    assert scope["changed-amount"].stage == "dependency_unresolved"
    assert response.business.consultation_coverage.scope_complete is False
    assert response.status == response.business.consultation_coverage.status == "incomplete"
    assert not response.business.errors and response.business.pricing is None and response.business.match is None
    assert [record["name"] for record in response.tool_results] == [
        "update_requirements", "search_knowledge", "explain_knowledge"]
    assert {parameter.name: parameter.value for parameter in response.business.requirements.parameters} == {
        "paid_seats": 80, "months": 12}
    after = runtime.state.current_task(recheck=False)
    for field in ("requirements", "pricing", "result_turn_id", "result_message_id"):
        assert after[field] == before[field]
    assert response.business.current_task.result_turn_id == before["result_turn_id"]
    assert "review:paid_seats" in response.business.pending
    body = response.business.presentation.answer_markdown
    assert "76,800.00" in body and "不是本轮新计算的结果" in body
    assert "依赖的修改或新计价尚未完成；不能用此前结果代答" in body
    assert "67,200.00" not in body and "84,000.00" not in body
    assert response.business.proposal is None and runtime.proposal is None
    assert asyncio.run(engine.submit_quote(sid, old_proposal.action_ref)).status == "not_saved"
    same_history(engine, response, message)
