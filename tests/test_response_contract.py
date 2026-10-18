"""Response/display contracts, using isolated state and model substitutes.

These tests prove compatibility and safe projections, not real model choices or
browser rendering. Normal browser validation must use the real application chain.
"""

import asyncio
from copy import deepcopy
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import TypeAdapter

from app.agents import orchestrator as orchestrator_module
from app.agents.orchestrator import Orchestrator
from app.config import Settings
from app.db.session import init_saas_db
from app.schemas.chat import ChatRequest, ChatResponse
from app.schemas.pricing import Months, PaidSeats
from app.services.catalog import get_catalog
from app.services.chat_render import render_business
from app.services.tool_display import tool_display_fields
from app.services.work_state import WorkState
from app.services.pricing import calculate_subscription_price
from app.services.knowledge import DEFAULT_INDEX_PATH, _read_index
from app.tools.business import BusinessToolRegistry
from controlled_review import answer_review, is_review_request, review_completion, review_from_candidate


def _tool(name, arguments, identity=None):
    raw = json.dumps(arguments, ensure_ascii=False)
    return SimpleNamespace(id=uuid4().hex if identity is None else identity, function=SimpleNamespace(name=name, arguments=raw))


def _completion(*calls, content="结束"):
    return SimpleNamespace(choices=[SimpleNamespace(
        message=SimpleNamespace(content=content, tool_calls=list(calls)), finish_reason="stop",
    )], model="controlled-response-model", usage=None)


def _scripted(monkeypatch, outputs, *, review_candidates=(), review_outputs=()):
    controlled_candidates, controlled_outputs = list(review_candidates), list(review_outputs)
    class ScriptedClient:
        captured = []
        remaining = list(outputs)
        review_calls = []
        review_candidates = controlled_candidates
        review_outputs = controlled_outputs

        def __init__(self, settings):
            pass

        async def chat(self, **kwargs):
            if is_review_request(kwargs):
                return answer_review(type(self), kwargs)
            type(self).captured.append(deepcopy(kwargs))
            assert type(self).remaining, "Unexpected extra model invocation"
            return type(self).remaining.pop(0)

    monkeypatch.setattr(orchestrator_module, "DeepSeekClient", ScriptedClient)
    return ScriptedClient


@pytest.fixture
def engine(tmp_path):
    settings = Settings(_env_file=None, deepseek_api_key="test", deepseek_default_max_completion_tokens=512,
        saas_database_url=f"sqlite:///{tmp_path / 'response-contract.db'}")
    init_saas_db(settings)
    return Orchestrator(settings)


def test_old_chat_response_remains_parseable_without_new_display_fields():
    old = {"message": "原有受控文本", "model": "legacy", "session_id": "session-1",
        "tool_results": [{"name": "query_catalog", "arguments": '{"plan_id":"team"}',
            "result": {"ok": True, "data": {"plan_id": "team"}}}],
        "business": {"turn_id": "turn-1", "intent": "unit_price",
            "requirements": {"requirement_id": "requirement-1"}}}
    restored = ChatResponse.model_validate(old)
    assert restored.business.catalog_context is None
    assert restored.tool_results == old["tool_results"]
    assert restored.message == old["message"]


def test_noncontiguous_model_quote_is_rejected_without_blame_for_clear_user_input(engine, monkeypatch):
    """Regression from a recorded real turn; model substitution only here."""
    user_text = "必须支持任务项目协作和数据导出，选择最低满足档。"
    rejected = _completion(_tool("update_requirements", {"intent": "selection", "add": [
        {"feature_id": "project_collaboration", "statement": "任务项目协作", "user_quote": "任务项目协作"},
        {"feature_id": "data_export", "statement": "数据导出", "user_quote": "必须支持数据导出"},
    ]}))
    candidate = json.loads(rejected.choices[0].message.tool_calls[0].function.arguments)
    review = review_from_candidate({"original_message": user_text, "first_candidate": candidate,
        "current_tasks": {}, "identities": {}})
    review["groups"] = [{"group_id": "atomic-source-check", "verdict": "accept", "reason": "confirmed",
        "operations": [op for group in review["groups"] for op in group["operations"]]}]
    client = _scripted(monkeypatch, [rejected], review_outputs=[review_completion(review)])
    response = asyncio.run(engine.chat(ChatRequest(message=user_text)))
    assert response.status == "needs_input"
    assert response.business.operation_feedback[0]["issue"] == "reference_missing"
    assert response.business.requirements.items == ()
    assert response.business.current_task.requirements.items == ()
    assert response.business.current_task.match is None and response.business.current_task.pricing is None
    assert response.business.match is None and response.business.proposal is None
    assert len(response.tool_results) == 1 and len(client.review_calls) == 1
    assert "原话位置无法核查" in response.message and "未更新" in response.message
    assert "输入还不够明确" not in response.message
    assert response.message.startswith(response.business.presentation.answer_markdown)
    assert response.message == render_business(response.business, response.status)
    assert engine.conversations.get_messages(response.session_id)[-1].content == response.message


