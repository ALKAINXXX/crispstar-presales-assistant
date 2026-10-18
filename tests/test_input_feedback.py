"""Input-review feedback/serialization checks; no model or browser claims.

All records are synthetic and the SQLite file belongs to pytest's temporary
directory. These checks use real catalog matching, arithmetic and persistence.
"""
import json

import pytest

from app.config import Settings
from app.db.session import init_saas_db
from app.schemas.chat import BusinessResponse, ChatResponse, CurrentTask
from app.schemas.matching import RequirementSet
from app.services.catalog import get_catalog
from app.services.chat_render import organize_business
from app.services.conversation_store import ConversationStore
from app.services.matching import match_requirements
from app.services.pricing import calculate_subscription_price
from app.services.tool_display import tool_display_fields


def selection():
    return RequirementSet.model_validate_json(json.dumps({"requirement_id": "synthetic-selection", "revision": 1,
        "items": [{"requirement_id": "synthetic-api", "statement": "必须支持产品 API",
            "user_quote": "需要API", "kind": "feature", "feature_id": "api_access"}],
        "parameters": [{"name": "paid_seats", "value": 80, "user_quote": "80席"},
            {"name": "months", "value": 12, "user_quote": "一年"}]}))


def business(**changes):
    value = BusinessResponse(turn_id="synthetic-turn", intent="selection", requirements=selection(),
        catalog_context=get_catalog(), **changes)
    return value


def test_group_feedback_preserves_only_the_known_target_and_same_formal_text(tmp_path):
    requirements = selection()
    matched = match_requirements(requirements)
    price = calculate_subscription_price("team", 80, 12)
    current = CurrentTask(status="current", last_result="not_updated", requirements=requirements,
        match=matched, pricing=price, result_turn_id="synthetic-old-result")
    value = business(current_task=current, operation_feedback=[
        {"group_id": "seat", "status": "rejected", "target": "selection", "field": "paid_seats",
            "issue": "uncertain", "previous_value": 80, "message": "席位改为约数尚未确定"},
        {"group_id": "api", "status": "accepted", "target": "selection", "field": "requirements",
            "message": "API需求仍保留", "retained": True},
        {"group_id": "unknown-target", "status": "blocked", "target": "none", "field": "months",
            "issue": "target_unknown", "previous_value": 6},
    ], semantic_review={"status": "verified", "dispatched": 1, "elapsed_ms": 123,
        "usage": {"prompt_tokens": 50, "completion_tokens": 20}})
    presentation, formal = organize_business(value, "needs_input")
    value.presentation = presentation
    retained = next(passage for passage in formal.split("\n\n") if "76,800.00" in passage)
    assert all(fact in retained for fact in ("团队版 Team", "80 席", "12 个月", "此前保留的结果", "不是本轮新计算的结果"))
    assert formal.count("76,800.00") == 1
    assert "6 个月" not in formal
    assert not presentation.show_current_result
    assert "此前评估仍有效" in formal
    assert value.current_task.result_turn_id == "synthetic-old-result"
    assert value.current_task.pricing.base_price_minor == 7_680_000
    assert value.proposal is None
    # Successful receipt detail belongs to the existing expandable group view.
    # Rejected inputs still expose their reason and retained value in the body,
    # without requiring the raw receipt's wording to lead the business answer.
    assert {item["group_id"] for item in presentation.feedback} == {"seat", "api", "unknown-target"}
    assert "席位改为约数尚未确定" in presentation.feedback[0]["message"]
    assert "API需求仍保留" in presentation.feedback[1]["message"]
    assert presentation.feedback[1]["message"] not in formal
    assert "付费席位" in presentation.answer_markdown
    assert "尚未核定这项修改属于哪个任务" in formal
    assert "尚未核定这项修改属于哪个任务" in presentation.answer_markdown
    settings = Settings(_env_file=None, saas_database_url=f"sqlite:///{tmp_path / 'feedback.db'}")
    init_saas_db(settings)
    store = ConversationStore(settings)
    sid = store.ensure_conversation(None, "合成反馈验证")
    row_id = store.add_turn(sid, "付费席位改为约70席", formal)
    response = ChatResponse(message=formal, model="service-feedback-check", session_id=sid,
        business=value, status="needs_input", message_id=row_id)
    restored = ChatResponse.model_validate_json(response.model_dump_json())
    assert restored.message == store.get_messages(sid)[-1].content
    assert restored.business.presentation.feedback == presentation.feedback


