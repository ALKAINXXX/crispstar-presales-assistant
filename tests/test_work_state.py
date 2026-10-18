"""Accepted business state, independent of model wording and text history.

Candidates below are controlled model proposals. The evidence-lifecycle fixture
replaces only the retrieval worker's returned observation; the actual
KnowledgeService still issues/owns IDs, validates the formal index and sources,
and checks the real condition with the production matcher. These tests prove
state/lifecycle and candidate rejection, not normal query embeddings or model
language understanding. Those require separate live-model verification.
"""

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.schemas.retrieval import RetrievalEvidence, RetrievalHit, RetrievalOptions
from app.services.knowledge import DEFAULT_INDEX_PATH, KnowledgeError, _read_index
from app.services.work_state import SessionWorkStore, WorkState
from app.services.pricing import calculate_subscription_price


CONDITION = "saml_sso_compatible_idp"


@pytest.fixture
def state():
    return WorkState(Settings(_env_file=None))


def feature(feature_id, quote, statement=None):
    return {"kind": "feature", "feature_id": feature_id,
            "statement": statement or quote, "user_quote": quote}


def parameter(name, value, quote):
    return {"name": name, "value": value, "user_quote": quote}


def condition(value, quote):
    return {"condition_id": CONDITION, "value": value, "user_quote": quote}


def patch(state, message, **values):
    return state.apply_patch({"intent": "selection", **values}, message)


def test_incremental_updates_retain_hard_unknown_ambiguous_and_explicit_parameters(state):
    first = "必须支持产品 API；必须支持 SCIM 自动开通；必须对接我们的系统；订阅一年。"
    patch(state, first, add=[
        feature("api_access", "必须支持产品 API"),
        {"kind": "unmodeled", "statement": "必须支持 SCIM 自动开通", "user_quote": "必须支持 SCIM 自动开通"},
        {"kind": "ambiguous", "statement": "必须对接我们的系统", "user_quote": "必须对接我们的系统"},
    ], parameters=[parameter("months", 12, "订阅一年")])
    accepted = state.requirements

    patch(state, "需要审计日志，明确购买 80 席。", add=[feature("audit_logs", "需要审计日志")],
          parameters=[parameter("paid_seats", 80, "明确购买 80 席")])
    assert state.requirements.items[:3] == accepted.items
    assert {p.name: p.value for p in state.requirements.parameters} == {"months": 12, "paid_seats": 80}
    assert state.requirements.requirement_id == accepted.requirement_id
    assert state.requirements.previous_revision == accepted.revision

    patch(state, "再解释一下价格口径。", intent="knowledge")
    assert len(state.requirements.items) == 4
    assert {i.kind for i in state.requirements.items} == {"feature", "unmodeled", "ambiguous"}
    result = state.match()
    assert result.status == "blocked"
    assert {"unmodeled_requirement", "ambiguous_requirement"} <= set(result.blocking_reasons)
    assert all(len(plan.items) == 4 for plan in result.plans)


def test_explicit_removal_changes_scope_revision_and_returns_reviewable_change(state):
    patch(state, "需要产品 API 和审计日志。", add=[
        feature("api_access", "产品 API"), feature("audit_logs", "审计日志")])
    previous = state.requirements
    target = next(i for i in previous.items if i.feature_id == "audit_logs")
    output = patch(state, "不再需要审计日志，API 仍必须保留。", remove=[{
        "requirement_id": target.requirement_id, "user_quote": "不再需要审计日志"}])

    assert state.requirements.requirement_id == previous.requirement_id
    assert state.requirements.previous_revision == previous.revision
    assert state.requirements.revision == previous.revision + 1
    assert [i.feature_id for i in state.requirements.items] == ["api_access"]
    assert any(target.statement in change and "删除" in change for change in output["changes"])
    assert output["requirements"] == state.requirements.model_dump(mode="json")
    assert state.match().selected_plan_id == "team"