def test_reassessment_without_accepted_change_explains_unchanged_facts_but_preserves_new_quote_identity(engine, monkeypatch):
    session = engine.conversations.ensure_conversation(None, "原API选型")
    state = engine.work.get(session).state
    state.apply_patch({"intent": "selection", "add": [
        {"feature_id": "api_access", "statement": "产品API", "user_quote": "需要API"}],
        "parameters": [{"name": "paid_seats", "value": 80, "user_quote": "80席"},
            {"name": "months", "value": 12, "user_quote": "12个月"}],
        "pricing_quote": "计算基价"}, "需要API，80席，12个月，计算基价")
    state.remember_result(state.match(), calculate_subscription_price("team", 80, 12), (), ())

    def script(patch):
        _scripted(monkeypatch, [
            _completion(_tool("update_requirements", patch)),
            _completion(_tool("match_requirements", {})),
            _completion(_tool("calculate_price", {"plan_id": "team", "paid_seats": 80, "months": 12})),
            _completion(),
        ])

    script({"intent": "selection"})
    unchanged = asyncio.run(engine.chat(ChatRequest(message="把席位改为大约70席，其他不变。", session_id=session)))
    assert unchanged.business.operation_feedback[0]["status"] == "rejected"
    assert unchanged.business.operation_feedback[0]["issue"] == "uncertain"
    assert "此前评估仍有效" in unchanged.business.presentation.answer_markdown
    assert unchanged.business.current_task.pricing.paid_seats == 80
    assert unchanged.business.current_task.pricing.base_price_minor == 7680000
    assert unchanged.business.proposal is None
    assert engine.conversations.get_messages(session)[-1].content == unchanged.message
    script({"intent": "selection", "parameters": [{"name": "paid_seats", "value": 80, "user_quote": "买80席"}]})
    renewed_quote = asyncio.run(engine.chat(ChatRequest(message="明确买80席，重新计算。", session_id=session)))
    assert renewed_quote.business.requirements == unchanged.business.requirements
    assert next(p for p in renewed_quote.business.requirements.parameters if p.name == "paid_seats").user_quote == "80席"
    assert any(note.get("quote") == "买80席" for note in state.source_notes)


def test_background_context_is_retained_without_claiming_selection_or_pricing_parameter_change(engine, monkeypatch):
    session = engine.conversations.ensure_conversation(None, "原API选型")
    state = engine.work.get(session).state
    state.apply_patch({"intent": "selection", "add": [
        {"feature_id": "api_access", "statement": "产品API", "user_quote": "需要API"}],
        "parameters": [{"name": "paid_seats", "value": 80, "user_quote": "80席"},
            {"name": "months", "value": 12, "user_quote": "12个月"}],
        "pricing_quote": "计算基价"}, "需要API，80席，12个月，计算基价")
    state.remember_result(state.match(), calculate_subscription_price("team", 80, 12), (), ())
    original = state.requirements
    context = "预计以后购买30席，先别修改当前方案。"
    _scripted(monkeypatch, [
        _completion(_tool("update_requirements", {"intent": "selection", "context_quotes": [context]})),
        _completion(_tool("match_requirements", {})),
        _completion(_tool("calculate_price", {"plan_id": "team", "paid_seats": 80, "months": 12})),
        _completion(),
    ])
    response = asyncio.run(engine.chat(ChatRequest(message=context, session_id=session)))
    assert response.business.requirements.context_quotes == original.context_quotes
    assert response.business.requirements.revision == original.revision
    assert any(note.get("quote") == context for note in state.source_notes)
    assert response.business.requirements.parameters == original.parameters
    assert response.business.requirements.items == original.items
    assert response.business.current_task.match == state.last_match
    assert "此前评估仍有效" in response.business.presentation.answer_markdown
    assert response.business.current_task.pricing.base_price_minor == 7680000
    assert engine.conversations.get_messages(session)[-1].content == response.message


