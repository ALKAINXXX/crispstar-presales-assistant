"""Reported ordinary questions: no invented inquiry or duplicate task duty.

Controlled review outputs exercise production acceptance, matching and chat.
They are regression evidence, not measurements of live model reliability.
"""
import asyncio

import pytest

from app.schemas.chat import ChatRequest
from test_consultation_scope_convergence import apply, item, relations
from test_presales_orchestrator import (
    call, completion, engine, review, same_history, script,
)
from test_proposals import seed


LONG_PRICE = "Business每席每月多少钱？这里只问单价，不要修改刚才的选型方案。"
PRIVATE = "我们必须把澄星协作私有化部署在自己的服务器上。Basic、Team、Business里哪个套餐能满足？"
AUDIT = "必须支持审计日志，哪个套餐能满足"


def selection_review(message, *, feature="audit_logs", requirement_quote="必须支持审计日志",
                     consultation_quote="哪个套餐能满足", verdict="accept", **consultation_fields):
    inquiry = item("selection-support", consultation_quote, ("basic", "team", "business"),
        aspect="support", feature=feature, **consultation_fields)
    groups = [{"group_id": "need", "verdict": verdict, "reason": "confirmed" if verdict == "accept" else "uncertain",
        "operations": [{"action": "add", "span": {"quote": requirement_quote},
            "speech_act": "instruction", "requirement": {"kind": "feature", "feature_id": feature,
                "statement": requirement_quote, "user_quote": requirement_quote}}]}]
    target = {"kind": "selection", "relation": "new", "span": {"quote": message}}
    return inquiry, groups, target


@pytest.mark.parametrize("limiter", [
    "这里只问单价，不要修改刚才的选型方案", "本次只问单价，不改当前方案", "仅查询单价",
])
def test_price_scope_limiter_does_not_create_another_question(limiter):
    message = f"Business每席每月多少钱？{limiter}。"
    state = apply(message, [item("rate", "Business每席每月多少钱", ("business",))])
    assert state.consultation_complete
    assert relations(state) == [("unit_price", ("business",), None, "accepted")]


def test_scope_limiter_does_not_hide_an_actual_second_question():
    message = LONG_PRICE + "另外Team每席每月多少钱？"
    state = apply(message, [item("rate", "Business每席每月多少钱", ("business",))])
    assert not state.consultation_complete
    assert relations(state) == [("unit_price", ("business",), None, "accepted"),
        ("unit_price", ("team",), None, "missing")]


def test_scope_limiter_without_previous_question_remains_an_inquiry():
    state = apply("这里只问单价。", [])
    assert not state.consultation_complete and state.consultation_questions


@pytest.mark.parametrize("message,feature,requirement_quote,consultation_quote", [
    (AUDIT, "audit_logs", "必须支持审计日志", "哪个套餐能满足"),
    (PRIVATE, "private_deployment", "我们必须把澄星协作私有化部署在自己的服务器上。",
        "Basic、Team、Business里哪个套餐能满足？"),
    ("必须支持私有化部署，哪个套餐能满足？", "private_deployment", "必须支持私有化部署", "哪个套餐能满足？"),
])
def test_selection_support_question_belongs_to_accepted_requirement(message, feature, requirement_quote, consultation_quote):
    inquiry, groups, target = selection_review(message, feature=feature,
        requirement_quote=requirement_quote, consultation_quote=consultation_quote)
    state = apply(message, [inquiry], intent="selection", groups=groups, target=target)
    assert state.review_complete and state.consultation_complete
    assert not state.turn_consultations and not state.consultation_feedback
    assert len(state.task_answer_questions) == 1
    assert state.task_answer_questions[0]["status"] == "business_owned"
    match = state.match()
    if feature == "private_deployment":
        assert not match.eligible_plan_ids and match.selected_plan_id is None
        assert all(not plan.satisfies_all for plan in match.plans)
    else:
        assert match.selected_plan_id == "business"


@pytest.mark.parametrize("problem", ["rejected_requirement", "wrong_feature", "bad_source", "unresolved_dependency"])
def test_task_ownership_requires_accepted_same_feature_and_valid_source(problem):
    inquiry, groups, target = selection_review(AUDIT, verdict="reject" if problem == "rejected_requirement" else "accept")
    if problem == "wrong_feature":
        inquiry["feature_id"] = "api_access"
    elif problem == "bad_source":
        inquiry["span"] = {"quote": "不存在的选型原话"}
    elif problem == "unresolved_dependency":
        groups.append({"group_id": "blocked", "verdict": "reject", "reason": "uncertain",
            "operations": [{"action": "note", "span": {"quote": "必须支持审计日志"}}]})
        inquiry["depends_on"] = ["blocked"]
    state = apply(AUDIT, [inquiry], intent="selection", groups=groups, target=target)
    assert not state.consultation_complete and not state.task_answer_questions


