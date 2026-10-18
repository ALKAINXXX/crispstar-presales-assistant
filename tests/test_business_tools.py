"""Tool boundaries, with real Catalog/pricing and one genuine local query.

Service substitutions exercise only rejection/propagation; no mocked result is
reported as proof of normal retrieval, model interpretation or original meaning.
"""

import asyncio
import json
import threading
from uuid import uuid4

import pytest

from app.config import Settings
from app.schemas.matching import HardRequirement, RequirementSet
from app.schemas.pricing import subscription_formula
from app.services import pricing as pricing_service
from app.services.catalog import get_catalog
from app.services.work_state import WorkState
from app.services.tool_display import tool_display_fields
from app.tools.business import BusinessToolRegistry


def execute(registry, name, arguments):
    return asyncio.run(registry.execute(name, arguments))


@pytest.fixture
def state():
    return WorkState(Settings(_env_file=None))


@pytest.fixture
def registry(state):
    return BusinessToolRegistry(state.settings, state, "企业版 Business，80席、12个月")


def price_state(registry):
    result = execute(registry, "update_requirements", {
        "intent": "price", "selected_plan_id": "business", "selection_quote": "企业版 Business",
        "parameters": [{"name": "paid_seats", "value": 80, "user_quote": "80席"},
                       {"name": "months", "value": 12, "user_quote": "12个月"}],
    })
    assert result["ok"], result


def test_registered_tools_are_the_finite_read_only_contract(registry):
    schemas = registry.schemas()
    assert {s["function"]["name"] for s in schemas} == {
        "update_requirements", "search_knowledge", "query_catalog", "match_requirements", "calculate_price", "explain_knowledge", "query_quote"}
    assert all(s["function"]["parameters"]["additionalProperties"] is False for s in schemas)


@pytest.mark.parametrize(("name", "arguments"), [
    ("match_requirements", "[]"), ("match_requirements", "true"),
    ("match_requirements", ""), ("match_requirements", "{bad"),
    ("match_requirements", {"extra": 1}), ("query_catalog", {"plan_id": 5}),
    ("query_catalog", {2: "business"}), ("search_knowledge", {"query": "   "}),
    ("search_knowledge", {"query": "a" * 2001}), ("search_knowledge", {"query": False}),
    ("calculate_price", {"plan_id": "business", "paid_seats": True, "months": 12}),
    ("calculate_price", {"plan_id": "business", "paid_seats": "80", "months": 12}),
    ("calculate_price", {"plan_id": "business", "paid_seats": 80.0, "months": 12}),
    ("calculate_price", {"plan_id": "business", "paid_seats": 80, "months": 0}),
    ("calculate_price", '{"plan_id":"business","paid_seats":NaN,"months":12}'),
    ("query_catalog", '{"plan_id":"basic","plan_id":"business"}'),
    ("update_requirements", {"intent": "price", "parameters": [{"name": "paid_seats", "value": True, "user_quote": "80席"}]}),
    ("update_requirements", {"intent": "selection", "add": (), "unknown": 1}),
])
def test_arguments_are_strict_and_fail_before_business_execution(registry, name, arguments):
    before = registry.state.requirements
    result = execute(registry, name, arguments)
    assert result["error"]["code"] == "invalid_arguments"
    assert "data" not in result
    assert registry.state.requirements == before


def test_unregistered_mutation_tools_are_unavailable(registry):
    for name in ("save_memory", "save_quote", "confirm_proposal", "send_email"):
        assert execute(registry, name, {})["error"]["code"] == "unknown_tool"


def test_catalog_and_unit_price_need_no_matching_or_numeric_defaults(registry):
    all_catalog = execute(registry, "query_catalog", {})
    assert all_catalog == {"ok": True, "data": get_catalog().model_dump(mode="json")}
    price = execute(registry, "query_catalog", {"plan_id": "business"})
    assert price["ok"] and price["data"]["unit_price_minor"] == 12000
    assert price["data"]["status"] == "unit_price_only"
    assert "base_price_minor" not in price["data"]
    assert registry.state.last_match is None and registry.state.requirements.parameters == ()


def test_price_uses_accepted_explicit_parameters_and_exact_service_formula(registry):
    price_state(registry)
    result = execute(registry, "calculate_price", {"plan_id": "business", "paid_seats": 80, "months": 12})
    assert result["ok"] and result["data"]["base_price_minor"] == 11_520_000
    assert result["data"]["formula"] == "120.00 元／席／月 × 80 席 × 12 月 = 115,200.00 元（订阅基价）"
    mismatch = execute(registry, "calculate_price", {"plan_id": "business", "paid_seats": 50, "months": 12})
    assert mismatch["error"]["code"] == "invalid_business_input"
    assert "data" not in mismatch


