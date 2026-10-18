"""Rejected numeric edits keep a separate, task-owned clarification context.

Controlled extraction/review here proves server acceptance and orchestration,
not real-model language accuracy. Domain match/pricing are real. WorkState
checks open no database; orchestrator checks use pytest's isolated tmp_path.
"""

import asyncio

import pytest

from app.config import Settings
from app.schemas.chat import ChatRequest
from app.services.chat_render import pending_questions
from app.services.pricing import calculate_subscription_price
from app.services.proposals import ProposalRejected, prepare_proposal
from app.services.work_state import WorkState
from test_input_orchestrator import call, completion, engine, group, script
from test_proposals import seed


def feature(quote="必须API"):
    return {"kind": "feature", "feature_id": "api_access", "statement": quote, "user_quote": quote}


def operation(action, quote, **values):
    return {"action": action, "span": {"quote": quote}, **values}


def accepted(identity, *operations):
    return {"group_id": identity, "verdict": "accept", "operations": operations}


def rejected(identity, field, quote, value):
    return {"group_id": identity, "verdict": "reject", "reason": "approximate", "operations": [
        operation("set", quote, field=field, value=value, certainty="approximate")]}


def apply(state, message, *groups, kind="selection", relation="continue", intent=None, target=None):
    state.begin_review_turn()
    context = state._selection_context if kind == "selection" else state._price_context if kind == "price" else None
    selected_target = target or {"kind": kind, "relation": relation,
        "task_id": context.requirements.requirement_id if context else None}
    state.apply_review({"intent": intent or kind, "target": selected_target, "coverage_complete": True,
        "groups": groups, "consultations": [], "consultation_coverage_complete": True}, message, {})


def end_turn(state, *, match=None, price=None, turn_id="controlled-turn"):
    pending, questions = pending_questions(state, price_requested=state.wants_price)
    if state.intent in {"selection", "price"}:
        state.pending = pending
    state.remember_result(match, price, pending, questions, turn_id=turn_id)


@pytest.fixture
def complete_state():
    state = WorkState(Settings(_env_file=None))
    message = "必须API，购买80席，订阅12个月，请算基价"
    state.apply_patch({"intent": "selection", "add": [feature()], "parameters": [
        {"name": "paid_seats", "value": 80, "user_quote": "购买80席"},
        {"name": "months", "value": 12, "user_quote": "订阅12个月"}], "pricing_quote": "请算基价"}, message)
    match = state.match()
    price = calculate_subscription_price("team", 80, 12)
    state.validate_price("team", 80, 12)
    state.record_pricing_result(price)
    state.remember_result(match, price, (), (), turn_id="controlled-original")
    state.bind_result_message("controlled-original", "controlled-original-message")
    return state


def parameters(state):
    return {p.name: p.value for p in state.requirements.parameters}


def assert_accepted(state):
    assert all(item["status"] == "accepted" for item in state.operation_feedback)
    assert not state.review_blockers


@pytest.mark.parametrize("field,answer,expected", [("paid_seats", "50", 50), ("months", "6", 6)])
def test_unique_rejected_edit_accepts_bare_correction_without_rewriting_retained_result(complete_state, field, answer, expected):
    state = complete_state
    before = state.current_task(recheck=False)
    before_items = state.requirements.items
    receipt = state._selection_context.pricing_requirements, state._selection_context.pricing_match
    quote = "改为约50席" if field == "paid_seats" else "期限改为约6个月"
    apply(state, quote, rejected("uncertain", field, quote, expected))
    end_turn(state)
    retained = state.current_task(recheck=False)
    for name in ("requirements", "pricing", "result_turn_id", "result_message_id", "pending"):
        assert retained[name] == before[name]
    assert (state._selection_context.pricing_requirements, state._selection_context.pricing_match) == receipt
    assert state.pending == ()

    apply(state, answer, accepted("correction", operation("set", answer, field=field, value=expected)))
    assert_accepted(state)
    values = parameters(state)
    assert values[field] == expected
    assert values["months" if field == "paid_seats" else "paid_seats"] == (12 if field == "paid_seats" else 80)
    assert state.requirements.items == before_items
    assert state.requirements.requirement_id == before["requirements"]["requirement_id"]
    assert state.requirements.revision == before["requirements"]["revision"] + 1
    assert state.current_task(recheck=False)["pricing"] is None
    match = state.match()
    with pytest.raises(ProposalRejected, match="match_or_price_missing"):
        prepare_proposal(state, session_id="controlled", turn_id="bare-answer", interaction_revision=2)
    price = calculate_subscription_price("team", values["paid_seats"], values["months"])
    state.validate_price("team", values["paid_seats"], values["months"])
    state.record_pricing_result(price)
    end_turn(state, match=match, price=price, turn_id="controlled-corrected")
    assert prepare_proposal(state, session_id="controlled", turn_id="bare-answer", interaction_revision=2).pricing == price


