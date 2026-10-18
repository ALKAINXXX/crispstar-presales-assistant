"""Proposal controlled model/worker/callback checks, never real Quote persistence.

Every chat row uses tmp_path. The conditional worker fixture returns a real
formal-index Source through the real KnowledgeService, but does not prove query
embeddings. Root's separately recorded finite live run supplies that evidence.
"""

import asyncio
from copy import deepcopy
from datetime import datetime, timezone
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.agents import orchestrator as module
from app.agents.orchestrator import Orchestrator
from app.llm.deepseek_client import ModelCallBudgetExceeded
from app.config import Settings
from app.db.session import init_saas_db
from app.schemas.chat import ChatRequest
from app.schemas.pricing import PricingResult, subscription_formula
from app.schemas.retrieval import RetrievalEvidence, RetrievalHit, RetrievalOptions
from app.services.catalog import get_catalog
from app.services.knowledge import DEFAULT_INDEX_PATH, _read_index
from app.services.proposals import ProposalRejected, prepare_proposal
from app.services.pricing import calculate_subscription_price
from app.tools.business import BusinessToolRegistry
from controlled_review import answer_review, is_review_request


CONDITION = "saml_sso_compatible_idp"
VIEW = "proposal-view-0123456789"


@pytest.fixture
def engine(tmp_path):
    settings = Settings(_env_file=None, deepseek_api_key="controlled", deepseek_default_max_completion_tokens=512,
        saas_database_url=f"sqlite:///{tmp_path / 'proposal-isolated-chat.db'}")
    init_saas_db(settings)
    return Orchestrator(settings)


def worker(monkeypatch, state, *, condition_source=True):
    index = _read_index(DEFAULT_INDEX_PATH)
    source = next(s for s in index.inputs.sources if
        ((CONDITION in s.condition_ids) if condition_source else (s.feature_ids == ("audit_logs",))))
    def execute(request):
        return RetrievalEvidence(retrieval_id=request["retrieval_id"], chain_id=request["chain_id"],
            query=request["query"], retrieved_at=datetime.now(timezone.utc).isoformat(),
            options=RetrievalOptions.model_validate(request["options"]), status="retrieved",
            hits=(RetrievalHit(rank=1, score=0.9, relevance="related", source=source),),
            product_id=index.inputs.product_id, dataset_version=index.inputs.dataset_version,
            index_build_id=index.build_id, index_inputs_sha256=index.inputs_sha256,
            embedding=index.inputs.embedding)
    monkeypatch.setattr(state.knowledge, "_execute", execute)
    return source


async def seed(engine, monkeypatch=None, *, session_id="selection", sso=False, selected=None, view_id=None):
    sid = engine.conversations.ensure_conversation(None, "隔离方案测试")
    runtime = engine.work.get(sid)
    state = runtime.state
    if sso:
        worker(monkeypatch, state)
    quote = "SSO必须有，审计必须有" if sso else "API必须有"
    message = quote + "，明确购买80席，订阅12个月，算金额，目前已有兼容身份提供方"
    if selected:
        message += "，主动选择" + selected
    patch = {"intent": "selection", "pricing_quote": "算金额",
        "add": [{"statement": quote, "user_quote": quote, "feature_id": feature}
                for feature in (("saml_sso", "audit_logs") if sso else ("api_access",))],
        "parameters": [{"name": "paid_seats", "value": 80, "user_quote": "明确购买80席"},
                       {"name": "months", "value": 12, "user_quote": "订阅12个月"}]}
    if sso:
        patch["conditions"] = [{"condition_id": CONDITION, "value": "yes", "user_quote": "目前已有兼容身份提供方"}]
    if selected:
        patch.update(selected_plan_id=selected, selection_quote="主动选择" + selected)
    registry = BusinessToolRegistry(engine.settings, state, message)
    assert (await registry.execute("update_requirements", json.dumps(patch, ensure_ascii=False)))["ok"]
    result = await registry.execute("match_requirements", "{}")
    assert result["ok"]
    match = state.last_match
    result = await registry.execute("calculate_price", json.dumps(
        {"plan_id": match.selected_plan_id, "paid_seats": 80, "months": 12}))
    assert result["ok"]
    price = PricingResult.model_validate_json(json.dumps(result["data"]))
    state.remember_result(match, price, (), (), turn_id="seed-turn")
    state.has_processed_turn = True
    runtime.invalidate_proposal("seed_chat")
    runtime.current_view_id = view_id
    runtime.proposal = prepare_proposal(state, session_id=sid, turn_id="seed-turn",
        interaction_revision=runtime.interaction_revision, view_id=view_id)
    return sid, runtime, runtime.proposal


