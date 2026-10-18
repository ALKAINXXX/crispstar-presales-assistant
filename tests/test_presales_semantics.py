"""Controlled semantic candidates test acceptance, not real-model success."""

import asyncio
import json
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.llm.semantic_verifier import SemanticVerifier, validate_first_candidate
from app.schemas.semantic import SemanticReview
from app.services.pricing import calculate_subscription_price
from app.services.work_state import WorkState


def item(identity, quote, *, plans=(), aspect="support", feature="api_access", depends_on=(), **values):
    return {"item_id": identity, "aspect": aspect, "plan_ids": plans, "feature_id": feature,
        "topic": quote, "span": {"quote": quote}, "depends_on": depends_on, **values}


def adjudicate(state, message, consultations=(), groups=(), *, intent="knowledge", kind="none",
               consultation_complete=True, candidate=None):
    state.begin_review_turn()
    target = {"kind": kind, "relation": "detour" if kind == "none" else "continue"}
    context = state._selection_context if kind == "selection" else state._price_context
    if context:
        target["task_id"] = context.requirements.requirement_id
    return state.apply_review({"intent": intent, "target": target, "coverage_complete": True,
        "consultation_coverage_complete": consultation_complete, "consultations": consultations,
        "groups": groups}, message, candidate or {})


def group(identity, action, quote, *, verdict="accept", reason="confirmed", **operation):
    return {"group_id": identity, "verdict": verdict, "reason": reason,
        "operations": [{"action": action, "span": {"quote": quote}, **operation}]}


@pytest.fixture
def state():
    return WorkState(Settings(_env_file=None))


def existing_selection(state):
    message = "必须有API，买80席，订12个月，算价格。"
    state.apply_patch({"intent": "selection", "add": [{"statement": "API", "user_quote": "必须有API",
        "kind": "feature", "feature_id": "api_access"}],
        "parameters": [{"name": "paid_seats", "value": 80, "user_quote": "买80席"},
                       {"name": "months", "value": 12, "user_quote": "订12个月"}],
        "pricing_quote": "算价格"}, message)
    match = state.match()
    price = calculate_subscription_price("team", 80, 12)
    state.validate_price("team", 80, 12)
    state.record_pricing_result(price)
    state.remember_result(match, price, (), (), turn_id="old-result")
    return state.requirements, match, price


def test_background_candidate_add_is_repaired_by_review_not_silently_dropped(state):
    message = "我们需要API，还需要Webhook。Team和Basic都支持这两项吗？"
    first = {"intent": "selection", "add": [{"statement": "API", "user_quote": "我们需要API",
        "feature_id": "api_access"}]}
    validate_first_candidate(first)
    accepted = adjudicate(state, message, [
        item("api", message, plans=("team", "basic")),
        item("webhook", message, plans=("team", "basic"), feature="webhook_notifications")], candidate=first)
    assert state.review_complete and state.consultation_complete
    assert state.requirements.items == ()
    assert state._selection_context is None and state._price_context is None
    assert state.requirements.selected_plan_id is None
    assert sum(len(i.plan_ids) for i in state.turn_consultations) == 4
    assert accepted["turn_consultations"][0]["plan_ids"] == ["team", "basic"]


def test_first_scope_omission_can_reach_one_review_but_review_scope_remains_strict():
    candidate = {"intent": "knowledge", "consultations": [{"item_id": "api", "aspect": "support",
        "topic": "Team支持API吗", "span": {"quote": "Team支持API吗"}}]}
    validate_first_candidate(candidate)
    with pytest.raises(ValidationError):
        SemanticReview.model_validate({"intent": "knowledge", "coverage_complete": True,
            "consultations": candidate["consultations"]})


def test_distinct_support_relations_are_not_crossed(state):
    message = "Team的API、Basic的Webhook是否支持？"
    adjudicate(state, message, [item("api", "Team的API", plans=("team",)),
        item("webhook", "Basic的Webhook", plans=("basic",), feature="webhook_notifications")])
    assert [(i.feature_id, i.plan_ids) for i in state.turn_consultations] == [
        ("api_access", ("team",)), ("webhook_notifications", ("basic",))]
    assert state.consultation_complete


