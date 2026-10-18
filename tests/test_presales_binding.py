"""Binding/projection boundaries with a labelled controlled retrieval worker.

WorkState, registry, owning KnowledgeService and fixed-index Source validation
remain real. This does not claim actual embeddings or model semantic accuracy.
"""

import asyncio
from copy import deepcopy
import json

import pytest

from app.config import Settings
from app.schemas.retrieval import RetrievalEvidence, RetrievalHit, RetrievalOptions
from app.services.knowledge import DEFAULT_INDEX_PATH, _read_index
from app.services.tool_display import tool_display_fields
from app.services.work_state import WorkState
from app.tools.business import BusinessToolRegistry


MESSAGE = "Team的产品API限流是多少？Basic的Webhook通知重试规则是什么？"


def consultation(identity, quote, plan, feature):
    return {"item_id": identity, "aspect": "detail", "plan_ids": [plan], "feature_id": feature,
        "topic": quote, "span": {"quote": quote}}


def execute(registry, name, arguments):
    return asyncio.run(registry.execute(name, arguments))


@pytest.fixture
def fixture(tmp_path, monkeypatch):
    settings = Settings(_env_file=None, saas_database_url=f"sqlite:///{tmp_path / 'binding.sqlite3'}")
    state = WorkState(settings)
    state.begin_review_turn()
    state.apply_review({"intent": "knowledge", "target": {"kind": "none", "relation": "detour"},
        "coverage_complete": True, "consultation_coverage_complete": True, "consultations": [
            consultation("api", "Team的产品API限流是多少", "team", "api_access"),
            consultation("hook", "Basic的Webhook通知重试规则是什么", "basic", "webhook_notifications")],
        "groups": []}, MESSAGE, {"intent": "knowledge"})
    index = _read_index(DEFAULT_INDEX_PATH)
    source = next(source for source in index.inputs.sources if source.section_id == "integration_api")
    observed = []

    def controlled_worker(request):
        observed.append(request["query"])
        return RetrievalEvidence(retrieval_id=request["retrieval_id"], chain_id=request["chain_id"],
            query=request["query"], retrieved_at=request["retrieved_at"],
            options=RetrievalOptions.model_validate(request["options"]), status="retrieved",
            hits=(RetrievalHit(rank=1, score=0.9, relevance="related", source=source),),
            product_id=index.inputs.product_id, dataset_version=index.inputs.dataset_version,
            index_build_id=index.build_id, index_inputs_sha256=index.inputs_sha256,
            embedding=index.inputs.embedding)

    monkeypatch.setattr(state.knowledge, "_execute", controlled_worker)
    registry = BusinessToolRegistry(settings, state, MESSAGE)
    return registry, source, observed


def search(registry, identities=("api",)):
    result = execute(registry, "search_knowledge", {"query": "产品API使用说明",
        "consultation_item_ids": list(identities)})
    assert result["ok"] and result["data"]["status"] == "retrieved"
    return result["data"]["retrieval_id"]


def explanation(identity, source, identities=("api",), quote=None):
    return {"retrieval_id": identity, "answerability": "supported",
        "consultation_item_ids": list(identities),
        "citations": [{"source_id": source.source_id, "quote": quote or source.raw_text}]}


@pytest.mark.parametrize("identities", [("missing",), ("api", "api"), ("api", "missing")])
def test_unknown_or_duplicate_search_scope_never_starts_retrieval(fixture, identities):
    registry, _, observed = fixture
    result = execute(registry, "search_knowledge", {"query": "PRIVATE_QUERY_SENTINEL",
        "consultation_item_ids": list(identities)})
    assert result["ok"] is False and observed == []
    assert result["error"]["code"] == "knowledge_search_scope_invalid"
    assert not registry.retrievals and not registry.retrieval_consultations
    shown = tool_display_fields("search_knowledge", json.dumps({"query": "PRIVATE_QUERY_SENTINEL",
        "consultation_item_ids": list(identities)}), result)
    assert shown["display_arguments_status"] == "rejected" and shown["display_arguments"] == {}
    assert "PRIVATE_QUERY_SENTINEL" not in json.dumps(shown)


