"""Finite intake contracts through real verifier parsing and orchestration.

Only model completions are controlled. Catalog, acceptance, coverage, official
answer and the isolated SQLite history are ordinary production paths.
"""
import asyncio

import pytest

from app.schemas.chat import BusinessResponse, ChatRequest, ConsultationCoverage
from app.schemas.matching import RequirementSet
from app.services.catalog import get_catalog
from app.services.pricing import get_unit_price as query_unit_price
from app.services.chat_render import organize_business
from test_presales_orchestrator import (
    call, completion, engine, inquiry, review, same_history, script,
)


def run_prices(engine, monkeypatch, message, items, *, candidate=None,
               intent="unit_price", queried=("business", "basic", "team")):
    calls = [call("update_requirements", candidate or {
        "intent": intent, "consultations": items}, "candidate")]
    calls.extend(call("query_catalog", {"plan_id": plan}, "actual-" + plan)
                 for plan in queried)
    provider = script(monkeypatch, [completion(*calls), completion(), completion()],
                      review(items, intent=intent))
    response = asyncio.run(engine.chat(ChatRequest(message=message)))
    assert len(provider.reviews) == 1
    same_history(engine, response, message)
    return response


@pytest.mark.parametrize("grouping", ["one", "reverse", "interleaved"])
def test_question_order_survives_item_packing_and_reverse_tool_order(engine, monkeypatch, grouping):
    message = "只问Team、Basic和Business的每席每月单价，请严格按这个顺序列出。"
    groups = {"one": [("team", "basic", "business")],
              "reverse": [("business",), ("basic",), ("team",)],
              "interleaved": [("team", "business"), ("basic",)]}[grouping]
    items = [inquiry("price-" + str(i), message, plans) for i, plans in enumerate(groups)]
    response = run_prices(engine, monkeypatch, message, items)
    assert response.status == "completed"
    assert [p.plan_id for p in response.business.unit_prices] == ["team", "basic", "business"]
    body = response.business.presentation.answer_markdown
    assert body.index("80.00") < body.index("40.00") < body.index("120.00")
    assert response.business.unit_price.plan_id == "team"


@pytest.mark.parametrize("quote_mode", ["full", "short"])
@pytest.mark.parametrize("aliases", ["基础版Basic和团队版Team", "基础版 Basic和团队版 Team"])
@pytest.mark.parametrize("wrong", [False, True])
def test_full_context_controls_short_alias_scope_without_adopting_wrong_review(
        engine, monkeypatch, quote_mode, aliases, wrong):
    message = "请告诉我全部套餐的每席每月单价，但不包括" + aliases + "。"
    quote = message if quote_mode == "full" else "全部套餐的每席每月单价"
    items = [inquiry("prices", quote, () if wrong else ("business",), all_plans=wrong)]
    response = run_prices(engine, monkeypatch, message, items)
    body = response.business.presentation.answer_markdown
    if wrong:
        assert response.status == "incomplete"
        assert response.business.unit_prices == []
        assert not response.business.consultation_coverage.scope_complete
        assert all(value not in body for value in ("40.00", "80.00", "120.00"))
        assert "澄清" not in body
    else:
        assert response.status == "completed"
        assert [p.plan_id for p in response.business.unit_prices] == ["business"]
        assert "120.00" in body and "40.00" not in body and "80.00" not in body
    assert len(response.business.unit_price_observations) == 3
    assert response.business.unit_price.plan_id == "team"


def test_irrelevant_exclusion_cannot_block_explicit_independent_prices(engine, monkeypatch):
    message = "Team每席每月单价多少？Basic不用查。Business每席每月单价多少？"
    response = run_prices(engine, monkeypatch, message, [
        inquiry("team-price", "Team每席每月单价多少", ("team",)),
        inquiry("business-price", "Business每席每月单价多少", ("business",))])
    assert response.status == "completed"
    assert [p.plan_id for p in response.business.unit_prices] == ["team", "business"]
    assert "40.00" not in response.business.presentation.answer_markdown


@pytest.mark.parametrize("wrong_intent", [False, True])
def test_entire_question_omission_survives_general_and_empty_review(engine, monkeypatch, wrong_intent):
    message = "Team每席每月单价多少？Basic支持API吗？"
    items = [] if wrong_intent else [inquiry("team-price", "Team每席每月单价多少", ("team",))]
    response = run_prices(engine, monkeypatch, message, items,
                          intent="general" if wrong_intent else "unit_price")
    coverage = response.business.consultation_coverage
    assert response.status == coverage.status == "incomplete"
    assert not coverage.scope_complete
    assert any(item.aspect == "support" and "basic" in item.plan_ids
               and item.stage == "extraction_incomplete" for item in coverage.items)
    assert "澄清" not in response.business.presentation.answer_markdown
    assert not response.business.requirements.items