def test_partial_accepted_term_and_rejected_seats_can_continue_from_unique_bare_answer(complete_state):
    state = complete_state
    old_items, old_id, old_revision = state.requirements.items, state.requirements.requirement_id, state.requirements.revision
    apply(state, "席位改为约50席，期限改为6个月",
        rejected("uncertain-seats", "paid_seats", "席位改为约50席", 50),
        accepted("term", operation("set", "期限改为6个月", field="months", value=6)))
    assert parameters(state) == {"paid_seats": 80, "months": 6}
    assert state.current_task(recheck=False)["pricing"] is None
    end_turn(state, match=state.match())
    apply(state, "50", accepted("seats", operation("set", "50", field="paid_seats", value=50)))
    assert_accepted(state)
    assert parameters(state) == {"paid_seats": 50, "months": 6}
    assert state.requirements.items == old_items and state.requirements.requirement_id == old_id
    assert state.requirements.revision == old_revision + 2


@pytest.mark.parametrize("answer,groups", [
    ("50", (accepted("seat", operation("set", "50", field="paid_seats", value=50)),)),
    ("50和6", (accepted("seat", operation("set", "50", field="paid_seats", value=50)),
        accepted("term", operation("set", "6", field="months", value=6)))),
])
def test_two_rejected_numeric_edits_never_guess_bare_number_mapping(complete_state, answer, groups):
    state = complete_state
    apply(state, "席位改为约50席，期限改为约6个月",
        rejected("seat", "paid_seats", "席位改为约50席", 50),
        rejected("term", "months", "期限改为约6个月", 6))
    end_turn(state)
    old = state.requirements
    apply(state, answer, *groups)
    assert state.requirements is old
    assert all(item["status"] == "rejected" and item["issue"] == "ambiguous" for item in state.operation_feedback)


def test_missing_term_plus_rejected_seat_edit_has_two_questions_and_does_not_accept_bare_months():
    state = WorkState(Settings(_env_file=None))
    state.apply_patch({"intent": "selection", "add": [feature()], "parameters": [
        {"name": "paid_seats", "value": 80, "user_quote": "购买80席"}], "pricing_quote": "请算基价"},
        "必须API，购买80席，请算基价")
    end_turn(state, match=state.match())
    assert state.pending == ("months",)
    apply(state, "席位改为约50席", rejected("seat", "paid_seats", "席位改为约50席", 50))
    end_turn(state)
    original = state.requirements
    apply(state, "6", accepted("term", operation("set", "6", field="months", value=6)))
    assert state.requirements is original
    assert state.operation_feedback[0]["issue"] == "ambiguous"


def test_knowledge_detour_preserves_clarification_for_return_to_same_selection(complete_state):
    state = complete_state
    selection_id = state.requirements.requirement_id
    apply(state, "席位改为约50席", rejected("seat", "paid_seats", "席位改为约50席", 50))
    end_turn(state)
    before = state.current_task(recheck=False)
    apply(state, "API是什么意思", kind="none", intent="knowledge", target={"kind": "none", "relation": "detour"})
    end_turn(state)
    assert state.current_task(recheck=False)["requirements"] == before["requirements"]
    apply(state, "50", accepted("seat", operation("set", "50", field="paid_seats", value=50)),
        relation="return", target={"kind": "selection", "relation": "return", "task_id": selection_id})
    assert_accepted(state)
    assert parameters(state) == {"paid_seats": 50, "months": 12}


def test_independent_price_cannot_consume_selection_clarification_but_return_can(complete_state):
    state = complete_state
    selection_id = state.requirements.requirement_id
    apply(state, "席位改为约50席", rejected("seat", "paid_seats", "席位改为约50席", 50))
    end_turn(state)
    message = "另算Basic，购买20席，订6个月"
    apply(state, message,
        accepted("plan", operation("select_plan", "Basic", value="basic")),
        accepted("seats", operation("set", "购买20席", field="paid_seats", value=20)),
        accepted("term", operation("set", "订6个月", field="months", value=6)), kind="price", relation="independent",
        target={"kind": "price", "relation": "independent", "span": {"quote": message}})
    end_turn(state)
    independent = state.requirements
    apply(state, "50", accepted("seat", operation("set", "50", field="paid_seats", value=50)), kind="price")
    assert state.requirements is independent and state.operation_feedback[0]["issue"] == "ambiguous"
    end_turn(state)
    apply(state, "50", accepted("seat", operation("set", "50", field="paid_seats", value=50)),
        relation="return", target={"kind": "selection", "relation": "return", "task_id": selection_id})
    assert_accepted(state)
    assert parameters(state) == {"paid_seats": 50, "months": 12}
    assert state._price_context.requirements is independent


