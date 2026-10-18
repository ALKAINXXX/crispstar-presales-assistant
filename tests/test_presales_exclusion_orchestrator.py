"""Third-round exclusion checks through the actual production entry.

Models are controlled; WorkState, real catalog, coverage, rendering and SQLite
history remain enabled. The support check labels its fixed-index recall worker
as controlled and does not claim real embedding/model validation.
"""

import asyncio
from copy import deepcopy
import json

import pytest

from app.agents import orchestrator as module
from app.llm.tool_protocol import validate_tool_message_sequence
from app.schemas.chat import ChatRequest
from app.schemas.retrieval import RetrievalEvidence, RetrievalHit, RetrievalOptions
from app.services.knowledge import DEFAULT_INDEX_PATH, _read_index
from controlled_review import is_review_request, review_completion
from test_presales_orchestrator import (call, completion, engine, inquiry,
                                        review, same_history, script)


def assert_read_only_acceptance(engine, response, accepted_prices):
    state = engine.work.get(response.session_id).state
    assert state.review_complete and not state.consultation_contract_missing
    assert response.business.semantic_review["consultation_contract_missing"] is False
    assert any(item.plan_ids == accepted_prices for item in state.turn_consultations)
    assert any(item["status"] == "accepted" for item in state.consultation_feedback)
    assert not state.requirements.items and not state.requirements.parameters
    assert not state.requirements.condition_assertions and state.requirements.selected_plan_id is None
    assert state._selection_context is None and state._price_context is None


@pytest.mark.parametrize("message", [
    "各套餐每席每月单价，Basic不用查。",
    "不查Basic。各套餐每席每月单价多少？",
])
def test_excluded_price_scope_completes_only_two_plans_with_real_legacy_projection(engine, monkeypatch, message):
    item = inquiry("prices", message, ("team", "business"))
    provider = script(monkeypatch, [completion(call("update_requirements", {"intent": "unit_price",
        "consultations": [item]}, "candidate"),
        call("query_catalog", {"plan_id": "team"}, "actual-team"),
        call("query_catalog", {"plan_id": "business"}, "actual-business")), completion()], review([item]))
    response = asyncio.run(engine.chat(ChatRequest(message=message)))
    assert_read_only_acceptance(engine, response, ("team", "business"))
    assert response.status == "completed" and not response.business.errors
    coverage = response.business.consultation_coverage
    assert coverage.scope_complete and coverage.status == "completed"
    assert [price.plan_id for item in coverage.items for price in item.unit_prices] == ["team", "business"]
    assert [call for item in coverage.items for call in item.tool_call_ids] == ["actual-team", "actual-business"]
    assert [price.plan_id for price in response.business.unit_prices] == ["team", "business"]
    assert response.business.unit_price.plan_id == "business" and response.business.unit_price.unit_price_minor == 12000
    assert [record["tool_call_id"] for record in response.tool_results] == ["candidate", "actual-team", "actual-business"]
    assert all(record["result"]["ok"] for record in response.tool_results)
    body = response.business.presentation.answer_markdown
    assert body.index("团队版 Team") < body.index("企业版 Business")
    assert body.count("80.00") == body.count("120.00") == 1
    assert "40.00" not in body and "基础版 Basic的订阅单价" not in body
    assert len(provider.reviews) == 1
    same_history(engine, response, message)