def test_atomic_multifield_rejection_names_affected_spans_and_retains_only_actual_old_result():
    requirements = selection()
    price = calculate_subscription_price("team", 80, 12)
    task = CurrentTask(status="current", last_result="not_updated", requirements=requirements,
        match=match_requirements(requirements), pricing=price, result_turn_id="old-atomic-result",
        result_message_id="old-atomic-row")
    spans = [{"quote": "席位改为约50席"}, {"quote": "期限改为约6个月"}]
    raw = [
        {"group_id": "atomic-purchase", "status": "rejected", "target": "selection", "field": "candidate",
            "issue": "approximate", "message": "本组购买参数尚未确定，未提交整组修改", "spans": spans},
        {"group_id": "unowned", "status": "blocked", "target": "none", "field": "months",
            "issue": "target_unknown", "previous_value": 6},
    ]
    value = business(current_task=task, operation_feedback=raw)
    before_feedback = json.dumps(value.operation_feedback, ensure_ascii=False)
    before_task = task.model_dump(mode="json")
    presentation, formal = organize_business(value, "needs_input")
    body = presentation.answer_markdown
    assert all(span["quote"] in body for span in spans)
    assert "未更新" in body and "未提交整组修改" in body
    assert price.plan_name in body and "80 席" in body and "12 个月" in body
    assert "76,800.00 元" in body and "此前保留的结果" in body
    assert "不是本轮新计算的结果" in body
    assert "仍保留此前核定的 6 个月" not in body
    assert "仍保留此前核定的 6 个月" not in presentation.feedback[1]["message"]
    assert {item["group_id"] for item in presentation.feedback} == {"atomic-purchase", "unowned"}
    assert all(span["quote"] in presentation.feedback[0]["message"] for span in spans)
    assert json.dumps(value.operation_feedback, ensure_ascii=False) == before_feedback
    assert "previous_value" not in value.operation_feedback[0]
    assert task.model_dump(mode="json") == before_task
    assert not presentation.show_current_result and value.pricing is None and value.proposal is None
    assert formal.startswith(body)


@pytest.mark.parametrize("issue,term", [
    ("uncertain", "还不确定"), ("ambiguous", "多个可能含义"),
    ("invalid_type", "须为整数"), ("invalid_range", "不表示产品容量"),
    ("candidate_disagreement", "系统提取"), ("reference_missing", "系统未能核对"),
    ("detail_uncovered", "硬细节尚未完整核对"), ("conditional", "尚未成立的条件"),
])
def test_distinct_reasons_do_not_collapse_to_user_ambiguity(issue, term):
    value = business(operation_feedback=[{"group_id": "synthetic-group", "status": "rejected",
        "target": "none", "field": "candidate", "issue": issue}])
    presentation, formal = organize_business(value, "needs_input")
    assert term in formal
    assert term in presentation.answer_markdown
    assert term in presentation.feedback[0]["message"]
    assert presentation.feedback[0]["group_id"] == "synthetic-group"
    assert presentation.feedback[0]["status"] == "rejected"


@pytest.mark.parametrize("code,state,term", [
    ("quote_in_trash", "trash", "最近删除中恢复"),
    ("quote_permanently_deleted", "permanently_deleted", "正文无法读取或恢复"),
    ("quote_not_found", "not_found", "不能据此判断它从未保存"),
])
def test_deleted_quote_safe_projection_and_formal_answer(code, state, term):
    poison = "DELETED_SNAPSHOT_SECRET"
    result = {"ok": False, "error": {"code": code, "message": poison, "snapshot": {"body": poison}}}
    view = tool_display_fields("query_quote", json.dumps({"quote_id": "Q-" + "A" * 32}), result)
    shown = view["display_result"]["error"]
    assert shown["current_status"] == state and term in shown["message"]
    assert poison not in json.dumps(view)
    value = BusinessResponse(turn_id="synthetic-quote-turn", intent="quote_query",
        requirements=RequirementSet(requirement_id="synthetic-query"), errors=[shown])
    presentation, formal = organize_business(value, "incomplete")
    assert term in formal and poison not in formal
    assert presentation.answer_markdown == formal
    assert value.queried_quotes == [] and value.proposal is None