@pytest.mark.parametrize("field,value,unit", [("paid_seats", 0, "席"), ("months", 13, "个月")])
def test_out_of_range_parameter_is_rejected_before_new_pricing(engine, monkeypatch, field, value, unit):
    session = engine.conversations.ensure_conversation(None, "原API选型")
    state = engine.work.get(session).state
    state.apply_patch({"intent": "selection", "add": [
        {"feature_id": "api_access", "statement": "产品API", "user_quote": "需要API"}],
        "parameters": [{"name": "paid_seats", "value": 80, "user_quote": "80席"},
            {"name": "months", "value": 12, "user_quote": "12个月"}],
        "pricing_quote": "计算基价"}, "需要API，80席，12个月，计算基价")
    state.remember_result(state.match(), calculate_subscription_price("team", 80, 12), (), ())
    operands = {"paid_seats": 80, "months": 12, field: value}
    quote = f"{value}{unit}"
    _scripted(monkeypatch, [
        _completion(_tool("update_requirements", {"intent": "selection", "parameters": [
            {"name": field, "value": value, "user_quote": quote}]})),
        _completion(_tool("match_requirements", {})),
        _completion(_tool("calculate_price", {"plan_id": "team", **operands})),
    ])
    response = asyncio.run(engine.chat(ChatRequest(message=f"改为{quote}，重新计算。", session_id=session)))
    assert response.status == "needs_input"
    assert {p.name: p.value for p in response.business.requirements.parameters} == {"paid_seats": 80, "months": 12}
    assert response.business.current_task.pricing.base_price_minor == 7_680_000 and response.business.pricing is None
    assert response.business.operation_feedback[0]["issue"] == "invalid_range"
    assert response.business.operation_feedback[0]["previous_value"] == (80 if field == "paid_seats" else 12)
    answer = response.business.presentation.answer_markdown
    assert "超出项目输入范围" in answer
    assert f"“{value}{unit}”没有被接受" in answer
    assert "之间的整数" in answer and "未更新" in answer
    limits = TypeAdapter(PaidSeats if field == "paid_seats" else Months).json_schema()
    rejected_passage = next(passage for passage in answer.split("\n\n") if f"“{value}{unit}”没有被接受" in passage)
    assert "未更新" in rejected_passage and "之间的整数" in rejected_passage
    assert str(limits["minimum"]) in rejected_passage and str(limits["maximum"]) in rejected_passage
    assert "项目输入范围" in rejected_passage
    assert answer.count(f"“{value}{unit}”没有被接受") == 1
    retained_result = [passage for passage in answer.split("\n\n") if "76,800" in passage]
    assert len(retained_result) == 1 and "不是本轮新计算的结果" in retained_result[0]
    assert "当前选型摘要" not in answer and "需求比较" not in answer
    assert response.business.proposal is None
    assert "76,800" in response.message and "不是本轮新计算的结果" in response.message
    assert engine.conversations.get_messages(session)[-1].content == response.message


@pytest.mark.parametrize("intent", ["selection", "price"])
def test_rejected_quantity_preserves_accepted_facts_in_formal_history(engine, monkeypatch, intent):
    session = engine.conversations.ensure_conversation(None, "retained quantity")
    state = engine.work.get(session).state
    patch = {"intent": intent, "pricing_quote": "算基价", "parameters": [
        {"name": "paid_seats", "value": 80, "user_quote": "80席"},
        {"name": "months", "value": 12, "user_quote": "12个月"}]}
    if intent == "selection":
        patch["add"] = [{"feature_id": "api_access", "statement": "产品API", "user_quote": "需要API"}]
    else:
        patch.update(selected_plan_id="team", selection_quote="Team")
    state.apply_patch(patch, "需要API，Team 80席，12个月，算基价")
    if intent == "selection":
        state.remember_result(state.match(), calculate_subscription_price("team", 80, 12), (), ())
    _scripted(monkeypatch, [_completion(_tool("update_requirements", {"intent": intent, "parameters": [
        {"name": "paid_seats", "value": 50, "user_quote": "50席"}]}))])
    response = asyncio.run(engine.chat(ChatRequest(message="大约购买50席", session_id=session)))
    assert response.status == "needs_input"
    assert {p.name: p.value for p in response.business.requirements.parameters} == {"paid_seats": 80, "months": 12}
    assert "未更新" in response.message
    if intent == "selection":
        assert response.business.current_task.last_result == "not_updated"
        assert response.business.current_task.pricing.base_price_minor == 7680000
        assert response.business.current_task.pricing.months == 12
        assert response.business.current_task.match.selected_plan_id == "team"
        assert "此前评估仍有效" in response.message
        retained = next(passage for passage in response.message.split("\n\n") if "76,800.00" in passage)
        assert all(fact in retained for fact in ("团队版 Team", "80 席", "12 个月", "此前保留的结果", "不是本轮新计算的结果"))
        assert response.message.count("76,800.00") == 1
        assert "当前选型摘要" not in response.message
        assert not response.business.presentation.show_current_result
    else:
        assert "仍保留此前核定的 80 席" in response.message
        assert response.business.pricing is None and "独立计价" in response.message
        assert "80 个付费席位" in response.message and "12 个月" in response.message
        assert "团队版 Team" in response.message
    assert engine.conversations.get_messages(session)[-1].content == response.message