def test_named_span_cannot_borrow_other_relation_identity_from_elsewhere(state):
    message = "Team的API、Basic的Webhook是否支持？"
    adjudicate(state, message, [item("wrong", "Team的API", plans=("basic",))])
    assert state.turn_consultations == () and not state.consultation_complete
    assert state.consultation_feedback[0]["issue"] == "reference_missing"


def test_consultation_insertion_keeps_exact_task_and_result_identity(state):
    requirements, match, price = existing_selection(state)
    message = "Business每席每月多少钱？Basic支持API吗？"
    adjudicate(state, message, [item("unit", "Business每席每月多少钱", plans=("business",),
        aspect="unit_price", feature=None), item("api", "Basic支持API", plans=("basic",))])
    assert state.requirements is requirements
    assert state.last_match is match and state._selection_context.pricing is price
    assert state._selection_context.result_turn_id == "old-result"
    assert state.active_intent == "selection" and state.intent == "knowledge"
    assert state.requirements.selected_plan_id is None


def test_explicit_edit_in_question_keeps_modification_and_independent_consultation(state):
    old, _, _ = existing_selection(state)
    message = "把席位改成50席后多少钱？Business的SSO有什么前提？"
    adjudicate(state, message, [item("amount", "把席位改成50席后多少钱", aspect="pricing",
        feature=None, depends_on=("seats",)), item("rule", "Business的SSO有什么前提", plans=("business",),
        aspect="prerequisite", feature="saml_sso")],
        [group("seats", "set", "把席位改成50席", field="paid_seats", value=50,
            speech_act="instruction"), group("price", "request_pricing", "后多少钱", speech_act="instruction")],
        intent="selection", kind="selection")
    assert state.requirements.requirement_id == old.requirement_id
    assert {p.name: p.value for p in state.requirements.parameters} == {"paid_seats": 50, "months": 12}
    assert state.operation_feedback[0]["status"] == "accepted"
    assert state._selection_context.pricing is None
    assert state.intent == "selection" and state.review_requested_execution == ("price",)
    assert [i.item_id for i in state.turn_consultations] == ["amount", "rule"]


def test_uncertain_edit_blocks_dependent_amount_but_keeps_independent_rule(state):
    old, match, price = existing_selection(state)
    message = "改成约70席后多少钱？Business的SSO有什么前提？"
    adjudicate(state, message, [item("amount", "改成约70席后多少钱", aspect="pricing", feature=None,
        depends_on=("seats",)), item("rule", "Business的SSO有什么前提", plans=("business",),
        aspect="prerequisite", feature="saml_sso")],
        [group("seats", "set", "改成约70席", verdict="reject", reason="uncertain",
            field="paid_seats", value=70, certainty="approximate")], intent="selection", kind="selection")
    assert state.requirements is old and state.last_match is match
    assert state._selection_context.pricing is price
    assert [i.item_id for i in state.turn_consultations] == ["rule"]
    assert state.consultation_feedback[0]["status"] == "blocked"
    assert not state.consultation_complete and state.review_blockers == ("paid_seats",)


def test_none_target_with_task_operation_remains_whole_review_rejection(state):
    message = "我们需要API，Team支持吗？"
    adjudicate(state, message, [item("api", message, plans=("team",))],
        [group("add", "add", "我们需要API", requirement={"statement": "API", "user_quote": "我们需要API",
            "feature_id": "api_access"})])
    assert not state.review_complete
    assert state.review_blockers == ("target_unknown",)
    assert state.turn_consultations == () and state.requirements.items == ()


def test_read_only_label_cannot_hide_accepted_real_edit(state):
    existing_selection(state)
    adjudicate(state, "把席位改成50席", groups=[group("seats", "set", "把席位改成50席",
        field="paid_seats", value=50, speech_act="instruction")], kind="selection", intent="knowledge")
    assert not state.review_complete and state.review_blockers == ("target_unknown",)
    assert next(p.value for p in state.requirements.parameters if p.name == "paid_seats") == 80


