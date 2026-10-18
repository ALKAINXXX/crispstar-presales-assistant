"""Finite source invariants, through the production one-review parser."""

import asyncio
import json
from types import SimpleNamespace

import pytest

from app.config import Settings
from app.llm.semantic_verifier import SemanticVerifier
from app.services.work_state import WorkState


def item(identity, quote, plans=(), *, aspect="unit_price", feature=None, **extra):
    return {"item_id": identity, "aspect": aspect, "plan_ids": list(plans),
        "feature_id": feature, "topic": quote, "span": {"quote": quote}, **extra}


def apply(message, items, *, intent="unit_price", first=None, state=None, target=None, groups=()):
    state = state or WorkState(Settings(_env_file=None))
    raw = {"intent": intent, "target": target or {"kind": "none", "relation": "detour"},
        "coverage_complete": True, "consultation_coverage_complete": True,
        "groups": groups, "consultations": items}

    class Provider:
        async def chat(self, **kwargs):
            assert kwargs["max_retries"] == 0
            assert kwargs["tool_choice"]["function"]["name"] == "adjudicate_turn"
            return SimpleNamespace(usage=None, choices=[SimpleNamespace(message=SimpleNamespace(
                tool_calls=[SimpleNamespace(function=SimpleNamespace(name="adjudicate_turn",
                    arguments=json.dumps(raw, ensure_ascii=False)))]))])

    review, metrics = asyncio.run(SemanticVerifier(state.settings).verify(Provider(), message,
        first or {"intent": intent}, state))
    assert metrics["status"] == "verified"
    state.begin_review_turn()
    state.apply_review(review, message, first)
    return state


def relations(state):
    return [(question["aspect"], tuple(question["plan_ids"]), question["feature_id"], question["status"])
        for question in state.consultation_questions]


@pytest.mark.parametrize("spelling", ["Basic和Team", "基础版Basic和团队版Team", "基础版 Basic、团队版 Team"])
@pytest.mark.parametrize("short", [False, True])
def test_complete_context_not_quote_length_owns_exclusion(spelling, short):
    message = f"请告诉我全部套餐的每席每月单价，但不包括{spelling}。"
    quote = "全部套餐的每席每月单价" if short else message
    correct = apply(message, [item("price", quote, ("business",))])
    assert correct.consultation_complete
    assert relations(correct) == [("unit_price", ("business",), None, "accepted")]
    for wrong in [item("wrong", quote, all_plans=True), item("wrong", quote, ("team", "business"))]:
        rejected = apply(message, [wrong])
        assert not rejected.consultation_complete and rejected.turn_consultations == ()
        assert all(question["status"] == "missing" for question in rejected.consultation_questions)


@pytest.mark.parametrize("items", [[], [item("team", "Team每席每月单价多少", ("team",))]])
def test_whole_question_inventory_survives_wrong_general_and_omission(items):
    message = "Team每席每月单价多少？Basic支持API吗？"
    state = apply(message, items, intent="general")
    assert not state.consultation_complete
    assert [(kind, plans, feature) for kind, plans, feature, _ in relations(state)] == [
        ("unit_price", ("team",), None), ("support", ("basic",), "api_access")]
    assert state.consultation_questions[1]["status"] == "missing"
    assert state.consultation_questions[1]["issue"] == "extraction_failed"


def test_one_review_corrects_first_candidate_without_second_adjudication():
    message = "请列出Team和Basic的每席每月单价。"
    first = {"intent": "unit_price", "consultations": [item("first", message, ("team",))]}
    state = apply(message, [item("checked", message, ("basic", "team"))], first=first)
    assert state.consultation_complete
    assert state.turn_consultations[0].plan_ids == ("team", "basic")
    assert all(question["source_item_ids"] == ["checked"] for question in state.consultation_questions)


@pytest.mark.parametrize("grouping", ["combined", "per_plan", "interleaved"])
def test_object_order_is_independent_of_item_packing(grouping):
    message = "只问Team、Basic和Business的每席每月单价，请按这个顺序列出。"
    inputs = [item("all", message, ("business", "team", "basic"))] if grouping == "combined" else [
        item("business", message, ("business",)), item("basic", message, ("basic",)),
        item("team", message, ("team",))] if grouping == "per_plan" else [
        item("outer", message, ("team", "business")), item("middle", message, ("basic",))]
    state = apply(message, inputs)
    assert state.consultation_complete
    assert [question["plan_ids"] for question in state.consultation_questions] == [["team"], ["basic"], ["business"]]


@pytest.mark.parametrize("by_feature", [False, True])
def test_four_relations_keep_object_then_capability_order(by_feature):
    message = "Team和Basic都支持API和Webhook吗？"
    inputs = [item("api", message, ("team", "basic"), aspect="support", feature="api_access"),
        item("hook", message, ("team", "basic"), aspect="support", feature="webhook_notifications")] if by_feature else [
        item(f"{plan}-{feature}", message, (plan,), aspect="support", feature=feature)
        for plan in ("basic", "team") for feature in ("webhook_notifications", "api_access")]
    state = apply(message, inputs, intent="knowledge")
    assert state.consultation_complete
    assert [(plans[0], feature) for _, plans, feature, _ in relations(state)] == [
        ("team", "api_access"), ("team", "webhook_notifications"),
        ("basic", "api_access"), ("basic", "webhook_notifications")]


