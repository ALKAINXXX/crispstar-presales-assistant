"""Quote deterministic evidence: real isolated SQLite, controlled upstream/faults.

The proposal seed executes the real requirement/matching/pricing services. Its
retrieval worker is controlled; this suite does not claim a real model or
embedding call. Those require separate live-model verification.
No fixture opens either configured production database.
"""

import asyncio
from datetime import datetime
import json
import sqlite3
import threading

import pytest
from pydantic import ValidationError

from app.agents import orchestrator as orchestrator_module
from app.agents.orchestrator import Orchestrator
from app.config import Settings
from app.db.session import connect_saas, init_saas_db, saas_database_path
from app.schemas.chat import ChatRequest
from app.services.catalog import get_catalog
from app.services.proposals import prepare_proposal
from app.services.quotes import QuoteActionOwnershipError, QuoteReadError
from app.tools.business import BusinessToolRegistry
from test_proposals import completion, scripted, seed


@pytest.fixture
def engine(tmp_path):
    settings = Settings(_env_file=None, deepseek_api_key="controlled",
        saas_database_url=f"sqlite:///{tmp_path / 'quotes-isolated.db'}")
    init_saas_db(settings)
    result = Orchestrator(settings)
    yield result


def counts(engine):
    with connect_saas(engine.settings) as db:
        return (db.execute("SELECT COUNT(*) FROM quotes").fetchone()[0],
                db.execute("SELECT COUNT(*) FROM quote_actions").fetchone()[0])


def unavailable(*args, **kwargs):
    raise OSError("controlled isolated database read failure")


def test_valid_submission_persists_exact_immutable_server_snapshot(engine, monkeypatch):
    async def scenario():
        sid, runtime, proposal = await seed(engine, monkeypatch, sso=True)
        assert counts(engine) == (0, 0)
        captured = []
        def inside_transaction(db, value):
            assert runtime.lock.locked() and value is proposal and db.in_transaction
            assert db.execute("SELECT COUNT(*) FROM quotes").fetchone()[0] == 1
            assert db.execute("SELECT COUNT(*) FROM quote_actions").fetchone()[0] == 1
            # A second real connection cannot see an uncommitted prospective ID.
            assert counts(engine) == (0, 0)
            captured.append(value)
        monkeypatch.setattr(engine.quotes, "_before_commit", inside_transaction)
        result = await engine.submit_quote(sid, proposal.action_ref)
        assert result.status == "saved" and result.quote is not None
        assert captured == [proposal] and runtime.proposal is None
        assert result.quote.snapshot == proposal
        assert result.quote.snapshot.pricing.base_price_minor == 11_520_000
        assert result.quote.snapshot.pricing.currency == "CNY"
        assert result.quote.snapshot.pricing.paid_seats == 80
        assert result.quote.snapshot.pricing.months == 12
        assert result.quote.snapshot.conditions[0].user_quote == "目前已有兼容身份提供方"
        assert result.quote.snapshot.adopted_sources == proposal.adopted_sources
        assert result.quote.snapshot.presentation.product_name
        assert result.quote.snapshot.presentation.features
        assert result.quote.snapshot.presentation.plans
        assert result.quote.snapshot.presentation.purpose == "internal_draft"
        assert result.quote.snapshot.presentation.amount_basis == "subscription_base_price"
        assert datetime.fromisoformat(result.quote.saved_at).tzinfo is not None
        assert counts(engine) == (1, 1)
        assert engine.quotes.get(result.quote.quote_id) == result.quote
        assert engine.quotes.find_action(sid, proposal.action_ref) == result.quote
        with pytest.raises(ValidationError):
            result.quote.snapshot.pricing.base_price_minor = 1
        with connect_saas(engine.settings) as db:
            row = db.execute("SELECT * FROM quote_actions").fetchone()
            assert dict(row) == {"action_ref": proposal.action_ref, "session_id": sid,
                "proposal_id": proposal.proposal_id, "quote_id": result.quote.quote_id}
    asyncio.run(scenario())


