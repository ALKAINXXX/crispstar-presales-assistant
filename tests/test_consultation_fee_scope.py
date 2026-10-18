"""Billing questions retain their own reviewed source anchors and tool kinds."""

import pytest

from test_consultation_scope_convergence import apply, item, relations


@pytest.mark.parametrize("question", [
    "Team每席每月单价多少？",
    "Team的月费是多少？",
    "Team的订阅价格是多少？",
    "Team的订阅费用是多少？",
])
def test_rate_questions_remain_catalog_unit_prices(question):
    state = apply(question, [item("rate", question, ("team",))])
    assert state.consultation_complete
    assert relations(state) == [("unit_price", ("team",), None, "accepted")]


@pytest.mark.parametrize("question", [
    "Team的订阅基价包含哪些费用？",
    "请说明Team的计费规则。",
    "Team的价格口径是什么？",
    "Team的单价含税吗？",
    "Team购买7席订阅3个月时费用包含哪些项目？",
])
def test_billing_properties_require_documentation_even_with_rate_or_quantity(question):
    state = apply(question, [item("fees", question, ("team",), aspect="detail")], intent="knowledge")
    assert state.consultation_complete
    assert relations(state) == [("detail", ("team",), None, "accepted")]
    assert not state.requirements.parameters and not state.wants_price


@pytest.mark.parametrize("question", [
    "Team购买7席订阅3个月的订阅费用是多少？",
    "Team当前方案的总价是多少？",
    "Team当前方案的金额是多少？",
    "Team总共多少钱？",
])
def test_numeric_totals_are_pricing_questions_not_catalog_rates(question):
    state = apply(question, [item("total", question, ("team",), aspect="pricing")], intent="knowledge")
    assert state.consultation_complete
    assert relations(state) == [("pricing", ("team",), None, "accepted")]
    assert not state.requirements.parameters and not state.wants_price


def test_multiple_rates_and_fee_scope_keep_distinct_accepted_identities():
    message = "请先列出 Basic、Team、Business 的每席每月单价，并说明订阅基价包含哪些费用。这里只做价格咨询。"
    plans = ("basic", "team", "business")
    state = apply(message, [item("rates", "请先列出 Basic、Team、Business 的每席每月单价", plans),
        item("fees", "并说明订阅基价包含哪些费用", plans, aspect="detail")])
    assert state.consultation_complete
    assert [(aspect, plan) for aspect, plan, _, _ in relations(state)] == [
        *(('unit_price', (plan,)) for plan in plans), *(('detail', (plan,)) for plan in plans)]
    assert [q["source_item_ids"] for q in state.consultation_questions] == [["rates"]] * 3 + [["fees"]] * 3


def test_mislabeled_fee_item_is_rejected_without_borrowing_rate_coverage():
    message = "Team每席每月单价多少，并说明订阅基价包含哪些费用？"
    state = apply(message, [item("rate", "Team每席每月单价多少", ("team",)),
        item("wrong", "并说明订阅基价包含哪些费用", aspect="pricing")])
    assert not state.consultation_complete
    assert relations(state) == [("unit_price", ("team",), None, "accepted"),
        ("detail", ("team",), None, "missing")]
    assert state.consultation_questions[1]["source_item_ids"] == []
    assert [accepted.item_id for accepted in state.turn_consultations] == ["rate"]


def test_inherited_role_does_not_answer_a_second_independent_rate_question():
    message = "Team每席每月单价多少，订阅价格呢？"
    state = apply(message, [item("first", "Team每席每月单价多少", ("team",))])
    assert not state.consultation_complete
    assert relations(state) == [("unit_price", ("team",), None, "accepted"),
        ("unit_price", ("team",), None, "missing")]
    assert [q["source_item_ids"] for q in state.consultation_questions] == [["first"], []]


def test_a_full_source_can_still_explicitly_cover_two_same_aspect_questions():
    message = "Team每席每月单价多少，订阅价格呢？"
    state = apply(message, [item("both", message, ("team",))])
    assert state.consultation_complete
    assert [q["source_item_ids"] for q in state.consultation_questions] == [["both"], ["both"]]


@pytest.mark.parametrize("question", [
    "使用前必须具备什么？",
    "需要先准备哪些条件？",
    "启用之前需要具备什么？",
])
def test_usage_requirements_are_prerequisites_not_support_questions(question):
    message = "Business的SSO是什么意思，" + question
    state = apply(message, [item("meaning", "Business的SSO是什么意思", ("business",),
        aspect="meaning", feature="saml_sso"), item("rule", question.rstrip("？"), ("business",),
        aspect="prerequisite", feature="saml_sso")], intent="knowledge")
    assert state.consultation_complete
    assert relations(state) == [("meaning", ("business",), "saml_sso", "accepted"),
        ("prerequisite", ("business",), "saml_sso", "accepted")]


def test_prerequisite_pointer_to_prior_definition_cannot_replace_asking_anchor():
    message = "Business的SSO是什么意思，使用前必须具备什么？"
    state = apply(message, [item("wrong", "Business的SSO是什么意思", ("business",),
        aspect="prerequisite", feature="saml_sso")], intent="knowledge")
    assert not state.consultation_complete and not state.turn_consultations
    assert state.consultation_feedback[0]["issue"] == "reference_missing"
    assert [q["source_item_ids"] for q in state.consultation_questions] == [[], []]