def test_two_actual_pairs_are_not_cartesian_and_wrong_relation_is_rejected():
    message = "Team的API、Basic的Webhook是否支持？"
    state = apply(message, [item("api", "Team的API", ("team",), aspect="support", feature="api_access"),
        item("hook", "Basic的Webhook", ("basic",), aspect="support", feature="webhook_notifications")], intent="knowledge")
    assert state.consultation_complete
    assert len(state.consultation_questions) == 2
    wrong = apply(message, [item("api", message, ("team", "basic"), aspect="support", feature="api_access")], intent="knowledge")
    assert not wrong.consultation_complete and wrong.turn_consultations == ()


def test_explicit_object_blocks_retain_three_relations_without_extra_fourth():
    message = "Team的API和Webhook、Basic的API是否支持？"
    state = apply(message, [item("api", message, ("team", "basic"), aspect="support", feature="api_access"),
        item("hook", message, ("team",), aspect="support", feature="webhook_notifications")], intent="knowledge")
    assert state.consultation_complete
    assert [(plans[0], feature) for _, plans, feature, _ in relations(state)] == [
        ("team", "api_access"), ("team", "webhook_notifications"), ("basic", "api_access")]
    wrong = apply(message, [item("hook", message, ("team", "basic"), aspect="support",
        feature="webhook_notifications")], intent="knowledge")
    assert not wrong.consultation_complete and wrong.turn_consultations == ()


def test_explicit_support_argument_does_not_turn_background_into_extra_question():
    message = "我们需要API和Webhook，Team支持API吗？"
    state = apply(message, [item("api", message, ("team",), aspect="support", feature="api_access")], intent="knowledge")
    assert state.consultation_complete
    assert relations(state) == [("support", ("team",), "api_access", "accepted")]
    wrong = apply(message, [item("hook", message, ("team",), aspect="support",
        feature="webhook_notifications")], intent="knowledge")
    assert not wrong.consultation_complete and wrong.turn_consultations == ()


@pytest.mark.parametrize("wrong", [False, True])
def test_another_plan_is_source_finite_remainder(wrong):
    message = "先比较Team和Basic。Team的单价多少？另一个套餐的单价呢？"
    state = apply(message, [item("first", "Team的单价多少", ("team",)),
        item("second", "另一个套餐的单价呢", ("team" if wrong else "basic",),
            reference_span={"quote": "先比较Team和Basic"})])
    assert state.consultation_complete is not wrong
    assert state.consultation_questions[1]["plan_ids"] == ["basic"]
    if wrong:
        assert state.consultation_questions[1]["status"] == "missing"
        assert state.consultation_questions[1]["source_item_ids"] == []


@pytest.mark.parametrize("all_flag", [False, True])
@pytest.mark.parametrize("declaration", ["correct", "short_exclusion", "wrong_basis", "array_only"])
def test_empty_scope_requires_equivalent_complete_expression_in_production_parse(declaration, all_flag):
    message = "请查所有套餐单价，但排除Basic、Team和Business。"
    inputs = [] if declaration == "array_only" else [item("empty", message, all_plans=all_flag,
        scope_basis={"kind": "named" if declaration == "wrong_basis" else "all_plans",
            "span": {"quote": "Basic" if declaration == "wrong_basis" else "所有套餐"}},
        scope_exclusions=[{"quote": "排除Basic" if declaration == "short_exclusion" else "排除Basic、Team和Business"}])]
    state = apply(message, inputs)
    assert state.consultation_complete is (declaration == "correct")
    assert len(state.consultation_questions) == 1 and state.consultation_questions[0]["plan_ids"] == []
    assert state.consultation_questions[0]["status"] == ("empty_scope" if declaration == "correct" else "missing")


@pytest.mark.parametrize("all_flag", [False, True])
@pytest.mark.parametrize("declaration", ["correct_expression", "explicit_equivalent", "short_exclusion",
    "wrong_basis", "conflicting_explicit"])
def test_nonempty_complete_expression_is_normalized_without_fixing_wrong_review(declaration, all_flag):
    message = "请告诉我全部套餐的每席每月单价，但不包括基础版Basic和团队版Team。"
    plans = ("business",) if declaration == "explicit_equivalent" else ("team",) \
        if declaration == "conflicting_explicit" else ()
    candidate = item("price", message, plans, all_plans=all_flag,
        scope_basis={"kind": "named" if declaration == "wrong_basis" else "all_plans",
            "span": {"quote": "Basic" if declaration == "wrong_basis" else message}},
        scope_exclusions=[{"quote": "不包括基础版Basic" if declaration == "short_exclusion"
            else "不包括基础版Basic和团队版Team"}])
    state = apply(message, [candidate])
    correct = declaration in {"correct_expression", "explicit_equivalent"}
    assert state.consultation_complete is correct
    assert relations(state) == [("unit_price", ("business",), None, "accepted" if correct else "missing")]
    if correct:
        assert state.turn_consultations[0].plan_ids == ("business",)
        assert state.consultation_feedback[0]["status"] == "accepted"
    else:
        assert state.turn_consultations == ()
        assert state.consultation_feedback[0]["issue"] == "reference_missing"


