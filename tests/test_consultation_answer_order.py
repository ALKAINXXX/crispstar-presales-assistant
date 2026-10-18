"""Canonical answer order with actual current sources and private SQLite.

Controlled providers exercise the production verifier and ordinary tools;
these checks do not claim real-model success.
"""
import asyncio
from copy import deepcopy
import json

import pytest

from app.agents import orchestrator as module
from app.llm.tool_protocol import validate_tool_message_sequence
from app.schemas.chat import BusinessResponse, ChatRequest, ConsultationCoverage
from app.schemas.matching import RequirementSet
from app.schemas.retrieval import RetrievalEvidence, RetrievalHit
from app.services.catalog import get_catalog
from app.services.chat_render import organize_business
from app.services.knowledge import prepare_corpus
from app.services.presales import TurnConsultationResults
from controlled_review import is_review_request, review_completion
from test_consultation_scope_convergence import apply, item
from test_presales_orchestrator import call, completion, engine, review, same_history
from test_presales_results import search_record


FIXED_B = "Business的SSO是什么，有什么使用前提？"
REVERSE_H2 = "企业版Business的SAML SSO需要什么前提，以及这项功能是什么意思？"
QUOTE_PREFIX = "根据本次实际检索到的产品资料，原文说明："
MEANING = "SAML SSO 表示采用 SAML 的单点登录能力"
RULE = "企业版 Business的 SAML SSO 使用前提："
RULE_QUOTE = "本节适用于企业版 Business 的 SAML SSO 需求"


def inquiries(message):
    # Candidate packing deliberately does not determine answer order.
    return [item("meaning", message, ("business",), aspect="meaning", feature="saml_sso"),
        item("rule", message, ("business",), aspect="prerequisite", feature="saml_sso")]


def sources():
    actual = prepare_corpus().sources
    return [next(s for s in actual if s.section_id == section)
        for section in ("sso_concept", "sso_prerequisite")]


def evidence(identity, actual):
    return RetrievalEvidence(retrieval_id=identity, chain_id="answer-order-isolated",
        query="SAML SSO 含义 使用前提 兼容身份提供方", retrieved_at="2026-10-09T00:00:00+00:00",
        options={}, status="retrieved", hits=tuple(RetrievalHit(rank=n + 1, score=.8,
            relevance="related", source=s) for n, s in enumerate(actual)))


def explained(observation, identities, units, actual):
    return {"retrieval_id": observation.retrieval_id, "query": observation.query,
        "consultation_item_ids": identities, "consultation_unit_ids": units,
        "answerability": "supported", "citations": [
            {"source_id": s.source_id, "quote": s.raw_text} for s in actual]}


def replay(message, packing, *, omit_meaning=False):
    submitted = inquiries(message)
    if omit_meaning:
        submitted = [submitted[1]]
    state = apply(message, submitted, intent="knowledge")
    actual = sources()
    units = {q["aspect"]: q["unit_id"] for q in state.consultation_questions}
    if omit_meaning:
        observations = [evidence("rule-search", [actual[1]])]
        answers = [explained(observations[0], ["rule"], [units["prerequisite"]], [actual[1]])]
        records = [search_record(observations[0], ["rule"])]
    elif packing.startswith("shared"):
        observations = [evidence("shared-search", actual)]
        adopted = list(reversed(actual)) if packing == "shared_reversed" else actual
        answers = [explained(observations[0], ["meaning", "rule"], list(units.values()), adopted)]
        records = [search_record(observations[0], ["meaning", "rule"])]
    else:
        observations = [evidence("meaning-search", [actual[0]]), evidence("rule-search", [actual[1]])]
        answers = [explained(observations[1], ["rule"], [units["prerequisite"]], [actual[1]]),
            explained(observations[0], ["meaning"], [units["meaning"]], [actual[0]])]
        records = [search_record(observations[0], ["meaning"]), search_record(observations[1], ["rule"])]
    results = TurnConsultationResults()
    results.add_catalog(get_catalog(), "actual-catalog")
    coverage = results.coverage(state.turn_consultations, questions=state.consultation_questions,
        scope_complete=state.consultation_complete, knowledge_answers=answers,
        evidence=observations, records=records)
    business = BusinessResponse(turn_id="answer-order-turn", intent="knowledge",
        requirements=RequirementSet(requirement_id="unchanged"), consultation_coverage=coverage,
        retrieval_evidence=observations, knowledge_answers=answers)
    return business


def assert_order(text, message):
    positions = [text.index(MEANING), text.index(RULE)]
    assert (positions[0] < positions[1]) == (message == FIXED_B), text
    assert (text.index(MEANING) < text.index(RULE_QUOTE)) == (message == FIXED_B), text


@pytest.mark.parametrize("message", [FIXED_B, REVERSE_H2])
@pytest.mark.parametrize("packing", ["shared", "shared_reversed", "split_reversed"])
def test_actual_quotes_follow_canonical_aspects_not_execution_or_citation_order(message, packing):
    business = replay(message, packing)
    assert [i.aspect for i in business.consultation_coverage.items] == (
        ["meaning", "prerequisite"] if message == FIXED_B else ["prerequisite", "meaning"])
    before = business.model_dump(mode="json")
    presentation, formal = organize_business(business, "completed")
    assert_order(presentation.answer_markdown, message)
    assert_order(formal, message)
    # Source adoption order and all actual receipts retain their old contract.
    adopted = list(dict.fromkeys(c["source_id"] for a in business.knowledge_answers for c in a["citations"]))
    assert presentation.used_source_ids == adopted
    assert not presentation.show_comparison and not presentation.show_current_result
    assert business.model_dump(mode="json") == before
    assert QUOTE_PREFIX in presentation.answer_markdown
    if packing.startswith("shared"):
        assert presentation.answer_markdown.count(QUOTE_PREFIX) == 1
    assert all("> " + c["quote"].replace("\n", "\n> ") in presentation.answer_markdown
        for a in business.knowledge_answers for c in a["citations"])


