"""Targeted follow-up questions with the real domain, controlled provider.

All SQLite records belong to pytest temporary directories. These scenarios
check formal feedback and receipt identities, not language-model accuracy.
"""

import asyncio
import json
import re

import pytest

from app.schemas.chat import BusinessResponse, CurrentTask, ChatRequest
from app.schemas.matching import RequirementSet
from app.schemas.pricing import format_cny_minor
from app.services.catalog import get_catalog
from app.services.chat_render import organize_business
from app.services.matching import match_requirements
from app.services.pricing import calculate_subscription_price, get_unit_price
from test_input_orchestrator import engine, script, call, completion, group, review_dispatches


def api(quote="需要API"):
    return {"statement": "产品API", "feature_id": "api_access", "user_quote": quote}


def assert_same_passage(text, *facts):
    """Scenario relationships, without a global paragraph or length budget."""
    passages = text.split("\n\n")
    assert any(all(fact in passage for fact in facts) for passage in passages), (facts, passages)


def assert_result_passage(text, price, *, review=False):
    """Keep the actual chosen object and its price understandable together."""
    passages = text.split("\n\n")
    result = next((passage for passage in passages if price.plan_name in passage
        and re.search(rf"(?<!\d){price.paid_seats}\s*(?:个付费席位|席)", passage)
        and re.search(rf"(?<!\d){price.months}\s*(?:个月|月)", passage)
        and format_cny_minor(price.base_price_minor) in passage), None)
    assert result is not None, passages
    if review:
        assert "尚未保存" in result and "核对" in result
        # The concrete review already owns the full scope and permission
        # controls; ordinary successful prose must not replay those notices.
        assert "下一条聊天或刷新" not in text
        assert "内部草案核对摘要" not in text
        assert not any(p.startswith("本项目不计算税") for p in passages)
    return result


def reviewed_turn(engine, monkeypatch, message, patch, groups):
    review = {"intent": "selection", "target": {"kind": "selection", "relation": "new", "span": {"quote": message}},
        "coverage_complete": True, "consultations": [], "consultation_coverage_complete": True, "groups": groups}
    provider = script(monkeypatch, [completion(call("update_requirements", patch)),
        completion(call("adjudicate_turn", review)), completion(call("match_requirements", {})), completion()])
    response = asyncio.run(engine.chat(ChatRequest(message=message)))
    assert len(review_dispatches(provider)) == 1
    assert engine.conversations.get_messages(response.session_id)[-1].content == response.message
    return response


@pytest.mark.parametrize("wrong_population,evaluation", [(False, False), (True, False), (False, True)])
def test_v20_only_asks_actual_seats_after_accepting_api_and_year_without_requesting_price(engine, monkeypatch, wrong_population, evaluation):
    message = "公司有50个人，订阅一年，需要API。"
    patch = {"intent": "selection", "add": [api()], "parameters": [{"name": "months", "value": 12, "user_quote": "订阅一年"}]}
    groups = [group("term", "set", "订阅一年", field="months", value=12), group("api", "add", "需要API", requirement=api())]
    if wrong_population:
        patch["parameters"].append({"name": "paid_seats", "value": 50, "user_quote": "公司有50个人"})
        groups.insert(0, group("population", "set", "公司有50个人", field="paid_seats", value=50, speech_act="background"))
    else:
        groups.insert(0, group("population-note", "note", "公司有50个人", speech_act="background"))
    if evaluation:
        groups.append(group("evaluation", "request_evaluation", "需要API"))
    response = reviewed_turn(engine, monkeypatch, message, patch, groups)
    assert response.status == "needs_input"
    assert {p.name: p.value for p in response.business.requirements.parameters} == {"months": 12}
    assert [item.feature_id for item in response.business.requirements.items] == ["api_access"]
    assert response.business.pending == ["paid_seats"]
    assert response.business.current_task.pending == ["paid_seats"]
    assert len(response.business.questions) == 1
    question = response.business.questions[0]
    assert "付费席位" in question and "几个月" not in question and "功能" not in question
    assert "此前核定" not in question
    assert response.message.count(question) == 1
    assert question in response.business.presentation.answer_markdown
    assert response.business.pricing is None and response.business.proposal is None
    assert engine.work.get(response.session_id).state.wants_price is False


def test_partial_confirmed_seats_only_asks_months_and_does_not_enable_calculation(engine, monkeypatch):
    message = "需要API，实际购买50席。"
    patch = {"intent": "selection", "add": [api()], "parameters": [{"name": "paid_seats", "value": 50, "user_quote": "实际购买50席"}]}
    response = reviewed_turn(engine, monkeypatch, message, patch, [group("api", "add", "需要API", requirement=api()),
        group("seats", "set", "实际购买50席", field="paid_seats", value=50)])
    assert response.status == "needs_input" and response.business.pending == ["months"]
    assert len(response.business.questions) == 1 and "几个月" in response.business.questions[0]
    assert response.message.count(response.business.questions[0]) == 1
    assert response.business.pricing is None and response.business.proposal is None
    assert not engine.work.get(response.session_id).state.wants_price