@pytest.mark.parametrize("message", ["各套餐每席每月单价，Basic不用查。", "不查Basic。各套餐每席每月单价多少？"])
def test_incorrect_all_plans_candidate_stays_rejected_after_successful_real_prices(engine, monkeypatch, message):
    wrong = inquiry("prices", message, (), all_plans=True)
    provider = script(monkeypatch, [completion(call("update_requirements", {"intent": "unit_price",
        "consultations": [wrong]}, "candidate"),
        call("query_catalog", {"plan_id": "team"}, "actual-team"),
        call("query_catalog", {"plan_id": "business"}, "actual-business")), completion(), completion()], review([wrong]))
    response = asyncio.run(engine.chat(ChatRequest(message=message)))
    state = engine.work.get(response.session_id).state
    assert state.review_complete and not state.turn_consultations and not state.consultation_complete
    assert state.consultation_feedback[0]["status"] == "rejected"
    assert response.status != "completed"
    scope = response.business.consultation_coverage
    assert not scope.scope_complete and scope.status != "completed"
    assert scope.items[0].status == "incomplete" and scope.items[0].stage == "extraction_incomplete"
    assert all(not item.unit_prices for item in scope.items)
    assert [record["tool_call_id"] for record in response.tool_results] == ["candidate", "actual-team", "actual-business"]
    assert all(record["result"]["ok"] for record in response.tool_results)
    assert response.business.unit_price.plan_id == "business"
    assert [entry.price.plan_id for entry in response.business.unit_price_observations] == ["team", "business"]
    assert "本次范围核对未完成" in response.business.presentation.answer_markdown
    assert all(value not in response.business.presentation.answer_markdown for value in ("40.00", "80.00", "120.00"))
    assert len(provider.reviews) == 1
    same_history(engine, response, message)


@pytest.mark.parametrize("message,price_quote,plans,api_plan,all_plans", [
    ("价格不查Basic。请列出其余套餐的每席每月单价；另外Basic支持产品API吗？",
        "价格不查Basic。请列出其余套餐的每席每月单价", ("team", "business"), "basic", False),
    ("Team支持产品API吗？另外请列出所有套餐的每席每月单价。",
        "请列出所有套餐的每席每月单价", ("basic", "team", "business"), "team", True),
])
def test_price_scope_and_independent_api_scope_do_not_borrow_each_others_objects(
        engine, monkeypatch, message, price_quote, plans, api_plan, all_plans):
    price = inquiry("prices", price_quote, plans, all_plans=all_plans)
    support_quote = api_plan.capitalize() + "支持产品API吗"
    support = {"item_id": "plan-api", "aspect": "support", "plan_ids": [api_plan],
        "feature_id": "api_access", "topic": support_quote, "span": {"quote": support_quote}}
    sid = engine.conversations.ensure_conversation(None, "isolated exclusion and support")
    state = engine.work.get(sid).state
    index = _read_index(DEFAULT_INDEX_PATH)
    source = next(source for source in index.inputs.sources if source.section_id == "integration_api")

    def controlled_worker(request):
        return RetrievalEvidence(retrieval_id=request["retrieval_id"], chain_id=request["chain_id"],
            query=request["query"], retrieved_at=request["retrieved_at"], options=RetrievalOptions.model_validate(request["options"]),
            status="retrieved", hits=(RetrievalHit(rank=1, score=0.9, relevance="related", source=source),),
            product_id=index.inputs.product_id, dataset_version=index.inputs.dataset_version,
            index_build_id=index.build_id, index_inputs_sha256=index.inputs_sha256, embedding=index.inputs.embedding)

    monkeypatch.setattr(state.knowledge, "_execute", controlled_worker)
    raw_review = review([price, support], intent="knowledge")

    class Provider:
        main_calls, reviews, retrieval_id = [], [], None

        def __init__(self, settings):
            pass

        async def chat(self, **kwargs):
            validate_tool_message_sequence(kwargs["messages"])
            if is_review_request(kwargs):
                type(self).reviews.append(deepcopy(kwargs))
                assert len(type(self).reviews) == 1
                return review_completion(raw_review)
            type(self).main_calls.append(deepcopy(kwargs))
            if len(type(self).main_calls) == 1:
                return completion(call("update_requirements", {"intent": "knowledge"}, "candidate"),
                    call("query_catalog", {}, "actual-catalog"),
                    call("search_knowledge", {"query": api_plan.capitalize() + " 产品API支持范围", "consultation_item_ids": ["plan-api"]}, "actual-search"))
            if len(type(self).main_calls) == 2:
                actual = next(json.loads(message["content"])["data"] for message in reversed(kwargs["messages"])
                    if message["role"] == "tool" and "retrieval_id" in (json.loads(message["content"]).get("data") or {}))
                type(self).retrieval_id = actual["retrieval_id"]
                return completion(call("explain_knowledge", {"retrieval_id": actual["retrieval_id"],
                    "answerability": "supported", "consultation_item_ids": ["plan-api"],
                    "citations": [{"source_id": source.source_id, "quote": source.raw_text.split("\n", 1)[1].split("。")[0] + "。"}]}, "actual-explanation"))
            assert len(type(self).main_calls) == 3
            return completion()

    monkeypatch.setattr(module, "DeepSeekClient", Provider)
    response = asyncio.run(engine.chat(ChatRequest(session_id=sid, message=message)))
    assert_read_only_acceptance(engine, response, plans)
    assert response.status == "completed" and response.business.consultation_coverage.scope_complete
    units = response.business.consultation_coverage.items
    prices = [item for item in units if "prices" in item.source_item_ids]
    scopes = {identity: item for item in units for identity in item.source_item_ids if identity != "prices"}
    assert [plan for item in prices for plan in item.plan_ids] == list(plans)
    assert [unit.plan_id for item in prices for unit in item.unit_prices] == list(plans)
    assert all(item.tool_call_ids == ["actual-catalog"] for item in prices)
    assert scopes["plan-api"].plan_ids == [api_plan] and scopes["plan-api"].status == "completed"
    assert len(scopes["plan-api"].support) == 1 and scopes["plan-api"].support[0].supported is (api_plan == "team")
    assert scopes["plan-api"].retrieval_ids == [Provider.retrieval_id]
    assert scopes["plan-api"].source_ids == [source.source_id]
    assert [unit.plan_id for unit in response.business.unit_prices] == list(plans)
    assert response.business.unit_price is None
    assert [entry.price.plan_id for entry in response.business.unit_price_observations] == ["basic", "team", "business"]
    body = response.business.presentation.answer_markdown
    assert "80.00" in body and "120.00" in body
    if api_plan == "basic":
        assert "40.00" not in body and "基础版 Basic不支持产品 API" in body
        assert "基础版 Basic的订阅单价" not in body
    else:
        assert "40.00" in body and "团队版 Team支持产品 API" in body
    assert len(Provider.reviews) == 1
    same_history(engine, response, message)