@pytest.mark.parametrize("target, quote", [("missing-id", "不要 API 了"), ("accepted", "可以买 Basic")])
def test_removal_needs_existing_identity_and_actual_explicit_instruction(state, target, quote):
    patch(state, "必须有产品 API", add=[feature("api_access", "必须有产品 API")])
    accepted = state.requirements
    identity = accepted.items[0].requirement_id if target == "accepted" else target
    with pytest.raises(ValueError):
        patch(state, quote, remove=[{"requirement_id": identity, "user_quote": quote}])
    assert state.requirements == accepted


def test_bad_user_reference_rejects_whole_patch_without_losing_previous_state(state):
    patch(state, "需要产品 API", add=[feature("api_access", "需要产品 API")])
    accepted = state.requirements
    with pytest.raises(ValueError, match="实际用户输入"):
        patch(state, "需要审计日志", add=[feature("audit_logs", "需要审计日志")],
              parameters=[parameter("paid_seats", 80, "购买 80 席")])
    assert state.requirements == accepted


def test_removal_must_name_the_target_instead_of_cancelling_another_accepted_need(state):
    patch(state, "必须有产品 API 和审计日志", add=[
        feature("api_access", "产品 API"), feature("audit_logs", "审计日志")])
    accepted = state.requirements
    api = next(i for i in accepted.items if i.feature_id == "api_access")
    with pytest.raises(ValueError, match="未指向该硬需求"):
        patch(state, "不再需要审计日志", remove=[{
            "requirement_id": api.requirement_id, "user_quote": "不再需要审计日志"}])
    assert state.requirements == accepted


def test_year_can_be_twelve_months_but_company_size_stays_context(state):
    patch(state, "公司约 80 人，订阅一年。", context_quotes=["公司约 80 人"],
          parameters=[parameter("months", 12, "订阅一年")])
    assert {p.name: p.value for p in state.requirements.parameters} == {"months": 12}
    assert state.requirements.context_quotes == ("公司约 80 人",)
    with pytest.raises(ValueError):
        state.validate_price("basic", 80, 12)


@pytest.mark.parametrize("value, quote", [(True, "1席"), (80.0, "80席"), (80.5, "80.5席"), ("80", "80席")])
def test_candidate_numeric_types_do_not_coerce_boolean_decimal_or_string(state, value, quote):
    with pytest.raises(ValidationError):
        patch(state, quote, parameters=[parameter("paid_seats", value, quote)])
    assert state.requirements.parameters == ()


@pytest.mark.parametrize("quote, value", [
    ("公司约 80 人", 80), ("我们有80人", 80), ("大概80席", 80),
    ("以后计划购买80席", 80), ("购买1.5席", 5), ("购买-80席", 80),
    ("购买负80席", 80), ("购买一两席", 2),
])
def test_company_population_approximation_future_or_numeric_suffix_is_not_explicit_seats(state, quote, value):
    with pytest.raises(ValueError):
        patch(state, quote, parameters=[parameter("paid_seats", value, quote)])
    assert state.requirements.parameters == ()


def test_bare_number_answers_only_one_identified_numeric_question(state):
    state.pending = ("paid_seats", "months")
    with pytest.raises(ValueError):
        patch(state, "80", parameters=[parameter("paid_seats", 80, "80")])
    assert state.requirements.parameters == ()
    state.pending = ("paid_seats",)
    patch(state, "80", parameters=[parameter("paid_seats", 80, "80")])
    assert state.requirements.parameters[0].value == 80


@pytest.mark.parametrize("message,name,value,quote", [
    ("计划购买约80席", "paid_seats", 80, "80席"),
    ("不是80席", "paid_seats", 80, "80席"),
    ("购买1.5席", "paid_seats", 5, "5席"),
    ("购买-80席", "paid_seats", 80, "80席"),
    ("计划订阅12个月", "months", 12, "12个月"),
])
def test_truncated_candidate_cannot_remove_actual_clause_qualifiers(state, message, name, value, quote):
    before = state.requirements
    with pytest.raises(ValueError) as failure:
        patch(state, message, parameters=[parameter(name, value, quote)])
    assert failure.value.details["field"] == name
    assert failure.value.details["issue"] == "parameter_not_explicit"
    assert state.requirements == before