def test_numeric_pending_survives_source_notes_and_can_receive_a_bare_answer_without_price_authority(engine, monkeypatch):
    first = reviewed_turn(engine, monkeypatch, "订阅一年，需要API。", {"intent": "selection", "add": [api()],
        "parameters": [{"name": "months", "value": 12, "user_quote": "订阅一年"}]}, [
        group("term", "set", "订阅一年", field="months", value=12), group("api", "add", "需要API", requirement=api())])
    sid, identity = first.session_id, first.business.current_task
    review = {"intent": "selection", "target": {"kind": "selection", "relation": "continue", "task_id": identity.requirements.requirement_id},
        "coverage_complete": True, "consultations": [], "consultation_coverage_complete": True,
        "groups": [group("note", "note", "补充一个背景备注", speech_act="background")]}
    script(monkeypatch, [completion(call("update_requirements", {"intent": "selection", "context_quotes": ["补充一个背景备注"]})),
        completion(call("adjudicate_turn", review))])
    note = asyncio.run(engine.chat(ChatRequest(message="补充一个背景备注", session_id=sid)))
    assert note.business.pending == ["paid_seats"] and len(note.business.questions) == 1
    assert note.business.current_task.result_turn_id == identity.result_turn_id
    assert note.business.current_task.result_message_id == identity.result_message_id
    assert note.business.current_task.requirements == identity.requirements
    assert note.business.proposal is None
    review["groups"] = [group("seat", "set", "50", field="paid_seats", value=50)]
    script(monkeypatch, [completion(call("update_requirements", {"intent": "selection", "parameters": [{"name": "paid_seats", "value": 50, "user_quote": "50"}]})),
        completion(call("adjudicate_turn", review)), completion(call("match_requirements", {})), completion()])
    answered = asyncio.run(engine.chat(ChatRequest(message="50", session_id=sid)))
    assert {p.name: p.value for p in answered.business.requirements.parameters} == {"paid_seats": 50, "months": 12}
    assert answered.business.pending == [] and answered.business.questions == []
    assert answered.business.pricing is None and answered.business.proposal is None
    assert not engine.work.get(sid).state.wants_price


@pytest.mark.parametrize("action,quote", [("cancel_pricing", "金额先放一边"), ("cancel_pricing", "只核对功能")])
def test_explicit_function_scope_does_not_collect_unused_partial_purchase_inputs(engine, monkeypatch, action, quote):
    message = f"需要API，订阅一年，{quote}。"
    patch = {"intent": "selection", "add": [api()], "parameters": [{"name": "months", "value": 12, "user_quote": "订阅一年"}]}
    if action == "cancel_pricing":
        patch["cancel_pricing_quote"] = quote
    response = reviewed_turn(engine, monkeypatch, message, patch, [group("api", "add", "需要API", requirement=api()),
        group("term", "set", "订阅一年", field="months", value=12), group("scope", action, quote)])
    assert response.business.questions == [] and response.business.pending == []
    assert response.business.match.status == "matched"
    assert response.business.pricing is None and response.business.proposal is None
    assert not engine.work.get(response.session_id).state.wants_price


def test_unknown_hard_detail_does_not_ask_quantities_that_cannot_complete_the_fit(engine, monkeypatch):
    message = "需要API，必须每秒支持十万次调用，订阅一年。"
    detail = {"statement": "每秒支持十万次调用", "kind": "unmodeled", "user_quote": "必须每秒支持十万次调用"}
    patch = {"intent": "selection", "add": [api(), detail], "parameters": [{"name": "months", "value": 12, "user_quote": "订阅一年"}]}
    response = reviewed_turn(engine, monkeypatch, message, patch, [group("api", "add", "需要API", requirement=api()),
        group("detail", "add", detail["user_quote"], requirement=detail), group("term", "set", "订阅一年", field="months", value=12)])
    assert response.business.match.status == "blocked"
    assert detail["statement"] in response.business.presentation.answer_markdown
    assert "现有资料无法确认" in response.business.presentation.answer_markdown
    assert "尚不能确认" in response.message or "无法确认" in response.message
    assert "paid_seats" not in response.business.pending
    assert not any("付费席位" in question for question in response.business.questions)
    assert response.business.pricing is None and response.business.proposal is None