def test_current_result_identity_survives_inquiry_and_rejection_then_moves_for_unassessed_change(engine, monkeypatch):
    """Controlled model responses, real catalog/matching/pricing/persistence."""
    async def run():
        _scripted(monkeypatch, [
            _completion(_tool("update_requirements", {"intent": "selection", "pricing_quote": "计算基价",
                "add": [{"feature_id": "api_access", "statement": "产品API", "user_quote": "需要API"}],
                "parameters": [{"name": "paid_seats", "value": 80, "user_quote": "80席"},
                    {"name": "months", "value": 12, "user_quote": "12个月"}]})),
            _completion(_tool("match_requirements", {})),
            _completion(_tool("calculate_price", {"plan_id": "team", "paid_seats": 80, "months": 12})),
            _completion(),
        ])
        first = await engine.chat(ChatRequest(message="需要API，80席，12个月，计算基价"))
        original_id = first.business.current_task.result_turn_id
        original_message = first.business.current_task.result_message_id
        assert original_id == first.business.turn_id
        assert original_message == first.message_id
        assert first.business.presentation.show_comparison and not first.business.presentation.show_current_result
        assert "需求比较" not in first.business.presentation.answer_markdown
        comparison_label = first.business.match.requirements.items[0].statement.removeprefix("必须支持 ")
        assert f"需求比较：\n\n- {comparison_label} — " in first.message
        # The answer gives the amount; the concrete confirmation object gives
        # the formula. Formal history retains the same formula exactly once.
        assert first.business.pricing.formula not in first.business.presentation.answer_markdown
        assert first.message.count(first.business.pricing.formula) == 1
        assert "76,800.00" in first.message and first.business.pricing.formula in first.message
        _scripted(monkeypatch, [
            _completion(_tool("update_requirements", {"intent": "unit_price"})),
            _completion(_tool("query_catalog", {"plan_id": "business"})), _completion(),
        ])
        inquiry = await engine.chat(ChatRequest(message="Business单价多少？", session_id=first.session_id))
        assert inquiry.business.current_task.result_turn_id == original_id
        assert inquiry.business.current_task.result_message_id == original_message
        assert inquiry.business.current_task.pricing.base_price_minor == 7680000
        assert "120.00 元／席／月" in inquiry.message
        assert "76,800" not in inquiry.message and "80 个付费席位" not in inquiry.message
        assert not inquiry.business.presentation.show_current_result
        assert not inquiry.business.presentation.show_comparison
        _scripted(monkeypatch, [_completion(_tool("update_requirements", {"intent": "selection",
            "parameters": [{"name": "paid_seats", "value": 50, "user_quote": "50席"}]}))])
        rejected = await engine.chat(ChatRequest(message="大约购买50席", session_id=first.session_id))
        assert rejected.business.current_task.result_turn_id == original_id
        assert rejected.business.current_task.result_message_id == original_message
        assert rejected.business.current_task.status == "current"
        assert rejected.business.current_task.pricing.base_price_minor == 7680000
        assert not rejected.business.presentation.show_current_result
        # Inject model unavailability after an accepted update; no match/price
        # substitute is permitted, and the original amount is no longer valid.
        _scripted(monkeypatch, [_completion(_tool("update_requirements", {"intent": "selection",
            "parameters": [{"name": "paid_seats", "value": 50, "user_quote": "购买50席"}]}))])
        changed = await engine.chat(ChatRequest(message="改为购买50席", session_id=first.session_id))
        assert changed.status == "incomplete"
        assert changed.business.current_task.status == "needs_reassessment"
        assert changed.business.current_task.result_turn_id == changed.business.turn_id != original_id
        assert changed.business.current_task.result_message_id == changed.message_id != original_message
        assert changed.business.current_task.pricing is None and changed.business.current_task.match is None
        assert changed.business.presentation.show_current_result
        assert not changed.business.presentation.show_comparison
        assert "76,800" not in changed.message and "50 个付费席位" in changed.message
        for response in (first, inquiry, rejected, changed):
            stored = next(message for message in engine.conversations.get_messages(first.session_id)
                if message.id == response.message_id)
            assert stored.content == response.message == render_business(response.business, response.status)
        return first, changed

    first, changed = asyncio.run(run())
    from fastapi.testclient import TestClient
    from app import main
    from app.services.work_state import SessionWorkStore
    monkeypatch.setattr(main, "orchestrator", engine)
    monkeypatch.setattr(main, "conversations", engine.conversations)
    client = TestClient(main.app)
    restored = client.get("/sessions/" + first.session_id).json()
    assert restored["business_state_available"]
    assert restored["current_task"]["result_message_id"] == changed.message_id
    assert restored["current_task"]["status"] == "needs_reassessment"
    assert restored["current_task"]["pricing"] is None
    assert any(row["id"] == changed.message_id and row["content"] == changed.message for row in restored["messages"])
    engine.work = SessionWorkStore(engine.settings)
    lost = client.get("/sessions/" + first.session_id).json()
    assert not lost["business_state_available"] and lost["current_task"] is None
    assert "重新说明" in lost["recovery_notice"]
    assert lost["messages"] == restored["messages"]
    assert engine.work._sessions == {}  # Reads never fabricate a recovered task.


