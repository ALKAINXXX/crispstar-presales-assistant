"""Input-review orchestration evidence: real verifier/domain services, controlled LLM.

Provider responses here are deliberate substitutes, never language-accuracy or
real-provider evidence. Each SQLite database is a new tmp_path synthetic store.
"""

import asyncio
from copy import deepcopy
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.agents import orchestrator as module
from app.agents.orchestrator import Orchestrator
from app.config import Settings
from app.db.session import init_saas_db
from app.llm.semantic_verifier import SemanticReviewFailed, SemanticVerifier
from app.schemas.chat import ChatRequest
from app.tools.business import BusinessToolRegistry


def call(name, arguments, identity=None):
    return SimpleNamespace(id=uuid4().hex if identity is None else identity, function=SimpleNamespace(name=name,
        arguments=arguments if isinstance(arguments, str) else json.dumps(arguments, ensure_ascii=False)))


def completion(*calls, content="受控模型结束", usage=None):
    return SimpleNamespace(model="controlled-input-review", usage=usage,
        choices=[SimpleNamespace(message=SimpleNamespace(content=content, tool_calls=list(calls)), finish_reason="stop")])


def script(monkeypatch, outputs):
    class ControlledProvider:
        captured = []
        remaining = list(outputs)

        def __init__(self, settings):
            pass

        async def chat(self, **kwargs):
            type(self).captured.append(deepcopy(kwargs))
            assert type(self).remaining, "Unexpected extra provider dispatch"
            output = type(self).remaining.pop(0)
            if isinstance(output, Exception):
                raise output
            if callable(output):
                return await output()
            return output

    monkeypatch.setattr(module, "DeepSeekClient", ControlledProvider)
    return ControlledProvider


@pytest.fixture
def engine(tmp_path):
    settings = Settings(_env_file=None, deepseek_api_key="controlled",
        deepseek_default_max_completion_tokens=512,
        saas_database_url=f"sqlite:///{tmp_path / 'input-synthetic.sqlite3'}")
    init_saas_db(settings)
    yield Orchestrator(settings)


def group(identity, action, quote, **kwargs):
    return {"group_id": identity, "verdict": "accept", "operations": [
        {"action": action, "span": {"quote": quote}, **kwargs}]}


def independent_price_review(message="Team，实际购买50席，订阅6个月，独立计价"):
    return {"intent": "price", "target": {"kind": "price", "relation": "independent", "span": {"quote": message}},
        "coverage_complete": True, "consultations": [], "consultation_coverage_complete": True, "groups": [
            group("plan", "select_plan", "Team", value="team"),
            group("seats", "set", "实际购买50席", field="paid_seats", value=50),
            group("months", "set", "订阅6个月", field="months", value=6)]}


def review_dispatches(provider):
    return [item for item in provider.captured if isinstance(item.get("tool_choice"), dict)
        and item["tool_choice"].get("function", {}).get("name") == "adjudicate_turn"]


@pytest.mark.parametrize("first_intent", ["general", "knowledge", "unit_price", "price", "selection", "quote_query"])
def test_every_first_candidate_intent_receives_one_independent_original_language_check(engine, monkeypatch, first_intent):
    message = "Team，实际购买50席，订阅6个月，独立计价"
    provider = script(monkeypatch, [completion(call("update_requirements", {"intent": first_intent})),
        completion(call("adjudicate_turn", independent_price_review(message))),
        completion(call("calculate_price", {"plan_id": "team", "paid_seats": 50, "months": 6})), completion()])
    response = asyncio.run(engine.chat(ChatRequest(message=message)))
    checks = review_dispatches(provider)
    assert len(checks) == 1 and checks[0]["max_retries"] == 0
    payload = json.loads(checks[0]["messages"][1]["content"])
    assert payload["original_message"] == message and payload["first_candidate"]["intent"] == first_intent
    assert response.business.semantic_review["dispatched"] == 1
    assert response.business.pricing.base_price_minor == 2_400_000
    assert response.business.proposal is None
    assert engine.conversations.get_messages(response.session_id)[-1].content == response.message


