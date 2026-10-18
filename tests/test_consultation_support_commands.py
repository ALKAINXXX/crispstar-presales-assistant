"""Source-owned expectations for support commands, independent of extraction."""
import pytest

from test_consultation_scope_convergence import apply, item, relations


ORIGINAL = "先说Basic的Webhook支持情况，再说Team的API支持情况，最后列出Basic、Team、Business的每席每月单价。"
EXPECTED = [("support", ("basic",), "webhook_notifications"),
    ("support", ("team",), "api_access"),
    ("unit_price", ("basic",), None), ("unit_price", ("team",), None),
    ("unit_price", ("business",), None)]


def requests():
    return [item("hook", "Basic的Webhook支持情况", ("basic",), aspect="support", feature="webhook_notifications"),
        item("api", "Team的API支持情况", ("team",), aspect="support", feature="api_access"),
        item("rates", "列出Basic、Team、Business的每席每月单价", ("basic", "team", "business"))]


@pytest.mark.parametrize("reviewed", ["empty", "rates_only", "complete"])
def test_all_five_source_obligations_survive_a_missing_model_item(reviewed):
    values = requests() if reviewed == "complete" else requests()[2:] if reviewed == "rates_only" else []
    state = apply(ORIGINAL, values, intent="general" if reviewed == "empty" else "knowledge")
    assert [(a, p, f) for a, p, f, _ in relations(state)] == EXPECTED
    assert state.consultation_complete is (reviewed == "complete")
    assert [q["status"] for q in state.consultation_questions] == (
        ["accepted"] * 5 if reviewed == "complete" else ["missing"] * 2 + ["accepted"] * 3
        if reviewed == "rates_only" else ["missing"] * 5)


@pytest.mark.parametrize("message", [
    "请说明Basic的Webhook支持情况，并说明Team的API支持情况。",
    "先说Basic的Webhook支持情况再说Team的API支持情况。",
    "说说Basic的Webhook支持情况，然后说一下Team的API支持情况。",
    "请说明Basic的Webhook支持情况如何，再说Team的API支持的情况。",
])
def test_adjacent_clear_commands_bind_their_actual_relationships(message):
    state = apply(message, [item("hook", message, ("basic",), aspect="support", feature="webhook_notifications"),
        item("api", message, ("team",), aspect="support", feature="api_access")], intent="knowledge")
    assert state.consultation_complete
    assert relations(state) == [(a, p, f, "accepted") for a, p, f in EXPECTED[:2]]


@pytest.mark.parametrize("wrong", ["swap", "cross"])
def test_new_command_forms_do_not_authorize_crossed_relationships(wrong):
    message = "先说Basic的Webhook支持情况，再说Team的API支持情况。"
    state = apply(message, [item("hook", message, ("team",) if wrong == "swap" else ("basic", "team"),
        aspect="support", feature="webhook_notifications"), item("api", message,
        ("basic",) if wrong == "swap" else ("basic", "team"), aspect="support", feature="api_access")], intent="knowledge")
    assert not state.consultation_complete and not state.turn_consultations
    assert relations(state) == [(a, p, f, "missing") for a, p, f in EXPECTED[:2]]


@pytest.mark.parametrize("message", [
    "必须支持API和Webhook，请选择最低满足档。",
    "销售说Basic的Webhook支持情况还没确认。",
    "说明书记录Team的API支持情况。",
    "说法是Team支持API。",
    "引用：“先说Basic的Webhook支持情况”。",
    "先别说Team的API支持情况。",
])
def test_purchase_background_and_quoted_speech_do_not_invent_a_support_inquiry(message):
    state = apply(message, [], intent="general")
    assert not state.consultation_questions and not state.turn_consultations


@pytest.mark.parametrize("question", ["请说明Team的API限流情况。", "先说Team的API返回格式。",
    "请说明Team的API支持JSON的情况。", "请说明Team的API兼容Oracle的情况。"])
def test_support_situation_does_not_swallow_real_or_unknown_details(question):
    state = apply(question, [item("detail", question, ("team",), aspect="detail", feature="api_access")], intent="knowledge")
    assert state.consultation_complete
    assert relations(state) == [("detail", ("team",), "api_access", "accepted")]


def test_support_command_preserves_explicit_catalog_exclusion():
    message = "请说明所有套餐的API支持情况，Basic不用查。"
    state = apply(message, [item("api", message, ("team", "business"), aspect="support", feature="api_access")], intent="knowledge")
    assert state.consultation_complete
    assert relations(state) == [("support", ("team",), "api_access", "accepted"),
        ("support", ("business",), "api_access", "accepted")]
    wrong = apply(message, [item("wrong", message, all_plans=True, aspect="support", feature="api_access")], intent="knowledge")
    assert not wrong.consultation_complete and not wrong.turn_consultations