@pytest.mark.parametrize("auxiliary_status", ["retrieved", "unavailable"])
def test_auxiliary_search_does_not_negate_supported_knowledge_answer(auxiliary_status):
    # Controlled response assembly only; this does not prove live retrieval.
    quote = get_catalog().conditions[0].statement
    source = next(s for s in _read_index(DEFAULT_INDEX_PATH).inputs.sources if quote in s.raw_text)
    answered = SimpleNamespace(retrieval_id="answered", status="retrieved", hits=[SimpleNamespace(source=source)])
    auxiliary = SimpleNamespace(retrieval_id="auxiliary", status=auxiliary_status, hits=[])
    business = SimpleNamespace(errors=[], recovery_notice=None, match=None, pricing=None, unit_price=None,
        catalog=None, intent="knowledge", knowledge_answers=[{"retrieval_id": "answered", "answerability": "supported",
            "citations": [{"source_id": source.source_id, "quote": quote}]}], retrieval_evidence=[answered, auxiliary],
        changes=[], current_task=None, questions=[], queried_quotes=[])
    text = render_business(business, "completed")
    assert quote in text and source.relative_path in text
    assert "根据本次实际检索到的产品资料，原文说明：" in text
    assert "> " + quote in text
    assert text.count(quote) == 1
    assert "尚未取得足以回答本问题的依据" not in text
    assert ("资料查询失败" in text) == (auxiliary_status == "unavailable")


def test_catalog_context_supplies_authoritative_identity_without_new_tools_or_business_tables(engine, monkeypatch):
    client = _scripted(monkeypatch, [
        _completion(_tool("update_requirements", {"intent": "unit_price"})),
        _completion(_tool("query_catalog", {"plan_id": "team"})), _completion(),
    ])
    response = asyncio.run(engine.chat(ChatRequest(message="Team 单价是多少？")))
    context = response.business.catalog_context
    assert context == get_catalog()
    assert context.product.product_id == get_catalog().product.product_id
    assert [(p.plan_id, p.name) for p in context.plans] == [(p.plan_id, p.name) for p in get_catalog().plans]
    assert [(c.condition_id, c.feature_id, c.name, c.statement) for c in context.conditions] == [
        (c.condition_id, c.feature_id, c.name, c.statement) for c in get_catalog().conditions]
    assert response.business.catalog is None
    assert response.business.match is None and response.business.pricing is None
    assert response.business.retrieval_evidence == []
    assert [r["name"] for r in response.tool_results] == ["update_requirements", "query_catalog"]
    assert len(client.captured) == 3
    assert response.message == render_business(response.business, response.status)
    assert "当前目录套餐" not in response.message and "完整满足集合" not in response.message
    assert engine.conversations.get_messages(response.session_id)[-1].content == response.message
    unit_record = response.tool_results[-1]
    assert unit_record["display_arguments"] == {"plan_id": "team"}
    assert unit_record["display_arguments_status"] == "accepted"
    assert unit_record["display_result"] == unit_record["result"]


def test_catalog_context_does_not_fake_a_catalog_query_or_complete_missing_knowledge(engine, monkeypatch):
    client = _scripted(monkeypatch, [
        _completion(_tool("update_requirements", {"intent": "knowledge"})),
        _completion(), _completion(),
    ])
    response = asyncio.run(engine.chat(ChatRequest(message="SSO 需要什么前提？")))
    assert response.business.catalog_context == get_catalog()
    assert response.business.catalog is None and response.business.match is None
    assert response.business.unit_price is None and response.business.pricing is None
    assert response.business.retrieval_evidence == [] and response.business.knowledge_answers == []
    assert response.status == "incomplete"
    assert not response.business.errors
    assert response.business.consultation_coverage.scope_complete
    assert response.business.consultation_coverage.items[0].stage == "not_queried"
    assert [r["name"] for r in response.tool_results] == ["update_requirements"]
    assert len(client.captured) == 3  # One reminder for the accepted execution gap.
    assert "当前目录套餐" not in response.message and "订阅基价：" not in response.message


def test_actual_catalog_query_is_distinct_from_the_always_available_identity_context(engine, monkeypatch):
    _scripted(monkeypatch, [
        _completion(_tool("update_requirements", {"intent": "unit_price"})),
        _completion(_tool("query_catalog", {})), _completion(),
    ])
    response = asyncio.run(engine.chat(ChatRequest(message="比较三档的目录单价")))
    assert response.business.catalog == response.business.catalog_context == get_catalog()
    assert response.tool_results[-1]["display_arguments"] == {}
    assert response.tool_results[-1]["display_result"] == response.tool_results[-1]["result"]
    assert all(plan.name in response.message for plan in get_catalog().plans)


def test_illegal_candidate_body_remains_only_in_raw_diagnostics(engine, monkeypatch):
    hidden = "Business 全满足且只要 1.00 元；已保存 BAD-QUOTE-ID"
    candidate = {"intent": "selection", "assistant_text": hidden,
        "add": [{"statement": "产品API", "feature_id": "api_access", "user_quote": "需要API"}]}
    client = _scripted(monkeypatch, [_completion(_tool("update_requirements", candidate), content=hidden)])
    response = asyncio.run(engine.chat(ChatRequest(message="需要API")))
    record = response.tool_results[0]
    assert hidden in record["arguments"]
    assert record["result"]["error"]["code"] == "semantic_review_candidate_invalid"
    assert client.review_calls == [] and response.business.semantic_review["dispatched"] == 0
    assert record["display_arguments"] == {} and record["display_arguments_status"] == "rejected"
    displayed = json.dumps({"arguments": record["display_arguments"], "result": record["display_result"]}, ensure_ascii=False)
    assert hidden not in displayed and "BAD-QUOTE-ID" not in displayed
    assert hidden not in response.message
    assert "首候选参数未通过格式或字段校验" in response.message
    assert "尚未进行独立核对与业务执行" in response.message
    assert record["display_result"]["error"]["step"] == "update_requirements"
    assert response.status == "incomplete" and response.business.requirements.items == ()
    assert engine.conversations.get_messages(response.session_id)[-1].content == response.message