def test_all_current_plans_expand_only_with_explicit_scope_and_dedupe(state):
    message = "请说所有套餐的每席每月单价。"
    adjudicate(state, message, [item("all", message, aspect="unit_price", feature=None, all_plans=True)],
        intent="unit_price")
    assert state.turn_consultations[0].plan_ids == ("basic", "team", "business")
    assert state.consultation_complete
    adjudicate(state, "Team和Team每席每月多少钱？", [item("one", "Team和Team每席每月多少钱",
        plans=("team", "team"), aspect="unit_price", feature=None)], intent="unit_price")
    assert state.turn_consultations[0].plan_ids == ("team",)
    assert state.consultation_complete


@pytest.mark.parametrize("consultation,issue", [
    (item("extra", "Team的API", plans=("basic",)), "unknown_identity"),
    (item("invented", "Team的API", plans=("team",), feature="not_catalog_feature"), "unknown_identity"),
    (item("source", "用户没有说这段", plans=("team",)), "reference_missing"),
    (item("all", "Team的API", all_plans=True), "reference_missing"),
])
def test_fabricated_scope_never_becomes_query_or_support(state, consultation, issue):
    adjudicate(state, "Team的API支持吗？", [consultation])
    assert state.turn_consultations == () and not state.consultation_complete
    assert state.consultation_feedback[0]["issue"] == issue
    assert state.requirements.items == ()


def test_rule_question_and_unknown_detail_are_not_customer_facts_or_hard_requirements(state):
    message = "Business的SSO需要什么前提？Team的API限流是多少？"
    adjudicate(state, message, [item("rule", "Business的SSO需要什么前提", plans=("business",),
        aspect="prerequisite", feature="saml_sso"), item("limit", "Team的API限流是多少", plans=("team",),
        aspect="detail")])
    assert state.consultation_complete
    assert state.requirements.items == () and state.requirements.condition_assertions == ()
    assert state.requirements.parameters == ()


def test_consultation_completeness_is_independent_and_cleared_next_turn(state):
    adjudicate(state, "Team支持API吗？", [item("api", "Team支持API", plans=("team",))],
        consultation_complete=False)
    assert state.review_complete and not state.consultation_complete
    assert state.review_blockers == ()
    state.begin_review_turn()
    assert state.turn_consultations == () and state.consultation_feedback == ()
    assert not state.consultation_complete
    adjudicate(state, "Team支持API吗？")
    assert state.review_complete and not state.consultation_complete


def test_schema_rejects_duplicate_item_identity_and_dangling_dependencies():
    raw = {"intent": "knowledge", "coverage_complete": True, "consultations": [
        item("api", "Team API", plans=("team",)), item("api", "Team API", plans=("team",))]}
    with pytest.raises(ValidationError):
        SemanticReview.model_validate(raw)
    raw["consultations"] = [item("api", "Team API", plans=("team",), depends_on=("missing",))]
    with pytest.raises(ValidationError):
        SemanticReview.model_validate(raw)


@pytest.mark.parametrize("scope", ["每个套餐", "每一个套餐", "各套餐", "各个套餐", "全部当前套餐"])
def test_local_universal_quantifier_accepts_current_catalog_scope(state, scope):
    message = f"请说明{scope}的每席每月单价。"
    adjudicate(state, message, [item("all", message, aspect="unit_price", feature=None, all_plans=True)],
        intent="unit_price")
    assert state.consultation_complete
    assert state.turn_consultations[0].plan_ids == ("basic", "team", "business")


@pytest.mark.parametrize("quoted_span", ["Team支持API吗", "所有套餐单价多少？另外Team支持API吗？"])
def test_all_plans_cannot_borrow_an_independent_question_quantifier(state, quoted_span):
    message = "所有套餐单价多少？另外Team支持API吗？"
    adjudicate(state, message, [item("api", quoted_span, all_plans=True)])
    assert state.turn_consultations == () and not state.consultation_complete
    assert state.consultation_feedback[0]["issue"] == "reference_missing"