def test_same_action_duplicate_and_concurrent_submissions_return_original(engine):
    async def scenario():
        sid, runtime, proposal = await seed(engine)
        results = await asyncio.gather(*(engine.submit_quote(sid, proposal.action_ref) for _ in range(6)))
        assert all(result.status == "saved" for result in results)
        assert all(result.quote == results[0].quote for result in results)
        assert runtime.proposal is None and counts(engine) == (1, 1)
        assert (await engine.submit_quote(sid, proposal.action_ref)).quote == results[0].quote
    asyncio.run(scenario())


def test_real_database_uniqueness_is_independent_of_memory_or_buttons(engine):
    async def scenario():
        sid, _, proposal = await seed(engine)
        quote = (await engine.submit_quote(sid, proposal.action_ref)).quote
        # Bypass no production API: verify the actual constraint directly in
        # this fixture's private DB, then roll the attempted duplicate back.
        with pytest.raises(sqlite3.IntegrityError):
            with connect_saas(engine.settings) as db:
                db.execute("""INSERT INTO quote_actions (action_ref, session_id, proposal_id, quote_id)
                    VALUES (?, ?, ?, ?)""", (proposal.action_ref, "other-session", "other-proposal", quote.quote_id))
        assert counts(engine) == (1, 1)
    asyncio.run(scenario())


def test_separately_prepared_same_content_actions_can_save_two_quotes(engine):
    async def scenario():
        sid, runtime, first = await seed(engine)
        one = await engine.submit_quote(sid, first.action_ref)
        second = prepare_proposal(runtime.state, session_id=sid, turn_id="separate-display",
            interaction_revision=runtime.interaction_revision, view_id=runtime.current_view_id)
        runtime.proposal = second
        two = await engine.submit_quote(sid, second.action_ref)
        assert one.status == two.status == "saved"
        assert first.requirements == second.requirements and first.pricing == second.pricing
        assert first.action_ref != second.action_ref and one.quote.quote_id != two.quote.quote_id
        assert counts(engine) == (2, 2)
    asyncio.run(scenario())


def test_fault_inside_quote_transaction_rolls_back_both_rows_and_receipt(engine, monkeypatch):
    async def scenario():
        sid, runtime, proposal = await seed(engine)
        observed = []
        def fail_before_commit(db, value):
            observed.append((db.in_transaction,
                db.execute("SELECT COUNT(*) FROM quotes").fetchone()[0],
                db.execute("SELECT COUNT(*) FROM quote_actions").fetchone()[0]))
            raise RuntimeError("controlled failure inside Quote transaction before commit")
        monkeypatch.setattr(engine.quotes, "_before_commit", fail_before_commit)
        result = await engine.submit_quote(sid, proposal.action_ref)
        assert observed == [(True, 1, 1)]
        assert result.status == "not_saved" and result.quote is None
        assert counts(engine) == (0, 0) and runtime.proposal is None
        assert (await engine.resolve_quote_action(sid, proposal.action_ref)).status == "not_saved"
        assert (await engine.submit_quote(sid, proposal.action_ref)).status == "not_saved"
    asyncio.run(scenario())


def test_commit_then_exception_is_recovered_from_real_committed_fact(engine, monkeypatch):
    async def scenario():
        sid, _, proposal = await seed(engine)
        def commit_then_drop_response(db):
            db.commit()
            raise OSError("controlled response lost after real SQLite commit")
        monkeypatch.setattr(engine.quotes, "_commit", commit_then_drop_response)
        result = await engine.submit_quote(sid, proposal.action_ref)
        assert result.status == "saved" and counts(engine) == (1, 1)
        recovered = await engine.resolve_quote_action(sid, proposal.action_ref)
        assert recovered.status == "saved" and recovered.quote == result.quote
    asyncio.run(scenario())


def test_commit_receipt_and_followup_read_failure_remain_unknown_until_recovered(engine, monkeypatch):
    async def scenario():
        sid, _, proposal = await seed(engine)
        original_find = engine.quotes.find_action
        def commit_then_reads_fail(db):
            db.commit()
            monkeypatch.setattr(engine.quotes, "find_action", unavailable)
            raise OSError("controlled lost receipt plus database unavailable")
        monkeypatch.setattr(engine.quotes, "_commit", commit_then_reads_fail)
        result = await engine.submit_quote(sid, proposal.action_ref)
        assert result.status == "unknown" and result.quote is None
        assert counts(engine) == (1, 1)
        assert (await engine.resolve_quote_action(sid, proposal.action_ref)).status == "unknown"
        monkeypatch.setattr(engine.quotes, "find_action", original_find)
        recovered = await engine.resolve_quote_action(sid, proposal.action_ref)
        assert recovered.status == "saved" and recovered.quote.snapshot == proposal
        assert (await engine.submit_quote(sid, proposal.action_ref)).quote == recovered.quote
        assert counts(engine) == (1, 1)
    asyncio.run(scenario())