@pytest.mark.parametrize("identities", [("hook",), ("api", "hook"), ("missing",), ("api", "api")])
def test_explanation_cannot_switch_or_expand_actual_search_scope(fixture, identities):
    registry, source, _ = fixture
    identity = search(registry, ("api",))
    result = execute(registry, "explain_knowledge", explanation(identity, source, identities))
    assert result["ok"] is False and registry.knowledge_answers == []
    assert result["error"]["code"] == "knowledge_explanation_invalid"
    assert registry.retrieval_consultations[identity] == ("api",)


def test_unbound_multi_item_search_cannot_be_relabelled_by_explanation(fixture):
    registry, source, _ = fixture
    identity = search(registry, ())
    assert registry.retrieval_consultations[identity] == ()
    rejected = execute(registry, "explain_knowledge", explanation(identity, source, ("api",)))
    assert rejected["ok"] is False and registry.knowledge_answers == []


def test_bound_search_allows_explicit_subset_and_does_not_complete_other_item(fixture):
    registry, source, _ = fixture
    identity = search(registry, ("api", "hook"))
    arguments = explanation(identity, source, ("api",))
    result = execute(registry, "explain_knowledge", arguments)
    assert result["ok"] and result["data"]["consultation_item_ids"] == ["api"]
    assert registry.retrieval_consultations[identity] == ("api", "hook")
    assert registry.knowledge_answers == [result["data"]]
    shown = tool_display_fields("explain_knowledge", json.dumps(arguments, ensure_ascii=False), result)
    assert shown["display_arguments_status"] == "accepted"
    assert shown["display_arguments"]["consultation_item_ids"] == ["api"]
    assert shown["display_result"] == result
    assert shown["display_result"]["data"]["citations"][0]["quote"] == source.raw_text


@pytest.mark.parametrize("quote", ["不是资料原文", "产品 API……集成", "产品  API"])
def test_binding_does_not_relax_exact_continuous_source_quote(fixture, quote):
    registry, source, _ = fixture
    identity = search(registry)
    assert quote not in source.raw_text
    result = execute(registry, "explain_knowledge", explanation(identity, source, quote=quote))
    assert result["ok"] is False and registry.knowledge_answers == []
    assert result["error"]["code"] == "knowledge_explanation_invalid"


def test_valid_current_source_id_cannot_borrow_a_different_turn_search(fixture):
    registry, source, _ = fixture
    identity = search(registry)
    next_turn = BusinessToolRegistry(registry.settings, registry.state, "接着说明")
    result = execute(next_turn, "explain_knowledge", explanation(identity, source))
    assert result["ok"] is False and next_turn.knowledge_answers == []


def test_search_and_explanation_projection_keep_actual_result_and_original_legacy_fields(fixture):
    registry, source, _ = fixture
    arguments = {"query": "产品API使用说明", "consultation_item_ids": ["api"]}
    result = execute(registry, "search_knowledge", arguments)
    original = deepcopy(result)
    shown = tool_display_fields("search_knowledge", json.dumps(arguments), result)
    assert shown["display_name"] == "search_knowledge"
    assert shown["display_arguments_status"] == "accepted" and shown["display_arguments"] == arguments
    assert shown["display_result"] == result == original
    assert shown["display_result"]["data"]["hits"][0]["source"]["relative_path"] == source.relative_path
    shown["display_result"]["data"]["query"] = "changed presentation copy"
    assert result == original


