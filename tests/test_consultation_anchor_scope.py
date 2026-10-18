"""Source-anchor obligations with explicit expectations through production review.

Only the one model review is controlled. Expected questions are written from
the original utterance; no inventory implementation generates the assertions.
"""
import pytest

from test_consultation_scope_convergence import apply, item, relations


SSO_CONTEXT = "我们正在了解企业版 Business 的 SAML SSO。使用这项功能需要具备什么前提？"
MULTI = "Business的SSO是什么，有什么使用前提？"
PAIR_FIRST = "Team的API、Basic的Webhook分别支持吗？"
FEATURE_FIRST = "API方面问Team，Webhook方面问Basic，分别支持吗？"
LOCAL_TOPIC = "API方面，团队版Team支持吗？Webhook通知方面，基础版Basic支持吗？"


@pytest.mark.parametrize("message,quote", [
    (SSO_CONTEXT, SSO_CONTEXT),
    (SSO_CONTEXT, "使用这项功能需要具备什么前提"),
    ("我们正在了解Business的SSO。需要什么前提？", "需要什么前提"),
    ("Business的SSO需要具备什么前提？", "Business的SSO需要具备什么前提"),
    ("我们正在了解Business的SSO。另外，这项功能需要什么前提？", "这项功能需要什么前提"),
])
def test_one_source_frame_carries_both_roles_for_prerequisite(message, quote):
    state = apply(message, [item("rule", quote, ("business",),
        aspect="prerequisite", feature="saml_sso")], intent="knowledge")
    assert state.consultation_complete
    assert relations(state) == [("prerequisite", ("business",), "saml_sso", "accepted")]
    assert not state.requirements.items and not state.requirements.condition_assertions


@pytest.mark.parametrize("aspect,question", [
    ("support", "这项功能支持吗？"),
    ("meaning", "这项功能是什么？"),
    ("detail", "这项功能兼容哪些身份提供方品牌？"),
])
def test_source_frame_is_shared_by_every_knowledge_aspect(aspect, question):
    message = "我们正在了解Business的SSO。" + question
    state = apply(message, [item("question", question, ("business",),
        aspect=aspect, feature="saml_sso")], intent="knowledge")
    assert state.consultation_complete
    assert relations(state) == [(aspect, ("business",), "saml_sso", "accepted")]


def test_explicit_role_overrides_inherited_role_without_broadcasting():
    message = "我们正在了解Business的SSO。Team的SSO是什么？"
    state = apply(message, [item("meaning", "Team的SSO是什么", ("team",),
        aspect="meaning", feature="saml_sso")], intent="knowledge")
    assert state.consultation_complete
    assert relations(state) == [("meaning", ("team",), "saml_sso", "accepted")]


def test_explicit_plan_selects_compatible_source_frame_before_inheriting_capability():
    message = "我们正在了解Team的API。我们正在了解Business的SSO。Business需要什么使用前提？"
    state = apply(message, [item("rule", "Business需要什么使用前提", ("business",),
        aspect="prerequisite", feature="saml_sso")], intent="knowledge")
    assert state.consultation_complete
    assert relations(state) == [("prerequisite", ("business",), "saml_sso", "accepted")]


@pytest.mark.parametrize("basis_kind,quote,accepted", [
    ("named", "我们正在了解企业版 Business 的 SAML SSO", True),
    ("reference", "我们正在了解企业版 Business 的 SAML SSO", True),
    ("named", "使用这项功能需要具备什么前提", False),
])
def test_explicit_background_basis_and_reference_basis_are_equivalent_when_source_proves_roles(basis_kind, quote, accepted):
    state = apply(SSO_CONTEXT, [item("rule", SSO_CONTEXT, ("business",), aspect="prerequisite",
        feature="saml_sso", scope_basis={"kind": basis_kind, "span": {"quote": quote}})], intent="knowledge")
    assert state.consultation_complete is accepted
    assert relations(state) == [("prerequisite", ("business",), "saml_sso", "accepted" if accepted else "missing")]


def test_independent_new_topic_does_not_inherit_old_plan_or_feature():
    message = "我们正在了解Business的SSO。另外，导出格式是什么？"
    state = apply(message, [item("format", "导出格式是什么", aspect="detail",
        feature="data_export")], intent="knowledge")
    assert state.consultation_complete
    assert relations(state) == [("detail", (), "data_export", "accepted")]