def test_first_candidate_float_representation_can_be_corrected_only_from_actual_integer_original(engine, monkeypatch):
    message = "Team，实际购买50席，订阅6个月，独立计价"
    candidate = {"intent": "price", "parameters": [{"name": "paid_seats", "value": 50.0, "user_quote": "实际购买50席"}]}
    provider = script(monkeypatch, [completion(call("update_requirements", candidate)),
        completion(call("adjudicate_turn", independent_price_review(message))),
        completion(call("calculate_price", {"plan_id": "team", "paid_seats": 50, "months": 6})), completion()])
    result = asyncio.run(engine.chat(ChatRequest(message=message)))
    assert result.business.pricing.paid_seats == 50
    assert len(review_dispatches(provider)) == 1


@pytest.mark.parametrize("candidate", [
    {"intent": "price", "amount": 1},
    {"intent": "price", "parameters": [{"name": "paid_seats", "value": 50,
        "user_quote": "实际购买50席", "confirmed": True}]},
    {"intent": "selection", "add": [{"statement": "API", "user_quote": "API",
        "kind": "feature", "feature_id": "api_access", "supported": True}]},
])
def test_invalid_whole_first_candidate_cannot_commit_a_valid_subset(engine, monkeypatch, candidate):
    provider = script(monkeypatch, [completion(call("update_requirements", candidate))])
    response = asyncio.run(engine.chat(ChatRequest(message="Team，实际购买50席，订阅6个月，独立计价")))
    assert response.status == "incomplete"
    assert response.business.semantic_review["dispatched"] == 0
    assert response.business.requirements.parameters == ()
    assert response.business.requirements.items == ()
    assert response.business.pricing is None and response.business.proposal is None
    assert not review_dispatches(provider)


def test_missing_first_candidate_is_reported_as_unchecked_system_failure(engine, monkeypatch):
    provider = script(monkeypatch, [completion(content="我已经改为50席。")])
    response = asyncio.run(engine.chat(ChatRequest(message="实际付费席位改为50席")))
    assert response.status == "incomplete" and response.business.requirements.parameters == ()
    assert response.business.semantic_review["error_code"] == "candidate_missing"
    assert not review_dispatches(provider)


def test_explicit_new_proposal_reuses_only_valid_receipts_without_relabeling_the_old_result(engine, monkeypatch):
    from test_proposals import seed
    sid, runtime, old_proposal = asyncio.run(seed(engine))
    before = runtime.state.current_task(recheck=False)
    review = {"intent": "selection", "target": {"kind": "selection", "relation": "continue",
        "task_id": runtime.state.requirements.requirement_id}, "coverage_complete": True,
        "consultations": [], "consultation_coverage_complete": True,
        "groups": [group("prepare", "request_proposal", "保存这个")]}
    script(monkeypatch, [completion(call("update_requirements", {"intent": "selection"})),
        completion(call("adjudicate_turn", review)), completion()])
    response = asyncio.run(engine.chat(ChatRequest(message="保存这个", session_id=sid)))
    assert response.status == "completed"
    assert response.business.proposal and response.business.proposal.action_ref != old_proposal.action_ref
    assert response.business.pricing == old_proposal.pricing
    after = runtime.state.current_task(recheck=False)
    assert after["result_turn_id"] == before["result_turn_id"]
    assert after["result_message_id"] == before["result_message_id"]
    assert after["requirements"] == before["requirements"]


def test_actual_fraction_is_never_rounded_to_valid_candidate_integer(engine, monkeypatch):
    message = "Team，实际购买50.5席，订阅6个月，独立计价"
    review = independent_price_review(message)
    review["groups"][1]["operations"][0]["span"]["quote"] = "实际购买50.5席"
    script(monkeypatch, [completion(call("update_requirements", {"intent": "price"})),
        completion(call("adjudicate_turn", review)), completion()])
    response = asyncio.run(engine.chat(ChatRequest(message=message)))
    assert "paid_seats" not in {item.name for item in response.business.requirements.parameters}
    assert response.business.pricing is None and response.business.proposal is None
    assert any(row["status"] != "accepted" for row in response.business.operation_feedback if row["field"] == "paid_seats")