def call(name, values, identity=None):
    return SimpleNamespace(id=uuid4().hex if identity is None else identity, function=SimpleNamespace(name=name,
        arguments=json.dumps(values, ensure_ascii=False)))


def completion(*calls):
    return SimpleNamespace(model="controlled-proposal-model", usage=None,
        choices=[SimpleNamespace(message=SimpleNamespace(content="本轮结束", tool_calls=list(calls)), finish_reason="stop")])


def scripted(monkeypatch, values):
    class Client:
        captured = []
        outputs = list(values)
        review_calls = []
        def __init__(self, settings):
            pass
        async def chat(self, **kwargs):
            if is_review_request(kwargs):
                return answer_review(type(self), kwargs)
            self.captured.append(deepcopy(kwargs))
            value = self.outputs.pop(0)
            if isinstance(value, Exception):
                raise value
            return value
    monkeypatch.setattr(module, "DeepSeekClient", Client)
    return Client


def test_local_budget_pause_is_truthful_same_source_and_revokes_old_permission(engine, monkeypatch):
    sid, runtime, old = asyncio.run(seed(engine))
    scripted(monkeypatch, [ModelCallBudgetExceeded("controlled local hard budget")])
    response = asyncio.run(engine.chat(ChatRequest(message="继续原选型", session_id=sid)))
    assert "model_budget_exhausted" in {e["code"] for e in response.business.errors}
    assert "model_unavailable" not in {e["code"] for e in response.business.errors}
    assert ModelCallBudgetExceeded.public_message in response.message
    assert "可稍后重试" not in response.message
    assert runtime.proposal is None and response.business.proposal is None
    assert response.business.current_task.pricing == old.pricing
    assert engine.conversations.get_messages(sid)[-1].content == response.message


def test_real_domain_pair_creates_immutable_complete_snapshot(engine, monkeypatch):
    sid, runtime, proposal = asyncio.run(seed(engine, monkeypatch, sso=True))
    assert proposal.selected_plan_id == proposal.lowest_plan_id == "business"
    assert proposal.pricing.base_price_minor == 11_520_000
    assert {i.feature_id for i in proposal.requirements.items} == {"saml_sso", "audit_logs"}
    assert proposal.conditions[0].user_quote == "目前已有兼容身份提供方"
    assert proposal.conditions[0].statement in proposal.adopted_sources[0].source.raw_text
    assert proposal.adopted_sources[0].chain_id == runtime.state.knowledge.chain_id
    assert proposal.adopted_sources[0].dataset_version == proposal.catalog_version
    assert proposal.save_available is True and "尚未保存" in proposal.notice
    assert len(proposal.action_ref) >= 40
    with pytest.raises(ValidationError):
        proposal.pricing.base_price_minor = 1
    with pytest.raises(ValidationError):
        proposal.requirements.items[0].statement = "改写"


def test_user_selection_retains_distinct_minimum_and_actual_selection(engine):
    _, _, proposal = asyncio.run(seed(engine, selected="Business".lower()))
    assert proposal.lowest_plan_id == "team" and proposal.selected_plan_id == "business"
    assert proposal.selection_reason == "user_selected"


def test_unadopted_detour_sources_never_enter_snapshot(engine, monkeypatch):
    sid, runtime, proposal = asyncio.run(seed(engine, monkeypatch, sso=True))
    worker(monkeypatch, runtime.state, condition_source=False)
    detour = runtime.state.knowledge.retrieve("审计保存期限")
    new = prepare_proposal(runtime.state, session_id=sid, turn_id="reprepare",
        interaction_revision=runtime.interaction_revision)
    assert all(item.retrieval_id != detour.retrieval_id for item in new.adopted_sources)
    assert new.adopted_sources == proposal.adopted_sources


@pytest.mark.parametrize("intent", ["unit_price", "price", "knowledge", "general", "unsupported_action"])
def test_pricing_and_lookup_intents_cannot_upgrade_retained_selection(engine, intent):
    sid, runtime, _ = asyncio.run(seed(engine))
    runtime.state.intent = intent
    with pytest.raises(ProposalRejected, match="intent_not_selection"):
        prepare_proposal(runtime.state, session_id=sid, turn_id="no", interaction_revision=2)