def test_new_selection_does_not_inherit_original_clarification_or_numeric_inputs(complete_state):
    state = complete_state
    apply(state, "席位改为约50席", rejected("seat", "paid_seats", "席位改为约50席", 50))
    end_turn(state)
    original_id = state.requirements.requirement_id
    message = "另一家客户重新选型，50"
    apply(state, message, accepted("seat", operation("set", "50", field="paid_seats", value=50)), relation="new",
        target={"kind": "selection", "relation": "new", "span": {"quote": message}})
    assert state.requirements.requirement_id != original_id
    assert state.requirements.parameters == () and state.requirements.items == ()
    assert state.operation_feedback[0]["issue"] == "ambiguous"


def test_explicit_corrected_value_consumes_clarification_and_later_bare_value_is_not_authorized(complete_state):
    state = complete_state
    apply(state, "席位改为约50席", rejected("seat", "paid_seats", "席位改为约50席", 50))
    end_turn(state)
    apply(state, "改为50席", accepted("seat", operation("set", "改为50席", field="paid_seats", value=50)))
    assert_accepted(state)
    end_turn(state, match=state.match())
    corrected = state.requirements
    apply(state, "60", accepted("seat", operation("set", "60", field="paid_seats", value=60)))
    assert state.requirements is corrected and state.operation_feedback[0]["issue"] == "ambiguous"


def test_confirmation_of_same_retained_value_consumes_clarification_without_new_revision(complete_state):
    state = complete_state
    before = state.current_task(recheck=False)
    original = state.requirements
    apply(state, "席位改为约50席", rejected("seat", "paid_seats", "席位改为约50席", 50))
    end_turn(state)
    apply(state, "80", accepted("seat", operation("set", "80", field="paid_seats", value=80)))
    assert_accepted(state)
    assert state.requirements is original
    end_turn(state)
    for name in ("requirements", "pricing", "result_turn_id", "result_message_id"):
        assert state.current_task(recheck=False)[name] == before[name]
    apply(state, "50", accepted("seat", operation("set", "50", field="paid_seats", value=50)))
    assert state.requirements is original and state.operation_feedback[0]["issue"] == "ambiguous"


def test_one_explicit_correction_leaves_only_the_other_numeric_clarification(complete_state):
    state = complete_state
    apply(state, "席位改为约50席，期限改为约6个月",
        rejected("seat", "paid_seats", "席位改为约50席", 50),
        rejected("term", "months", "期限改为约6个月", 6))
    end_turn(state)
    apply(state, "改为50席", accepted("seat", operation("set", "改为50席", field="paid_seats", value=50)))
    assert state.operation_feedback[0]["status"] == "accepted"
    assert state.review_blockers == ("months",)
    end_turn(state, match=state.match())
    apply(state, "6", accepted("term", operation("set", "6", field="months", value=6)))
    assert_accepted(state)
    assert parameters(state) == {"paid_seats": 50, "months": 6}


def controlled_review(state, *groups):
    return {"intent": "selection", "target": {"kind": "selection", "relation": "continue",
        "task_id": state.requirements.requirement_id}, "coverage_complete": True,
        "consultations": [], "consultation_coverage_complete": True, "groups": groups}


def test_orchestrated_unique_bare_correction_reprices_current_revision_and_never_saves_automatically(engine, monkeypatch):
    sid, runtime, old_proposal = asyncio.run(seed(engine))
    before = runtime.state.current_task(recheck=False)
    quote = "席位改为约50席"
    provider = script(monkeypatch, [completion(call("update_requirements", {"intent": "selection", "parameters": [
        {"name": "paid_seats", "value": 50, "user_quote": quote}]})),
        completion(call("adjudicate_turn", controlled_review(runtime.state, rejected("seat", "paid_seats", quote, 50))))])
    declined = asyncio.run(engine.chat(ChatRequest(message=quote, session_id=sid)))
    assert provider.remaining == []
    assert declined.business.pending == ["review:paid_seats"] and len(declined.business.questions) == 1
    assert runtime.state.pending == () and declined.business.current_task.pending == []
    for name in ("requirements", "pricing", "result_turn_id", "result_message_id"):
        assert runtime.state.current_task(recheck=False)[name] == before[name]
    assert declined.business.proposal is None and runtime.proposal is None
    assert engine.quotes.list_quotes() == []

    corrected_review = controlled_review(runtime.state,
        accepted("seat", operation("set", "50", field="paid_seats", value=50)))
    provider = script(monkeypatch, [completion(call("update_requirements", {"intent": "selection", "parameters": [
        {"name": "paid_seats", "value": 50, "user_quote": "50"}]})),
        completion(call("adjudicate_turn", corrected_review)), completion(call("match_requirements", {})),
        completion(call("calculate_price", {"plan_id": "team", "paid_seats": 50, "months": 12})), completion()])
    corrected = asyncio.run(engine.chat(ChatRequest(message="50", session_id=sid)))
    assert corrected.business.operation_feedback[0]["status"] == "accepted"
    assert provider.remaining == []
    assert corrected.status == "completed" and corrected.business.pending == []
    assert corrected.business.pricing == calculate_subscription_price("team", 50, 12)
    proposal = corrected.business.proposal
    assert proposal is not None and proposal.proposal_id != old_proposal.proposal_id
    assert proposal.requirements == corrected.business.requirements
    assert proposal.pricing == corrected.business.pricing
    assert corrected.business.current_task.result_turn_id == corrected.business.turn_id
    assert corrected.business.current_task.result_message_id == corrected.message_id
    assert engine.quotes.list_quotes() == []
    assert asyncio.run(engine.resolve_quote_action(sid, old_proposal.action_ref)).status == "not_saved"
    assert engine.conversations.get_messages(sid)[-1].content == corrected.message