def test_wrong_antecedent_is_not_repaired_by_server():
    state = apply(SSO_CONTEXT, [item("wrong", SSO_CONTEXT, ("team",),
        aspect="prerequisite", feature="api_access")], intent="knowledge")
    assert not state.consultation_complete and not state.turn_consultations
    assert relations(state) == [("prerequisite", ("business",), "saml_sso", "missing")]


def test_competing_source_relationships_require_real_local_clarification():
    message = "我们正在了解Team的API，也在了解Business的SSO。它的使用前提是什么？"
    state = apply(message, [item("guess", "它的使用前提是什么", ("business",),
        aspect="prerequisite", feature="saml_sso")], intent="knowledge")
    assert not state.consultation_complete and not state.turn_consultations
    assert state.consultation_questions
    assert any(q["status"] == "ambiguous" for q in state.consultation_questions)


@pytest.mark.parametrize("background", ["Business", "Team和Business"])
def test_partial_background_is_refined_by_compatible_explicit_local_relationship(background):
    message = "我们正在了解" + background + "。Business的SSO是什么，有什么使用前提？"
    state = apply(message, [item("meaning", "Business的SSO是什么", ("business",),
        aspect="meaning", feature="saml_sso"), item("rule", "有什么使用前提", ("business",),
        aspect="prerequisite", feature="saml_sso")], intent="knowledge")
    assert state.consultation_complete
    assert relations(state) == [("meaning", ("business",), "saml_sso", "accepted"),
        ("prerequisite", ("business",), "saml_sso", "accepted")]


@pytest.mark.parametrize("aspect,question,complete", [
    ("meaning", "API是什么意思？", True),
    ("support", "API支持吗？", False),
    ("prerequisite", "API的使用前提是什么？", False),
])
def test_generic_definition_does_not_require_resolving_plan_dependent_questions(aspect, question, complete):
    message = "Team支持API吗？Basic支持API吗？" + question
    inputs = [item(p, p + "支持API吗", (p.lower(),), aspect="support", feature="api_access")
        for p in ("Team", "Basic")]
    inputs.append(item("last", question, ("team",) if aspect == "support" else (),
        aspect=aspect, feature="api_access"))
    state = apply(message, inputs, intent="knowledge")
    assert state.consultation_complete is complete
    assert relations(state)[:2] == [("support", ("team",), "api_access", "accepted"),
        ("support", ("basic",), "api_access", "accepted")]
    assert relations(state)[2] == (aspect, (), "api_access", "accepted" if complete else "ambiguous")


def test_unresolved_reference_is_not_accepted_as_a_generic_question():
    message = "这项功能的使用前提是什么？"
    state = apply(message, [item("generic", message, aspect="prerequisite")], intent="knowledge")
    assert not state.consultation_complete and not state.turn_consultations
    assert state.consultation_questions
    assert all(q["status"] == "missing" for q in state.consultation_questions)
    assert any(q.get("unresolved_reason") for q in state.consultation_questions)


@pytest.mark.parametrize("short", [False, True])
def test_multiple_inquiries_share_roles_but_keep_aspects_and_source_identity(short):
    meanings = "Business的SSO是什么" if short else MULTI
    rules = "有什么使用前提" if short else MULTI
    state = apply(MULTI, [item("meaning", meanings, ("business",), aspect="meaning", feature="saml_sso"),
        item("rule", rules, ("business",), aspect="prerequisite", feature="saml_sso")], intent="knowledge")
    assert state.consultation_complete
    assert relations(state) == [("meaning", ("business",), "saml_sso", "accepted"),
        ("prerequisite", ("business",), "saml_sso", "accepted")]
    assert len({q["question_id"] for q in state.consultation_questions}) == 2
    assert [q["source_item_ids"] for q in state.consultation_questions] == [["meaning"], ["rule"]]


@pytest.mark.parametrize("empty", [False, True])
def test_missing_aspect_survives_partial_review_and_wrong_general(empty):
    inputs = [] if empty else [item("rule", MULTI, ("business",), aspect="prerequisite", feature="saml_sso")]
    state = apply(MULTI, inputs, intent="general" if empty else "knowledge")
    assert not state.consultation_complete
    assert relations(state) == [("meaning", ("business",), "saml_sso", "missing"),
        ("prerequisite", ("business",), "saml_sso", "missing" if empty else "accepted")]