def test_discarded_response_and_broken_auxiliary_chat_store_do_not_undo_save(engine, monkeypatch):
    async def scenario():
        sid, _, proposal = await seed(engine)
        monkeypatch.setattr(engine.conversations, "add_turn", unavailable)
        # Deliberately discard the returned receipt, as a disconnected caller
        # would. No subsequent model or auxiliary text is needed for recovery.
        await engine.submit_quote(sid, proposal.action_ref)
        recovered = await engine.resolve_quote_action(sid, proposal.action_ref)
        assert recovered.status == "saved" and recovered.quote.snapshot == proposal
        assert counts(engine) == (1, 1)
    asyncio.run(scenario())


def test_database_read_failure_never_announces_definitive_non_save(engine, monkeypatch):
    async def scenario():
        sid, runtime, proposal = await seed(engine)
        original_find = engine.quotes.find_action
        monkeypatch.setattr(engine.quotes, "find_action", unavailable)
        for operation in (engine.submit_quote, engine.resolve_quote_action):
            result = await operation(sid, proposal.action_ref)
            assert result.status == "unknown" and result.quote is None
        assert counts(engine) == (0, 0)
        monkeypatch.setattr(engine.quotes, "find_action", original_find)
        result = await engine.resolve_quote_action(sid, proposal.action_ref)
        assert result.status == "not_saved" and runtime.proposal is None
        late = await engine.submit_quote(sid, proposal.action_ref)
        assert late.status == "not_saved" and counts(engine) == (0, 0)
    asyncio.run(scenario())


def test_check_first_closes_creation_permission_and_late_submit_cannot_reverse_it(engine):
    async def scenario():
        sid, runtime, proposal = await seed(engine)
        result = await engine.resolve_quote_action(sid, proposal.action_ref)
        assert result.status == "not_saved" and result.quote is None and runtime.proposal is None
        late_results = await asyncio.gather(*(engine.submit_quote(sid, proposal.action_ref) for _ in range(3)))
        assert all(result.status == "not_saved" for result in late_results)
        assert counts(engine) == (0, 0)
    asyncio.run(scenario())


def test_submit_first_keeps_lock_until_commit_so_result_check_cannot_misreport(engine, monkeypatch):
    async def scenario():
        sid, runtime, proposal = await seed(engine)
        entered, release = asyncio.Event(), asyncio.Event()
        original_write = engine._write_quote
        async def delayed_write(value):
            assert runtime.lock.locked() and value is proposal
            entered.set()
            await release.wait()
            return await original_write(value)
        monkeypatch.setattr(engine, "_write_quote", delayed_write)
        submitting = asyncio.create_task(engine.submit_quote(sid, proposal.action_ref))
        await asyncio.wait_for(entered.wait(), 5)
        checked = await asyncio.wait_for(engine.resolve_quote_action(sid, proposal.action_ref), 5)
        assert checked.status == "unknown" and checked.code == "action_in_flight"
        assert runtime.lock.locked() and counts(engine) == (0, 0)
        release.set()
        saved = await submitting
        checked = await engine.resolve_quote_action(sid, proposal.action_ref)
        assert saved.status == checked.status == "saved" and saved.quote == checked.quote
        assert counts(engine) == (1, 1)
    asyncio.run(scenario())