@pytest.mark.parametrize("basis_quote", ["Basic、Team、Business", "Basic、Team、Business里哪个套餐能满足？"])
def test_actual_review_scope_basis_can_include_the_question_punctuation(basis_quote):
    inquiry, groups, target = selection_review(PRIVATE, feature="private_deployment",
        requirement_quote="我们必须把澄星协作私有化部署在自己的服务器上。",
        consultation_quote="Basic、Team、Business里哪个套餐能满足？",
        scope_basis={"kind": "named", "span": {"quote": basis_quote}})
    state = apply(PRIVATE, [inquiry], intent="selection", groups=groups, target=target)
    assert state.consultation_complete and len(state.task_answer_questions) == 1


def test_separate_support_consultation_still_needs_its_own_answer():
    message = AUDIT + "。另外Team支持API吗？请提供原文依据。"
    inquiry, groups, target = selection_review(message)
    state = apply(message, [inquiry], intent="selection", groups=groups, target=target)
    assert not state.consultation_complete
    assert relations(state) == [("support", ("team",), "api_access", "missing")]
    assert len(state.task_answer_questions) == 1


def test_long_source_cannot_swallow_independent_consultation():
    message = AUDIT + "。另外Team支持API吗？"
    inquiry, groups, target = selection_review(message, consultation_quote=message)
    state = apply(message, [inquiry], intent="selection", groups=groups, target=target)
    assert not state.consultation_complete and not state.task_answer_questions


@pytest.mark.parametrize("short", [False, True])
def test_explicit_documentation_request_is_not_owned_by_catalog_matching(short):
    message = "必须支持审计日志，哪个套餐能满足，请给出原文依据。"
    inquiry, groups, target = selection_review(message, consultation_quote="哪个套餐能满足" if short else message)
    state = apply(message, [inquiry], intent="selection", groups=groups, target=target)
    assert not state.task_answer_questions and not state.consultation_complete


def test_plan_subset_cannot_be_silently_replaced_by_catalog_wide_selection():
    message = "必须支持审计日志，Basic和Team里哪个套餐能满足？"
    inquiry, groups, target = selection_review(message, consultation_quote="Basic和Team里哪个套餐能满足")
    inquiry["plan_ids"] = ["basic", "team"]
    state = apply(message, [inquiry], intent="selection", groups=groups, target=target)
    assert not state.task_answer_questions and not state.consultation_complete


def test_model_item_id_cannot_erase_an_unanswered_independent_question():
    message = AUDIT + "。另外Team支持API吗？"
    inquiry, groups, target = selection_review(message)
    missing = apply(message, [], intent="selection", groups=groups, target=target).consultation_questions[0]
    inquiry["item_id"] = missing["unit_id"]
    state = apply(message, [inquiry], intent="selection", groups=groups, target=target)
    assert not state.consultation_complete
    assert relations(state) == [("support", ("team",), "api_access", "missing")]


def test_long_price_detour_keeps_the_original_task_and_revokes_old_save(engine, monkeypatch):
    sid, runtime, old_proposal = asyncio.run(seed(engine))
    before = runtime.state.current_task(recheck=False)
    inquiry = item("rate", "Business每席每月多少钱", ("business",))
    script(monkeypatch, [completion(call("update_requirements", {"intent": "unit_price"}, "candidate"),
        call("query_catalog", {"plan_id": "business"}, "rate")), completion()], review([inquiry]))
    response = asyncio.run(engine.chat(ChatRequest(message=LONG_PRICE, session_id=sid)))
    assert response.status == "completed", response.message
    assert runtime.state.current_task(recheck=False) == before
    assert response.business.unit_price.plan_id == "business"
    assert response.business.unit_price.unit_price_minor == 12000
    assert response.business.proposal is None and runtime.proposal is None
    assert asyncio.run(engine.submit_quote(sid, old_proposal.action_ref)).status == "not_saved"
    assert engine.quotes.list_quotes() == []
    same_history(engine, response, LONG_PRICE)


def test_task_owned_question_still_requires_actual_matching(engine, monkeypatch):
    inquiry, groups, target = selection_review(AUDIT)
    script(monkeypatch, [completion(call("update_requirements", {"intent": "selection"}, "candidate")), completion()],
        review([inquiry], intent="selection", target=target, groups=groups))
    response = asyncio.run(engine.chat(ChatRequest(message=AUDIT)))
    assert response.status != "completed"
    assert any(error["code"] == "matching_not_completed" for error in response.business.errors)
    assert response.business.match is None and response.business.proposal is None