@pytest.mark.parametrize("mutation", ["missing_price", "missing_match", "pending", "old_revision", "wrong_version", "client_price", "wrong_receipt"])
def test_inconsistent_and_incomplete_combinations_rejected(engine, mutation):
    sid, runtime, _ = asyncio.run(seed(engine))
    context = runtime.state._selection_context
    if mutation == "missing_price":
        context.pricing = None
    elif mutation == "missing_match":
        context.match = runtime.state.last_match = None
    elif mutation == "pending":
        context.pending = runtime.state.pending = ("months",)
    elif mutation == "old_revision":
        context.match = context.match.model_copy(update={"requirements": context.requirements.model_copy(update={"revision": 1})})
        runtime.state.last_match = context.match
    elif mutation == "wrong_version":
        context.pricing = context.pricing.model_copy(update={"catalog_version": "wrong"})
    elif mutation == "client_price":
        old = context.pricing
        context.pricing = old.model_copy(update={"unit_price_minor": 1, "base_price_minor": old.paid_seats * old.months,
            "formula": subscription_formula(1, old.paid_seats, old.months)})
        context.pricing_requirements, context.pricing_match = context.requirements, context.match
    else:
        context.pricing_requirements = None
    with pytest.raises((ProposalRejected, ValueError)):
        prepare_proposal(runtime.state, session_id=sid, turn_id="no", interaction_revision=2)


def test_new_same_amount_revision_cannot_reuse_old_price_receipt(engine):
    sid, runtime, original = asyncio.run(seed(engine, selected="business"))
    state = runtime.state
    state.apply_patch({"intent": "selection", "add": [{"feature_id": "audit_logs", "statement": "审计必须有", "user_quote": "审计必须有"}]}, "审计必须有")
    match = state.match()
    # Price is monetarily correct even though it was executed for an older
    # requirement revision. Without a fresh real execution it is not a pair.
    state.remember_result(match, original.pricing, (), (), turn_id="new-revision")
    with pytest.raises(ProposalRejected, match="pricing_requirement_revision_mismatch"):
        prepare_proposal(state, session_id=sid, turn_id="new", interaction_revision=2)


@pytest.mark.parametrize("kind", ["unknown", "unsupported", "missing_condition", "conflict", "wrong_index_version"])
def test_domain_blockers_never_produce_proposal(engine, monkeypatch, kind):
    sid, runtime, proposal = asyncio.run(seed(engine, monkeypatch, sso=kind in {"missing_condition", "conflict", "wrong_index_version"}))
    state = runtime.state
    if kind in {"unknown", "unsupported"}:
        add = {"statement": "SCIM必须有", "user_quote": "SCIM必须有", "kind": "unmodeled"} if kind == "unknown" else {
            "statement": "私有化必须有", "user_quote": "私有化必须有", "feature_id": "private_deployment"}
        state.apply_patch({"intent": "selection", "add": [add]}, add["user_quote"])
        state.match()
    elif kind == "missing_condition":
        state.knowledge._issued.clear()
    elif kind == "conflict":
        adopted = proposal.adopted_sources[0]
        state.knowledge.record_condition_conflict(adopted.retrieval_id, condition_id=CONDITION,
            source_ids=(adopted.source.source_id,), reason="隔离受控冲突", origin="controlled_test")
    else:
        e = state._selection_context.match.retrieval_evidence[0]
        wrong = e.model_copy(update={"dataset_version": "wrong-version"})
        state.knowledge._issued[e.retrieval_id] = wrong
    with pytest.raises((ProposalRejected, ValueError)):
        prepare_proposal(state, session_id=sid, turn_id="no", interaction_revision=2)


def test_correct_bound_action_only_supplies_server_object_and_consumes_permission(engine):
    async def scenario():
        sid, runtime, proposal = await seed(engine)
        captured = []
        async def submitter(value):
            assert runtime.lock.locked()
            captured.append(value)
            return "controlled-object-check-only"
        assert await engine.guarded_submit(sid, proposal.action_ref, submitter) == "controlled-object-check-only"
        assert captured == [proposal] and runtime.proposal is None
        with pytest.raises(ProposalRejected):
            await engine.guarded_submit(sid, proposal.action_ref, submitter)
    asyncio.run(scenario())