def test_reviewed_update_displays_service_outcome_without_revalidating_first_candidate():
    result = {"ok": True, "data": {"requirements": selection().model_dump(mode="json"),
        "intent": "selection", "operation_feedback": [{"group_id": "seat", "status": "rejected",
            "field": "paid_seats", "target": "selection", "issue": "uncertain", "previous_value": 80}],
        "semantic_review": {"status": "verified", "dispatched": 1}}}
    raw = '{"intent":"selection","parameters":[{"name":"paid_seats","value":50.0,"user_quote":"50"}]}'
    shown = tool_display_fields("update_requirements", raw, result)
    assert shown["display_arguments_status"] == "accepted"
    assert shown["display_arguments"] == {}
    assert shown["display_result"]["data"]["requirements"] == result["data"]["requirements"]
    assert shown["display_result"]["data"]["operation_feedback"][0]["status"] == "rejected"
    assert '50.0' not in json.dumps(shown)


def test_legacy_response_without_review_and_feedback_remains_read_only_data():
    old = {"message": "旧正式文字", "model": "legacy", "session_id": "old-session", "business": {
        "turn_id": "old-turn", "intent": "selection", "requirements": {"requirement_id": "old-requirements"},
        "presentation": {"answer_markdown": "旧正式文字"}}}
    value = ChatResponse.model_validate(old)
    assert value.message == "旧正式文字"
    assert value.business.semantic_review is None and value.business.operation_feedback == []
    assert value.business.presentation.feedback == [] and value.business.proposal is None


@pytest.mark.parametrize("requirement,reason,other_reason", [
    ({"requirement_id": "private", "statement": "私有化部署", "user_quote": "必须私有化部署",
        "kind": "feature", "feature_id": "private_deployment"}, "产品目录明确不支持", "现有资料无法确认"),
    ({"requirement_id": "capacity", "statement": "每秒十万次API调用", "user_quote": "必须每秒十万次API调用",
        "kind": "unmodeled"}, "现有资料无法确认", "产品目录明确不支持"),
])
def test_blocked_business_reason_and_next_step_are_visible_and_distinguish_unknown_from_unsupported(requirement, reason, other_reason):
    requirements = RequirementSet.model_validate_json(json.dumps({"requirement_id": "synthetic-blocked", "items": [requirement]}))
    matched = match_requirements(requirements)
    value = BusinessResponse(turn_id="synthetic-blocked-turn", intent="selection", requirements=requirements,
        catalog_context=get_catalog(), match=matched)
    presentation, formal = organize_business(value, "completed")
    assert matched.status == "blocked" and matched.selected_plan_id is None
    assert requirement["statement"] in presentation.answer_markdown
    if requirement["kind"] == "feature":
        # The body may say directly which requirement is unsupported; the
        # formal comparison still retains the catalog-owned reason wording.
        assert "不支持" + requirement["statement"] in presentation.answer_markdown
        expected_status, expected_code = "unsupported", "catalog_unsupported"
    else:
        assert reason in presentation.answer_markdown
        expected_status, expected_code = "unknown", "unmodeled_requirement"
        assert "不支持" + requirement["statement"] not in presentation.answer_markdown
    assert other_reason not in presentation.answer_markdown
    for plan in matched.plans:
        row = next(item for item in plan.items if item.requirement_id == requirement["requirement_id"])
        assert row.status == expected_status and expected_code in row.reason_codes
    assert "不能确认完整适配" in presentation.answer_markdown
    assert "请澄清" in presentation.answer_markdown or "请明确" in presentation.answer_markdown
    assert reason in formal and formal.startswith(presentation.answer_markdown)
    assert value.pricing is None and value.proposal is None