@pytest.mark.parametrize("message,feature,requirement_quote,consultation_quote", [
    (AUDIT, "audit_logs", "必须支持审计日志", "哪个套餐能满足"),
    (PRIVATE, "private_deployment", "我们必须把澄星协作私有化部署在自己的服务器上。",
        "Basic、Team、Business里哪个套餐能满足？"),
])
def test_selection_chat_completes_with_real_matching(engine, monkeypatch, message, feature, requirement_quote, consultation_quote):
    inquiry, groups, target = selection_review(message, feature=feature,
        requirement_quote=requirement_quote, consultation_quote=consultation_quote)
    provider = script(monkeypatch, [completion(call("update_requirements", {"intent": "selection"}, "candidate"),
        call("match_requirements", {}, "match")), completion()],
        review([inquiry], intent="selection", target=target, groups=groups))
    response = asyncio.run(engine.chat(ChatRequest(message=message)))
    assert response.status == "completed", response.message
    assert response.business.match is not None and not response.business.errors
    assert "尚未完整核定" not in response.message
    assert response.business.pricing is None and response.business.proposal is None
    assert engine.quotes.list_quotes() == [] and len(provider.reviews) == 1
    same_history(engine, response, message)


def test_catalog_unsupported_task_finishes_without_reopening_review(engine, monkeypatch):
    inquiry, groups, target = selection_review(PRIVATE, feature="private_deployment",
        requirement_quote="我们必须把澄星协作私有化部署在自己的服务器上。",
        consultation_quote="Basic、Team、Business里哪个套餐能满足？")
    provider = script(monkeypatch, [completion(call("update_requirements", {"intent": "selection"}, "candidate"),
        call("match_requirements", {}, "actual-match")),
        completion(call("update_requirements", {"intent": "selection"}, "unnecessary-update"))],
        review([inquiry], intent="selection", target=target, groups=groups))
    response = asyncio.run(engine.chat(ChatRequest(message=PRIVATE)))
    assert response.status == "completed", response.message
    assert response.business.match.blocking_reasons == ("catalog_unsupported",)
    assert len(provider.sent) == len(provider.reviews) == 1
    assert [record["name"] for record in response.tool_results] == ["update_requirements", "match_requirements"]
    assert response.business.proposal is None and engine.quotes.list_quotes() == []


def test_unsupported_match_does_not_end_before_independent_inquiry(engine, monkeypatch):
    message = PRIVATE + "另外Team支持API吗？"
    inquiry, groups, target = selection_review(message, feature="private_deployment",
        requirement_quote="我们必须把澄星协作私有化部署在自己的服务器上。",
        consultation_quote="Basic、Team、Business里哪个套餐能满足？")
    independent = item("api", "Team支持API吗", ("team",), aspect="support", feature="api_access")
    provider = script(monkeypatch, [completion(call("update_requirements", {"intent": "selection"}, "candidate"),
        call("match_requirements", {}, "actual-match")), completion(), completion()],
        review([inquiry, independent], intent="selection", target=target, groups=groups))
    response = asyncio.run(engine.chat(ChatRequest(message=message)))
    assert len(provider.sent) > 1
    assert response.status != "completed" and response.business.match is not None
    assert response.business.consultation_coverage.status == "incomplete"


def test_unknown_hard_detail_does_not_use_the_catalog_only_exit(engine, monkeypatch):
    message = PRIVATE + "还必须支持CSV格式。"
    inquiry, groups, target = selection_review(message, feature="private_deployment",
        requirement_quote="我们必须把澄星协作私有化部署在自己的服务器上。",
        consultation_quote="Basic、Team、Business里哪个套餐能满足？")
    groups.append({"group_id": "detail", "verdict": "accept", "operations": [{"action": "add",
        "span": {"quote": "还必须支持CSV格式"}, "speech_act": "instruction", "requirement": {
            "statement": "CSV格式", "user_quote": "还必须支持CSV格式", "kind": "unmodeled"}}]})
    provider = script(monkeypatch, [completion(call("update_requirements", {"intent": "selection"}, "candidate"),
        call("match_requirements", {}, "actual-match")), completion()],
        review([inquiry], intent="selection", target=target, groups=groups))
    response = asyncio.run(engine.chat(ChatRequest(message=message)))
    assert len(provider.sent) == 2
    assert any(requirement.kind == "unmodeled" for requirement in response.business.requirements.items)
    assert set(response.business.match.blocking_reasons) != {"catalog_unsupported"}
    assert response.business.proposal is None and engine.quotes.list_quotes() == []