def test_rejected_empty_seats_have_one_question_without_claiming_a_retained_previous_value(engine, monkeypatch):
    message = "需要API，预计购买50席，订阅6个月，请算金额。"
    patch = {"intent": "selection", "pricing_quote": "请算金额", "add": [api()], "parameters": [
        {"name": "paid_seats", "value": 50, "user_quote": "预计购买50席"}, {"name": "months", "value": 6, "user_quote": "订阅6个月"}]}
    rejected = group("seats", "set", "预计购买50席", field="paid_seats", value=50, certainty="planned")
    rejected.update(verdict="reject", reason="uncertain")
    response = reviewed_turn(engine, monkeypatch, message, patch, [group("api", "add", "需要API", requirement=api()), rejected,
        group("term", "set", "订阅6个月", field="months", value=6), group("price", "request_pricing", "请算金额")])
    assert response.business.pending == ["paid_seats"]
    assert len(response.business.questions) == 1
    assert "此前核定" not in response.business.questions[0]
    assert "此前核定值仍保留" not in response.message
    assert response.message.count(response.business.questions[0]) == 1
    assert "付费席位" in response.business.presentation.answer_markdown
    assert "未更新" in response.business.presentation.answer_markdown
    assert "预计购买50席" in next(item["message"] for item in response.business.presentation.feedback
        if item["group_id"] == "seats")
    assert {item["group_id"] for item in response.business.presentation.feedback} == {"api", "seats", "term", "price"}
    assert {item["status"] for item in response.business.presentation.feedback} == {"accepted", "rejected"}


def assert_formal_storage_bridge(engine, response):
    """The independent history text starts with the same server-owned answer."""
    assert response.message.startswith(response.business.presentation.answer_markdown)
    rows = engine.conversations.get_messages(response.session_id)
    assert rows[-1].id == response.message_id
    assert rows[-1].content == response.message
    return response.message


def controlled_selection(engine, monkeypatch, *, message, patch, groups, session_id=None, price=None, finish_price=True):
    target = {"kind": "selection", "relation": "new", "span": {"quote": message}}
    if session_id:
        target = {"kind": "selection", "relation": "continue",
            "task_id": engine.work.get(session_id).state.requirements.requirement_id}
    review = {"intent": "selection", "target": target, "coverage_complete": True,
        "consultations": [], "consultation_coverage_complete": True, "groups": groups}
    outputs = [completion(call("update_requirements", patch)), completion(call("adjudicate_turn", review)),
        completion(call("match_requirements", {}))]
    if price:
        outputs.append(completion(call("calculate_price", price)))
        if finish_price:
            outputs.append(completion())
    provider = script(monkeypatch, outputs)
    response = asyncio.run(engine.chat(ChatRequest(message=message, session_id=session_id)))
    assert len(review_dispatches(provider)) == 1
    assert provider.remaining == []
    assert_formal_storage_bridge(engine, response)
    # The structured feedback retains every operation and original quotation.
    assert [item["group_id"] for item in response.business.presentation.feedback] == [item["group_id"] for item in groups]
    for item, projected in zip(response.business.operation_feedback, response.business.presentation.feedback):
        assert projected["status"] == item["status"]
        for span in item.get("spans", []):
            assert span["quote"] in projected["message"]
        if item["status"] == "accepted":
            assert projected["message"] not in response.message
    return response, provider