@pytest.mark.parametrize("name", ["calculate_price", "update_requirements", "save_quote"])
def test_rejected_echoes_cannot_return_through_display_result(name):
    hidden = "HIDDEN_BAD_BODY：只需1元；已保存FAKE-QUOTE"
    raw_result = {"ok": False, "error": {"code": "invalid_arguments", "message": hidden,
        "rejected_value": hidden, "candidate": {"unknown": hidden}}}
    before = deepcopy(raw_result)
    display = tool_display_fields(name, json.dumps({"unknown": hidden}), raw_result)
    assert raw_result == before  # Keep the original API trace untouched.
    assert display["display_arguments_status"] == "rejected" and display["display_arguments"] == {}
    assert display["display_result"]["ok"] is False
    assert display["display_result"]["error"]["code"] == "invalid_arguments"
    assert display["display_result"]["error"]["step"] == name
    assert "类型、字段、范围" in display["display_result"]["error"]["message"]
    assert hidden not in json.dumps(display, ensure_ascii=False)


def test_successful_duplicate_candidate_does_not_display_unused_model_wording():
    state = WorkState(Settings(_env_file=None))
    registry = BusinessToolRegistry(state.settings, state, "需要API")
    original = {"intent": "selection", "add": [{"statement": "产品API", "feature_id": "api_access", "user_quote": "需要API"}]}
    assert asyncio.run(registry.execute("update_requirements", original))["ok"]
    hidden = "HIDDEN_BAD_BODY：已保存FAKE-QUOTE且只需1元"
    duplicate = {"intent": "selection", "add": [{"statement": hidden,
        "feature_id": "api_access", "user_quote": "需要API"}]}
    result = asyncio.run(registry.execute("update_requirements", duplicate))
    feature_name = next(feature.name for feature in get_catalog().features if feature.feature_id == "api_access")
    assert result["ok"] is True
    assert state.requirements.items[0].statement in {feature_name, "必须支持 " + feature_name}
    display = tool_display_fields("update_requirements", json.dumps(duplicate, ensure_ascii=False), result)
    assert display["display_arguments_status"] == "accepted"
    assert display["display_arguments"] == {}
    assert display["display_result"]["data"]["requirements"] == result["data"]["requirements"]
    assert hidden not in json.dumps(display, ensure_ascii=False)


@pytest.mark.parametrize("kind,feature_id,user_quote", [
    ("feature", "api_access", "需要产品API"),
    ("unmodeled", None, "必须有SCIM自动开通"),
    ("ambiguous", None, "需要高级接入"),
])
def test_first_requirement_model_statement_cannot_add_price_or_save_claim_to_accepted_facts(
    engine, monkeypatch, kind, feature_id, user_quote,
):
    """Poison only candidate wording, while its feature identity/quote is valid."""
    hidden = "HIDDEN_FIRST_STATEMENT：Business全部满足且只需1.00元；已保存FAKE-QUOTE"
    candidate = {"statement": hidden, "feature_id": feature_id, "kind": kind, "user_quote": user_quote}
    outputs = [_completion(_tool("update_requirements", {"intent": "selection", "add": [candidate]}),
                           _tool("match_requirements", {}, "match-first"))]
    if kind == "feature":
        outputs.append(_completion())
    outputs.extend([_completion(_tool("update_requirements", {"intent": "general"})), _completion()])
    client = _scripted(monkeypatch, outputs)

    async def run():
        first = await engine.chat(ChatRequest(message=user_quote))
        next_input_index = len(client.captured)
        second = await engine.chat(ChatRequest(message="解释刚才的结论", session_id=first.session_id))
        return first, second, next_input_index

    first, second, next_input_index = asyncio.run(run())
    accepted = first.business.requirements.items[0]
    assert accepted.user_quote == user_quote and accepted.kind == kind and accepted.feature_id == feature_id
    if kind == "feature":
        feature_name = next(feature.name for feature in get_catalog().features if feature.feature_id == feature_id)
        assert accepted.statement in {feature_name, "必须支持 " + feature_name}
        assert first.business.match.lowest_plan_id == "team" and first.status == "completed"
    else:
        assert accepted.statement == user_quote
        assert first.business.match.lowest_plan_id is None and first.business.match.status == "blocked"
        if kind == "ambiguous":
            assert first.status == "needs_input" and first.business.questions
        else:
            assert first.status == "completed" and first.business.questions == []
            assert first.business.match.status == "blocked"
    update = first.tool_results[0]
    assert hidden in update["arguments"]  # Preserve actual raw API diagnostics.
    assert update["result"]["ok"] is True and update["display_arguments_status"] == "accepted"
    assert update["display_arguments"] == {}
    assert update["display_result"]["data"]["requirements"]["items"] == first.business.requirements.model_dump(mode="json")["items"]
    for response in (first, second):
        assert hidden not in response.message
        assert hidden not in json.dumps(response.business.model_dump(mode="json"), ensure_ascii=False)
        safe_traces = [{key: value for key, value in record.items() if key.startswith("display_")}
                       for record in response.tool_results]
        assert hidden not in json.dumps(safe_traces, ensure_ascii=False)
    history = engine.conversations.get_messages(first.session_id)
    assert [message.content for message in history if message.role == "assistant"] == [first.message, second.message]
    assert all(hidden not in message.content for message in history)
    next_input = client.captured[next_input_index]["messages"]
    assert hidden not in json.dumps(next_input, ensure_ascii=False)
    assert [message["content"] for message in next_input if message["role"] == "assistant"] == [first.message]