@pytest.mark.parametrize("message,aspects", [
    ("Business的SSO的使用前提是什么？", ["prerequisite"]),
    ("支持Business的SSO的前提是什么？", ["prerequisite"]),
    ("Business是否支持SSO，需要什么前提？", ["support", "prerequisite"]),
])
def test_question_object_distinguishes_modifier_from_independent_predicate(message, aspects):
    state = apply(message, [item(str(i), message, ("business",), aspect=a, feature="saml_sso")
        for i, a in enumerate(aspects)], intent="knowledge")
    assert state.consultation_complete
    assert [(a, p, f) for a, p, f, _ in relations(state)] == [
        (a, ("business",), "saml_sso") for a in aspects]


@pytest.mark.parametrize("question,aspect", [
    ("什么是SSO？", "meaning"),
    ("SSO的身份提供方是什么意思？", "meaning"),
    ("SSO的使用前提是什么？", "prerequisite"),
    ("SSO兼容哪些身份提供方品牌？", "detail"),
])
def test_copular_predicate_classifies_its_object_instead_of_surrounding_nouns(question, aspect):
    message = "我们正在了解Business的SSO。" + question
    state = apply(message, [item("object", question, ("business",), aspect=aspect,
        feature="saml_sso")], intent="knowledge")
    assert state.consultation_complete
    assert relations(state) == [(aspect, ("business",), "saml_sso", "accepted")]


@pytest.mark.parametrize("message", [
    "Team的API、Webhook方面问Basic，分别支持吗？",
    "API方面问Team、Basic的Webhook，分别支持吗？",
    "Team的API和Webhook、Business的SSO分别支持吗？",
    "团队版Team的API、基础版Basic的Webhook分别支持吗？",
    "API方面问团队版 Team、Webhook方面问基础版 Basic，分别支持吗？",
])
def test_each_ownership_component_can_choose_its_own_orientation(message):
    expected = [("team", "api_access"), ("basic", "webhook_notifications")] \
        if "Basic" in message else [("team", "api_access"), ("team", "webhook_notifications"),
                                   ("business", "saml_sso")]
    state = apply(message, [item(str(i), message, (p,), aspect="support", feature=f)
        for i, (p, f) in enumerate(expected)], intent="knowledge")
    assert state.consultation_complete
    assert [(a, p, f) for a, p, f, _ in relations(state)] == [
        ("support", (p,), f) for p, f in expected]


@pytest.mark.parametrize("wrong", [False, True])
def test_explicit_clause_topic_is_bound_before_combining_coordinated_relations(wrong):
    state = apply(LOCAL_TOPIC, [item("api", "团队版Team支持吗", ("team",), aspect="support",
        feature="webhook_notifications" if wrong else "api_access"),
        item("hook", "基础版Basic支持吗", ("basic",), aspect="support",
        feature="api_access" if wrong else "webhook_notifications")], intent="knowledge")
    assert state.consultation_complete is (not wrong)
    assert relations(state) == [("support", ("team",), "api_access", "missing" if wrong else "accepted"),
        ("support", ("basic",), "webhook_notifications", "missing" if wrong else "accepted")]


def test_local_topic_can_be_one_explicit_capability_list():
    message = "API和Webhook方面，Team支持吗？"
    state = apply(message, [item(f, message, ("team",), aspect="support", feature=f)
        for f in ("api_access", "webhook_notifications")], intent="knowledge")
    assert state.consultation_complete
    assert relations(state) == [("support", ("team",), "api_access", "accepted"),
        ("support", ("team",), "webhook_notifications", "accepted")]


@pytest.mark.parametrize("message", [PAIR_FIRST, FEATURE_FIRST])
@pytest.mark.parametrize("short", [False, True])
def test_bidirectional_pairs_have_exactly_the_same_two_relations(message, short):
    quotes = ("Team的API", "Basic的Webhook") if message == PAIR_FIRST else (
        "API方面问Team", "Webhook方面问Basic")
    inputs = [item("api", quotes[0] if short else message, ("team",), aspect="support", feature="api_access"),
        item("hook", quotes[1] if short else message, ("basic",), aspect="support", feature="webhook_notifications")]
    state = apply(message, inputs, intent="knowledge")
    assert state.consultation_complete
    assert relations(state) == [("support", ("team",), "api_access", "accepted"),
        ("support", ("basic",), "webhook_notifications", "accepted")]