@pytest.mark.parametrize("message", ["以后准备兼容身份提供方", "没有兼容身份提供方", "兼容身份提供方还不确定"])
def test_short_condition_quote_cannot_hide_future_negation_or_uncertainty(state, message):
    with pytest.raises(ValueError):
        patch(state, message, conditions=[condition("yes", "兼容身份提供方")])
    assert state.requirements.condition_assertions == ()


def test_distinct_clauses_accept_multiple_explicit_facts_without_using_background_uncertainty(state):
    patch(state, "公司约80人，明确购买50席，订阅一年，目前已具备兼容身份提供方。",
        parameters=[parameter("paid_seats", 50, "50席"), parameter("months", 12, "一年")],
        conditions=[condition("yes", "兼容身份提供方")])
    assert {p.name: p.value for p in state.requirements.parameters} == {"paid_seats": 50, "months": 12}
    assert state.requirements.condition_assertions[0].value == "yes"


def test_independent_price_does_not_replace_selected_plan_parameters_or_current_price(state):
    patch(state, "API必须有，主动选择Business，80席、12个月，算价", add=[feature("api_access", "API必须有")],
        selected_plan_id="business", selection_quote="主动选择Business", pricing_quote="算价",
        parameters=[parameter("paid_seats", 80, "80席"), parameter("months", 12, "12个月")])
    original = state.requirements
    match = state.match()
    price = calculate_subscription_price("business", 80, 12)
    state.remember_result(match, price, (), ())
    assert state.current_task()["pricing"]["base_price_minor"] == 11_520_000
    patch(state, "临时计算Team，50席、6个月多少钱", intent="price", selected_plan_id="team", selection_quote="Team",
        parameters=[parameter("paid_seats", 50, "50席"), parameter("months", 6, "6个月")])
    state.validate_price("team", 50, 6)
    inquiry = calculate_subscription_price("team", 50, 6)
    state.remember_result(None, inquiry, (), ())
    assert inquiry.base_price_minor == 2_400_000
    task = state.current_task()
    assert task["requirements"] == original.model_dump(mode="json")
    assert task["match"]["selected_plan_id"] == "business"
    assert task["pricing"]["base_price_minor"] == 11_520_000
    patch(state, "返回之前的选型")
    assert state.requirements == original
    assert state.match().selected_plan_id == "business"
    state.validate_price("business", 80, 12)


def test_independent_price_cannot_borrow_selection_parameters_and_rejected_switch_keeps_task(state):
    patch(state, "API必须有，80席、12个月", add=[feature("api_access", "API必须有")],
        parameters=[parameter("paid_seats", 80, "80席"), parameter("months", 12, "12个月")])
    original = state.requirements
    with pytest.raises(ValueError):
        patch(state, "临时计算Team", intent="price", selected_plan_id="team", selection_quote="Team", parameters=[parameter("months", 6, "6个月")])
    assert state.requirements == original and state.intent == "selection"
    patch(state, "临时计算Team", intent="price", selected_plan_id="team", selection_quote="Team")
    assert state.requirements.parameters == ()
    with pytest.raises(ValueError):
        state.validate_price("team", 80, 12)
    assert state.current_task()["requirements"] == original.model_dump(mode="json")


def test_return_from_independent_price_can_remove_one_original_need_and_keep_original_parameters(state):
    patch(state, "API必须有，导出必须有，80席、12个月，算价", add=[feature("api_access", "API必须有"), feature("data_export", "导出必须有")],
        pricing_quote="算价", parameters=[parameter("paid_seats", 80, "80席"), parameter("months", 12, "12个月")])
    identity = next(i.requirement_id for i in state.requirements.items if i.feature_id == "api_access")
    state.remember_result(state.match(), calculate_subscription_price("team", 80, 12), (), ())
    patch(state, "临时计算Team，50席、6个月", intent="price", selected_plan_id="team", selection_quote="Team",
        parameters=[parameter("paid_seats", 50, "50席"), parameter("months", 6, "6个月")])
    patch(state, "回原选型，取消API", remove=[{"requirement_id": identity, "user_quote": "取消API"}])
    assert [i.feature_id for i in state.requirements.items] == ["data_export"]
    assert {p.name: p.value for p in state.requirements.parameters} == {"paid_seats": 80, "months": 12}
    assert state.current_task()["status"] == "needs_reassessment"
    assert state.current_task()["pricing"] is None
    assert state.match().selected_plan_id == "basic"
    assert any("已删除" in change and "API" in change for change in state.changes)