def test_problem_screenshot_followup_change_and_detour_keep_formal_history_and_save_identities(engine, monkeypatch):
    """Real domain arithmetic and stores; controlled extraction, never live LLM."""
    first_message = "公司大约80人，需要API，订一年，帮我算价。"
    first, _ = controlled_selection(engine, monkeypatch, message=first_message,
        patch={"intent": "selection", "add": [api()], "pricing_quote": "帮我算价",
            "parameters": [{"name": "months", "value": 12, "user_quote": "订一年"}]},
        groups=[group("population", "note", "公司大约80人", speech_act="background"),
            group("api", "add", "需要API", requirement=api()),
            group("term", "set", "订一年", field="months", value=12),
            group("price", "request_pricing", "帮我算价")])
    sid = first.session_id
    first_body = first.business.presentation.answer_markdown
    first_paragraph = first_body.split("\n\n", 1)[0]
    assert "Team" in first_paragraph and "最低档" in first_paragraph
    assert "API" in first_paragraph and "Basic" in first_paragraph and "不支持" in first_paragraph
    assert "已加入需求：" not in first_body
    assert "付费席位" in first_body and "暂不能计算订阅总价" in first_body
    assert "API" in first_body and "12" in first_body
    assert {p.name: p.value for p in first.business.requirements.parameters} == {"months": 12}
    assert first.business.pending == ["paid_seats"]
    assert first.business.pricing is None and first.business.proposal is None
    assert first.business.current_task.result_turn_id == first.business.turn_id
    question = first.business.questions[0]
    assert first.business.pending == ["paid_seats"] and len(first.business.questions) == 1
    assert first_body.count(question) == 1 and first.message.count(question) == 1
    assert first_body.index(first_paragraph) < first_body.index(question)
    assert_same_passage(first_body, "12", "个月", "公司", "80", "席位")
    assert any(word in first_body for word in ("未作为", "没有作为", "不能作为", "未采用", "不自动"))
    assert engine.quotes.list_quotes() == []

    completed, completed_provider = controlled_selection(engine, monkeypatch, message="80", session_id=sid,
        patch={"intent": "selection", "parameters": [{"name": "paid_seats", "value": 80, "user_quote": "80"}]},
        groups=[group("seats", "set", "80", field="paid_seats", value=80)],
        price={"plan_id": "team", "paid_seats": 80, "months": 12})
    expected80 = calculate_subscription_price("team", 80, 12)
    assert completed.business.pricing == expected80 and expected80.base_price_minor == 7_680_000
    completed_body = completed.business.presentation.answer_markdown
    assert_result_passage(completed_body, expected80, review=True)
    assert completed_body.count(format_cny_minor(expected80.base_price_minor)) == 1
    assert expected80.plan_name in completed_body
    assert expected80.formula not in completed_body
    assert completed.message.count(expected80.formula) == 1
    assert first.business.presentation.show_comparison
    assert not completed.business.presentation.show_comparison
    assert "需求比较" not in completed.message
    assert "已接受需求：产品 API" in completed.message
    assert not completed.business.presentation.show_current_result
    assert "尚未保存" in completed_body and "确认并保存草案" in completed_body
    assert completed.business.proposal.pricing == expected80
    assert completed.business.proposal.requirements == completed.business.requirements
    assert completed.business.proposal.action_ref not in completed.message
    assert completed.business.current_task.result_turn_id == completed.business.turn_id
    assert completed.business.current_task.result_message_id == completed.message_id
    assert first.message in [item["content"] for item in completed_provider.captured[0]["messages"] if item["role"] == "assistant"]

    modified, modified_provider = controlled_selection(engine, monkeypatch,
        message="付费席位改为50席，其余不变。", session_id=sid,
        patch={"intent": "selection", "parameters": [{"name": "paid_seats", "value": 50, "user_quote": "付费席位改为50席"}]},
        groups=[group("seats", "set", "付费席位改为50席", field="paid_seats", value=50)],
        price={"plan_id": "team", "paid_seats": 50, "months": 12})
    expected50 = calculate_subscription_price("team", 50, 12)
    assert modified.business.pricing == expected50 and expected50.base_price_minor == 4_800_000
    modified_body = modified.business.presentation.answer_markdown
    changed_result = assert_result_passage(modified_body, expected50, review=True)
    assert "改为" in changed_result and re.search(r"80\s*席", changed_result)
    assert "本次采用：" not in modified_body
    assert "80" in modified_body and "50" in modified_body and "改为" in modified_body
    assert expected50.formula not in modified_body
    assert modified.message.count(expected50.formula) == 1
    assert format_cny_minor(expected80.base_price_minor) not in modified_body
    assert "API" not in modified_body and "已接受需求：产品 API" in modified.message
    assert not modified.business.presentation.show_comparison
    assert "需求比较" not in modified.message
    assert not modified.business.presentation.show_current_result
    assert {p.name: p.value for p in modified.business.requirements.parameters} == {"paid_seats": 50, "months": 12}
    assert [item.feature_id for item in modified.business.requirements.items] == ["api_access"]
    assert modified.business.proposal.action_ref != completed.business.proposal.action_ref
    assert modified.business.proposal.pricing == expected50
    assert modified.business.current_task.result_turn_id != completed.business.current_task.result_turn_id
    assert (asyncio.run(engine.resolve_quote_action(sid, completed.business.proposal.action_ref))).status == "not_saved"
    assert completed.message in [item["content"] for item in modified_provider.captured[0]["messages"] if item["role"] == "assistant"]

    review = {"intent": "unit_price", "coverage_complete": True,
        "target": {"kind": "none", "relation": "detour"}, "groups": [],
        "consultation_coverage_complete": True, "consultations": [{
            "item_id": "business-unit", "aspect": "unit_price", "plan_ids": ["business"],
            "topic": "Business 单价多少？", "span": {"quote": "Business 单价多少？"}}]}
    provider = script(monkeypatch, [completion(call("update_requirements", {"intent": "unit_price"})),
        completion(call("adjudicate_turn", review)), completion(call("query_catalog", {"plan_id": "business"})), completion()])
    detour = asyncio.run(engine.chat(ChatRequest(message="Business 单价多少？", session_id=sid)))
    assert len(review_dispatches(provider)) == 1 and provider.remaining == []
    expected_unit = get_unit_price("business")
    assert detour.business.unit_price == expected_unit and expected_unit.unit_price_minor == 12_000
    detour_body = detour.business.presentation.answer_markdown
    assert expected_unit.plan_name in detour_body.split("\n\n", 1)[0]
    assert format_cny_minor(expected_unit.unit_price_minor) + " 元／席／月" in detour_body.split("\n\n", 1)[0]
    assert expected50.formula not in detour_body and "需求比较" not in detour_body
    assert "API" not in detour_body and "12 个月" not in detour_body
    assert "原选型保持不变" not in detour_body
    assert detour.business.questions == [] and "请补充" not in detour_body and "核对" not in detour_body
    assert "尚未保存" not in detour_body and "内部草案" not in detour_body
    assert not detour.business.presentation.show_current_result
    assert detour.business.current_task.requirements == modified.business.current_task.requirements
    assert detour.business.current_task.pricing == expected50
    assert detour.business.current_task.result_turn_id == modified.business.current_task.result_turn_id
    assert detour.business.current_task.result_message_id == modified.message_id
    assert detour.business.proposal is None
    assert asyncio.run(engine.resolve_quote_action(sid, modified.business.proposal.action_ref)).status == "not_saved"
    assert engine.quotes.list_quotes() == []
    assert_formal_storage_bridge(engine, detour)
    historical = [item["content"] for item in provider.captured[0]["messages"] if item["role"] == "assistant"]
    assert historical == [first.message, completed.message, modified.message]
    assert engine.conversations.get_messages(sid)[3].content == completed.message
    assert format_cny_minor(expected80.base_price_minor) in completed.message