def test_cancelled_http_waiter_cannot_unlock_a_still_committing_sqlite_worker(engine, monkeypatch):
    async def scenario():
        sid, runtime, proposal = await seed(engine)
        entered, release = asyncio.Event(), threading.Event()
        loop = asyncio.get_running_loop()
        def pause_real_transaction(db, value):
            loop.call_soon_threadsafe(entered.set)
            assert release.wait(10), "test did not release isolated transaction"
        monkeypatch.setattr(engine.quotes, "_before_commit", pause_real_transaction)
        submitting = asyncio.create_task(engine.submit_quote(sid, proposal.action_ref))
        try:
            await asyncio.wait_for(entered.wait(), 5)
            submitting.cancel()
            await asyncio.sleep(0)
            checked = await asyncio.wait_for(engine.resolve_quote_action(sid, proposal.action_ref), 5)
            assert checked.status == "unknown" and checked.code == "action_in_flight"
            assert runtime.lock.locked()
            submitting.cancel()
            await asyncio.sleep(0)
            assert runtime.lock.locked()
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await submitting
        checked = await engine.resolve_quote_action(sid, proposal.action_ref)
        assert checked.status == "saved" and checked.quote.snapshot == proposal
        assert counts(engine) == (1, 1)
    asyncio.run(scenario())


def test_new_message_first_invalidates_old_action_while_preserving_current_price(engine, monkeypatch):
    async def scenario():
        sid, runtime, proposal = await seed(engine)
        scripted(monkeypatch, [RuntimeError("controlled unavailable model")])
        await engine.chat(ChatRequest(session_id=sid, message="新插问"))
        result = await engine.submit_quote(sid, proposal.action_ref)
        assert result.status == "not_saved" and counts(engine) == (0, 0)
        assert runtime.state.current_task()["pricing"] == proposal.pricing.model_dump(mode="json")
    asyncio.run(scenario())


def test_saved_result_survives_later_message_and_page_invalidation(engine, monkeypatch):
    async def scenario():
        sid, _, proposal = await seed(engine, view_id="quotes-controlled-page-view")
        original = await engine.submit_quote(sid, proposal.action_ref)
        scripted(monkeypatch, [RuntimeError("controlled unavailable model")])
        await engine.chat(ChatRequest(session_id=sid, message="随后消息"))
        await engine.invalidate_view(proposal.view_id, "refresh")
        assert (await engine.submit_quote(sid, proposal.action_ref)).quote == original.quote
        assert (await engine.resolve_quote_action(sid, proposal.action_ref)).quote == original.quote
        assert counts(engine) == (1, 1)
    asyncio.run(scenario())


@pytest.mark.parametrize("kind", ["forged", "cross_session", "stale", "unicode", "client_changed_action"])
def test_invalid_actions_never_create_or_claim_another_result(engine, kind):
    async def scenario():
        sid, runtime, proposal = await seed(engine)
        target_sid, action = sid, proposal.action_ref
        if kind == "cross_session":
            target_sid = engine.conversations.ensure_conversation(None, "other isolated session")
        elif kind == "stale":
            await engine.invalidate_proposal(sid, "refresh")
        elif kind == "unicode":
            action = "伪造动作"
        elif kind == "client_changed_action":
            action = action[:-1] + ("a" if action[-1] != "a" else "b")
        else:
            action = "invented-action"
        result = await engine.submit_quote(target_sid, action)
        assert result.status == "not_saved" and result.quote is None
        assert counts(engine) == (0, 0)
        if kind != "stale":
            assert runtime.proposal is proposal
    asyncio.run(scenario())


def test_saved_action_ownership_is_checked_before_exposing_persistent_receipt(engine):
    async def scenario():
        sid, _, proposal = await seed(engine)
        saved = await engine.submit_quote(sid, proposal.action_ref)
        other = engine.conversations.ensure_conversation(None, "other isolated session")
        for operation in (engine.submit_quote, engine.resolve_quote_action):
            rejected = await operation(other, proposal.action_ref)
            assert rejected.status == "not_saved" and rejected.quote is None
        with pytest.raises(QuoteActionOwnershipError):
            engine.quotes.find_action(other, proposal.action_ref)
        # Local single-user Quote numbering intentionally crosses chat scope.
        assert engine.quotes.get(saved.quote.quote_id) == saved.quote
        assert counts(engine) == (1, 1)
    asyncio.run(scenario())


def test_changed_current_server_object_is_rejected_without_silent_repricing(engine):
    async def scenario():
        sid, runtime, proposal = await seed(engine)
        runtime.state.apply_patch({"intent": "selection", "parameters": [
            {"name": "paid_seats", "value": 50, "user_quote": "明确改为50席"}]}, "明确改为50席")
        result = await engine.submit_quote(sid, proposal.action_ref)
        assert result.status == "not_saved" and result.quote is None
        assert counts(engine) == (0, 0) and runtime.proposal is None
    asyncio.run(scenario())