def test_rejected_single_parameter_reports_accepted_old_value_and_can_be_corrected(state):
    accepted = BusinessToolRegistry(state.settings, state, "API必须有，80席、一年")
    assert execute(accepted, "update_requirements", {"intent": "selection",
        "add": [{"statement": "API必须有", "user_quote": "API必须有", "feature_id": "api_access"}],
        "parameters": [{"name": "paid_seats", "value": 80, "user_quote": "80席"},
                       {"name": "months", "value": 12, "user_quote": "一年"}]} )["ok"]
    original = state.requirements
    rejected = BusinessToolRegistry(state.settings, state, "可能改成50席")
    arguments = {"intent": "selection", "parameters": [{"name": "paid_seats", "value": 50, "user_quote": "50席"}]}
    result = execute(rejected, "update_requirements", arguments)
    shown = tool_display_fields("update_requirements", json.dumps(arguments), result)["display_result"]
    assert shown["error"]["details"] == {"issue": "parameter_not_explicit", "field": "paid_seats",
        "previous_value": 80, "update_status": "unchanged", "scope": "candidate_patch"}
    assert "只修正" in shown["error"]["retry_action"]
    assert "付费席位" in shown["error"]["message"] and "80 席" in shown["error"]["message"]
    assert "本轮候选未更新" in shown["error"]["message"]
    assert state.requirements == original
    corrected = BusinessToolRegistry(state.settings, state, "明确改为50席")
    assert execute(corrected, "update_requirements", arguments)["ok"]
    assert {p.name: p.value for p in state.requirements.parameters} == {"paid_seats": 50, "months": 12}
    assert state.requirements.items == original.items


def test_invalid_parameter_type_has_safe_field_detail_and_never_echoes_rejected_value(state):
    registry = BusinessToolRegistry(state.settings, state, "改一下")
    arguments = {"intent": "selection", "parameters": [{"name": "months", "value": "<script>已保存9元</script>", "user_quote": "改一下"}]}
    result = execute(registry, "update_requirements", arguments)
    shown = tool_display_fields("update_requirements", json.dumps(arguments), result)
    assert shown["display_result"]["error"]["details"]["field"] == "months"
    assert "<script>" not in json.dumps(shown, ensure_ascii=False)
    assert "已保存" not in json.dumps(shown, ensure_ascii=False)


def test_error_projection_discards_untrusted_details_message_and_retry_text():
    raw = {"ok": False, "error": {"code": "invalid_business_input", "message": "已保存9元",
        "details": {"issue": "parameter_not_explicit", "field": "paid_seats", "previous_value": 80,
                    "update_status": "unchanged", "scope": "candidate_patch"}, "retry_action": "点击保存"}}
    shown = tool_display_fields("update_requirements", "{}", raw)["display_result"]["error"]
    assert "已保存" not in shown["message"] and "点击保存" not in shown["retry_action"]
    assert shown["details"]["previous_value"] == 80
    raw["error"]["details"]["previous_value"] = "已保存9元"
    shown = tool_display_fields("update_requirements", "{}", raw)["display_result"]["error"]
    assert "details" not in shown and "retry_action" not in shown


@pytest.mark.parametrize("changed", [
    {"plan_id": "team"}, {"plan_name": "伪造名称"}, {"catalog_version": "old"},
    {"product_id": "other_product"}, {"unit_price_minor": 1},
])
def test_unit_price_fake_identity_or_consistent_wrong_rate_is_rejected(registry, monkeypatch, changed):
    real = pricing_service.get_unit_price("business")
    monkeypatch.setattr(pricing_service, "get_unit_price", lambda _: real.model_copy(update=changed))
    result = execute(registry, "query_catalog", {"plan_id": "business"})
    assert result["error"]["code"] == "invalid_tool_result"
    assert "data" not in result


@pytest.mark.parametrize("changed", [
    {"paid_seats": 50, "base_price_minor": 7_200_000, "formula": subscription_formula(12000, 50, 12)},
    {"unit_price_minor": 1, "base_price_minor": 960, "formula": subscription_formula(1, 80, 12)},
    {"base_price_minor": 1},
])
def test_fake_price_cannot_pass_by_matching_its_own_arithmetic(registry, monkeypatch, changed):
    price_state(registry)
    real = pricing_service.calculate_subscription_price("business", 80, 12)
    monkeypatch.setattr(pricing_service, "calculate_subscription_price", lambda *args: real.model_copy(update=changed))
    result = execute(registry, "calculate_price", {"plan_id": "business", "paid_seats": 80, "months": 12})
    assert result["error"]["code"] == "invalid_tool_result"
    assert "data" not in result