@pytest.mark.parametrize("kind,user_quote", [
    ("unmodeled", "必须有SCIM自动开通"),
    ("ambiguous", "需要高级接入"),
])
def test_duplicate_unresolved_candidate_keeps_verified_quote_and_does_not_display_new_model_wording(kind, user_quote):
    state = WorkState(Settings(_env_file=None))
    registry = BusinessToolRegistry(state.settings, state, user_quote)
    original = {"intent": "selection", "add": [{"statement": "原始模型候选", "feature_id": None,
                                               "kind": kind, "user_quote": user_quote}]}
    assert asyncio.run(registry.execute("update_requirements", original))["ok"]
    identity = state.requirements.items[0].requirement_id
    hidden = "HIDDEN_DUPLICATE_STATEMENT：已保存FAKE-QUOTE且只需1元"
    duplicate = {"intent": "selection", "add": [{"statement": hidden, "feature_id": None,
                                                "kind": kind, "user_quote": user_quote}]}
    result = asyncio.run(registry.execute("update_requirements", duplicate))
    assert result["ok"] is True and len(state.requirements.items) == 1
    assert state.requirements.items[0].requirement_id == identity
    assert state.requirements.items[0].statement == user_quote and result["data"]["changes"] == []
    display = tool_display_fields("update_requirements", json.dumps(duplicate, ensure_ascii=False), result)
    assert display["display_arguments_status"] == "accepted"
    assert display["display_arguments"] == {}
    assert hidden not in json.dumps(display, ensure_ascii=False)


@pytest.mark.parametrize("kind", ["unmodeled", "ambiguous"])
def test_unresolved_requirement_cannot_borrow_feature_identity_to_claim_catalog_support(engine, monkeypatch, kind):
    candidate = {"statement": "HIDDEN_HYBRID：已保存FAKE-QUOTE", "kind": kind,
                 "feature_id": "api_access", "user_quote": "要求高级接入"}
    _scripted(monkeypatch, [_completion(_tool("update_requirements", {"intent": "selection", "add": [candidate]}))])
    response = asyncio.run(engine.chat(ChatRequest(message="要求高级接入")))
    assert response.status == "needs_input" and response.business.requirements.items == ()
    assert response.business.operation_feedback[0]["status"] == "rejected"
    assert response.business.operation_feedback[0]["issue"] == "unknown_identity"
    assert response.business.proposal is None
    assert response.business.match is None and response.business.pricing is None
    assert "HIDDEN_HYBRID" not in response.message
    assert "HIDDEN_HYBRID" not in json.dumps(response.business.model_dump(mode="json"), ensure_ascii=False)


def test_accepted_user_parameters_and_quotes_remain_visible_without_price_recalculation(engine, monkeypatch):
    patch = {"intent": "price", "selected_plan_id": "team", "selection_quote": "Team",
        "parameters": [{"name": "paid_seats", "value": 80, "user_quote": "明确购买80席"},
            {"name": "months", "value": 12, "user_quote": "一年"}]}
    _scripted(monkeypatch, [
        _completion(_tool("update_requirements", patch)),
        _completion(_tool("calculate_price", {"plan_id": "team", "paid_seats": 80, "months": 12})),
        _completion(),
    ])
    response = asyncio.run(engine.chat(ChatRequest(message="Team，明确购买80席一年，计算订阅基价")))
    assert response.status == "completed" and response.business.match is None
    update, price = response.tool_results
    assert update["display_arguments"] == {}
    assert update["display_result"]["data"]["requirements"]["parameters"] == patch["parameters"]
    assert update["display_result"]["data"]["requirements"]["selected_plan_id"] == "team"
    assert update["display_result"]["data"]["requirements"] == response.business.requirements.model_dump(mode="json")
    assert price["display_arguments"] == {"plan_id": "team", "paid_seats": 80, "months": 12}
    assert price["display_result"] == price["result"]
    assert price["display_result"]["data"]["formula"] == response.business.pricing.formula
    assert price["display_result"]["data"]["base_price_minor"] == 7_680_000
    assert response.business.pricing.fit_status == "not_assessed"