def test_review_projection_uses_accepted_consultations_instead_of_bad_first_candidate(fixture):
    registry, _, _ = fixture
    accepted = registry.state.prompt_state(recheck_current=False)
    raw_candidate = {"intent": "selection", "add": [{"statement": "PRIVATE_WRONG_TASK",
        "user_quote": "Team的产品API限流是多少", "feature_id": "api_access"}]}
    shown = tool_display_fields("update_requirements", json.dumps(raw_candidate), {"ok": True, "data": accepted})
    assert shown["display_arguments_status"] == "accepted" and shown["display_arguments"] == {}
    data = shown["display_result"]["data"]
    assert [item["item_id"] for item in data["turn_consultations"]] == ["api", "hook"]
    assert data["consultation_complete"] is True and data["requirements"]["items"] == []
    assert data["intent"] == "knowledge" and "PRIVATE_WRONG_TASK" not in json.dumps(shown)


def two_segments(source):
    # Both segments are separate, complete, unchanged original sentences.
    sentences = source.raw_text.split("\n", 1)[1].split("。")
    return [{"source_id": source.source_id, "quote": sentence + "。"}
        for sentence in sentences[:2]]


def test_same_source_distinct_continuous_segments_validate_unique_source_once(fixture, monkeypatch):
    registry, source, _ = fixture
    identity = search(registry, ("api", "hook"))
    validate = registry.state.knowledge.validate_citations
    checked = []

    def tracked(retrieval_id, source_ids):
        checked.append((retrieval_id, source_ids))
        return validate(retrieval_id, source_ids)

    monkeypatch.setattr(registry.state.knowledge, "validate_citations", tracked)
    arguments = explanation(identity, source, ("api", "hook"))
    arguments["citations"] = two_segments(source)
    result = execute(registry, "explain_knowledge", arguments)
    assert result["ok"] and result["data"]["citations"] == arguments["citations"]
    assert checked == [(identity, (source.source_id,))]
    assert result["data"]["consultation_item_ids"] == ["api", "hook"]
    shown = tool_display_fields("explain_knowledge", json.dumps(arguments, ensure_ascii=False), result)
    assert shown["display_arguments_status"] == "accepted"
    assert shown["display_arguments"]["citations"] == arguments["citations"]
    assert shown["display_result"]["data"]["citations"] == arguments["citations"]


def test_same_source_exact_duplicate_segment_dedupes_without_losing_bindings(fixture):
    registry, source, _ = fixture
    identity = search(registry, ("api", "hook"))
    segments = two_segments(source)
    arguments = explanation(identity, source, ("api", "hook"))
    arguments["citations"] = [segments[0], segments[1], segments[0]]
    result = execute(registry, "explain_knowledge", arguments)
    assert result["ok"] and result["data"]["citations"] == segments
    assert result["data"]["consultation_item_ids"] == ["api", "hook"]
    shown = tool_display_fields("explain_knowledge", json.dumps(arguments, ensure_ascii=False), result)
    assert shown["display_arguments_status"] == "accepted"
    assert shown["display_arguments"]["citations"] == segments
    assert shown["display_arguments"]["consultation_item_ids"] == ["api", "hook"]


@pytest.mark.parametrize("defect", ["discontinuous", "changed_quote", "foreign_source", "too_many", "too_long"])
def test_one_invalid_segment_rejects_whole_explanation_with_specific_nonpurchase_error(fixture, defect):
    registry, source, _ = fixture
    identity = search(registry)
    arguments = explanation(identity, source)
    arguments["citations"] = two_segments(source)
    if defect == "discontinuous":
        arguments["citations"][1]["quote"] = "本版目录……同步行为。"
    elif defect == "changed_quote":
        arguments["citations"][1]["quote"] += "PRIVATE_INVENTED_SUFFIX"
    elif defect == "foreign_source":
        arguments["citations"][1]["source_id"] = "PRIVATE_FOREIGN_SOURCE"
    elif defect == "too_many":
        arguments["citations"] = [arguments["citations"][0]] * 5
    elif defect == "too_long":
        arguments["citations"][1]["quote"] = "x" * 1001
    result = execute(registry, "explain_knowledge", arguments)
    assert result["ok"] is False and registry.knowledge_answers == []
    assert result["error"]["code"] == "knowledge_explanation_invalid"
    shown = tool_display_fields("explain_knowledge", json.dumps(arguments, ensure_ascii=False), result)
    assert shown["display_arguments_status"] == "rejected" and shown["display_arguments"] == {}
    assert "details" not in shown["display_result"]["error"] and "retry_action" not in shown["display_result"]["error"]
    assert "PRIVATE_" not in json.dumps(shown) and "购买" not in shown["display_result"]["error"]["message"]