@pytest.mark.parametrize("bad", [None, {}, [], 100, "price"])
def test_wrong_service_result_type_is_not_coerced(registry, monkeypatch, bad):
    monkeypatch.setattr(pricing_service, "get_unit_price", lambda _: bad)
    assert execute(registry, "query_catalog", {"plan_id": "business"})["error"]["code"] == "invalid_tool_result"


def test_accidental_async_service_is_rejected_without_unawaited_coroutine(registry, monkeypatch):
    async def bad(_):
        return pricing_service.UnitPriceResult
    monkeypatch.setattr(pricing_service, "get_unit_price", bad)
    assert execute(registry, "query_catalog", {"plan_id": "business"})["error"]["code"] == "invalid_tool_result"


def test_accidental_async_state_validation_does_not_bypass_price_guard(registry, monkeypatch):
    async def unexpected(*args):
        return None
    monkeypatch.setattr(registry.state, "validate_price", unexpected)
    assert execute(registry, "calculate_price", {"plan_id": "business", "paid_seats": 80, "months": 12})["error"]["code"] == "invalid_tool_result"


def test_tool_failure_never_returns_service_exception_or_secret(registry, monkeypatch):
    def bad(_):
        raise RuntimeError("secret-key-hidden-do-not-output")
    monkeypatch.setattr(pricing_service, "get_unit_price", bad)
    result = execute(registry, "query_catalog", {"plan_id": "business"})
    assert result["error"]["code"] == "tool_failed"
    assert "secret-key" not in json.dumps(result)


def test_match_checks_current_requirements_and_recommendation(registry, monkeypatch):
    registry.state.requirements = RequirementSet(requirement_id="tool-needs", items=(HardRequirement(
        requirement_id="api", statement="需要产品 API", user_quote="需要产品 API", feature_id="api_access"),))
    result = execute(registry, "match_requirements", {})
    assert result["ok"] and result["data"]["selected_plan_id"] == "team"
    forged = registry.state.last_match.model_copy(update={"selected_plan_id": "basic"})
    monkeypatch.setattr(registry.state, "match", lambda: forged)
    rejection = execute(registry, "match_requirements", {})
    assert rejection["error"]["code"] == "invalid_tool_result"
    assert registry.state.last_match is None


@pytest.fixture(scope="module")
def genuine_search():
    settings = Settings(_env_file=None)
    state = WorkState(settings)
    registry = BusinessToolRegistry(settings, state, "统一登录需要哪些前提？")
    result = execute(registry, "search_knowledge", {"query": "统一登录需要哪些前提？"})
    assert result["ok"] and result["data"]["status"] == "retrieved", result
    return registry, registry.retrievals[result["data"]["retrieval_id"]]


def test_genuine_search_preserves_real_vectors_and_original_sources(genuine_search):
    registry, evidence = genuine_search
    assert evidence.embedding.dimension == 512
    assert evidence.embedding.model == "BAAI/bge-small-zh-v1.5"
    source = next(h.source for h in evidence.hits if h.relevance == "related" and h.source.condition_ids)
    quote = get_catalog().conditions[0].statement
    result = execute(registry, "explain_knowledge", {"retrieval_id": evidence.retrieval_id,
        "answerability": "supported", "citations": [{"source_id": source.source_id, "quote": quote}]})
    assert result["ok"]
    assert result["data"]["citations"] == [{"source_id": source.source_id, "quote": quote}]
    assert "answer" not in result["data"] and evidence.semantic_support == "not_assessed"


@pytest.mark.parametrize("quote", ["", "不存在的原文", "客户须明确确认……兼容的身份提供方。"])
def test_explanation_rejects_blank_invented_or_discontinuous_quote(genuine_search, quote):
    registry, evidence = genuine_search
    source = next(h.source for h in evidence.hits if h.relevance == "related")
    result = execute(registry, "explain_knowledge", {"retrieval_id": evidence.retrieval_id,
        "answerability": "supported", "citations": [{"source_id": source.source_id, "quote": quote}]})
    assert not result["ok"] and "data" not in result