def test_accepted_selection_revision_invalidates_previous_amount_without_losing_unchanged_need(state):
    patch(state, "API必须有，80席、12个月，算价", add=[feature("api_access", "API必须有")], pricing_quote="算价",
        parameters=[parameter("paid_seats", 80, "80席"), parameter("months", 12, "12个月")])
    state.remember_result(state.match(), calculate_subscription_price("team", 80, 12), (), ())
    patch(state, "改为50席", parameters=[parameter("paid_seats", 50, "50席")])
    task = state.current_task()
    assert task["status"] == "needs_reassessment" and task["match"] is None and task["pricing"] is None
    assert [i["feature_id"] for i in task["requirements"]["items"]] == ["api_access"]
    assert {p["name"]: p["value"] for p in task["requirements"]["parameters"]} == {"paid_seats": 50, "months": 12}


def test_current_task_keeps_prior_valid_result_with_explicit_not_updated_identity(state):
    patch(state, "API必须有，80席、12个月，算价", add=[feature("api_access", "API必须有")], pricing_quote="算价",
        parameters=[parameter("paid_seats", 80, "80席"), parameter("months", 12, "12个月")])
    state.remember_result(state.match(), calculate_subscription_price("team", 80, 12), (), ())
    before = state.requirements
    with pytest.raises(ValueError):
        patch(state, "约50席", parameters=[parameter("paid_seats", 50, "50席")])
    state.remember_result(None, None, (), ())
    task = state.current_task()
    assert task["status"] == "current" and task["last_result"] == "not_updated"
    assert task["requirements"] == before.model_dump(mode="json")
    assert task["pricing"]["base_price_minor"] == 7_680_000


@pytest.mark.parametrize("intent,message,selection_quote", [
    ("knowledge", "SSO有什么前提？", None),
    ("unit_price", "Business每席多少钱？", "Business"),
])
def test_lookup_wording_is_not_a_selection_revision_or_reason_to_hide_current_amount(state, intent, message, selection_quote):
    patch(state, "API必须有，80席、12个月，算价", add=[feature("api_access", "API必须有")], pricing_quote="算价",
        parameters=[parameter("paid_seats", 80, "80席"), parameter("months", 12, "12个月")])
    state.remember_result(state.match(), calculate_subscription_price("team", 80, 12), (), ())
    original = state.requirements
    patch(state, message, intent=intent, context_quotes=[message], selection_quote=selection_quote)
    assert state.requirements == original
    assert state.current_task()["match"]["selected_plan_id"] == "team"
    assert state.current_task()["pricing"]["base_price_minor"] == 7_680_000


def test_state_presence_does_not_create_or_restore_any_runtime():
    store = SessionWorkStore(Settings(_env_file=None))
    assert not store.has_state("missing") and store._sessions == {}
    runtime = store.get("a")
    assert not store.has_state("a")
    runtime.state.has_processed_turn = True
    assert store.has_state("a")
    assert not SessionWorkStore(Settings(_env_file=None)).has_state("a")


def test_single_pending_condition_accepts_specific_reply_without_filling_other_facts(state):
    state.pending = ("condition:" + CONDITION,)
    patch(state, "是的", conditions=[condition("yes", "是的")])
    assert state.requirements.condition_assertions[0].value == "yes"
    assert state.requirements.parameters == ()


@pytest.mark.parametrize("answer", ["好的", "可以", "嗯"])
def test_generic_assent_does_not_fill_one_of_multiple_pending_facts(state, answer):
    state.pending = ("paid_seats", "condition:" + CONDITION)
    with pytest.raises(ValueError):
        patch(state, answer, conditions=[condition("yes", answer)])
    assert state.requirements.condition_assertions == ()
    assert state.requirements.parameters == ()