@pytest.mark.parametrize("defect", ["substituted_original", "missing_unique_source", "wrong_chain", "low_relevance"])
def test_same_source_segments_still_require_real_original_chain_and_related_hit(fixture, monkeypatch, defect):
    registry, source, _ = fixture
    identity = search(registry)
    arguments = explanation(identity, source)
    arguments["citations"] = two_segments(source)
    if defect == "substituted_original":
        monkeypatch.setattr(registry.state.knowledge, "validate_citations", lambda *args:
            (source.model_copy(update={"raw_text": source.raw_text + "伪造后缀"}),))
    elif defect == "missing_unique_source":
        monkeypatch.setattr(registry.state.knowledge, "validate_citations", lambda *args: ())
    elif defect == "wrong_chain":
        registry.retrievals[identity] = registry.retrievals[identity].model_copy(update={"chain_id": "foreign"})
    else:
        original = registry.retrievals[identity]
        registry.retrievals[identity] = original.model_copy(update={"hits": tuple(hit.model_copy(update={
            "relevance": "low", "score": 0.1}) for hit in original.hits)})
    result = execute(registry, "explain_knowledge", arguments)
    assert result["ok"] is False and registry.knowledge_answers == []
    assert result["error"]["code"] == ("knowledge_explanation_invalid" if defect == "low_relevance" else "invalid_tool_result")


def test_same_source_segments_keep_known_condition_conflict_blocking(tmp_path, monkeypatch):
    """Controlled fixed-index SSO recall; actual conflict authority is retained."""
    from test_proposals import worker

    settings = Settings(_env_file=None, saas_database_url=f"sqlite:///{tmp_path / 'sso-binding.sqlite3'}")
    state = WorkState(settings)
    original = "Team支持SSO吗？Business使用SSO需要什么前提？"
    state.begin_review_turn()
    state.apply_review({"intent": "knowledge", "target": {"kind": "none", "relation": "detour"},
        "coverage_complete": True, "consultation_coverage_complete": True, "groups": [],
        "consultations": [{"item_id": "team-sso", "aspect": "support", "plan_ids": ["team"],
            "feature_id": "saml_sso", "topic": "Team是否支持SSO", "span": {"quote": "Team支持SSO吗"}},
            {"item_id": "rule", "aspect": "prerequisite", "plan_ids": ["business"],
            "feature_id": "saml_sso", "topic": "SSO前提", "span": {"quote": "Business使用SSO需要什么前提"}}]},
        original, {"intent": "knowledge"})
    source = worker(monkeypatch, state)
    registry = BusinessToolRegistry(settings, state, original)
    actual = execute(registry, "search_knowledge", {"query": "SSO前提", "consultation_item_ids": ["rule"]})
    assert actual["ok"] and actual["data"]["status"] == "retrieved"
    identity = actual["data"]["retrieval_id"]
    state.knowledge.record_condition_conflict(identity, condition_id=source.condition_ids[0],
        source_ids=(source.source_id,), reason="controlled retained condition conflict", origin="controlled_test")
    arguments = explanation(identity, source, ("rule",))
    arguments["citations"] = two_segments(source)
    result = execute(registry, "explain_knowledge", arguments)
    assert result["ok"] is False and registry.knowledge_answers == []
    assert result["error"]["code"] == "evidence_conflict"
    assert result["error"]["retrieval_ids"] == [identity]


def test_single_scope_inferred_binding_is_projected_as_actual_accepted_item(fixture):
    registry, source, _ = fixture
    registry.state.turn_consultations = registry.state.turn_consultations[:1]
    identity = search(registry, ())
    arguments = explanation(identity, source, ())
    arguments["citations"] = two_segments(source)
    result = execute(registry, "explain_knowledge", arguments)
    assert result["ok"] and result["data"]["consultation_item_ids"] == ["api"]
    shown = tool_display_fields("explain_knowledge", json.dumps(arguments), result)
    assert shown["display_arguments_status"] == "accepted"
    assert shown["display_arguments"]["consultation_item_ids"] == ["api"]