@pytest.mark.parametrize("tool_name,arguments", [
    ("update_requirements", {"intent": "price", "parameters": [{"name": "paid_seats", "value": 80, "user_quote": "实际购买50席"}]}),
    ("calculate_price", {"plan_id": "business", "paid_seats": 50, "months": 6}),
    ("calculate_price", {"plan_id": "team", "paid_seats": 80, "months": 6}),
])
def test_later_mutable_tool_cannot_add_facts_or_change_adjudicated_plan(engine, monkeypatch, tool_name, arguments):
    message = "Team，实际购买50席，订阅6个月，独立计价"
    provider = script(monkeypatch, [completion(call("update_requirements", {"intent": "price"})),
        completion(call("adjudicate_turn", independent_price_review(message))), completion(call(tool_name, arguments))])
    response = asyncio.run(engine.chat(ChatRequest(message=message)))
    assert response.tool_results[-1]["result"]["ok"] is False
    assert response.business.requirements.selected_plan_id == "team"
    assert {p.name: p.value for p in response.business.requirements.parameters} == {"paid_seats": 50, "months": 6}
    assert response.business.pricing is None and len(review_dispatches(provider)) == 1


@pytest.mark.parametrize("failure,code", [
    (RuntimeError("PRIVATE_KEY_SENTINEL"), "semantic_review_unavailable"),
    (completion(call("adjudicate_turn", "{bad-json")), "semantic_review_invalid"),
    (completion(call("adjudicate_turn", '{"intent":"general","intent":"price","coverage_complete":true}')), "semantic_review_invalid"),
    (completion(call("adjudicate_turn", {"intent": "general", "coverage_complete": True, "amount": 1})), "semantic_review_invalid"),
])
def test_review_failure_keeps_old_facts_and_result_identity_but_revokes_unsubmitted_permission(engine, monkeypatch, failure, code):
    from test_proposals import seed
    sid, runtime, proposal = asyncio.run(seed(engine))
    before = runtime.state.current_task(recheck=False)
    provider = script(monkeypatch, [completion(call("update_requirements", {"intent": "selection"})), failure])
    response = asyncio.run(engine.chat(ChatRequest(message="付费席位改为50，其余不变", session_id=sid)))
    assert code in {error["code"] for error in response.business.errors}
    current = runtime.state.current_task(recheck=False)
    assert current["requirements"] == before["requirements"]
    assert current["pricing"] == before["pricing"]
    assert current["result_turn_id"] == before["result_turn_id"]
    assert runtime.proposal is None and response.business.proposal is None
    assert len(review_dispatches(provider)) == 1 and response.business.semantic_review["dispatched"] == 1
    assert "PRIVATE_KEY_SENTINEL" not in response.message
    assert asyncio.run(engine.submit_quote(sid, proposal.action_ref)).status == "not_saved"


def test_verifier_records_exact_usage_and_minimal_tasks_without_amount_or_save_authority(engine):
    from test_proposals import seed
    _, runtime, old = asyncio.run(seed(engine))
    captured = []
    class Provider:
        async def chat(self, **kwargs):
            captured.append(kwargs)
            return completion(call("adjudicate_turn", {"intent": "general", "coverage_complete": True}),
                usage=SimpleNamespace(prompt_tokens=103, completion_tokens=17, total_tokens=120))
    review, metrics = asyncio.run(SemanticVerifier(engine.settings).verify(Provider(), "你好", {"intent": "general"}, runtime.state))
    assert metrics["usage"] == {"prompt_tokens": 103, "completion_tokens": 17, "total_tokens": 120}
    assert metrics["status"] == "verified" and metrics["dispatched"] == 1 and metrics["elapsed_ms"] >= 0
    payload = json.loads(captured[0]["messages"][1]["content"])
    assert "pricing" not in payload["current_tasks"].get("current_selection", {})
    assert old.action_ref not in json.dumps(payload) and "base_price_minor" not in json.dumps(payload)
    assert review.intent == "general"