def test_missing_meaning_keeps_its_failure_before_the_independent_real_prerequisite():
    business = replay(FIXED_B, "shared", omit_meaning=True)
    assert [i.stage for i in business.consultation_coverage.items] == ["extraction_incomplete", "answered"]
    presentation, _ = organize_business(business, "incomplete")
    assert presentation.answer_markdown.index("本次范围核对未完成") < presentation.answer_markdown.index(RULE)
    assert MEANING not in presentation.answer_markdown
    assert presentation.used_source_ids == [sources()[1].source_id]


def test_genuine_legacy_quotes_keep_the_existing_template_and_completion_order():
    business = replay(FIXED_B, "shared_reversed").model_copy(update={"consultation_coverage": None})
    presentation, _ = organize_business(business, "completed")
    expected = "\n\n".join([QUOTE_PREFIX, *("> " + s.raw_text.replace("\n", "\n> ")
        for s in reversed(sources()))])
    assert presentation.answer_markdown == expected
    assert presentation.used_source_ids == [s.source_id for s in reversed(sources())]


def test_current_empty_inventory_does_not_invent_consultation_or_drop_actual_quotes():
    business = replay(FIXED_B, "shared_reversed").model_copy(update={
        "consultation_coverage": ConsultationCoverage(items=[])})
    presentation, _ = organize_business(business, "completed")
    assert RULE not in presentation.answer_markdown
    assert MEANING in presentation.answer_markdown
    assert presentation.used_source_ids == [s.source_id for s in reversed(sources())]
    assert "当前套餐：" not in presentation.answer_markdown


@pytest.mark.parametrize("message", [FIXED_B, REVERSE_H2])
def test_shared_real_search_and_explanation_follow_both_original_question_orders(engine, monkeypatch, message):
    submitted = inquiries(message)

    class Provider:
        sent, reviews = [], []

        def __init__(self, settings):
            pass

        async def chat(self, **kwargs):
            validate_tool_message_sequence(kwargs["messages"])
            if is_review_request(kwargs):
                type(self).reviews.append(deepcopy(kwargs))
                assert len(type(self).reviews) == 1
                return review_completion(review(submitted, intent="knowledge"))
            type(self).sent.append(deepcopy(kwargs))
            phase = len(type(self).sent)
            if phase == 1:
                return completion(call("update_requirements", {"intent": "knowledge", "consultations": submitted}, "candidate"),
                    call("query_catalog", {}, "actual-catalog"))
            if phase == 2:
                return completion(call("search_knowledge", {"query": "SAML SSO 含义 使用前提 兼容身份提供方",
                    "consultation_item_ids": ["meaning", "rule"]}, "actual-shared-search"))
            if phase == 3:
                result = next(json.loads(m["content"]) for m in kwargs["messages"]
                    if m["role"] == "tool" and m.get("tool_call_id") == "actual-shared-search")
                assert result["ok"] and result["data"]["status"] == "retrieved"
                actual = [next(h["source"] for h in result["data"]["hits"]
                    if h["relevance"] == "related" and h["source"]["section_id"] == section)
                    for section in ("sso_concept", "sso_prerequisite")]
                return completion(call("explain_knowledge", {"retrieval_id": result["data"]["retrieval_id"],
                    "consultation_item_ids": ["meaning", "rule"], "answerability": "supported",
                    "citations": [{"source_id": s["source_id"], "quote": s["raw_text"]} for s in actual]}, "actual-shared-explanation"))
            assert phase == 4
            return completion()

    monkeypatch.setattr(module, "DeepSeekClient", Provider)
    response = asyncio.run(engine.chat(ChatRequest(message=message)))
    assert response.status == "completed", response.message
    assert len(Provider.reviews) == 1
    coverage = response.business.consultation_coverage
    assert [i.aspect for i in coverage.items] == (["meaning", "prerequisite"]
        if message == FIXED_B else ["prerequisite", "meaning"])
    assert all(i.status == "completed" and i.retrieval_ids and i.source_ids for i in coverage.items)
    assert len(response.business.knowledge_answers) == 1
    accepted = response.business.knowledge_answers[0]
    assert set(accepted["consultation_unit_ids"]) == {i.item_id for i in coverage.items}
    assert_order(response.business.presentation.answer_markdown, message)
    assert_order(response.message, message)
    assert response.business.presentation.answer_markdown.count(QUOTE_PREFIX) == 1
    assert response.business.presentation.used_source_ids == [c["source_id"] for c in accepted["citations"]]
    assert all(r["result"]["ok"] for r in response.tool_results)
    assert not response.business.presentation.show_comparison and not response.business.presentation.show_current_result
    assert response.business.current_task is None and response.business.proposal is None and response.business.pricing is None
    same_history(engine, response, message)