@pytest.mark.parametrize("bad", ["forged", "unicode_forged", "cross_session", "confirmed", "client_amount"])
def test_forgery_cross_session_and_client_claims_do_not_authorize(engine, bad):
    async def scenario():
        sid, runtime, proposal = await seed(engine)
        async def no_write(value):
            pytest.fail("untrusted action reached callback")
        if bad in {"forged", "unicode_forged"}:
            with pytest.raises(ProposalRejected):
                await engine.guarded_submit(sid, "伪造动作" if bad == "unicode_forged" else "invented-action", no_write)
        elif bad == "cross_session":
            other = engine.conversations.ensure_conversation(None, "其他隔离会话")
            with pytest.raises(ProposalRejected):
                await engine.guarded_submit(other, proposal.action_ref, no_write)
        else:
            claims = {"confirmed": True} if bad == "confirmed" else {"base_price_minor": 1}
            with pytest.raises(TypeError):
                await engine.guarded_submit(sid, proposal.action_ref, no_write, **claims)
        assert runtime.proposal is proposal and proposal.pricing.base_price_minor == 7_680_000
    asyncio.run(scenario())


@pytest.mark.parametrize("event", ["refresh", "switch", "history_reenter", "new_chat", "view_exit"])
def test_lifecycle_invalidates_permission_and_preserves_selection(engine, event):
    async def scenario():
        sid, runtime, proposal = await seed(engine)
        original = runtime.state.current_task()
        assert (await engine.invalidate_proposal(sid, event))["invalidated"]
        assert runtime.proposal is None and runtime.state.current_task() == original
        with pytest.raises(ProposalRejected):
            await engine.guarded_submit(sid, proposal.action_ref, lambda _: None)
    asyncio.run(scenario())


def test_restart_and_current_version_change_reject_old_action(engine, monkeypatch):
    async def scenario():
        sid, runtime, proposal = await seed(engine)
        new_engine = Orchestrator(engine.settings)
        with pytest.raises(ProposalRejected):
            await new_engine.guarded_submit(sid, proposal.action_ref, lambda _: None)
        monkeypatch.setattr("app.services.proposals.get_catalog", lambda: get_catalog().model_copy(update={"version": "new-version"}))
        with pytest.raises(ProposalRejected):
            await engine.guarded_submit(sid, proposal.action_ref, lambda _: None)
        assert runtime.proposal is None
    asyncio.run(scenario())


@pytest.mark.parametrize("case", ["save_words", "good", "unit_detour", "rejected_change", "model_failure"])
def test_every_accepted_chat_invalidates_before_model_even_rejected_or_failed(engine, monkeypatch, case):
    async def scenario():
        sid, runtime, proposal = await seed(engine)
        state_before = runtime.state.current_task()
        if case == "model_failure":
            outputs, message = [RuntimeError("controlled failure")], "继续原选型"
        elif case == "rejected_change":
            message = "大约改成50席"
            outputs = [completion(call("update_requirements", {"intent": "selection", "parameters": [
                {"name": "paid_seats", "value": 50, "user_quote": message}]}))]
        elif case == "unit_detour":
            message = "Business单价多少"
            outputs = [completion(call("update_requirements", {"intent": "unit_price"})),
                       completion(call("query_catalog", {"plan_id": "business"})), completion()]
        else:
            message = "保存这个" if case == "save_words" else "好的"
            outputs = [completion(call("update_requirements", {"intent": "unsupported_action"})), completion()]
        BaseClient = scripted(monkeypatch, outputs)
        original_chat = BaseClient.chat
        async def chat(self, **kwargs):
            assert runtime.proposal is None
            assert proposal.action_ref not in json.dumps(kwargs, ensure_ascii=False)
            return await original_chat(self, **kwargs)
        monkeypatch.setattr(BaseClient, "chat", chat)
        response = await engine.chat(ChatRequest(session_id=sid, message=message))
        assert response.business.proposal is None and runtime.proposal is None
        assert runtime.state.current_task()["requirements"] == state_before["requirements"]
        assert runtime.state.current_task()["pricing"] == state_before["pricing"]
        with pytest.raises(ProposalRejected):
            await engine.guarded_submit(sid, proposal.action_ref, lambda _: None)
    asyncio.run(scenario())