@pytest.mark.parametrize("value, quote", [
    ("yes", "我们现在已有兼容的身份提供方"),
    ("no", "我们现在没有兼容的身份提供方"),
    ("unknown", "目前还不确定身份系统是否兼容"),
])
def test_explicit_condition_reply_keeps_yes_no_unknown_and_its_user_basis(state, value, quote):
    patch(state, quote, conditions=[condition(value, quote)])
    accepted = state.requirements.condition_assertions[0]
    assert accepted.value == value
    assert accepted.user_quote == quote
    patch(state, "增加产品 API。", add=[feature("api_access", "产品 API")])
    assert state.requirements.condition_assertions == (accepted,)


@pytest.mark.parametrize("value, quote", [
    ("yes", "没有兼容的身份提供方"),
    ("no", "已有兼容的身份提供方"),
    ("yes", "以后计划准备兼容身份提供方"),
    ("yes", "我们已有身份提供方，兼容性还不确定"),
])
def test_contradictory_future_or_ambiguous_assertion_cannot_become_confirmed_condition(state, value, quote):
    with pytest.raises(ValueError):
        patch(state, quote, conditions=[condition(value, quote)])
    assert state.requirements.condition_assertions == ()


def test_catalog_outside_feature_reference_must_not_claim_known_support(state):
    with pytest.raises(ValueError, match="未建模"):
        patch(state, "必须有 SCIM", add=[feature("scim", "必须有 SCIM")])
    assert state.requirements.items == ()


def test_standalone_pricing_requires_user_selected_plan_and_exact_accepted_parameters(state):
    patch(state, "按 Team 计算，购买80席，一年。", intent="price", selected_plan_id="team",
          selection_quote="按 Team 计算", parameters=[
              parameter("paid_seats", 80, "购买80席"), parameter("months", 12, "一年")])
    assert state.requirements.items == ()
    state.validate_price("team", 80, 12)
    for values in (("business", 80, 12), ("team", 50, 12), ("team", 80, 1)):
        with pytest.raises(ValueError):
            state.validate_price(*values)


def test_selection_price_uses_current_complete_match_and_invalidates_old_match(state):
    patch(state, "必须有 API，购买80席，一年，请算金额。", add=[feature("api_access", "必须有 API")],
          pricing_quote="请算金额",
          parameters=[parameter("paid_seats", 80, "购买80席"), parameter("months", 12, "一年")])
    with pytest.raises(ValueError, match="先取得"):
        state.validate_price("team", 80, 12)
    assert state.match().selected_plan_id == "team"
    state.validate_price("team", 80, 12)
    with pytest.raises(ValueError):
        state.validate_price("basic", 80, 12)

    patch(state, "还必须有审计日志", add=[feature("audit_logs", "必须有审计日志")])
    with pytest.raises(ValueError):
        state.validate_price("team", 80, 12)
    assert state.match().selected_plan_id == "business"
    state.validate_price("business", 80, 12)


def test_price_tool_can_recover_omitted_selected_plan_only_from_current_unique_catalog_label(state):
    message = "请按 Team 计算80席一年的订阅基价。"
    patch(state, message, intent="price", parameters=[
        parameter("paid_seats", 80, "80席"), parameter("months", 12, "一年")])
    previous = state.requirements
    assert previous.selected_plan_id is None
    state.accept_price_plan("team", message)
    assert state.requirements.selected_plan_id == "team"
    assert state.requirements.parameters == previous.parameters
    assert state.requirements.requirement_id == previous.requirement_id
    assert state.requirements.previous_revision == previous.revision
    assert any("Team" in q and q in message for q in state.requirements.context_quotes)
    assert state.requirements.items == ()
    state.validate_price("team", 80, 12)


@pytest.mark.parametrize("message, proposed_plan", [
    ("请比较 Team 和 Business 的订阅基价", "team"),
    ("请按 Team 计算订阅基价", "business"),
    ("按刚才那个方案算一下", "team"),
])
def test_price_plan_fallback_does_not_choose_between_labels_or_invent_reference(state, message, proposed_plan):
    patch(state, message, intent="price")
    accepted = state.requirements
    with pytest.raises(ValueError):
        state.accept_price_plan(proposed_plan, message)
    assert state.requirements == accepted
    assert state.requirements.selected_plan_id is None