@pytest.mark.parametrize("mutation", ["presentation_product", "presentation_feature", "lowest_name",
    "selected_name", "current_catalog_plan_name", "current_catalog_feature_name"])
def test_changed_snapshot_or_same_version_display_facts_require_fresh_confirmation(engine, monkeypatch, mutation):
    async def scenario():
        sid, runtime, proposal = await seed(engine, selected="business")
        assert proposal.lowest_plan_id == "team" and proposal.selected_plan_id == "business"
        if mutation.startswith("current_catalog"):
            catalog = get_catalog()
            if mutation == "current_catalog_plan_name":
                altered = catalog.model_copy(update={"plans": tuple(plan.model_copy(
                    update={"name": "changed " + plan.name}) for plan in catalog.plans)})
            else:
                altered = catalog.model_copy(update={"features": tuple(feature.model_copy(
                    update={"name": "changed " + feature.name}) for feature in catalog.features)})
            assert altered.version == proposal.catalog_version
            monkeypatch.setattr("app.services.proposals.get_catalog", lambda: altered)
        elif mutation == "presentation_product":
            runtime.proposal = proposal.model_copy(update={"presentation": proposal.presentation.model_copy(
                update={"product_name": "changed product"})})
        elif mutation == "presentation_feature":
            runtime.proposal = proposal.model_copy(update={"presentation": proposal.presentation.model_copy(
                update={"features": tuple(feature.model_copy(update={"name": "changed feature"})
                    for feature in proposal.presentation.features)})})
        else:
            field = "lowest_plan_name" if mutation == "lowest_name" else "selected_plan_name"
            runtime.proposal = proposal.model_copy(update={field: "changed plan name"})
        result = await engine.submit_quote(sid, proposal.action_ref)
        assert result.status == "not_saved" and result.code == "proposal_facts_changed"
        assert result.quote is None and runtime.proposal is None and counts(engine) == (0, 0)
    asyncio.run(scenario())


@pytest.mark.parametrize("statement", ["UPDATE quotes SET snapshot_json = '{}'",
    "DELETE FROM quotes", "UPDATE quote_actions SET session_id = 'changed-session'",
    "DELETE FROM quote_actions"])
def test_saved_quote_and_action_rows_are_database_immutable(engine, statement):
    async def scenario():
        sid, _, proposal = await seed(engine)
        saved = await engine.submit_quote(sid, proposal.action_ref)
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            with connect_saas(engine.settings) as db:
                db.execute(statement)
        assert engine.quotes.get(saved.quote.quote_id) == saved.quote
        assert engine.quotes.find_action(sid, proposal.action_ref) == saved.quote
        assert counts(engine) == (1, 1)
    asyncio.run(scenario())


def test_restart_uses_same_database_preserves_success_but_never_replays_pending(engine):
    async def scenario():
        sid, _, proposal = await seed(engine)
        saved = await engine.submit_quote(sid, proposal.action_ref)
        pending_sid, _, pending = await seed(engine)
        db_path = saas_database_path(engine.settings)
        assert db_path.exists()
        restarted = Orchestrator(engine.settings)
        assert restarted.work.peek(sid) is None and restarted.work.peek(pending_sid) is None
        assert (await restarted.resolve_quote_action(sid, proposal.action_ref)).quote == saved.quote
        assert (await restarted.submit_quote(sid, proposal.action_ref)).quote == saved.quote
        assert restarted.quotes.get(saved.quote.quote_id) == saved.quote
        assert (await restarted.submit_quote(pending_sid, pending.action_ref)).status == "not_saved"
        assert (await restarted.resolve_quote_action(pending_sid, pending.action_ref)).status == "not_saved"
        assert db_path.exists() and counts(engine) == (1, 1)
    asyncio.run(scenario())