def test_a_nonquestion_local_topic_still_substantiates_its_asking_predicate():
    message = "Team的API，返回格式是什么？"
    state = apply(message, [item("format", "Team的API", ("team",),
        aspect="detail", feature="api_access")], intent="knowledge")
    assert state.consultation_complete
    assert relations(state) == [("detail", ("team",), "api_access", "accepted")]


@pytest.mark.parametrize("join,fee_request", [
    ("，", "并说明费用构成"),
    ("，", "再说明费用构成"),
    ("，", "说明费用构成"),
    ("，", "并解释费用构成"),
    ("", "并说明费用构成"),
    ("以及", "说明费用构成"),
    ("并且", "解释费用构成"),
])
def test_omitted_politeness_does_not_erase_a_second_billing_obligation(join, fee_request):
    rate = "请列出Basic、Team、Business的单价"
    message = rate + join + fee_request + "。"
    plans = ("basic", "team", "business")
    state = apply(message, [item("rates", rate, plans),
        item("fees", fee_request, plans, aspect="detail")])
    assert state.consultation_complete
    assert relations(state) == [
        *(("unit_price", (plan,), None, "accepted") for plan in plans),
        *(("detail", (plan,), None, "accepted") for plan in plans)]
    assert [question["source_item_ids"] for question in state.consultation_questions] == [
        ["rates"]] * 3 + [["fees"]] * 3
    assert not state.requirements.parameters and not state.wants_price


@pytest.mark.parametrize("only_aspect", ["unit_price", "detail"])
def test_one_whole_sentence_item_cannot_complete_two_different_billing_obligations(only_aspect):
    message = "请列出Basic、Team、Business的单价，并说明费用构成。"
    plans = ("basic", "team", "business")
    state = apply(message, [item("only", message, plans, aspect=only_aspect)])
    assert not state.consultation_complete
    assert len(state.consultation_questions) == 6
    assert {question["aspect"] for question in state.consultation_questions} == {"unit_price", "detail"}
    for question in state.consultation_questions:
        same = question["aspect"] == only_aspect
        assert question["status"] == ("accepted" if same else "missing")
        assert question["source_item_ids"] == (["only"] if same else [])


@pytest.mark.parametrize("membership", [
    "折扣和实施费有没有算在里面",
    "折扣和实施费是否算在内",
    "折扣和实施费有没有计入订阅基价",
    "折扣和实施费算进去了吗",
])
def test_fee_membership_is_documentation_and_inherits_the_single_prior_plan(membership):
    first = "Team每席每月的价格是否含税"
    message = first + "？" + membership + "？"
    state = apply(message, [item("tax", first, ("team",), aspect="detail"),
        item("membership", membership, ("team",), aspect="detail")], intent="knowledge")
    assert state.consultation_complete
    assert relations(state) == [("detail", ("team",), None, "accepted"),
        ("detail", ("team",), None, "accepted")]
    assert [q["source_item_ids"] for q in state.consultation_questions] == [["tax"], ["membership"]]


def test_fee_membership_cannot_be_answered_by_a_support_item():
    first = "Team的单价含税吗"
    second = "折扣和实施费有没有算在里面"
    message = first + "？" + second + "？"
    state = apply(message, [item("tax", first, ("team",), aspect="detail"),
        item("wrong", second, ("team",), aspect="support", feature="api_access")], intent="knowledge")
    assert not state.consultation_complete
    assert relations(state) == [("detail", ("team",), None, "accepted"),
        ("detail", ("team",), None, "missing")]
    assert state.consultation_feedback[1]["issue"] == "reference_missing"


def test_joined_fee_request_keeps_the_prior_plan_exclusion():
    rate = "请列出所有套餐单价，但不包括Basic和Team"
    fee = "并说明费用构成"
    message = rate + "，" + fee + "。"
    state = apply(message, [item("rate", rate, ("business",)),
        item("fees", fee, ("business",), aspect="detail")])
    assert state.consultation_complete
    assert relations(state) == [("unit_price", ("business",), None, "accepted"),
        ("detail", ("business",), None, "accepted")]
    wrong = apply(message, [item("rate", rate, ("business",)),
        item("fees", fee, ("basic", "business"), aspect="detail")])
    assert not wrong.consultation_complete
    assert wrong.consultation_feedback[1]["issue"] == "reference_missing"


def test_second_request_cannot_borrow_the_first_rate_anchor():
    first = "请列出Basic、Team、Business的单价"
    message = first + "，并说明费用构成。"
    state = apply(message, [item("wrong", first, ("basic", "team", "business"), aspect="detail")])
    assert not state.consultation_complete and not state.turn_consultations
    assert state.consultation_feedback[0]["issue"] == "reference_missing"
    assert all(question["status"] == "missing" for question in state.consultation_questions)


@pytest.mark.parametrize("question", [
    "Team购买7席订3个月计入折扣后一共多少钱？",
    "Team购买7席订3个月算入实施费后总价是多少？",
])
def test_included_cost_background_does_not_erase_requested_amount(question):
    state = apply(question, [item("amount", question, ("team",), aspect="pricing")], intent="knowledge")
    assert state.consultation_complete
    assert relations(state) == [("pricing", ("team",), None, "accepted")]


@pytest.mark.parametrize("background", [
    "查回草案的总价。",
    "查看当前费用。",
    "说明书里面记录Team的费用构成。",
])
def test_new_command_recognition_does_not_create_background_or_history_inquiries(background):
    state = apply(background, [], intent="general")
    assert not state.consultation_questions and not state.turn_consultations