@pytest.mark.parametrize("reason,verdict,verified", [
    ("hypothesis", "reject", True),
    ("hypothesis", "unresolved", True),
    ("hypothesis", "accept", False),
    ("unknown_purchase_reason", "reject", False),
])
def test_hypothesis_reason_explains_rejection_without_authorizing_purchase(engine, monkeypatch, reason, verdict, verified):
    from test_proposals import seed
    sid, runtime, old_proposal = asyncio.run(seed(engine))
    before = runtime.state.current_task(recheck=False)
    message = "如果将来需要，再购买30席；现在别改方案。"
    review = {"intent": "selection", "target": {"kind": "selection", "relation": "continue",
        "task_id": runtime.state.requirements.requirement_id}, "coverage_complete": True,
        "consultations": [], "consultation_coverage_complete": True,
        "groups": [{"group_id": "future-background", "verdict": verdict, "reason": reason,
            "operations": [{"action": "note", "span": {"quote": message},
                "speech_act": "hypothesis", "certainty": "conditional", "time_scope": "future"}]}]}
    provider = script(monkeypatch, [completion(call("update_requirements", {"intent": "selection",
        "parameters": [{"name": "paid_seats", "value": 30, "user_quote": "再购买30席"}]})),
        completion(call("adjudicate_turn", review)), completion()])
    response = asyncio.run(engine.chat(ChatRequest(message=message, session_id=sid)))
    assert response.business.semantic_review["status"] == ("verified" if verified else "failed")
    errors = {error["code"] for error in response.business.errors}
    assert ("semantic_review_invalid" in errors) is not verified
    if verified:
        assert response.business.operation_feedback[0]["issue"] == "conditional"
        assert response.business.operation_feedback[0]["status"] != "accepted"
    after = runtime.state.current_task(recheck=False)
    for key in ("requirements", "pricing", "result_turn_id", "result_message_id"):
        assert after[key] == before[key]
    assert response.business.proposal is None and runtime.proposal is None
    assert len(review_dispatches(provider)) == 1
    assert review_dispatches(provider)[0]["max_retries"] == 0
    assert engine.conversations.get_messages(sid)[-1].content == response.message
    assert asyncio.run(engine.submit_quote(sid, old_proposal.action_ref)).status == "not_saved"


def test_sdk_adapter_disables_retries_on_only_the_requested_check(monkeypatch, engine):
    from app.llm import deepseek_client
    sent, options = [], []
    class SDK:
        def __init__(self, **kwargs):
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))
        def with_options(self, **kwargs):
            options.append(kwargs)
            return self
        async def create(self, **kwargs):
            sent.append(kwargs)
            return completion()
    monkeypatch.setattr(deepseek_client, "AsyncOpenAI", SDK)
    async def scenario():
        client = deepseek_client.DeepSeekClient(engine.settings)
        await client.chat(messages=[{"role": "user", "content": "check"}], max_retries=0)
        await client.chat(messages=[{"role": "user", "content": "ordinary"}])
    asyncio.run(scenario())
    assert options == [{"max_retries": 0, "timeout": 45.0}] and len(sent) == 2


def test_direct_registry_float_candidate_is_schema_error_and_cannot_mutate(engine):
    sid = engine.conversations.ensure_conversation(None, "synthetic direct schema case")
    state = engine.work.get(sid).state
    before = state.requirements
    result = asyncio.run(BusinessToolRegistry(engine.settings, state, "明确购买50席").execute(
        "update_requirements", {"intent": "selection", "parameters": [{"name": "paid_seats", "value": 50.0, "user_quote": "明确购买50席"}]}))
    assert result["ok"] is False and result["error"]["code"] == "invalid_arguments"
    assert state.requirements == before


def test_unestablished_review_target_cannot_commit_otherwise_valid_parameter_groups(engine, monkeypatch):
    from test_proposals import seed
    sid, runtime, proposal = asyncio.run(seed(engine))
    before = runtime.state.requirements
    review = {"intent": "selection", "coverage_complete": True,
        "target": {"kind": "selection", "relation": "continue", "task_id": "unowned-task"},
        "groups": [group("seats", "set", "实际购买50席", field="paid_seats", value=50)]}
    provider = script(monkeypatch, [completion(call("update_requirements", {"intent": "selection"})),
        completion(call("adjudicate_turn", review))])
    response = asyncio.run(engine.chat(ChatRequest(session_id=sid, message="实际购买50席，其余不变")))
    assert runtime.state.requirements == before and response.business.proposal is None
    assert "semantic_review_facts_invalid" in {error["code"] for error in response.business.errors}
    assert response.business.operation_feedback[0]["issue"] == "target_unknown"
    assert "previous_value" not in response.business.operation_feedback[0]
    assert len(review_dispatches(provider)) == 1