def test_full_span_keeps_explicit_non_cartesian_relations(state):
    message = "Team的API、Basic的Webhook支持吗？"
    adjudicate(state, message, [item("api", message, plans=("team", "basic"))])
    assert state.turn_consultations == () and not state.consultation_complete
    assert state.consultation_feedback[0]["issue"] == "reference_missing"


def test_unique_universal_antecedent_and_pronoun_may_share_current_message(state):
    message = "我想了解所有套餐。它们是否支持API？"
    adjudicate(state, message, [item("api", "它们是否支持API", all_plans=True,
        reference_span={"quote": "我想了解所有套餐"})])
    assert state.consultation_complete
    assert state.turn_consultations[0].plan_ids == ("basic", "team", "business")
    adjudicate(state, message, [item("api", message, all_plans=True)])
    assert state.consultation_complete


@pytest.mark.parametrize("message,antecedent", [
    ("所有套餐的单价是多少？它们是否支持API？", "所有套餐的单价是多少？"),
    ("我想了解所有套餐。另问Team。它们是否支持API？", "我想了解所有套餐"),
])
def test_universal_reference_is_not_another_query_or_competing_antecedent(state, message, antecedent):
    adjudicate(state, message, [item("api", "它们是否支持API", all_plans=True,
        reference_span={"quote": antecedent})])
    assert not state.consultation_complete
    if message.startswith("所有套餐的单价"):
        assert state.turn_consultations[0].plan_ids == ("basic", "team", "business")
        assert any(entry["status"] == "missing" and entry["aspect"] == "unit_price"
                   for entry in state.consultation_questions)
    else:
        assert state.turn_consultations == ()


def test_named_pronoun_reference_preserves_exact_two_plan_scope(state):
    message = "先比较Team和Basic。它们是否支持API？另外Business单价多少？"
    adjudicate(state, message, [item("api", "它们是否支持API", plans=("team", "basic"),
        reference_span={"quote": "先比较Team和Basic"})])
    assert not state.consultation_complete and state.turn_consultations[0].plan_ids == ("team", "basic")
    assert any(entry["status"] == "missing" and entry["plan_ids"] == ["business"]
               for entry in state.consultation_questions)
    adjudicate(state, message, [item("api", "它们是否支持API", plans=("business",),
        reference_span={"quote": "先比较Team和Basic"})])
    assert not state.consultation_complete and state.turn_consultations == ()


def test_implicit_two_capabilities_keep_actual_four_relations(state):
    message = "需要API和Webhook。Team和Basic支持这两项吗？"
    adjudicate(state, message, [item("api", "Team和Basic支持这两项", plans=("team", "basic"),
        reference_span={"quote": "需要API和Webhook"}),
        item("webhook", "Team和Basic支持这两项", plans=("team", "basic"), feature="webhook_notifications",
        reference_span={"quote": "需要API和Webhook"})])
    assert state.consultation_complete
    assert sum(len(i.plan_ids) for i in state.turn_consultations) == 4


def test_capability_pronoun_cannot_borrow_unrelated_feature_from_another_question(state):
    message = "需要API和Webhook。Team支持这两项吗？另外Business的SSO前提是什么？"
    adjudicate(state, message, [item("wrong", "Team支持这两项", plans=("team",), feature="saml_sso",
        reference_span={"quote": "需要API和Webhook"})])
    assert state.turn_consultations == () and not state.consultation_complete
    assert state.consultation_feedback[0]["issue"] == "reference_missing"


@pytest.mark.parametrize("message,plans,all_plans,accepted", [
    ("除了Basic，各套餐每席每月单价多少？", ("team", "business"), False, True),
    ("除了Basic，各套餐每席每月单价多少？", (), True, False),
    ("各套餐每席每月单价多少，Basic除外？", ("basic", "team", "business"), False, False),
    ("只问Team每席每月单价，Basic不用查。", ("team",), False, True),
    ("只问Team每席每月单价，Basic不用查。", ("team", "basic"), False, False),
])
def test_explicit_exclusion_and_limited_plan_scope_are_not_expanded(state, message, plans, all_plans, accepted):
    adjudicate(state, message, [item("unit", message, plans=plans, aspect="unit_price", feature=None,
        all_plans=all_plans)], intent="unit_price")
    assert state.consultation_complete is accepted
    if accepted:
        assert state.turn_consultations[0].plan_ids == plans
    else:
        assert state.turn_consultations == ()