def test_single_independent_reader_repairs_first_candidate_omission(engine, monkeypatch):
    message = "请列出Team和Basic的每席每月单价。"
    first = inquiry("price", message, ("team",))
    corrected = inquiry("price", message, ("team", "basic"))
    response = run_prices(engine, monkeypatch, message, [corrected],
                          candidate={"intent": "unit_price", "consultations": [first]})
    assert response.status == "completed"
    assert [p.plan_id for p in response.business.unit_prices] == ["team", "basic"]


def test_one_review_can_correct_wrong_purchase_candidate_without_task_creation(engine, monkeypatch):
    message = "我们需要API。Team支持API吗？"
    candidate = {"intent": "selection", "add": [{"statement": "API", "user_quote": "我们需要API",
        "kind": "feature", "feature_id": "api_access"}]}
    checked = inquiry("api", "Team支持API吗", ("team",), aspect="support")
    checked["feature_id"] = "api_access"
    provider = script(monkeypatch, [completion(call("update_requirements", candidate, "candidate")),
                                   completion(), completion()], review([checked], intent="knowledge"))
    response = asyncio.run(engine.chat(ChatRequest(message=message)))
    state = engine.work.get(response.session_id).state
    assert len(provider.reviews) == 1 and state.consultation_complete
    assert state._selection_context is None and state._price_context is None
    assert not state.requirements.items and not state.requirements.parameters
    assert state.turn_consultations[0].item_id == "api"
    assert response.status == "incomplete"  # Accepted question still needs real tools.
    assert response.business.consultation_coverage.items[0].stage == "not_queried"


@pytest.mark.parametrize("representation", ["complete", "wrong_basis", "short_exclusion", "extra_plan"])
@pytest.mark.parametrize("all_plans", [False, True])
def test_complete_range_expression_is_equivalent_not_a_claimed_empty_result(
        engine, monkeypatch, representation, all_plans):
    message = "请告诉我全部套餐的每席每月单价，但不包括基础版Basic和团队版Team。"
    checked = inquiry("c1", message, all_plans=all_plans)
    checked.update(scope_basis={"kind": "all_plans", "span": {"quote": message}},
                   scope_exclusions=[{"quote": "不包括基础版Basic和团队版Team"}])
    if representation == "wrong_basis":
        checked["scope_basis"] = {"kind": "named", "span": {"quote": "Basic"}}
    elif representation == "short_exclusion":
        checked["scope_exclusions"] = [{"quote": "不包括基础版Basic"}]
    elif representation == "extra_plan":
        checked["plan_ids"] = ["team", "business"]
    response = run_prices(engine, monkeypatch, message, [checked])
    if representation == "complete":
        assert response.status == "completed"
        assert [price.plan_id for price in response.business.unit_prices] == ["business"]
        state = engine.work.get(response.session_id).state
        assert state.turn_consultations[0].plan_ids == ("business",)
        assert "120.00" in response.business.presentation.answer_markdown
    else:
        assert response.status == "incomplete"
        assert not response.business.consultation_coverage.scope_complete
        assert not response.business.unit_prices
        assert not engine.work.get(response.session_id).state.turn_consultations


@pytest.mark.parametrize("wrong_basis", [False, True])
def test_real_paired_short_named_basis_preserves_relation_identity(engine, monkeypatch, wrong_basis):
    message = "Team的API、Basic的Webhook是否支持？"
    items = []
    for item_id, quote, plan, feature in (
            ("c1", "Team的API", "team", "api_access"),
            ("c2", "Basic的Webhook", "basic", "webhook_notifications")):
        item = inquiry(item_id, quote, (plan,), aspect="support")
        item["feature_id"] = feature
        item["scope_basis"] = {"kind": "named", "span": {"quote": quote}}
        items.append(item)
    if wrong_basis:
        items[0]["scope_basis"]["span"]["quote"] = "Basic的Webhook"
    provider = script(monkeypatch, [completion(call("update_requirements", {
        "intent": "knowledge", "consultations": items}, "candidate")), completion(), completion()],
        review(items, intent="knowledge"))
    response = asyncio.run(engine.chat(ChatRequest(message=message)))
    state = engine.work.get(response.session_id).state
    assert len(provider.reviews) == 1
    assert response.status == "incomplete"  # No actual knowledge tools were submitted.
    assert state.consultation_complete is (not wrong_basis)
    assert [(item.plan_ids, item.feature_id) for item in state.turn_consultations] == (
        [(('basic',), 'webhook_notifications')] if wrong_basis else
        [(('team',), 'api_access'), (('basic',), 'webhook_notifications')])
    assert len(response.business.consultation_coverage.items) == 2
    assert response.business.consultation_coverage.items[0].stage == (
        "extraction_incomplete" if wrong_basis else "not_queried")
    same_history(engine, response, message)