def test_natural_quote_lookup_later_tool_id_must_equal_the_reviewed_original_object(engine, monkeypatch):
    reviewed, unreviewed = "Q-" + "1" * 32, "Q-" + "2" * 32
    reads = []
    monkeypatch.setattr(engine.quotes, "get", lambda identity: reads.append(identity))
    provider = script(monkeypatch, [completion(call("update_requirements", {"intent": "quote_query"})),
        completion(call("adjudicate_turn", {"intent": "quote_query", "coverage_complete": True, "quote_id": reviewed})),
        completion(call("query_quote", {"quote_id": unreviewed}))])
    response = asyncio.run(engine.chat(ChatRequest(message="按编号查草案 " + reviewed)))
    assert "quote_query_scope_mismatch" in {error["code"] for error in response.business.errors}
    assert reads == [] and response.business.queried_quotes == []
    assert len(review_dispatches(provider)) == 1


@pytest.mark.parametrize("invalidated", ["adopted_evidence", "catalog_version"])
def test_v30_exact_save_request_preserves_facts_and_revokes_invalid_evidence_or_catalog_results(engine, monkeypatch, invalidated):
    """V30 is controlled invalidation, not a real model/corpus change claim."""
    from app.schemas.pricing import PricingResult
    from app.services.catalog import get_catalog
    from app.services.proposals import ProposalRejected, prepare_proposal
    from test_proposals import seed
    async def scenario():
        sid, runtime, old = await seed(engine, monkeypatch, sso=invalidated == "adopted_evidence")
        state = runtime.state
        if invalidated == "adopted_evidence":
            message = "明确购买50席，其余不变，重新计算订阅基价"
            registry = BusinessToolRegistry(engine.settings, state, message)
            result = await registry.execute("update_requirements", {"intent": "selection", "parameters": [
                {"name": "paid_seats", "value": 50, "user_quote": "明确购买50席"}]})
            assert result["ok"]
            assert (await registry.execute("match_requirements", {}))["ok"]
            result = await registry.execute("calculate_price", {"plan_id": "business", "paid_seats": 50, "months": 12})
            assert result["ok"]
            state.remember_result(state.last_match, PricingResult.model_validate(result["data"]), (), (), turn_id="synthetic-S2")
            runtime.proposal = old = prepare_proposal(state, session_id=sid, turn_id="synthetic-S2",
                interaction_revision=runtime.interaction_revision)
            assert old.pricing.base_price_minor == 7_200_000
        before = state.requirements
        if invalidated == "adopted_evidence":
            adopted = state._selection_context.match.retrieval_evidence[0]
            state.knowledge._issued[adopted.retrieval_id] = adopted.model_copy(update={"dataset_version": "synthetic-invalid-version"})
        else:
            changed = get_catalog().model_copy(update={"version": "synthetic-next-catalog-version"})
            for path in ("app.services.work_state.get_catalog", "app.services.matching.get_catalog", "app.services.proposals.get_catalog"):
                monkeypatch.setattr(path, lambda: changed)
        request = "沿用刚才方案准备保存"
        review = {"intent": "selection", "target": {"kind": "selection", "relation": "continue", "task_id": before.requirement_id},
            "coverage_complete": True, "consultations": [], "consultation_coverage_complete": True,
            "groups": [group("save-request", "request_proposal", request)]}
        provider = script(monkeypatch, [completion(call("update_requirements", {"intent": "selection"})),
            completion(call("adjudicate_turn", review)), completion(), completion()])
        response = await engine.chat(ChatRequest(session_id=sid, message=request))
        assert state.requirements.items == before.items and state.requirements.parameters == before.parameters
        assert state.requirements.condition_assertions == before.condition_assertions
        assert response.business.current_task.match is None and response.business.current_task.pricing is None
        assert response.business.current_task.status == "needs_reassessment"
        assert response.business.proposal is None and runtime.proposal is None
        assert (await engine.submit_quote(sid, old.action_ref)).status == "not_saved"
        with pytest.raises((ProposalRejected, ValueError)):
            prepare_proposal(state, session_id=sid, turn_id="invalid-must-not-save", interaction_revision=runtime.interaction_revision)
        assert len(review_dispatches(provider)) == 1
    asyncio.run(scenario())