def test_split_price_items_follow_explicit_scope_order_despite_reverse_real_queries(engine, monkeypatch):
    message = "只问Team和Basic的每席每月单价，其他套餐这次不问。"
    team = inquiry("team-price", message, ("team",))
    basic = inquiry("basic-price", message, ("basic",))
    provider = script(monkeypatch, [completion(call("update_requirements", {"intent": "unit_price",
        "consultations": [team, basic]}, "candidate"),
        call("query_catalog", {"plan_id": "basic"}, "actual-basic"),
        call("query_catalog", {"plan_id": "team"}, "actual-team")), completion()], review([team, basic]))
    response = asyncio.run(engine.chat(ChatRequest(message=message)))
    assert_read_only_acceptance(engine, response, ("team",))
    state = engine.work.get(response.session_id).state
    assert [(item.item_id, item.plan_ids) for item in state.turn_consultations] == [
        ("team-price", ("team",)), ("basic-price", ("basic",))]
    assert [item["status"] for item in state.consultation_feedback] == ["accepted", "accepted"]
    assert response.status == "completed" and not response.business.errors
    coverage = response.business.consultation_coverage
    assert coverage.scope_complete and coverage.status == "completed"
    assert [(item.source_item_ids[0], item.plan_ids, item.tool_call_ids) for item in coverage.items] == [
        ("team-price", ["team"], ["actual-team"]),
        ("basic-price", ["basic"], ["actual-basic"])]
    assert [price.plan_id for item in coverage.items for price in item.unit_prices] == ["team", "basic"]
    assert [price.plan_id for price in response.business.unit_prices] == ["team", "basic"]
    assert [entry.price.plan_id for entry in response.business.unit_price_observations] == ["basic", "team"]
    assert response.business.unit_price.plan_id == "team" and response.business.unit_price.unit_price_minor == 8000
    assert [record["tool_call_id"] for record in response.tool_results] == ["candidate", "actual-basic", "actual-team"]
    assert all(record["result"]["ok"] for record in response.tool_results)
    body = response.business.presentation.answer_markdown
    assert body.index("团队版 Team") < body.index("基础版 Basic")
    assert body.count("80.00") == body.count("40.00") == 1
    assert "120.00" not in body
    assert len(provider.reviews) == 1
    same_history(engine, response, message)