@pytest.mark.parametrize("message,plans", [
    ("只问Team和Basic每席每月单价。", ("team", "basic")),
    ("仅比较Basic、Team每席每月单价。", ("basic", "team")),
])
def test_limited_continuous_plan_list_preserves_each_named_plan_and_order(state, message, plans):
    adjudicate(state, message, [item("unit", message, plans=plans, aspect="unit_price", feature=None)],
        intent="unit_price")
    assert state.consultation_complete and state.turn_consultations[0].plan_ids == plans


def test_limited_list_does_not_borrow_or_restrict_an_independent_question(state):
    message = "只问Team和Basic每席每月单价；另外Business支持API吗？"
    adjudicate(state, message, [item("unit", message, plans=("team", "basic"), aspect="unit_price", feature=None),
        item("api", message, plans=("business",))])
    assert state.consultation_complete
    assert [entry.plan_ids for entry in state.turn_consultations] == [("team", "basic"), ("business",)]
    adjudicate(state, message, [item("unit", message, plans=("team", "basic", "business"),
        aspect="unit_price", feature=None)], intent="unit_price")
    assert not state.consultation_complete and state.turn_consultations == ()
    assert state.consultation_feedback[0]["issue"] == "reference_missing"


@pytest.mark.parametrize("message", ["全部套餐的每席每月单价，但不包括Basic和Team。",
    "各套餐的每席每月单价，Basic、Team除外。"])
def test_continuous_exclusion_list_removes_every_named_plan(state, message):
    adjudicate(state, message, [item("unit", message, plans=("business",), aspect="unit_price", feature=None)],
        intent="unit_price")
    assert state.consultation_complete and state.turn_consultations[0].plan_ids == ("business",)
    adjudicate(state, message, [item("unit", message, plans=("team", "business"), aspect="unit_price", feature=None)],
        intent="unit_price")
    assert not state.consultation_complete and state.turn_consultations == ()


@pytest.mark.parametrize("message,quote,reference", [
    ("各套餐每席每月单价，Basic不用查。", "各套餐每席每月单价，Basic不用查。", None),
    ("各套餐每席每月单价多少？Basic不用查。", "各套餐每席每月单价多少", "Basic不用查"),
    ("不查Basic。各套餐每席每月单价多少？", "不查Basic。各套餐每席每月单价多少？", None),
    ("不查Basic。各套餐每席每月单价多少？", "各套餐每席每月单价多少", "不查Basic"),
    ("除了Basic，各套餐每席每月单价多少？", "除了Basic，各套餐每席每月单价多少？", None),
    ("各套餐的每席每月单价，Basic除外。", "各套餐的每席每月单价，Basic除外。", None),
])
def test_attached_plan_exclusion_accepts_exact_remaining_scope_and_rejects_full_scope(state, message, quote, reference):
    source = {"reference_span": {"quote": reference}} if reference else {}
    correct = item("unit", quote, plans=("team", "business"), aspect="unit_price", feature=None, **source)
    adjudicate(state, message, [correct], intent="unit_price")
    assert state.consultation_complete
    assert state.turn_consultations[0].plan_ids == ("team", "business")
    assert state.consultation_feedback[0]["status"] == "accepted"
    wrong = item("unit", quote, aspect="unit_price", feature=None, all_plans=True, **source)
    adjudicate(state, message, [wrong], intent="unit_price")
    assert not state.consultation_complete and state.turn_consultations == ()
    assert state.consultation_feedback[0]["status"] == "rejected"
    assert state.consultation_feedback[0]["issue"] == "reference_missing"