def test_orchestrated_two_corrections_keep_unanswered_term_blocking_price_and_new_proposal(engine, monkeypatch):
    sid, runtime, old_proposal = asyncio.run(seed(engine))
    message = "席位改为约50席，期限改为约6个月"
    outputs = [completion(call("update_requirements", {"intent": "selection", "parameters": [
        {"name": "paid_seats", "value": 50, "user_quote": "席位改为约50席"},
        {"name": "months", "value": 6, "user_quote": "期限改为约6个月"}]})),
        completion(call("adjudicate_turn", controlled_review(runtime.state,
            rejected("seat", "paid_seats", "席位改为约50席", 50),
            rejected("term", "months", "期限改为约6个月", 6))))]
    provider = script(monkeypatch, outputs)
    declined = asyncio.run(engine.chat(ChatRequest(message=message, session_id=sid)))
    assert provider.remaining == []
    assert set(declined.business.pending) == {"review:paid_seats", "review:months"}
    assert declined.business.proposal is None and engine.quotes.list_quotes() == []

    seat_review = controlled_review(runtime.state,
        accepted("seat", operation("set", "50席", field="paid_seats", value=50)))
    provider = script(monkeypatch, [completion(call("update_requirements", {"intent": "selection", "parameters": [
        {"name": "paid_seats", "value": 50, "user_quote": "50席"}]})),
        completion(call("adjudicate_turn", seat_review)), completion(call("match_requirements", {})),
        completion(call("calculate_price", {"plan_id": "team", "paid_seats": 50, "months": 12}))])
    seat_only = asyncio.run(engine.chat(ChatRequest(message="50席", session_id=sid)))
    assert provider.remaining == []
    assert seat_only.business.operation_feedback[0]["status"] == "accepted"
    assert parameters(runtime.state) == {"paid_seats": 50, "months": 12}
    assert seat_only.business.pending == ["review:months"] and len(seat_only.business.questions) == 1
    assert "期限" in seat_only.business.questions[0]
    assert seat_only.business.pricing is None and seat_only.business.proposal is None
    assert runtime.proposal is None and engine.quotes.list_quotes() == []
    price_attempt = next(record for record in seat_only.tool_results if record["name"] == "calculate_price")
    assert not price_attempt["result"]["ok"]
    assert price_attempt["result"]["error"]["details"]["issue"] == "review_dependency"

    month_review = controlled_review(runtime.state,
        accepted("term", operation("set", "6", field="months", value=6)))
    provider = script(monkeypatch, [completion(call("update_requirements", {"intent": "selection", "parameters": [
        {"name": "months", "value": 6, "user_quote": "6"}]})),
        completion(call("adjudicate_turn", month_review)), completion(call("match_requirements", {})),
        completion(call("calculate_price", {"plan_id": "team", "paid_seats": 50, "months": 6})), completion()])
    completed = asyncio.run(engine.chat(ChatRequest(message="6", session_id=sid)))
    assert provider.remaining == []
    assert completed.status == "completed" and completed.business.pending == []
    assert completed.business.pricing == calculate_subscription_price("team", 50, 6)
    assert completed.business.proposal is not None
    assert completed.business.proposal.requirements == completed.business.requirements
    assert completed.business.proposal.pricing == completed.business.pricing
    assert engine.quotes.list_quotes() == []
    assert asyncio.run(engine.resolve_quote_action(sid, old_proposal.action_ref)).status == "not_saved"