def test_explanation_cannot_borrow_previous_turn_or_different_chain(genuine_search, registry):
    first, evidence = genuine_search
    args = {"retrieval_id": evidence.retrieval_id, "answerability": "insufficient", "citations": []}
    next_turn = BusinessToolRegistry(first.settings, first.state, "请接着解释")
    assert not execute(next_turn, "explain_knowledge", args)["ok"]
    assert not execute(registry, "explain_knowledge", args)["ok"]


def test_supported_requires_citation_and_insufficient_does_not_become_supported(genuine_search):
    registry, evidence = genuine_search
    args = {"retrieval_id": evidence.retrieval_id, "answerability": "supported", "citations": []}
    assert not execute(registry, "explain_knowledge", args)["ok"]
    args["answerability"] = "insufficient"
    result = execute(registry, "explain_knowledge", args)
    assert result["ok"] and result["data"]["answerability"] == "insufficient"
    assert evidence.semantic_support == "not_assessed"


def test_citation_service_cannot_substitute_original_or_return_coroutine(genuine_search, monkeypatch):
    registry, evidence = genuine_search
    source = next(hit.source for hit in evidence.hits if hit.relevance == "related")
    args = {"retrieval_id": evidence.retrieval_id, "answerability": "supported",
            "citations": [{"source_id": source.source_id, "quote": source.raw_text}]}
    monkeypatch.setattr(registry.state.knowledge, "validate_citations", lambda *args: (source.model_copy(update={"raw_text": "伪造的原文"}),))
    assert execute(registry, "explain_knowledge", args)["error"]["code"] == "invalid_tool_result"

    async def unexpected(*args):
        return (source,)
    monkeypatch.setattr(registry.state.knowledge, "validate_citations", unexpected)
    assert execute(registry, "explain_knowledge", args)["error"]["code"] == "invalid_tool_result"


def test_worker_failure_is_an_unavailable_observation_and_cannot_be_cited(registry, monkeypatch):
    def failed_worker(_):
        raise RuntimeError("controlled worker failure")
    monkeypatch.setattr(registry.state.knowledge, "_execute", failed_worker)
    result = execute(registry, "search_knowledge", {"query": "统一登录需要哪些前提"})
    assert result["ok"] and result["data"]["status"] == "unavailable"
    assert result["data"]["error"]["code"] == "retrieval_failed"
    assert result["data"]["hits"] == []
    explanation = execute(registry, "explain_knowledge", {"retrieval_id": result["data"]["retrieval_id"],
        "answerability": "insufficient", "citations": []})
    assert explanation["error"]["code"] == "evidence_invalid"


def test_controlled_low_hit_cannot_be_promoted_to_explanation(genuine_search, registry, monkeypatch):
    _, actual = genuine_search
    hit = actual.hits[0].model_copy(update={"score": 0.1, "relevance": "low"})
    evidence = actual.model_copy(update={"retrieval_id": uuid4().hex, "chain_id": registry.state.knowledge.chain_id,
        "status": "low_relevance", "hits": (hit,)})
    registry.state.knowledge._issued[evidence.retrieval_id] = evidence
    monkeypatch.setattr(registry.state.knowledge, "retrieve", lambda _: evidence)
    result = execute(registry, "search_knowledge", {"query": evidence.query})
    assert result["ok"] and result["data"]["status"] == "low_relevance"
    explanation = execute(registry, "explain_knowledge", {"retrieval_id": evidence.retrieval_id,
        "answerability": "supported", "citations": [{"source_id": hit.source.source_id, "quote": hit.source.raw_text}]})
    assert not explanation["ok"]


@pytest.mark.parametrize("answerability", ["supported", "insufficient"])
def test_known_same_condition_conflict_survives_clean_later_explanation(genuine_search, registry, monkeypatch, answerability):
    """Copy observations into an isolated chain; only conflict propagation tested."""
    _, original = genuine_search
    knowledge = registry.state.knowledge
    prior = original.model_copy(update={"retrieval_id": uuid4().hex, "chain_id": knowledge.chain_id})
    knowledge._issued[prior.retrieval_id] = prior
    source = next(hit.source for hit in prior.hits if hit.source.condition_ids)
    known = knowledge.record_condition_conflict(prior.retrieval_id,
        condition_id=source.condition_ids[0], source_ids=(source.source_id,),
        reason="隔离测试已知矛盾传播，未改正式资料。", origin="controlled_test")
    clean = original.model_copy(update={"retrieval_id": uuid4().hex, "chain_id": knowledge.chain_id})
    knowledge._issued[clean.retrieval_id] = clean
    monkeypatch.setattr(knowledge, "retrieve", lambda _: clean)
    assert execute(registry, "search_knowledge", {"query": clean.query})["ok"]
    result = execute(registry, "explain_knowledge", {"retrieval_id": clean.retrieval_id,
        "answerability": answerability, "citations": [{"source_id": source.source_id, "quote": source.raw_text}]})
    assert result["error"]["code"] == "evidence_conflict"
    assert result["error"]["retrieval_ids"] == [known.retrieval_id]
    assert result["error"]["condition_ids"] == list(source.condition_ids)
    assert "data" not in result and registry.knowledge_answers == []
    assert knowledge.evidence(known.retrieval_id).conflicts == known.conflicts
    assert knowledge.evidence(clean.retrieval_id).semantic_support == "not_assessed"
    assert knowledge.evidence(clean.retrieval_id).semantic_review is None