def test_saved_history_never_uses_changed_price_names_or_source_from_current_materials(engine, monkeypatch, tmp_path):
    async def scenario():
        sid, _, proposal = await seed(engine, monkeypatch, sso=True)
        saved = await engine.submit_quote(sid, proposal.action_ref)
        catalog = get_catalog()
        altered = catalog.model_copy(update={"version": "controlled-quotes-next-version",
            "plans": tuple(plan.model_copy(update={"name": "Changed " + plan.name,
                "unit_price_minor": plan.unit_price_minor + 9000}) for plan in catalog.plans)})
        isolated_catalog = tmp_path / "next-version-catalog.json"
        isolated_source = tmp_path / "next-version-source.json"
        isolated_catalog.write_text(altered.model_dump_json(), encoding="utf-8")
        changed_sources = [item.source.model_copy(update={"raw_text": "changed isolated original text"}).model_dump(mode="json")
            for item in proposal.adopted_sources]
        isolated_source.write_text(json.dumps(changed_sources, ensure_ascii=False), encoding="utf-8")
        monkeypatch.setattr("app.services.proposals.get_catalog", lambda: altered)
        monkeypatch.setattr("app.services.knowledge._read_index", unavailable)
        assert json.loads(isolated_catalog.read_text(encoding="utf-8"))["version"] != proposal.catalog_version
        assert json.loads(isolated_source.read_text(encoding="utf-8"))[0]["raw_text"] != proposal.adopted_sources[0].source.raw_text
        restored = engine.quotes.get(saved.quote.quote_id)
        assert restored == saved.quote
        assert restored.snapshot.adopted_sources == proposal.adopted_sources
        assert restored.snapshot.pricing == proposal.pricing
        assert (await engine.resolve_quote_action(sid, proposal.action_ref)).quote == saved.quote
        assert counts(engine) == (1, 1)
    asyncio.run(scenario())


def test_submission_resolution_and_number_lookup_never_require_model_calls(engine, monkeypatch):
    async def scenario():
        sid, _, proposal = await seed(engine)
        class ForbiddenModel:
            def __init__(self, *args, **kwargs):
                pytest.fail("Quote service called a model")
        monkeypatch.setattr(orchestrator_module, "DeepSeekClient", ForbiddenModel)
        saved = await engine.submit_quote(sid, proposal.action_ref)
        assert saved.status == "saved"
        assert (await engine.resolve_quote_action(sid, proposal.action_ref)).quote == saved.quote
        assert engine.quotes.get(saved.quote.quote_id) == saved.quote
        names = {schema["function"]["name"] for schema in BusinessToolRegistry(
            engine.settings, engine.work.get(sid).state, "read only").schemas()}
        assert "query_quote" in names
        assert not any("save" in name or "submit" in name or "create_quote" == name for name in names)
    asyncio.run(scenario())


@pytest.mark.parametrize("quote_id,code", [("bad-id", "invalid_quote_id"),
    ("Q-" + "0" * 32, "quote_not_found"), ("Q-" + "z" * 32, "invalid_quote_id")])
def test_invalid_or_absent_number_lookup_is_accurate_and_never_writes(engine, quote_id, code):
    with pytest.raises(QuoteReadError) as error:
        engine.quotes.get(quote_id)
    assert error.value.code == code and counts(engine) == (0, 0)


def test_real_database_read_fault_is_not_confused_with_missing_quote(engine, monkeypatch):
    monkeypatch.setattr("app.services.quotes.connect_saas", unavailable)
    with pytest.raises(QuoteReadError) as error:
        engine.quotes.get("Q-" + "0" * 32)
    assert error.value.code == "quote_read_unavailable"
    assert counts(engine) == (0, 0)


def test_additive_saas_initialization_preserves_chat_and_other_databases(engine, tmp_path):
    sid = engine.conversations.ensure_conversation(None, "existing isolated chat")
    engine.conversations.add_turn(sid, "existing question", "existing answer")
    unrelated = tmp_path / "existing-unrelated.db"
    with sqlite3.connect(unrelated) as db:
        db.execute("CREATE TABLE sentinel (value TEXT)")
        db.execute("INSERT INTO sentinel VALUES ('preserved')")
    before = unrelated.read_bytes()
    init_saas_db(engine.settings)
    assert unrelated.read_bytes() == before
    assert [message.content for message in engine.conversations.get_messages(sid)] == ["existing question", "existing answer"]
    assert counts(engine) == (0, 0)