def test_scope_and_explicit_plan_changes_keep_the_new_comparison_visible_with_save_object(engine, monkeypatch):
    collaboration = {"statement": "任务与项目协作", "feature_id": "project_collaboration", "user_quote": "需要项目协作"}
    initial, _ = controlled_selection(engine, monkeypatch,
        message="需要项目协作，实际购买80席，订阅12个月，算基价。",
        patch={"intent": "selection", "add": [collaboration], "pricing_quote": "算基价", "parameters": [
            {"name": "paid_seats", "value": 80, "user_quote": "实际购买80席"},
            {"name": "months", "value": 12, "user_quote": "订阅12个月"}]},
        groups=[group("collaboration", "add", "需要项目协作", requirement=collaboration),
            group("seats", "set", "实际购买80席", field="paid_seats", value=80),
            group("term", "set", "订阅12个月", field="months", value=12),
            group("pricing", "request_pricing", "算基价")],
        price={"plan_id": "basic", "paid_seats": 80, "months": 12})
    added, _ = controlled_selection(engine, monkeypatch,
        message="还需要API。", session_id=initial.session_id,
        patch={"intent": "selection", "add": [api()]},
        groups=[group("api", "add", "需要API", requirement=api())],
        price={"plan_id": "team", "paid_seats": 80, "months": 12})
    chosen, _ = controlled_selection(engine, monkeypatch,
        message="当前方案选择Business。", session_id=initial.session_id,
        patch={"intent": "selection", "selected_plan_id": "business", "selection_quote": "选择Business"},
        groups=[group("plan", "select_plan", "选择Business", value="business")],
        price={"plan_id": "business", "paid_seats": 80, "months": 12})
    for response in (initial, added, chosen):
        assert response.business.comparison_changed
        assert response.business.presentation.show_comparison
        assert response.business.proposal is not None
        assert not response.business.presentation.show_current_result
        assert response.message.count("需求比较：") == 1
        assert response.message.count(response.business.pricing.formula) == 1
        assert response.business.proposal.requirements == response.business.match.requirements
    assert initial.business.match.lowest_plan_id == "basic"
    assert added.business.match.lowest_plan_id == "team"
    assert "已加入需求：产品 API" in added.business.presentation.answer_markdown
    assert_same_passage(added.business.presentation.answer_markdown, "API", "Team", "Basic", "不支持")
    assert chosen.business.match.lowest_plan_id == "team"
    assert chosen.business.match.selected_plan_id == "business"
    assert chosen.business.match.selection_reason == "user_selected"
    assert "Business" in chosen.business.presentation.answer_markdown
    assert "Team" in chosen.business.presentation.answer_markdown
    prepared_review = {"intent": "selection", "target": {"kind": "selection", "relation": "continue",
        "task_id": chosen.business.requirements.requirement_id}, "coverage_complete": True,
        "consultations": [], "consultation_coverage_complete": True,
        "groups": [group("prepare", "request_proposal", "重新准备保存核对")]}
    provider = script(monkeypatch, [completion(call("update_requirements", {"intent": "selection"})),
        completion(call("adjudicate_turn", prepared_review)), completion()])
    prepared = asyncio.run(engine.chat(ChatRequest(message="重新准备保存核对。", session_id=initial.session_id)))
    assert provider.remaining == [] and len(review_dispatches(provider)) == 1
    assert prepared.business.proposal and prepared.business.proposal.action_ref != chosen.business.proposal.action_ref
    assert prepared.business.current_task.result_turn_id == chosen.business.current_task.result_turn_id
    assert not prepared.business.presentation.show_comparison and not prepared.business.presentation.show_current_result
    assert "需求比较" not in prepared.message
    assert_result_passage(prepared.business.presentation.answer_markdown, prepared.business.proposal.pricing, review=True)
    assert prepared.business.presentation.answer_markdown.count(format_cny_minor(prepared.business.proposal.pricing.base_price_minor)) == 1
    assert "已接受需求：任务与项目协作、产品 API" in prepared.message
    assert "最低满足档为 团队版 Team" in prepared.message and "当前套餐由用户选择" in prepared.message
    assert prepared.message.count(prepared.business.proposal.pricing.formula) == 1
    assert_formal_storage_bridge(engine, prepared)
    assert engine.quotes.list_quotes() == []