def test_price_tool_fallback_does_not_override_previously_explicit_plan_selection(state):
    patch(state, "先按 Business 计算80席一年", intent="price", selected_plan_id="business",
          selection_quote="按 Business 计算", parameters=[
              parameter("paid_seats", 80, "80席"), parameter("months", 12, "一年")])
    accepted = state.requirements
    state.accept_price_plan("team", "改成 Team 计算")
    assert state.requirements == accepted
    with pytest.raises(ValueError):
        state.validate_price("team", 80, 12)


def test_explicitly_cancelling_price_preserves_accepted_parameters_and_matching_scope(state):
    patch(state, "必须有 API，购买80席一年，并算金额", add=[feature("api_access", "必须有 API")],
          pricing_quote="并算金额", parameters=[
              parameter("paid_seats", 80, "购买80席"), parameter("months", 12, "一年")])
    assert state.match().selected_plan_id == "team"
    state.validate_price("team", 80, 12)
    accepted = state.requirements

    output = patch(state, "先只比较，不计算金额", cancel_pricing_quote="只比较")
    assert state.wants_price is False
    assert output["wants_price"] is False
    assert state.requirements.items == accepted.items
    assert state.requirements.parameters == accepted.parameters
    assert state.match().selected_plan_id == "team"
    with pytest.raises(ValueError):
        state.validate_price("team", 80, 12)


@pytest.mark.parametrize("message, quote", [("好的", "好的"), ("继续选型", "只比较")])
def test_cancelling_price_requires_explicit_current_user_instruction(state, message, quote):
    patch(state, "必须有 API，请算金额", add=[feature("api_access", "必须有 API")], pricing_quote="请算金额")
    accepted = state.requirements
    with pytest.raises(ValueError):
        patch(state, message, cancel_pricing_quote=quote)
    assert state.wants_price is True
    assert state.requirements == accepted


@pytest.mark.parametrize("condition_pending", [(), ("condition:" + CONDITION,)])
def test_price_cancel_during_information_detour_invalidates_numeric_question_only(state, condition_pending):
    patch(state, "必须有 API，请算一年金额", add=[feature("api_access", "必须有 API")],
          pricing_quote="请算一年金额", parameters=[parameter("months", 12, "一年")])
    state.pending = ("paid_seats", *condition_pending)
    patch(state, "Team单价多少？之前报价不要计算价格", intent="unit_price", cancel_pricing_quote="不要计算价格")
    assert state.pending == condition_pending and state.wants_price is False
    assert state.active_intent == "selection" and state.intent == "unit_price"
    assert {p.name: p.value for p in state.requirements.parameters} == {"months": 12}
    accepted = state.requirements
    with pytest.raises(ValueError):
        patch(state, "80", parameters=[parameter("paid_seats", 80, "80")])
    assert state.requirements == accepted


@pytest.fixture
def controlled_condition_worker(monkeypatch):
    """Only _execute is replaced: no vector search success is claimed here."""
    index = _read_index(DEFAULT_INDEX_PATH)
    source = next(s for s in index.inputs.sources if CONDITION in s.condition_ids)
    calls = []

    def attach(state):
        def execute(request):
            calls.append((state.knowledge.chain_id, request["query"]))
            return RetrievalEvidence(
                retrieval_id=request["retrieval_id"], chain_id=request["chain_id"], query=request["query"],
                retrieved_at=datetime.now(timezone.utc).isoformat(),
                options=RetrievalOptions.model_validate(request["options"]), status="retrieved",
                hits=(RetrievalHit(rank=1, score=0.9, relevance="related", source=source),),
                product_id=index.inputs.product_id, dataset_version=index.inputs.dataset_version,
                index_build_id=index.build_id, index_inputs_sha256=index.inputs_sha256,
                embedding=index.inputs.embedding)
        monkeypatch.setattr(state.knowledge, "_execute", execute)
        return calls

    return attach


def accept_sso(state):
    patch(state, "必须有 SAML SSO，我们已有兼容的身份提供方。", add=[
        feature("saml_sso", "必须有 SAML SSO")], conditions=[
            condition("yes", "我们已有兼容的身份提供方")])