def test_prepare_existing_task_mints_new_object_without_restating_or_writing(engine, monkeypatch):
    async def scenario():
        sid, runtime, old = await seed(engine)
        Client = scripted(monkeypatch, [completion(call("update_requirements", {"intent": "selection"})),
            completion(call("match_requirements", {}, "match"),
                       call("calculate_price", {"plan_id": "team", "paid_seats": 80, "months": 12}, "price")), completion()])
        response = await engine.chat(ChatRequest(session_id=sid, message="请按原方案准备草案"))
        new = response.business.proposal
        assert new and new.requirements == old.requirements and new.pricing == old.pricing
        assert new.proposal_id != old.proposal_id and new.action_ref != old.action_ref
        assert new.action_ref not in response.message
        assert all(new.action_ref not in json.dumps(k, ensure_ascii=False) for k in Client.captured)
        assert engine.conversations.get_messages(sid)[-1].content == response.message
        assert "尚未保存" in response.message and "确认并保存草案" in response.message
        assert response.business.presentation.answer_markdown in response.message
    asyncio.run(scenario())


def test_message_first_rejects_old_action_before_any_callback(engine, monkeypatch):
    async def scenario():
        sid, runtime, old = await seed(engine)
        entered, release = asyncio.Event(), asyncio.Event()
        class Client:
            def __init__(self, settings):
                pass
            async def chat(self, **kwargs):
                entered.set()
                await release.wait()
                raise RuntimeError("controlled model failure")
        monkeypatch.setattr(module, "DeepSeekClient", Client)
        message = asyncio.create_task(engine.chat(ChatRequest(session_id=sid, message="新消息")))
        await entered.wait()
        async def callback(value):
            pytest.fail("old action callback entered after accepted message")
        submit = asyncio.create_task(engine.guarded_submit(sid, old.action_ref, callback))
        await asyncio.sleep(0)
        assert not submit.done() and runtime.proposal is None
        release.set()
        await message
        with pytest.raises(ProposalRejected):
            await submit
    asyncio.run(scenario())


def test_controlled_submission_first_holds_same_boundary_through_callback(engine, monkeypatch):
    async def scenario():
        sid, runtime, proposal = await seed(engine)
        entered, release, model_entered = asyncio.Event(), asyncio.Event(), asyncio.Event()
        order = []
        async def callback(value):
            assert runtime.lock.locked() and value is proposal
            order.append("callback-enter")
            entered.set()
            await release.wait()
            order.append("callback-exit")
            return "only-a-controlled-substitute"
        class Client:
            def __init__(self, settings):
                pass
            async def chat(self, **kwargs):
                order.append("model-enter")
                model_entered.set()
                raise RuntimeError("controlled model failure")
        monkeypatch.setattr(module, "DeepSeekClient", Client)
        submit = asyncio.create_task(engine.guarded_submit(sid, proposal.action_ref, callback))
        await entered.wait()
        message = asyncio.create_task(engine.chat(ChatRequest(session_id=sid, message="后续消息")))
        await asyncio.sleep(0)
        assert not model_entered.is_set()
        release.set()
        assert await submit == "only-a-controlled-substitute"
        await message
        assert order == ["callback-enter", "callback-exit", "model-enter"]
    asyncio.run(scenario())


def test_unknown_session_view_revocation_prevents_late_first_chat_proposal(engine, monkeypatch):
    async def scenario():
        await engine.invalidate_view(VIEW, "refresh")
        Client = scripted(monkeypatch, [completion(call("update_requirements", {"intent": "selection", "pricing_quote": "算金额",
            "add": [{"statement": "API必须有", "user_quote": "API必须有", "feature_id": "api_access"}],
            "parameters": [{"name": "paid_seats", "value": 80, "user_quote": "80席"},
                           {"name": "months", "value": 12, "user_quote": "12个月"}]})),
            completion(call("match_requirements", {}, "match"), call("calculate_price", {
                "plan_id": "team", "paid_seats": 80, "months": 12}, "price")), completion()])
        response = await engine.chat(ChatRequest(message="API必须有，80席，12个月，算金额", view_id=VIEW))
        runtime = engine.work.get(response.session_id)
        assert response.status == "completed" and response.business.proposal is None and runtime.proposal is None
        assert runtime.state.current_task()["pricing"]["base_price_minor"] == 7_680_000
    asyncio.run(scenario())