@pytest.mark.parametrize("assertion,quote,reason,state_label", [
    ("no", "目前不具备兼容身份提供方", "当前不具备使用前提", "不支持"),
    ("unknown", "目前不确定是否具备兼容身份提供方", "当前使用前提待确认", "前提待确认"),
])
def test_changed_sso_prerequisite_expands_actual_comparison_and_retains_all_blockers(engine, monkeypatch,
        assertion, quote, reason, state_label):
    from test_proposals import worker
    sid = engine.conversations.ensure_conversation(None, "合成SSO前提比较")
    state = engine.work.get(sid).state
    state.apply_patch({"intent": "selection", "add": [{"feature_id": "saml_sso", "statement": "SAML SSO",
        "user_quote": "需要SSO"}]}, "需要SSO")
    worker(monkeypatch, state)
    state.remember_result(state.match(), None, ("condition:saml_sso_compatible_idp",),
        ("目前具备兼容身份提供方吗？",), turn_id="synthetic-old-sso-turn")
    review = {"intent": "selection", "target": {"kind": "selection", "relation": "continue",
        "task_id": state.requirements.requirement_id}, "coverage_complete": True,
        "consultations": [], "consultation_coverage_complete": True,
        "groups": [group("condition", "set", quote,
            field="saml_sso_compatible_idp", value=assertion)]}
    outputs = [completion(call("update_requirements", {"intent": "selection",
        "conditions": [{"condition_id": "saml_sso_compatible_idp", "value": assertion,
            "user_quote": quote}]})),
        completion(call("adjudicate_turn", review)), completion(call("match_requirements", {}))]
    if assertion == "no":
        outputs.append(completion())
    provider = script(monkeypatch, outputs)
    response = asyncio.run(engine.chat(ChatRequest(message=quote + "。", session_id=sid)))
    assert provider.remaining == [] and len(review_dispatches(provider)) == 1
    assert response.business.comparison_changed and response.business.presentation.show_comparison
    assert response.business.match.status == "blocked" and response.business.match.selected_plan_id is None
    assert response.business.proposal is None and response.business.pricing is None
    assert reason in response.business.presentation.answer_markdown
    assert "助手未验证实际身份系统" in response.business.presentation.answer_markdown
    assert "不支持" in response.message and state_label in response.message
    assert_formal_storage_bridge(engine, response)


