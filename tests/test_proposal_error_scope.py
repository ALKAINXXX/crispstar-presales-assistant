"""Proposal eligibility is the actual target's facts, not a global error flag.

Controlled tool faults; real review, matching, pricing and temporary SQLite.
"""
import asyncio

import pytest

from app.schemas.chat import ChatRequest
from app.tools.business import BusinessToolRegistry
from test_input_orchestrator import engine, script, call, completion, group


@pytest.mark.parametrize("failed_tool", ["query_catalog", "calculate_price"])
def test_target_receipts_determine_proposal_despite_an_unrelated_failure(engine, monkeypatch, failed_tool):
    message = "需要API，实际购买50席，订阅12个月，算价并准备草案；另外显示产品目录。"
    patch = {"intent": "selection", "pricing_quote": "算价", "add": [
        {"statement": "产品API", "feature_id": "api_access", "user_quote": "需要API"}],
        "parameters": [{"name": "paid_seats", "value": 50, "user_quote": "实际购买50席"},
                       {"name": "months", "value": 12, "user_quote": "订阅12个月"}]}
    review = {"intent": "selection", "coverage_complete": True,
        "target": {"kind": "selection", "relation": "new", "span": {"quote": message}},
        "groups": [group("api", "add", "需要API", requirement=patch["add"][0]),
                   group("seats", "set", "实际购买50席", field="paid_seats", value=50),
                   group("months", "set", "订阅12个月", field="months", value=12),
                   group("price", "request_pricing", "算价"),
                   group("prepare", "request_proposal", "准备草案"),
                   group("catalog", "note", "另外显示产品目录", speech_act="question")]}
    execute = BusinessToolRegistry.execute

    async def controlled_fault(self, name, arguments):
        if name == failed_tool:
            return {"ok": False, "error": {"code": "controlled_tool_unavailable", "scope": "catalog" if name == "query_catalog" else "pricing",
                "message": "受控隔离故障：" + name + "暂时不可用。"}}
        return await execute(self, name, arguments)

    monkeypatch.setattr(BusinessToolRegistry, "execute", controlled_fault)
    script(monkeypatch, [completion(call("update_requirements", patch)),
        completion(call("adjudicate_turn", review)),
        completion(call("match_requirements", {}, "match"), call("calculate_price", {"plan_id": "team", "paid_seats": 50, "months": 12}, "price")),
        completion(call("query_catalog", {})), completion()])
    response = asyncio.run(engine.chat(ChatRequest(message=message)))
    assert response.business.errors
    assert response.business.match is not None, response.message
    assert response.business.match.status == "matched"
    if failed_tool == "query_catalog":
        assert response.business.pricing.base_price_minor == 4_800_000
        assert response.business.proposal is not None
        assert response.business.proposal.pricing == response.business.pricing
        body = response.business.presentation.answer_markdown
        assert "48,000.00" in body and "尚未保存" in body and "核对" in body
        assert "保存成功" not in body
    else:
        assert response.business.pricing is None
        assert response.business.proposal is None
    assert engine.conversations.get_messages(response.session_id)[-1].content == response.message
    assert engine.quotes.list_quotes() == []