def test_condition_evidence_can_follow_same_requirement_across_explicit_parameter_revision(state, controlled_condition_worker):
    calls = controlled_condition_worker(state)
    accept_sso(state)
    first = state.match()
    assert first.status == "matched"
    assert first.selected_plan_id == "business"
    assert len(calls) == 1
    evidence_id = first.retrieval_evidence[0].retrieval_id
    previous = state.requirements

    patch(state, "购买80席，订阅一年，请算金额。", pricing_quote="请算金额", parameters=[
        parameter("paid_seats", 80, "购买80席"), parameter("months", 12, "订阅一年")])
    assert state.requirements.items == previous.items
    assert state.requirements.revision > previous.revision
    assert state.last_match is None
    current = state.match()
    assert len(calls) == 1
    assert [e.retrieval_id for e in current.retrieval_evidence] == [evidence_id]
    assert current.requirements == state.requirements
    assert current.status == "matched"
    state.validate_price("business", 80, 12)


def test_failed_condition_retrieval_remains_a_failure_record_and_later_match_can_retry(state, monkeypatch, controlled_condition_worker):
    accept_sso(state)
    accepted = state.requirements

    def failed_worker(request):
        raise RuntimeError("Controlled worker failure; no real outage is claimed.")

    monkeypatch.setattr(state.knowledge, "_execute", failed_worker)
    failed = state.match()
    assert failed.status == "blocked"
    assert state.requirements == accepted
    assert failed.retrieval_evidence[0].status == "unavailable"
    failed_id = failed.retrieval_evidence[0].retrieval_id
    business = next(p for p in failed.plans if p.plan_id == "business")
    assert business.items[0].status == "pending_condition"
    assert "retrieval_failed" in business.blocking_reasons
    assert "catalog_unsupported" not in business.blocking_reasons

    calls = controlled_condition_worker(state)
    recovered = state.match()
    assert state.requirements == accepted
    assert recovered.status == "matched"
    assert recovered.selected_plan_id == "business"
    assert len(calls) == 1
    assert failed_id not in {e.retrieval_id for e in recovered.retrieval_evidence}
    assert state.knowledge.evidence(failed_id).status == "unavailable"


def test_deleted_then_readded_requirement_gets_new_identity_and_fresh_evidence(state, controlled_condition_worker):
    calls = controlled_condition_worker(state)
    accept_sso(state)
    first = state.match()
    original = state.requirements.items[0]
    old_evidence_id = first.retrieval_evidence[0].retrieval_id

    patch(state, "不再需要 SAML SSO", remove=[{
        "requirement_id": original.requirement_id, "user_quote": "不再需要 SAML SSO"}])
    assert state.requirements.items == ()
    assert state.last_match is None
    # Do not match in the empty intermediate revision: old issuer records may
    # still exist, but that must not make a re-added requirement reuse them.
    assert state.knowledge.evidence(old_evidence_id).retrieval_id == old_evidence_id
    accept_sso(state)
    assert state.requirements.items[0].requirement_id != original.requirement_id
    current = state.match()
    assert len(calls) == 2
    assert old_evidence_id not in {e.retrieval_id for e in current.retrieval_evidence}
    assert current.status == "matched"


def test_expired_owner_record_cannot_be_replaced_with_serialized_previous_match(state, controlled_condition_worker):
    calls = controlled_condition_worker(state)
    accept_sso(state)
    original = state.match()
    identity = original.retrieval_evidence[0].retrieval_id
    for position in range(17):
        state.knowledge.retrieve(f"isolated lifecycle observation {position}")
    with pytest.raises(KnowledgeError):
        state.knowledge.evidence(identity)
    try:
        current = state.match()
    except KnowledgeError:
        # A failed refresh may safely stop here, but must invalidate the old
        # complete-match credential used by selection pricing.
        assert state.last_match is None
    else:
        # A bounded fresh retrieval is also valid; the prior serialized Match
        # cannot become evidence just because its requirement revision agrees.
        assert current.status == "matched"
        assert current.selected_plan_id == "business"
        assert len(calls) == 19
        assert identity not in {e.retrieval_id for e in current.retrieval_evidence}