@pytest.mark.parametrize("stage", ["model", "semantic_review"])
def test_system_failure_keeps_old_result_identity_and_revokes_save_without_success_prose(engine, monkeypatch, stage):
    from test_proposals import seed
    sid, runtime, old_proposal = asyncio.run(seed(engine))
    before = runtime.state.current_task(recheck=False)
    outputs = [RuntimeError("PRIVATE_SYSTEM_FAILURE_SENTINEL")]
    if stage == "semantic_review":
        outputs.insert(0, completion(call("update_requirements", {"intent": "selection"})))
    provider = script(monkeypatch, outputs)
    response = asyncio.run(engine.chat(ChatRequest(message="付费席位改为50席，其余不变", session_id=sid)))
    assert response.status == "incomplete"
    assert len(review_dispatches(provider)) == (1 if stage == "semantic_review" else 0)
    body = response.business.presentation.answer_markdown
    assert "未完成" in body
    assert "重试" in body if stage == "model" else "重新发送" in body
    assert "保留" in body and "PRIVATE_SYSTEM_FAILURE_SENTINEL" not in response.message
    assert before["pricing"]["plan_name"] in body and "80 席" in body and "12 个月" in body
    assert format_cny_minor(before["pricing"]["base_price_minor"]) in body
    assert "此前保留的结果" in body and "不是本轮新计算的结果" in body
    assert "尚未保存" not in body and "方案已准备" not in body
    assert response.business.pricing is None and response.business.proposal is None
    assert response.business.operation_feedback == []
    assert not response.business.presentation.show_current_result
    after = runtime.state.current_task(recheck=False)
    for field in ("requirements", "pricing", "result_turn_id", "result_message_id"):
        assert after[field] == before[field]
    assert runtime.proposal is None
    assert asyncio.run(engine.resolve_quote_action(sid, old_proposal.action_ref)).status == "not_saved"
    assert engine.quotes.list_quotes() == []
    assert_formal_storage_bridge(engine, response)


@pytest.mark.parametrize("code", ["semantic_review_failed", "semantic_review_timeout", "extraction_failed"])
def test_unreceived_fact_failure_gives_resend_step_without_concealing_retained_result(code):
    from test_input_feedback import selection
    requirements = selection()
    price = calculate_subscription_price("team", 80, 12)
    task = CurrentTask(status="current", last_result="not_updated", requirements=requirements,
        match=match_requirements(requirements), pricing=price, result_turn_id="old-failed-result")
    value = BusinessResponse(turn_id="new-failed-turn", intent="selection", requirements=requirements,
        current_task=task, errors=[{"code": code, "message": "controlled failure"}])
    presentation, formal = organize_business(value, "incomplete")
    body = presentation.answer_markdown
    assert "重新发送尚未接收的信息或修改" in body
    if code == "semantic_review_timeout":
        assert "等待本次后台处理结束，再重新发送" in body
        assert "等待超时不表示后台工作已经结束" in body
    else:
        assert "此前有效事实无需重述" in body
    assert price.plan_name in body and "80 席" in body and "12 个月" in body
    assert format_cny_minor(price.base_price_minor) in body
    assert "不是本轮新计算的结果" in body and "方案已准备" not in body
    assert value.proposal is None and value.pricing is None
    assert task.result_turn_id == "old-failed-result" and not presentation.show_current_result
    assert formal.startswith(body)


def test_partial_update_exposes_retained_seats_adopted_term_and_blocked_new_price_and_save(engine, monkeypatch):
    from test_proposals import seed
    sid, runtime, old_proposal = asyncio.run(seed(engine))
    old_task = runtime.state.current_task(recheck=False)
    rejected = group("seats", "set", "付费席位改为约50席", field="paid_seats", value=50, certainty="approximate")
    rejected.update(verdict="reject", reason="approximate")
    response, _ = controlled_selection(engine, monkeypatch,
        message="付费席位改为约50席，订阅期限改为6个月，其余不变。", session_id=sid,
        patch={"intent": "selection", "parameters": [
            {"name": "paid_seats", "value": 50, "user_quote": "付费席位改为约50席"},
            {"name": "months", "value": 6, "user_quote": "订阅期限改为6个月"}]},
        groups=[rejected, group("term", "set", "订阅期限改为6个月", field="months", value=6)],
        price={"plan_id": "team", "paid_seats": 80, "months": 6}, finish_price=False)
    body = response.business.presentation.answer_markdown
    assert response.status == "incomplete" and response.business.pending == ["review:paid_seats"]
    assert "付费席位未更新" in body and "约数" in body
    assert "仍保留此前核定的 80 席" in body
    assert_same_passage(body, "付费席位未更新", "约数", "80 席")
    assert "订阅期限从 12 个月改为 6 个月" in body
    assert "80 席" in body and "6 个月" in body
    assert "本次采用：" not in body
    assert_same_passage(body, "6 个月", "还没有计算出新的订阅基价")
    assert "还没有计算出新的订阅基价" in body and response.business.pricing is None
    assert_same_passage(body, "付费席位的修改尚未确定", "还没有计算出新的订阅基价", "没有可提交", "保存对象")
    assert "席位和期限已明确" not in body
    assert "请补充" in body
    assert "76,800" not in body and "方案已准备" not in body
    assert "这次要采用的实际付费席位" in body
    assert len(response.business.questions) == 1
    assert body.count(response.business.questions[0]) == 1
    assert [item.feature_id for item in response.business.requirements.items] == ["api_access"]
    assert {p.name: p.value for p in response.business.requirements.parameters} == {"paid_seats": 80, "months": 6}
    assert response.business.proposal is None and runtime.proposal is None
    assert response.business.current_task.result_turn_id != old_task["result_turn_id"]
    assert response.business.current_task.pricing is None
    assert response.business.current_task.match.selected_plan_id == "team"
    assert any(error["code"] == "pricing_not_completed" for error in response.business.errors)
    assert response.tool_results[-1]["result"]["error"]["code"] == "invalid_business_input"
    assert response.tool_results[-1]["result"]["error"]["details"]["issue"] == "review_dependency"
    assert asyncio.run(engine.resolve_quote_action(sid, old_proposal.action_ref)).status == "not_saved"
    assert engine.quotes.list_quotes() == []