@pytest.mark.parametrize("extra_query", [False, True])
def test_legal_empty_scope_parses_and_completes_without_catalog_requirement(engine, monkeypatch, extra_query):
    message = "请查所有套餐的每席每月单价，但排除Basic、Team和Business。"
    empty = inquiry("empty-prices", message)
    empty.update(scope_basis={"kind": "all_plans", "span": {"quote": "所有套餐"}},
                 scope_exclusions=[{"quote": "排除Basic、Team和Business"}])
    calls = [call("update_requirements", {"intent": "unit_price", "consultations": [empty]}, "candidate")]
    if extra_query:
        calls.append(call("query_catalog", {}, "extra-actual-catalog"))
    provider = script(monkeypatch, [completion(*calls), completion()], review([empty]))
    response = asyncio.run(engine.chat(ChatRequest(message=message)))
    assert len(provider.reviews) == 1 and len(provider.sent) == 2
    assert response.status == "completed" and not response.business.errors
    assert response.business.consultation_coverage.scope_complete
    assert all(item.stage == "empty_scope" for item in response.business.consultation_coverage.items)
    assert response.business.unit_prices == [] and response.business.unit_price is None
    assert "本次没有剩余查询对象" in response.business.presentation.answer_markdown
    assert all(value not in response.message for value in ("40.00", "80.00", "120.00"))
    assert len(response.business.unit_price_observations) == (3 if extra_query else 0)
    assert response.business.proposal is None
    same_history(engine, response, message)


def test_missing_scope_expression_remains_invalid_in_production_review(engine, monkeypatch):
    message = "Team的单价多少？"
    no_scope = inquiry("missing-scope", message)
    provider = script(monkeypatch, [completion(call("update_requirements", {
        "intent": "unit_price", "consultations": [no_scope]}, "candidate")), completion()], review([no_scope]))
    response = asyncio.run(engine.chat(ChatRequest(message=message)))
    assert len(provider.reviews) == 1
    assert response.status == "incomplete"
    assert response.business.semantic_review["error_code"] == "semantic_review_invalid"
    assert not any(record["name"] == "query_catalog" for record in response.tool_results)


@pytest.mark.parametrize("bad_expression", ["basis", "exclusions"])
def test_empty_scope_cannot_hide_wrong_review_basis_or_incomplete_exclusion(engine, monkeypatch, bad_expression):
    message = "请查所有套餐的每席每月单价，但排除Basic、Team和Business。"
    empty = inquiry("empty-prices", message)
    empty.update(scope_basis={"kind": "all_plans", "span": {"quote": "所有套餐"}},
                 scope_exclusions=[{"quote": "排除Basic、Team和Business"}])
    if bad_expression == "basis":
        empty["scope_basis"] = {"kind": "named", "span": {"quote": "Basic"}}
    else:
        empty["scope_exclusions"] = [{"quote": "排除Basic"}]
    script(monkeypatch, [completion(call("update_requirements", {
        "intent": "unit_price", "consultations": [empty]}, "candidate")), completion()], review([empty]))
    response = asyncio.run(engine.chat(ChatRequest(message=message)))
    assert response.status == "incomplete"
    assert not response.business.consultation_coverage.scope_complete
    assert not any(item.stage == "empty_scope" for item in response.business.consultation_coverage.items)
    assert all(item["status"] != "accepted" for item in
               engine.work.get(response.session_id).state.consultation_feedback)


def test_current_contract_can_represent_genuine_absence_of_consultation(engine, monkeypatch):
    message = "你好，谢谢。"
    provider = script(monkeypatch, [completion(call("update_requirements", {
        "intent": "general", "consultations": []}, "candidate")), completion()], review([], intent="general"))
    response = asyncio.run(engine.chat(ChatRequest(message=message)))
    assert len(provider.reviews) == 1
    assert response.status == "completed"
    assert response.business.consultation_coverage.items == []
    assert response.business.consultation_coverage.scope_complete
    assert not engine.work.get(response.session_id).state.consultation_questions


@pytest.mark.parametrize("field", ["collection", "single", "catalog"])
@pytest.mark.parametrize("contract", [True, False])
def test_three_body_fallbacks_require_absent_contract_and_preserve_legacy(field, contract):
    business = BusinessResponse(turn_id="body-gates", intent="unit_price",
        requirements=RequirementSet(requirement_id="body-gates"),
        consultation_coverage=ConsultationCoverage(scope_complete=False, status="incomplete")
            if contract else None)
    if field == "collection":
        business.unit_prices = [query_unit_price("team")]
    elif field == "single":
        business.unit_price = query_unit_price("team")
    else:
        business.catalog = get_catalog()
    presentation, formal = organize_business(business, "incomplete" if contract else "completed")
    assert ("80.00" in presentation.answer_markdown) is (not contract)
    assert presentation.answer_markdown in formal
    if field == "single":
        assert business.intent == "unit_price" and business.unit_price.plan_id == "team"