@pytest.mark.parametrize("wrong", ["swap", "cross"])
def test_feature_first_wrong_pairs_cannot_borrow_entities_in_full_quote(wrong):
    api_plans = ("basic",) if wrong == "swap" else ("team", "basic")
    hook_plans = ("team",) if wrong == "swap" else ("team", "basic")
    state = apply(FEATURE_FIRST, [item("api", FEATURE_FIRST, api_plans, aspect="support", feature="api_access"),
        item("hook", FEATURE_FIRST, hook_plans, aspect="support", feature="webhook_notifications")], intent="knowledge")
    assert not state.consultation_complete and not state.turn_consultations
    assert [(a, p, f) for a, p, f, _ in relations(state)] == [
        ("support", ("team",), "api_access"), ("support", ("basic",), "webhook_notifications")]


@pytest.mark.parametrize("omit_format", [False, True])
def test_same_feature_two_details_never_deduplicate_or_union_evidence(omit_format):
    message = "Team的API限流是多少，返回格式是什么？"
    limit = item("limit", "Team的API限流是多少", ("team",), aspect="detail", feature="api_access")
    inputs = [limit] if omit_format else [limit, item("format", "返回格式是什么", ("team",),
        aspect="detail", feature="api_access")]
    state = apply(message, inputs, intent="knowledge")
    assert state.consultation_complete is (not omit_format)
    assert relations(state) == [("detail", ("team",), "api_access", "accepted"),
        ("detail", ("team",), "api_access", "missing" if omit_format else "accepted")]
    assert len({q["unit_id"] for q in state.consultation_questions}) == 2
    assert state.consultation_questions[1]["source_item_ids"] == ([] if omit_format else ["format"])


@pytest.mark.parametrize("repeated_topic", [False, True])
def test_full_reference_does_not_let_one_detail_topic_cover_another(repeated_topic):
    message = "Team的API限流是多少，返回格式是什么？"
    limit = item("limit", message, ("team",), aspect="detail", feature="api_access")
    limit["topic"] = "API限流是多少"
    second = item("second", message, ("team",), aspect="detail", feature="api_access")
    second["topic"] = "API限流是多少" if repeated_topic else "API返回格式是什么"
    state = apply(message, [limit, second], intent="knowledge")
    assert state.consultation_complete is (not repeated_topic)
    assert len(state.consultation_questions) == 2
    assert state.consultation_questions[0]["status"] == "accepted"
    assert state.consultation_questions[1]["status"] == ("missing" if repeated_topic else "accepted")
    assert "limit" not in state.consultation_questions[1]["source_item_ids"]


def test_same_aspect_legal_merge_explicitly_carries_both_detail_objects():
    message = "Team的API限流是多少，返回格式是什么？"
    state = apply(message, [item("both-details", message, ("team",), aspect="detail", feature="api_access")],
        intent="knowledge")
    assert state.consultation_complete and len(state.consultation_questions) == 2
    assert len({q["question_id"] for q in state.consultation_questions}) == 2
    assert [q["source_item_ids"] for q in state.consultation_questions] == [["both-details"], ["both-details"]]


def test_external_full_exclusion_preserves_each_empty_capability_obligation():
    message = "全部套餐是否支持API和Webhook？Basic、Team、Business不用查。"
    inputs = [item(feature, "全部套餐是否支持API和Webhook", aspect="support", feature=feature,
        scope_basis={"kind": "all_plans", "span": {"quote": "全部套餐"}},
        scope_exclusions=[{"quote": "Basic、Team、Business不用查"}])
        for feature in ("api_access", "webhook_notifications")]
    state = apply(message, inputs, intent="knowledge")
    assert state.consultation_complete
    assert relations(state) == [("support", (), "api_access", "empty_scope"),
        ("support", (), "webhook_notifications", "empty_scope")]
    assert [q["source_item_ids"] for q in state.consultation_questions] == [["api_access"], ["webhook_notifications"]]


@pytest.mark.parametrize("message", [
    "我们正在了解Business的SSO。",
    "原话是“Business的SSO是什么，有什么使用前提？”；这里只记录引文。",
    "不问Business的SSO含义和前提，只记录这项功能。",
])
def test_background_quote_and_negative_request_are_not_inquiries(message):
    state = apply(message, [], intent="general")
    assert state.consultation_complete and not state.consultation_questions