def test_current_result_question_is_once_in_the_formal_body_without_mutating_task_identity():
    requirements = RequirementSet.model_validate_json(json.dumps({"requirement_id": "synthetic-pending", "revision": 1,
        "items": [{"requirement_id": "api", "statement": "产品API", "user_quote": "需要API", "kind": "feature", "feature_id": "api_access"}],
        "parameters": [{"name": "months", "value": 12, "user_quote": "订阅一年"}]}))
    question = "实际购买多少个付费席位？"
    task = CurrentTask(status="current", last_result="updated", requirements=requirements,
        match=match_requirements(requirements), result_turn_id="synthetic-question-turn", result_message_id="original-row",
        pending=["paid_seats"], questions=[question])
    before = task.model_dump(mode="json")
    value = BusinessResponse(turn_id="synthetic-question-turn", intent="selection", requirements=requirements,
        catalog_context=get_catalog(), current_task=task, match=task.match, pending=["paid_seats"], questions=[question])
    presentation, formal = organize_business(value, "needs_input")
    assert presentation.show_current_result
    assert presentation.answer_markdown.count(question) == 1
    assert formal.count(question) == 1
    assert task.model_dump(mode="json") == before


def test_turn_question_is_in_formal_current_result_body_even_when_task_has_no_pending_question():
    requirements = RequirementSet.model_validate_json(json.dumps({"requirement_id": "synthetic-current", "revision": 1}))
    task = CurrentTask(status="current", last_result="updated", requirements=requirements,
        result_turn_id="synthetic-turn", result_message_id="old-message", pending=[], questions=[])
    before = task.model_dump(mode="json")
    question = "这次要采用哪个套餐，还是按最低满足档选型？"
    value = BusinessResponse(turn_id="synthetic-turn", intent="selection", requirements=requirements,
        current_task=task, catalog_context=get_catalog(), pending=["review:selected_plan_id"], questions=[question])
    presentation, formal = organize_business(value, "needs_input")
    assert presentation.show_current_result
    assert presentation.answer_markdown.count(question) == 1 and formal.count(question) == 1
    assert task.model_dump(mode="json") == before


def test_future_prerequisite_explanation_connects_rejection_to_retained_unknown_without_claiming_fit_or_save():
    requirements = RequirementSet.model_validate_json(json.dumps({"requirement_id": "synthetic-future-sso",
        "items": [{"requirement_id": "sso", "statement": "SAML SSO", "user_quote": "需要SSO",
            "feature_id": "saml_sso", "kind": "feature"}],
        "condition_assertions": [{"condition_id": "saml_sso_compatible_idp", "value": "unknown",
            "user_quote": "目前不确定是否具备兼容身份提供方"}]}))
    question = "目前是否具备兼容身份提供方？可以回答具备、不具备或不确定。"
    task = CurrentTask(status="current", last_result="not_updated", requirements=requirements,
        match=match_requirements(requirements), result_turn_id="old-pending-sso",
        pending=["condition:saml_sso_compatible_idp"], questions=[question])
    value = BusinessResponse(turn_id="future-sso-turn", intent="selection", requirements=requirements,
        current_task=task, catalog_context=get_catalog(), pending=task.pending, questions=[question],
        operation_feedback=[{"group_id": "future", "target": "selection", "field": "saml_sso_compatible_idp",
            "status": "rejected", "issue": "future", "previous_value": "unknown",
            "spans": [{"quote": "以后准备兼容身份提供方"}]}])
    before = value.model_dump(mode="json")
    presentation, formal = organize_business(value, "needs_input")
    body = presentation.answer_markdown
    assert_same_passage(body, "以后准备兼容身份提供方", "未来计划", "未更新", "不确定")
    assert body.count(question) == 1 and formal.count(question) == 1
    assert "方案已准备" not in body and "已通过" not in body and "完整满足" not in body
    assert "付费席位" not in body and "订阅期限" not in body
    assert value.model_dump(mode="json") == before
    assert value.proposal is None and value.pricing is None
    assert task.requirements.condition_assertions[0].value == "unknown"
    assert formal.startswith(body)