def test_revocation_while_first_model_inflight_cannot_mint_late_proposal(engine, monkeypatch):
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        Script = scripted(monkeypatch, [completion(call("update_requirements", {"intent": "selection", "pricing_quote": "算金额",
            "add": [{"statement": "API必须有", "user_quote": "API必须有", "feature_id": "api_access"}],
            "parameters": [{"name": "paid_seats", "value": 80, "user_quote": "80席"},
                           {"name": "months", "value": 12, "user_quote": "12个月"}]})),
            completion(call("match_requirements", {}, "match"), call("calculate_price", {
                "plan_id": "team", "paid_seats": 80, "months": 12}, "price")), completion()])
        original = Script.chat
        async def chat(self, **kwargs):
            if not Script.captured:
                entered.set()
                await release.wait()
            return await original(self, **kwargs)
        monkeypatch.setattr(Script, "chat", chat)
        chat_task = asyncio.create_task(engine.chat(ChatRequest(message="API必须有，80席，12个月，算金额", view_id=VIEW)))
        await entered.wait()
        invalidation = asyncio.create_task(engine.invalidate_view(VIEW, "view_exit"))
        await asyncio.sleep(0)
        assert VIEW in engine._revoked_views and not invalidation.done()
        release.set()
        response = await chat_task
        await invalidation
        assert response.business.proposal is None and engine.work.get(response.session_id).proposal is None
    asyncio.run(scenario())


def test_foreign_or_revoked_view_cannot_pass_bound_action(engine):
    async def scenario():
        sid, runtime, proposal = await seed(engine, view_id=VIEW)
        await engine.invalidate_view(VIEW, "switch")
        with pytest.raises(ProposalRejected):
            await engine.guarded_submit(sid, proposal.action_ref, lambda _: None)
        assert runtime.state.current_task()["pricing"]["base_price_minor"] == 7_680_000
    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["exception", "malformed_callback"])
def test_callback_failure_never_leaves_reusable_creation_permission(engine, failure):
    async def scenario():
        sid, runtime, proposal = await seed(engine)
        async def raising(value):
            raise RuntimeError("controlled callback failure")
        callback = raising if failure == "exception" else lambda value: None
        with pytest.raises(RuntimeError if failure == "exception" else TypeError):
            await engine.guarded_submit(sid, proposal.action_ref, callback)
        assert runtime.proposal is None
        with pytest.raises(ProposalRejected):
            await engine.guarded_submit(sid, proposal.action_ref, raising)
    asyncio.run(scenario())


def test_view_revocation_waits_for_already_admitted_controlled_callback(engine):
    async def scenario():
        sid, runtime, proposal = await seed(engine, view_id=VIEW)
        entered, release = asyncio.Event(), asyncio.Event()
        order = []
        async def callback(value):
            order.append("callback-enter")
            entered.set()
            await release.wait()
            assert runtime.lock.locked() and value is proposal
            order.append("callback-exit")
            return "controlled-callback-finished"
        submit = asyncio.create_task(engine.guarded_submit(sid, proposal.action_ref, callback))
        await entered.wait()
        revoke = asyncio.create_task(engine.invalidate_view(VIEW, "view_exit"))
        await asyncio.sleep(0)
        assert VIEW in engine._revoked_views and not revoke.done()
        release.set()
        assert await submit == "controlled-callback-finished"
        await revoke
        order.append("invalidation-finished")
        assert order == ["callback-enter", "callback-exit", "invalidation-finished"]
        assert runtime.proposal is None
    asyncio.run(scenario())


def test_accepted_fifty_seats_forms_fresh_72000_object_and_rejects_old(engine, monkeypatch):
    async def scenario():
        sid, runtime, original = await seed(engine, monkeypatch, sso=True)
        scripted(monkeypatch, [completion(call("update_requirements", {"intent": "selection", "parameters": [
            {"name": "paid_seats", "value": 50, "user_quote": "明确改为50席"}]})),
            completion(call("match_requirements", {}, "match"), call("calculate_price", {
                "plan_id": "business", "paid_seats": 50, "months": 12}, "price")), completion()])
        response = await engine.chat(ChatRequest(session_id=sid, message="明确改为50席，其他不变"))
        proposal = response.business.proposal
        assert original.pricing.base_price_minor == 11_520_000
        assert proposal.pricing.base_price_minor == 7_200_000
        assert proposal.requirements.revision > original.requirements.revision
        assert proposal.action_ref != original.action_ref and runtime.proposal is proposal
        assert proposal.adopted_sources == original.adopted_sources
        with pytest.raises(ProposalRejected):
            await engine.guarded_submit(sid, original.action_ref, lambda _: None)
    asyncio.run(scenario())
