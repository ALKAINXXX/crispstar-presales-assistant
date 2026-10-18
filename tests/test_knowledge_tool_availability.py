"""Tool-choice prerequisites; controlled models do not prove model behavior."""
import asyncio
import json

from app.config import Settings
from app.llm.deepseek_tool_contract import ToolContract
from app.schemas.chat import ChatRequest
from app.schemas.retrieval import RetrievalEvidence, RetrievalOptions
from app.services.work_state import WorkState
from app.tools.business import BusinessToolRegistry
from test_consultation_anchor_orchestrator import knowledge_provider, assert_knowledge_complete
from test_consultation_scope_convergence import item
from test_presales_orchestrator import engine


def names(registry):
    return {schema["function"]["name"] for schema in registry.available_schemas()}


def test_catalog_cannot_authorize_knowledge_explanation():
    state = WorkState(Settings(_env_file=None))
    registry = BusinessToolRegistry(state.settings, state, "Team支持API吗？")
    assert "explain_knowledge" not in names(registry)
    assert asyncio.run(registry.execute("query_catalog", {}))["ok"]
    assert "explain_knowledge" not in names(registry)
    forged = asyncio.run(registry.execute("explain_knowledge", {
        "retrieval_id": "retrieval_catalog_support", "citations": [], "answerability": "insufficient"}))
    assert forged["error"]["code"] == "knowledge_explanation_invalid"
    assert not registry.knowledge_answers


def test_actual_empty_receipt_can_explain_insufficiency_without_inventing_sources():
    # Controlled empty observation exercises the prerequisite gate only.
    state = WorkState(Settings(_env_file=None))
    observation = RetrievalEvidence(retrieval_id="empty-receipt", chain_id="controlled-chain",
        query="资料外问题", retrieved_at="2026-10-14T00:00:00Z", status="empty", options=RetrievalOptions())
    class EmptyKnowledge:
        chain_id = observation.chain_id
        def retrieve(self, query):
            assert query == observation.query
            return observation
        def evidence(self, identity):
            assert identity == observation.retrieval_id
            return observation
        def validate_citations(self, identity, sources):
            assert identity == observation.retrieval_id and sources == ()
            return ()
        def condition_conflict_ids(self, conditions):
            assert conditions == ()
            return ()
    state.knowledge = EmptyKnowledge()
    registry = BusinessToolRegistry(state.settings, state, observation.query)
    assert asyncio.run(registry.execute("search_knowledge", {"query": observation.query}))["ok"]
    assert "explain_knowledge" in names(registry)
    args = {"retrieval_id": observation.retrieval_id, "citations": [], "answerability": "insufficient"}
    assert asyncio.run(registry.execute("explain_knowledge", args))["ok"]
    # An already dispatched explanation remains schema-readable on later
    # strict requests; no disappearing-tool protocol or extra retry is needed.
    contract = ToolContract(registry.available_schemas())
    assert json.loads(contract.decode_arguments("explain_knowledge",
        contract.encode_arguments("explain_knowledge", args))) == args
    another_turn = BusinessToolRegistry(state.settings, state, observation.query)
    assert "explain_knowledge" not in names(another_turn)
    assert asyncio.run(another_turn.execute("explain_knowledge", args))["error"]["code"] == "knowledge_explanation_invalid"


def test_ordinary_orchestrator_exposes_explanation_after_real_search_receipts(engine, monkeypatch):
    # Controlled model, real native local retrieval and original citations.
    items = [item("api", "Team的API", ("team",), aspect="support", feature="api_access"),
        item("webhook", "Basic的Webhook", ("basic",), aspect="support", feature="webhook_notifications")]
    provider = knowledge_provider(monkeypatch, items)
    response = asyncio.run(engine.chat(ChatRequest(message="Team的API、Basic的Webhook分别支持吗？")))
    assert_knowledge_complete(response, [("support", ("team",), "api_access"),
        ("support", ("basic",), "webhook_notifications")])
    assert len(provider.sent) == 4 and len(provider.reviews) == 1
    exposed = [{s["function"]["name"] for s in request["tools"]} for request in provider.sent]
    assert all("explain_knowledge" not in batch for batch in exposed[:2])
    assert all("explain_knowledge" in batch for batch in exposed[2:])
    assert len(response.business.retrieval_evidence) == 2
    assert not response.business.requirements.items and response.business.proposal is None