def test_selection_price_cannot_use_matching_snapshot_after_its_issuer_evidence_expires(state, controlled_condition_worker):
    controlled_condition_worker(state)
    accept_sso(state)
    patch(state, "购买80席，订阅一年，请算金额", pricing_quote="请算金额", parameters=[
        parameter("paid_seats", 80, "购买80席"), parameter("months", 12, "订阅一年")])
    state.match()
    state.validate_price("business", 80, 12)
    for position in range(17):
        state.knowledge.retrieve(f"isolated pricing lifecycle observation {position}")
    with pytest.raises((KnowledgeError, ValueError)):
        state.validate_price("business", 80, 12)


def test_current_task_rechecks_expired_evidence_without_new_search(state, controlled_condition_worker):
    # Worker substitution only exercises record lifecycle, not live retrieval.
    calls = controlled_condition_worker(state)
    accept_sso(state)
    patch(state, "80席、一年、请算金额", pricing_quote="请算金额", parameters=[
        parameter("paid_seats", 80, "80席"), parameter("months", 12, "一年")])
    state.remember_result(state.match(), calculate_subscription_price("business", 80, 12), (), ())
    assert state.current_task()["status"] == "current"
    for position in range(17):
        state.knowledge.retrieve(f"isolated task lifecycle observation {position}")
    count = len(calls)
    task = state.current_task()
    assert len(calls) == count
    assert task["status"] == "needs_reassessment"
    assert task["match"] is None and task["pricing"] is None


def test_recognized_condition_conflict_blocks_price_and_stays_relevant_in_new_parameter_revision(state, controlled_condition_worker):
    controlled_condition_worker(state)
    accept_sso(state)
    patch(state, "购买80席，订阅一年，请算金额", pricing_quote="请算金额", parameters=[
        parameter("paid_seats", 80, "购买80席"), parameter("months", 12, "订阅一年")])
    original = state.match()
    evidence = original.retrieval_evidence[0]
    state.knowledge.record_condition_conflict(
        evidence.retrieval_id, condition_id=CONDITION,
        source_ids=(evidence.hits[0].source.source_id,),
        reason="Controlled test of a server-recognized relevant contradiction; not a corpus change.",
        origin="controlled_test")
    with pytest.raises((KnowledgeError, ValueError)):
        state.validate_price("business", 80, 12)
    patch(state, "改为50席", parameters=[parameter("paid_seats", 50, "改为50席")])
    current = state.match()
    assert current.status == "blocked"
    assert "condition_evidence_conflict" in current.blocking_reasons
    assert current.retrieval_evidence[0].conflicts[0].origin == "controlled_test"
    with pytest.raises(ValueError):
        state.validate_price("business", 50, 12)


def test_new_session_and_restart_do_not_restore_business_facts_or_evidence_from_text(controlled_condition_worker):
    settings = Settings(_env_file=None)
    store = SessionWorkStore(settings)
    original = store.get("conversation-a")
    controlled_condition_worker(original.state)
    accept_sso(original.state)
    original.state.match()
    identity = original.state.last_match.retrieval_evidence[0].retrieval_id

    assert store.get("conversation-a") is original
    another = store.get("conversation-b")
    restarted = SessionWorkStore(settings).get("conversation-a")
    for runtime in (another, restarted):
        state = runtime.state
        assert state.requirements.requirement_id != original.state.requirements.requirement_id
        assert state.knowledge.chain_id != original.state.knowledge.chain_id
        assert state.requirements.items == state.requirements.parameters == state.requirements.condition_assertions == ()
        assert state.last_match is None
        assert state.evidence_bindings == {}
        assert runtime.lock is not original.lock
        with pytest.raises(KnowledgeError, match="本业务链"):
            state.knowledge.evidence(identity)
        # Old assistant/history text is never a valid reference for this turn.
        with pytest.raises(ValueError, match="实际用户输入"):
            patch(state, "继续算一下", parameters=[parameter("paid_seats", 80, "购买80席")])