def test_excluded_only_plan_names_still_establish_current_catalog_minus_exact_list(state):
    message = "全部套餐的每席每月单价，但不包括基础版 Basic和团队版 Team。"
    adjudicate(state, message, [item("unit", message, plans=("business",), aspect="unit_price", feature=None)],
        intent="unit_price")
    assert state.consultation_complete and state.turn_consultations[0].plan_ids == ("business",)
    for extra in ("basic", "team"):
        adjudicate(state, message, [item("unit", message, plans=(extra, "business"), aspect="unit_price", feature=None)],
            intent="unit_price")
        assert not state.consultation_complete and state.turn_consultations == ()


def test_explicit_two_plan_list_and_other_plans_not_asked_remains_exact(state):
    message = "只问Team和Basic的每席每月单价，其他套餐这次不问。"
    adjudicate(state, message, [item("unit", message, plans=("team", "basic"), aspect="unit_price", feature=None)],
        intent="unit_price")
    assert state.consultation_complete and state.turn_consultations[0].plan_ids == ("team", "basic")
    adjudicate(state, message, [item("unit", message, plans=("team", "basic", "business"),
        aspect="unit_price", feature=None)], intent="unit_price")
    assert not state.consultation_complete and state.turn_consultations == ()


def test_price_exclusion_applies_to_rest_price_scope_and_never_to_independent_basic_support(state):
    message = "价格不查Basic。请列出其余套餐的每席每月单价；另外Basic支持产品API吗？"
    units = item("unit", "请列出其余套餐的每席每月单价", plans=("team", "business"),
        aspect="unit_price", feature=None, reference_span={"quote": "价格不查Basic"})
    support = item("api", "Basic支持产品API吗", plans=("basic",))
    adjudicate(state, message, [units, support])
    assert state.consultation_complete
    assert [(entry.item_id, entry.plan_ids) for entry in state.turn_consultations] == [
        ("unit", ("team", "business")), ("api", ("basic",))]
    for wrong in (units | {"plan_ids": (), "all_plans": True},
                  support | {"plan_ids": ("team", "business")}):
        adjudicate(state, message, [wrong])
        assert not state.consultation_complete and state.turn_consultations == ()


def test_independent_team_support_and_all_prices_keep_separate_ranges(state):
    message = "Team支持产品API吗？另外请列出所有套餐的每席每月单价。"
    adjudicate(state, message, [item("api", message, plans=("team",)),
        item("unit", message, all_plans=True, aspect="unit_price", feature=None)])
    assert state.consultation_complete
    assert [(entry.item_id, entry.plan_ids) for entry in state.turn_consultations] == [
        ("api", ("team",)), ("unit", ("basic", "team", "business"))]
    adjudicate(state, message, [item("api", message, all_plans=True)])
    assert not state.consultation_complete and state.turn_consultations == ()


def test_untyped_exclusion_between_competing_questions_is_not_assigned_by_guess(state):
    message = "Team支持API吗？Basic不用查。各套餐每席每月单价多少？"
    for consultation in (item("api", "Team支持API吗", plans=("team",)),
                         item("unit", "各套餐每席每月单价多少", all_plans=True, aspect="unit_price", feature=None)):
        adjudicate(state, message, [consultation])
        assert not state.consultation_complete
        assert [(entry.item_id, entry.plan_ids) for entry in state.turn_consultations] == (
            [("api", ("team",))] if consultation["item_id"] == "api" else [])
        assert any(entry["status"] == "ambiguous" and entry["aspect"] == "unit_price"
                   for entry in state.consultation_questions)


def test_unique_anaphoric_remaining_scope_preserves_antecedent_exclusion(state):
    message = "除了Basic，各套餐。它们支持API吗？"
    source = {"reference_span": {"quote": "除了Basic，各套餐"}}
    adjudicate(state, message, [item("api", "它们支持API吗", plans=("team", "business"), **source)])
    assert state.consultation_complete and state.turn_consultations[0].plan_ids == ("team", "business")
    adjudicate(state, message, [item("api", "它们支持API吗", all_plans=True, **source)])
    assert not state.consultation_complete and state.turn_consultations == ()