def test_conflict_on_a_different_source_does_not_hide_unrelated_explanation(genuine_search, registry, monkeypatch):
    _, original = genuine_search
    knowledge = registry.state.knowledge
    isolated = original.model_copy(update={"retrieval_id": uuid4().hex, "chain_id": knowledge.chain_id})
    knowledge._issued[isolated.retrieval_id] = isolated
    conditional = next(hit.source for hit in isolated.hits if hit.source.condition_ids)
    unrelated = next(hit.source for hit in isolated.hits if hit.relevance == "related" and not hit.source.condition_ids)
    knowledge.record_condition_conflict(isolated.retrieval_id,
        condition_id=conditional.condition_ids[0], source_ids=(conditional.source_id,),
        reason="隔离的条件冲突。", origin="controlled_test")
    clean = original.model_copy(update={"retrieval_id": uuid4().hex, "chain_id": knowledge.chain_id})
    knowledge._issued[clean.retrieval_id] = clean
    monkeypatch.setattr(knowledge, "retrieve", lambda _: clean)
    assert execute(registry, "search_knowledge", {"query": clean.query})["ok"]
    result = execute(registry, "explain_knowledge", {"retrieval_id": clean.retrieval_id,
        "answerability": "insufficient", "citations": [{"source_id": unrelated.source_id, "quote": unrelated.raw_text}]})
    assert result["ok"] and result["data"]["answerability"] == "insufficient"
    assert knowledge.evidence(clean.retrieval_id).semantic_support == "not_assessed"
    assert knowledge.evidence(clean.retrieval_id).semantic_review is None


@pytest.mark.parametrize("cancel_count", [1, 2])
def test_cancelled_tool_retains_consumer_lock_until_native_mutation_finishes(registry, monkeypatch, cancel_count):
    """An actual blocked worker and two turns test ordering, not timing estimates."""
    started, release = threading.Event(), threading.Event()
    events = []
    original = registry.state.apply_patch

    def blocked_patch(raw, message):
        if message == registry.message:
            events.append("first_started")
            started.set()
            assert release.wait(5), "Test worker was not released"
            result = original(raw, message)
            events.append("first_changed")
            return result
        events.append("second_started")
        result = original(raw, message)
        events.append("second_changed")
        return result

    monkeypatch.setattr(registry.state, "apply_patch", blocked_patch)

    async def run():
        lock = asyncio.Lock()
        second_registry = BusinessToolRegistry(registry.settings, registry.state, "改成50席")

        async def turn(tool_registry, seats, quote):
            async with lock:
                return await tool_registry.execute("update_requirements", {
                    "intent": "price", "parameters": [{"name": "paid_seats", "value": seats, "user_quote": quote}]})

        first = asyncio.create_task(turn(registry, 80, "80席"))
        second = None
        try:
            assert await asyncio.to_thread(started.wait, 2)
            first.cancel()
            await asyncio.sleep(0)
            second = asyncio.create_task(turn(second_registry, 50, "50席"))
            for _ in range(cancel_count - 1):
                first.cancel()
                await asyncio.sleep(0)
            await asyncio.sleep(0)
            assert not first.done() and not second.done()
            assert lock.locked() and events == ["first_started"]
            assert registry.state.requirements.parameters == ()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await first
            result = await second
            assert result["ok"], result
            assert events == ["first_started", "first_changed", "second_started", "second_changed"]
            assert registry.state.requirements.parameters[0].value == 50
        finally:
            release.set()
            await asyncio.gather(first, *([second] if second is not None else []), return_exceptions=True)

    asyncio.run(run())