def test_unknown_tool_has_a_true_failed_trace_without_candidate_save_claim(engine, monkeypatch):
    hidden = "HIDDEN_BAD_BODY：已保存FAKE-QUOTE"
    _scripted(monkeypatch, [_completion(_tool("update_requirements", {"intent": "general"}, "normalize"),
        _tool("save_quote", {"confirmation": hidden}))])
    response = asyncio.run(engine.chat(ChatRequest(message="保存这个")))
    record = response.tool_results[-1]
    assert record["name"] == "save_quote" and hidden in record["arguments"]
    assert record["result"]["ok"] is False
    assert record["display_result"]["ok"] is False
    assert record["display_result"]["error"]["code"] == "unknown_tool"
    assert record["display_arguments_status"] == "rejected" and record["display_arguments"] == {}
    assert hidden not in json.dumps(record["display_result"], ensure_ascii=False)
    assert hidden not in response.message


def test_unavailable_mapping_does_not_fall_back_to_untrusted_raw_data():
    hidden = "HIDDEN_BAD_BODY：1元；已保存FAKE-QUOTE"
    raw = {"ok": True, "data": {"unknown": hidden}}
    display = tool_display_fields("unregistered_future_tool", '{}', raw)
    assert display["display_arguments_status"] == "unavailable"
    assert display["display_arguments"] == {}
    assert display["display_result"]["ok"] is True  # Display absence does not invent an execution failure.
    assert display["display_result"]["display_status"] == "unavailable"
    assert hidden not in json.dumps(display, ensure_ascii=False)


def test_error_echo_cannot_reenter_business_text_storage_or_next_turn_history(engine, monkeypatch):
    hidden = "HIDDEN_BAD_BODY：Business只需1元；已保存FAKE-QUOTE"
    real_execute = BusinessToolRegistry.execute

    async def echoed_failure(self, name, arguments):
        if name == "query_catalog":
            return {"ok": False, "error": {"code": "invalid_arguments", "message": hidden,
                "rejected_value": hidden}}
        return await real_execute(self, name, arguments)

    monkeypatch.setattr(BusinessToolRegistry, "execute", echoed_failure)
    client = _scripted(monkeypatch, [
        _completion(_tool("update_requirements", {"intent": "unit_price"}, "normalize"),
            _tool("query_catalog", {"plan_id": "team"})),
        _completion(_tool("update_requirements", {"intent": "general"})), _completion(),
    ])

    async def run():
        first = await engine.chat(ChatRequest(message="测试故障"))
        second = await engine.chat(ChatRequest(message="继续", session_id=first.session_id))
        return first, second

    first, second = asyncio.run(run())
    assert first.tool_results[-1]["result"]["error"]["message"] == hidden
    assert first.tool_results[-1]["display_result"]["error"]["code"] == "invalid_arguments"
    assert first.business.errors[0] == first.tool_results[-1]["display_result"]["error"]
    assert hidden not in first.message and hidden not in second.message
    assert hidden not in json.dumps(first.business.model_dump(mode="json"), ensure_ascii=False)
    assert all(hidden not in message.content for message in engine.conversations.get_messages(first.session_id))
    assert hidden not in json.dumps(client.captured[-1]["messages"], ensure_ascii=False)


def test_conflict_failure_preserves_actual_association_ids_with_controlled_message():
    raw = {"ok": False, "error": {"code": "evidence_conflict", "message": "不可回显的异常信息",
        "retrieval_ids": ["e6edc667f6b14d57aaa32f4db7556d13"],
        "condition_ids": ["saml_sso_compatible_idp"]}}
    display = tool_display_fields("explain_knowledge", '{}', raw)
    error = display["display_result"]["error"]
    assert error["retrieval_ids"] == raw["error"]["retrieval_ids"]
    assert error["condition_ids"] == raw["error"]["condition_ids"]
    assert error["code"] == "evidence_conflict" and error["step"] == "explain_knowledge"
    assert "已有已知冲突" in error["message"] and error["message"] != raw["error"]["message"]


def test_free_body_disguised_as_an_unknown_tool_name_remains_only_in_raw_identity(engine, monkeypatch):
    hidden = "HIDDEN_BAD_BODY：Business只需1元；已保存FAKE-QUOTE"
    _scripted(monkeypatch, [_completion(_tool("update_requirements", {"intent": "general"}, "normalize"), _tool(hidden, {}))])
    response = asyncio.run(engine.chat(ChatRequest(message="测试未知工具")))
    record = response.tool_results[-1]
    assert record["name"] == hidden
    assert record["display_name"] == "未注册工具（名称无效）"
    assert record["display_result"]["error"]["code"] == "unknown_tool"
    assert record["display_result"]["error"]["step"] == "unknown_tool"
    display = {key: value for key, value in record.items() if key.startswith("display_")}
    assert hidden not in json.dumps(display, ensure_ascii=False)
    assert hidden not in json.dumps(response.business.model_dump(mode="json"), ensure_ascii=False)
    assert hidden not in response.message