def test_explicit_positive_price_list_restores_source_order_and_requires_whole_question_coverage(state):
    message = "只问Team和Basic的每席每月单价，其他套餐这次不问。"
    for plans in (("team", "basic"), ("basic", "team")):
        adjudicate(state, message, [item("unit", message, plans=plans, aspect="unit_price", feature=None)], intent="unit_price")
        assert state.consultation_complete and state.turn_consultations[0].plan_ids == ("team", "basic")
        assert state.consultation_feedback[0]["plan_ids"] == ["team", "basic"]
    for plans in (("team",), ("team", "basic", "business")):
        adjudicate(state, message, [item("unit", message, plans=plans, aspect="unit_price", feature=None)], intent="unit_price")
        assert not state.consultation_complete
        if "business" in plans:
            assert state.turn_consultations == ()
            assert state.consultation_feedback[0]["issue"] == "unknown_identity"
        else:
            assert state.turn_consultations[0].plan_ids == ("team",)
            assert any(entry["status"] == "missing" and entry["plan_ids"] == ["basic"]
                       for entry in state.consultation_questions)


def test_explicit_price_list_can_split_into_items_without_losing_order_or_scope(state):
    message = "只问Team和Basic的每席每月单价。"
    adjudicate(state, message, [item("basic", message, plans=("basic",), aspect="unit_price", feature=None),
        item("team", message, plans=("team",), aspect="unit_price", feature=None)], intent="unit_price")
    assert state.consultation_complete
    assert [(entry.item_id, entry.plan_ids) for entry in state.turn_consultations] == [
        ("team", ("team",)), ("basic", ("basic",))]


def test_price_scope_group_check_does_not_tighten_legal_split_support_relations(state):
    message = "只问Team和Basic是否支持API？"
    adjudicate(state, message, [item("team-api", message, plans=("team",)),
        item("basic-api", message, plans=("basic",))])
    assert state.consultation_complete
    assert [(entry.item_id, entry.plan_ids) for entry in state.turn_consultations] == [
        ("team-api", ("team",)), ("basic-api", ("basic",))]


def test_remaining_catalog_price_scope_can_split_into_per_plan_items(state):
    message = "各套餐每席每月单价，Basic不用查。"
    adjudicate(state, message, [item("business", message, plans=("business",), aspect="unit_price", feature=None),
        item("team", message, plans=("team",), aspect="unit_price", feature=None)], intent="unit_price")
    assert state.consultation_complete
    assert [(entry.item_id, entry.plan_ids) for entry in state.turn_consultations] == [
        ("team", ("team",)), ("business", ("business",))]


def test_distinct_price_questions_cannot_fill_each_others_missing_positive_list_member(state):
    message = "只问Team和Basic的每席每月单价。另问仅比较Basic、Team每席每月单价。"
    adjudicate(state, message, [item("first", "只问Team和Basic的每席每月单价", plans=("team",),
        aspect="unit_price", feature=None), item("second", "仅比较Basic、Team每席每月单价", plans=("basic",),
        aspect="unit_price", feature=None)], intent="unit_price")
    assert not state.consultation_complete
    assert [(entry.item_id, entry.plan_ids) for entry in state.turn_consultations] == [
        ("first", ("team",)), ("second", ("basic",))]
    missing = [entry for entry in state.consultation_questions if entry["status"] == "missing"]
    assert len(missing) == 2 and missing[0]["question_id"] != missing[1]["question_id"]


@pytest.mark.parametrize("include_reference", [False, True])
def test_remaining_scope_uses_unique_finite_antecedent_before_catalog_default(state, include_reference):
    message = "先比较Team和Basic。价格不查Basic。请列出其余套餐的每席每月单价。"
    source = {"reference_span": {"quote": "先比较Team和Basic"}} if include_reference else {}
    quote = "请列出其余套餐的每席每月单价"
    adjudicate(state, message, [item("unit", quote, plans=("team",), aspect="unit_price", feature=None, **source)],
        intent="unit_price")
    assert state.consultation_complete and state.turn_consultations[0].plan_ids == ("team",)
    adjudicate(state, message, [item("unit", quote, plans=("team", "business"), aspect="unit_price", feature=None, **source)],
        intent="unit_price")
    assert not state.consultation_complete and state.turn_consultations == ()