def install_normalized_binding_fixture(registry):
    """Exercise the downstream certified mapping, not intake language rules."""
    original = registry.state.turn_consultations[0]
    registry.state.turn_consultations = (original.model_copy(update={"plan_ids": ("team", "basic")}),
                                       registry.state.turn_consultations[1])
    registry.state.consultation_scope_reviewed = True
    registry.state.consultation_questions = (
        {"unit_id": "q-api:team", "question_id": "q-api", "order": 0, "status": "accepted",
         "aspect": "detail", "plan_ids": ["team"], "feature_id": "api_access", "source_item_ids": ["api"]},
        {"unit_id": "q-api:basic", "question_id": "q-api", "order": 1, "status": "accepted",
         "aspect": "detail", "plan_ids": ["basic"], "feature_id": "api_access", "source_item_ids": ["api"]},
        {"unit_id": "q-hook:basic", "question_id": "q-hook", "order": 2, "status": "accepted",
         "aspect": "detail", "plan_ids": ["basic"], "feature_id": "webhook_notifications", "source_item_ids": ["hook"]})


def test_normalized_unit_subset_of_original_search_preserves_precise_association(fixture):
    registry, source, _ = fixture
    install_normalized_binding_fixture(registry)
    identity = search(registry, ("api",))
    result = execute(registry, "explain_knowledge", explanation(identity, source, ("q-api:basic",)))
    assert result["ok"]
    assert result["data"]["consultation_item_ids"] == ["q-api:basic"]
    assert result["data"]["consultation_unit_ids"] == ["q-api:basic"]
    assert registry.retrieval_consultations[identity] == ("api",)
    assert registry.retrieval_consultation_units[identity] == ("q-api:team", "q-api:basic")


@pytest.mark.parametrize("identities", [("api",), ("q-api:team",), ("q-hook:basic",)])
def test_normalized_search_subset_cannot_expand_through_original_or_other_question(fixture, identities):
    registry, source, _ = fixture
    install_normalized_binding_fixture(registry)
    identity = search(registry, ("q-api:basic",))
    result = execute(registry, "explain_knowledge", explanation(identity, source, identities))
    assert not result["ok"] and not registry.knowledge_answers
    assert result["error"]["code"] == "knowledge_explanation_invalid"


def test_normalized_unit_is_invalid_when_original_relation_is_incompatible(fixture):
    registry, _, observed = fixture
    install_normalized_binding_fixture(registry)
    registry.state.consultation_questions += ({"unit_id": "bad-borrowed", "question_id": "q-bad",
        "order": 3, "status": "accepted", "aspect": "support", "plan_ids": ["team"],
        "feature_id": "webhook_notifications", "source_item_ids": ["api"]},)
    result = execute(registry, "search_knowledge", {"query": "无效绑定不得执行",
        "consultation_item_ids": ["bad-borrowed"]})
    assert not result["ok"] and observed == []
    assert result["error"]["code"] == "knowledge_search_scope_invalid"


@pytest.mark.parametrize("status", ["missing", "ambiguous", "blocked", "empty_scope"])
def test_nonaccepted_normalized_unit_cannot_become_tool_identity(fixture, status):
    registry, _, observed = fixture
    install_normalized_binding_fixture(registry)
    registry.state.consultation_questions += ({"unit_id": "not-accepted", "question_id": "q-pending",
        "order": 3, "status": status, "aspect": "detail", "plan_ids": ["team"],
        "feature_id": "api_access", "source_item_ids": ["api"]},)
    result = execute(registry, "search_knowledge", {"query": "无效绑定不得执行",
        "consultation_item_ids": ["not-accepted"]})
    assert not result["ok"] and observed == []