@pytest.mark.parametrize("wrong_basis", [False, True])
def test_paired_named_basis_belongs_to_actual_feature_relation(wrong_basis):
    message = "Team的API、Basic的Webhook是否支持？"
    state = apply(message, [item("c1", "Team的API", ("team",), aspect="support", feature="api_access",
        scope_basis={"kind": "named", "span": {"quote": "Basic的Webhook" if wrong_basis else "Team的API"}}),
        item("c2", "Basic的Webhook", ("basic",), aspect="support", feature="webhook_notifications",
        scope_basis={"kind": "named", "span": {"quote": "Basic的Webhook"}})], intent="knowledge")
    assert state.consultation_complete is not wrong_basis
    assert [question["scope_basis_ids"] for question in state.consultation_questions] == [["team"], ["basic"]]
    assert state.consultation_questions[1]["status"] == "accepted"
    assert state.consultation_questions[0]["status"] == ("missing" if wrong_basis else "accepted")
    if wrong_basis:
        assert state.consultation_feedback[0]["issue"] == "reference_missing"


@pytest.mark.parametrize("message,feature,plans", [
    ("Business的SSO兼容哪些身份提供方品牌？", "saml_sso", ("business",)),
    ("Basic支持SCIM自动开通吗？", None, ("basic",)),
    ("Basic的Webhook通知重试规则是什么？", "webhook_notifications", ("basic",)),
])
def test_clear_unmodeled_detail_is_retained_without_claiming_fact(message, feature, plans):
    state = apply(message, [item("detail", message, plans, aspect="detail", feature=feature)], intent="knowledge")
    assert state.consultation_complete
    assert relations(state) == [("detail", plans, feature, "accepted")]


def test_customer_uncertainty_is_background_to_actual_rule_question():
    message = "Business的SSO需要什么前提？我们只有公司账号，身份提供方是否兼容还不确定，这次只咨询规则。"
    state = apply(message, [item("rule", "Business的SSO需要什么前提", ("business",),
        aspect="prerequisite", feature="saml_sso")], intent="knowledge")
    assert state.consultation_complete and len(state.consultation_questions) == 1
    assert state.requirements.condition_assertions == ()


@pytest.mark.parametrize("explicit_consultation", [False, True])
def test_pure_task_and_explicit_total_consultation_share_business_dependency_boundary(explicit_consultation):
    state = WorkState(Settings(_env_file=None))
    state.apply_patch({"intent": "selection", "add": [{"statement": "API", "user_quote": "必须有API",
        "kind": "feature", "feature_id": "api_access"}], "parameters": [
        {"name": "paid_seats", "value": 80, "user_quote": "买80席"},
        {"name": "months", "value": 12, "user_quote": "订12个月"}]}, "必须有API，买80席，订12个月。")
    message = "把席位改成50席后算当前费用；另外Business每席每月单价多少？"
    pricing = item("total", "把席位改成50席后算当前费用", aspect="pricing", depends_on=["seats"])
    inputs = [pricing] if explicit_consultation else []
    inputs.append(item("independent", "Business每席每月单价多少", ("business",)))
    groups = [{"group_id": "seats", "verdict": "accept", "operations": [{"action": "set",
        "field": "paid_seats", "value": 50, "span": {"quote": "把席位改成50席"}}]},
        {"group_id": "price", "verdict": "accept", "operations": [{"action": "request_pricing",
            "span": {"quote": "算当前费用"}}]}]
    apply(message, inputs, state=state, intent="selection",
        target={"kind": "selection", "relation": "continue", "task_id": state.requirements.requirement_id},
        groups=groups)
    assert state.review_complete and state.consultation_complete
    assert state.review_requested_execution == ("price",)
    assert state.requirements.parameters[0].value == 50
    assert [question["aspect"] for question in state.consultation_questions] == (["pricing", "unit_price"]
        if explicit_consultation else ["unit_price"])
    assert bool(state.task_answer_questions) is not explicit_consultation


def test_task_ownership_never_swallows_wrong_submitted_total_dependency():
    state = WorkState(Settings(_env_file=None))
    message = "把席位改成50席后算当前费用。"
    apply(message, [item("total", message, aspect="pricing")], state=state, intent="selection",
        target={"kind": "selection", "relation": "new", "span": {"quote": message}},
        groups=[{"group_id": "seats", "verdict": "accept", "operations": [{"action": "set",
            "field": "paid_seats", "value": 50, "span": {"quote": "把席位改成50席"}}]},
            {"group_id": "price", "verdict": "accept", "operations": [{"action": "request_pricing",
                "span": {"quote": "算当前费用"}}]}])
    assert state.review_complete and not state.consultation_complete
    assert state.turn_consultations == () and state.task_answer_questions == ()
    assert state.consultation_feedback[0]["issue"] == "dependency_unresolved"