def test_remaining_scope_does_not_reuse_finite_antecedent_across_independent_question_boundary(state):
    message = "先比较Team和Basic。另问价格不查Basic。请列出其余套餐的每席每月单价。"
    quote = "请列出其余套餐的每席每月单价"
    adjudicate(state, message, [item("unit", quote, plans=("team", "business"), aspect="unit_price", feature=None)],
        intent="unit_price")
    assert state.consultation_complete and state.turn_consultations[0].plan_ids == ("team", "business")
    adjudicate(state, message, [item("unit", quote, plans=("team",), aspect="unit_price", feature=None,
        reference_span={"quote": "先比较Team和Basic"})], intent="unit_price")
    assert not state.consultation_complete and state.turn_consultations == ()


def production_review(raw, state, message):
    class ControlledProvider:
        async def chat(self, **kwargs):
            assert kwargs["max_retries"] == 0
            assert kwargs["tool_choice"]["function"]["name"] == "adjudicate_turn"
            assert {"consultations", "consultation_coverage_complete"} <= set(
                kwargs["tools"][0]["function"]["parameters"]["required"])
            return SimpleNamespace(usage=None, choices=[SimpleNamespace(message=SimpleNamespace(tool_calls=[
                SimpleNamespace(function=SimpleNamespace(name="adjudicate_turn", arguments=json.dumps(raw, ensure_ascii=False)))
            ]))])
    return asyncio.run(SemanticVerifier(state.settings).verify(ControlledProvider(), message,
        {"intent": raw["intent"]}, state))


@pytest.mark.parametrize("kind", ["selection", "price"])
@pytest.mark.parametrize("missing", [("consultations",), ("consultation_coverage_complete",),
    ("consultations", "consultation_coverage_complete")])
def test_production_parse_missing_consultation_contract_never_defaults_complete_or_drops_edit(state, kind, missing):
    message = "Team，购买50席，订阅6个月。"
    raw = {"intent": kind, "target": {"kind": kind, "relation": "new", "span": {"quote": message}},
        "coverage_complete": True, "consultations": [], "consultation_coverage_complete": True,
        "groups": [group("plan", "select_plan", "Team", value="team"),
            group("seats", "set", "购买50席", field="paid_seats", value=50),
            group("months", "set", "订阅6个月", field="months", value=6)]}
    for key in missing:
        raw.pop(key)
    review, metrics = production_review(raw, state, message)
    assert review.consultation_contract_missing and not review.consultation_coverage_complete
    assert metrics["consultation_contract_missing"]
    state.begin_review_turn()
    state.apply_review(review, message)
    assert state.review_complete and state.consultation_scope_reviewed
    assert state.consultation_contract_missing and not state.consultation_complete
    assert {p.name: p.value for p in state.requirements.parameters} == {"paid_seats": 50, "months": 6}
    assert state.requirements.selected_plan_id == "team"
    assert state.review_blockers == ()


@pytest.mark.parametrize("kind", ["selection", "price"])
def test_production_parse_explicit_empty_consultations_is_compatible_with_pure_business_turn(state, kind):
    message = "Team，购买50席，订阅6个月。"
    raw = {"intent": kind, "target": {"kind": kind, "relation": "new", "span": {"quote": message}},
        "coverage_complete": True, "consultations": [], "consultation_coverage_complete": True,
        "groups": [group("seats", "set", "购买50席", field="paid_seats", value=50)]}
    review, metrics = production_review(raw, state, message)
    assert not review.consultation_contract_missing and review.consultation_coverage_complete
    assert not metrics["consultation_contract_missing"]
    state.begin_review_turn()
    state.apply_review(review, message)
    assert state.review_complete and state.consultation_complete and not state.consultation_contract_missing
    assert next(p.value for p in state.requirements.parameters if p.name == "paid_seats") == 50
