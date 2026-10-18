"""Input-review deterministic acceptance, using only synthetic process-local facts.

These controlled candidates do not claim real-model language accuracy. Ordinary
catalog matching/pricing/Proposal validation remain the production mechanisms.
No application SQLite file or user record is read by this suite.
"""

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.services.pricing import calculate_subscription_price
from app.services.proposals import ProposalRejected, prepare_proposal
from app.services.work_state import InputRejected, WorkState


@pytest.fixture
def state():
    return WorkState(Settings(_env_file=None))


def op(action, quote, **values):
    return {"action": action, "span": {"quote": quote}, **values}


def group(identity, *operations, verdict="accept", reason="confirmed", depends_on=()):
    return {"group_id": identity, "operations": operations, "verdict": verdict,
            "reason": reason, "depends_on": depends_on}


def feature(identity, quote, kind="feature"):
    return {"kind": kind, "feature_id": identity, "statement": quote, "user_quote": quote}


def review(state, message, *groups, kind="selection", relation="continue", target=None,
           intent=None, coverage_complete=True, candidate=None, **values):
    state.begin_review_turn()
    context = state._selection_context if kind == "selection" else state._price_context
    task_id = context.requirements.requirement_id if context else (
        state.requirements.requirement_id if state._working_intent is None else None)
    target = target or {"kind": kind, "relation": relation, "task_id": task_id}
    return state.apply_review({"intent": intent or kind, "target": target,
        "coverage_complete": coverage_complete, "groups": groups, **values}, message, candidate or {})


def s1(state):
    message = "必须支持API，购买80席，订阅一年，请算基价。"
    state.apply_patch({"intent": "selection", "add": [feature("api_access", "必须支持API")],
        "parameters": [{"name": "paid_seats", "value": 80, "user_quote": "购买80席"},
                       {"name": "months", "value": 12, "user_quote": "订阅一年"}],
        "pricing_quote": "请算基价"}, message)
    match = state.match()
    price = calculate_subscription_price("team", 80, 12)
    state.validate_price("team", 80, 12)
    state.record_pricing_result(price)
    state.remember_result(match, price, (), (), turn_id="synthetic-old-turn")
    state.bind_result_message("synthetic-old-turn", "synthetic-old-message")
    return state.requirements, match, price


@pytest.mark.parametrize("quote", ["实际付费席位数为50", "实际购买五十席", "改为50席"])
def test_v01_v02_v03_verified_integer_expression_and_float_representation(state, quote):
    original, _, _ = s1(state)
    review(state, quote, group("seats", op("set", quote, field="paid_seats", value=50)),
        candidate={"parameters": [{"name": "paid_seats", "value": 50.0, "user_quote": quote}]})
    assert {p.name: p.value for p in state.requirements.parameters} == {"paid_seats": 50, "months": 12}
    assert state.requirements.requirement_id == original.requirement_id
    assert state.requirements.revision == original.revision + 1
    assert not state.review_preserve_results
    assert state.match().selected_plan_id == "team"
    state.validate_price("team", 50, 12)


def test_v20_population_rejection_preserves_other_accepted_groups(state):
    message = "公司有50个人，订阅一年，需要API。"
    review(state, message,
        group("population", op("set", "公司有50个人", field="paid_seats", value=50,
            speech_act="background")),
        group("term", op("set", "订阅一年", field="months", value=12)),
        group("api", op("add", "需要API", requirement=feature("api_access", "需要API"))))
    assert {p.name: p.value for p in state.requirements.parameters} == {"months": 12}
    assert [item.feature_id for item in state.requirements.items] == ["api_access"]
    assert state.operation_feedback[0]["issue"] == "not_actual_purchase"
    assert state.operation_feedback[1]["status"] == "accepted"
    assert state.match().selected_plan_id == "team"
    with pytest.raises(InputRejected):
        state.validate_price("team", 50, 12)


@pytest.mark.parametrize("term", ["订阅一整年", "订阅一个整年", "订阅一个完整的年度", "订阅一整个年"])
def test_complete_year_quantity_preserves_partial_acceptance_without_population_purchase(state, term):
    message = f"公司有50个人，{term}，需要API。"
    review(state,message,
        group("population",op("set","公司有50个人",field="paid_seats",value=50,speech_act="background")),
        group("term",op("set",term,field="months",value=12)),
        group("api",op("add","需要API",requirement=feature("api_access","需要API"))))
    assert {p.name:p.value for p in state.requirements.parameters}=={"months":12}
    assert [item.feature_id for item in state.requirements.items]==["api_access"]
    assert state.operation_feedback[0]["issue"]=="not_actual_purchase"
    assert state.operation_feedback[1]["status"]=="accepted"
    with pytest.raises(ProposalRejected):
        prepare_proposal(state,session_id="synthetic",turn_id="year",interaction_revision=1)


@pytest.mark.parametrize("quote,value,issue", [
    ("预计订一整年",12,"uncertain"), ("如果订一整年",12,"conditional"),
    ("不订一整年",12,"negative"), ("订负一整年",12,"invalid_type"),
    ("订-1整年",12,"invalid_type"), ("订1.5整年",12,"invalid_type"),
    ("订两整年",24,"invalid_range"), ("订一整年半",18,"invalid_range"),
    ("订一整年",13,"extraction_failed"),
])
def test_complete_year_normalization_preserves_number_direction_and_range_guards(state,quote,value,issue):
    old,match,price=s1(state)
    review(state,quote,group("term",op("set",quote,field="months",value=value)))
    assert state.requirements is old
    assert state.last_match is match
    assert state._selection_context.pricing is price
    assert state.operation_feedback[0]["issue"]==issue


def test_complete_year_source_reaches_real_price_and_bound_proposal(state):
    message="需要API，实际购买50席，订一整年，请计算订阅基价。"
    review(state,message,
        group("api",op("add","需要API",requirement=feature("api_access","需要API"))),
        group("seats",op("set","实际购买50席",field="paid_seats",value=50)),
        group("term",op("set","订一整年",field="months",value=12)),
        group("price",op("request_pricing","请计算订阅基价",speech_act="instruction")))
    match=state.match()
    state.validate_price("team",50,12)
    price=calculate_subscription_price("team",50,12)
    state.record_pricing_result(price)
    state.remember_result(match,price,(),())
    proposal=prepare_proposal(state,session_id="synthetic",turn_id="complete-year",interaction_revision=1)
    assert proposal.pricing.base_price_minor==4_800_000
    assert next(p for p in proposal.requirements.parameters if p.name=="months").user_quote=="订一整年"


def test_v21_irrelevant_idp_rejection_does_not_block_complete_api_selection(state):
    message = "必须有API，实际买50席，订6个月，不需要SSO，请算价格。"
    review(state, message,
        group("api", op("add", "必须有API", requirement=feature("api_access", "必须有API"))),
        group("seats", op("set", "实际买50席", field="paid_seats", value=50)),
        group("term", op("set", "订6个月", field="months", value=6)),
        group("idp", op("set", "不需要SSO", field="saml_sso_compatible_idp", value="no"),
            verdict="reject", reason="negative"),
        group("price", op("request_pricing", "请算价格", speech_act="instruction")))
    assert state.review_blockers == ()
    assert state.requirements.condition_assertions == ()
    state.validate_price("team", 50, 6) if state.last_match else state.match()
    state.validate_price("team", 50, 6)
    price = calculate_subscription_price("team", 50, 6)
    state.record_pricing_result(price)
    state.remember_result(state.last_match, price, (), ())
    assert prepare_proposal(state, session_id="synthetic", turn_id="t", interaction_revision=1).pricing.base_price_minor == 2_400_000


def test_v07_uncertain_modification_retains_exact_old_fact_result_and_receipt(state):
    requirements, match, price = s1(state)
    receipt = state._selection_context.pricing_requirements
    message = "付费席位改成约70席，其他不变。"
    review(state, message, group("seats", op("set", "约70席", field="paid_seats", value=70,
        certainty="approximate")))
    assert state.requirements is requirements
    assert state.last_match is match
    assert state._selection_context.pricing is price
    assert state._selection_context.pricing_requirements is receipt
    assert state.review_preserve_results
    assert state.operation_feedback[0]["previous_value"] == 80
    with pytest.raises(ProposalRejected):
        prepare_proposal(state, session_id="synthetic", turn_id="new", interaction_revision=2)


def test_v08_clear_is_unknown_and_revokes_dependent_results(state):
    old, _, _ = s1(state)
    review(state, "原80席不作数，数量还没确定，期限一年。",
        group("clear", op("clear", "原80席不作数，数量还没确定", field="paid_seats", certainty="uncertain")),
        group("term", op("set", "期限一年", field="months", value=12)))
    assert [p.name for p in state.requirements.parameters] == ["months"]
    assert state.requirements.items == old.items
    assert state.last_match is None
    assert state._selection_context.pricing is None


def test_v09_bare_answer_requires_one_unique_pending_question(state):
    s1(state)
    state.pending = ("paid_seats",)
    review(state, "50", group("seats", op("set", "50", field="paid_seats", value=50)))
    assert next(p.value for p in state.requirements.parameters if p.name == "paid_seats") == 50
    state.pending = ("paid_seats", "months")
    review(state, "50和6", group("numbers", op("set", "50", field="paid_seats", value=50),
        op("set", "6", field="months", value=6)))
    assert state.operation_feedback[0]["issue"] == "ambiguous"
    assert next(p.value for p in state.requirements.parameters if p.name == "months") == 12


@pytest.mark.parametrize("value,quote,issue", [(50,"实际买50.5席","invalid_type"), (501,"实际买501席","invalid_range"), (-50,"实际买-50席","invalid_type")])
def test_v10_no_truncation_of_actual_decimal_or_out_of_range(state, value, quote, issue):
    review(state, quote, group("seats", op("set", quote, field="paid_seats", value=value)))
    assert state.requirements.parameters == ()
    assert state.operation_feedback[0]["issue"] == issue


def test_v16_replace_group_is_atomic_if_new_item_unreliable(state):
    old, _, _ = s1(state)
    old_api = old.items[0]
    message = "API撤回，改为对接现有系统。"
    review(state, message, group("replacement", op("replace", message,
        requirement_id=old_api.requirement_id, requirement=feature(None, "对接现有系统", "ambiguous"))))
    assert state.requirements is old
    assert state.operation_feedback[0]["issue"] == "ambiguous"


def test_v16_valid_replace_and_numeric_correction_share_one_revision(state):
    old, _, _ = s1(state)
    message = "撤回API，改为只需数据导出，原80不算改50席。"
    review(state, message,
        group("replace", op("replace", "撤回API，改为只需数据导出", requirement_id=old.items[0].requirement_id,
            requirement=feature("data_export", "只需数据导出"))),
        group("correction", op("clear", "原80不算改50席", field="paid_seats"),
            op("set", "原80不算改50席", field="paid_seats", value=50)))
    assert state.requirements.revision == old.revision + 1
    assert [item.feature_id for item in state.requirements.items] == ["data_export"]
    assert state.match().selected_plan_id == "basic"


@pytest.mark.parametrize("quote", ["不再需要API吗？", "不需要取消API，原要求保留。"])
def test_v17_question_or_retention_does_not_remove_old_requirement(state, quote):
    old, _, _ = s1(state)
    review(state, quote, group("remove", op("remove", quote, requirement_id=old.items[0].requirement_id),
        verdict="reject", reason="question" if "？" in quote else "negative"))
    assert state.requirements is old


def test_conflicting_final_values_across_groups_are_both_rejected(state):
    old, _, _ = s1(state)
    review(state, "改50席，改60席。",
        group("a", op("set", "改50席", field="paid_seats", value=50)),
        group("b", op("set", "改60席", field="paid_seats", value=60)))
    assert state.requirements is old
    assert [entry["issue"] for entry in state.operation_feedback] == ["conflict", "conflict"]


def test_target_failure_never_guesses_previous_value_or_commits_other_parameters(state):
    old, _, _ = s1(state)
    review(state, "改50席", group("seats", op("set", "改50席", field="paid_seats", value=50)),
        target={"kind": "selection", "relation": "continue", "task_id": "missing-task"})
    assert state.requirements is old
    assert not state.review_complete
    assert "previous_value" not in state.operation_feedback[0]


def test_v24_v25_new_same_intent_task_does_not_inherit_parameters(state):
    old, _, _ = s1(state)
    message = "新的客户只需要数据导出，他的席位期限还没确定。"
    review(state, message, group("export", op("add", "只需要数据导出", requirement=feature("data_export", "只需要数据导出"))),
        target={"kind": "selection", "relation": "new", "span": {"quote": "新的客户"}})
    assert state.requirements.requirement_id != old.requirement_id
    assert state.requirements.parameters == ()
    assert state.match().selected_plan_id == "basic"


def test_v26_authorized_reuse_is_specific_and_independent_price_has_no_proposal(state):
    old, _, _ = s1(state)
    message = "另算Basic，用同样的席位和期限。"
    review(state, message,
        group("plan", op("select_plan", "另算Basic", value="basic", speech_act="instruction")),
        group("price", op("request_pricing", "另算Basic", speech_act="instruction")),
        kind="price", target={"kind": "price", "relation": "independent", "copy_from": "selection",
            "reuse_fields": ["paid_seats", "months"], "span": {"quote": message}})
    assert state._selection_context.requirements is old
    assert {p.name: p.value for p in state.requirements.parameters} == {"paid_seats":80,"months":12}
    state.validate_price("basic",80,12)
    with pytest.raises(ProposalRejected):
        prepare_proposal(state, session_id="synthetic", turn_id="independent", interaction_revision=2)


def test_v27_unmodeled_detail_blocks_complete_match_and_save(state):
    message = "必须支持CSV导出，实际买50席，订12个月。"
    review(state, message,
        group("detail", op("add", "必须支持CSV导出", requirement=feature("data_export", "必须支持CSV导出")),
              op("add", "必须支持CSV导出", requirement=feature(None, "CSV导出", "unmodeled"))),
        group("seats", op("set", "实际买50席", field="paid_seats", value=50)),
        group("term", op("set", "订12个月", field="months", value=12)))
    assert state.match().status == "blocked"
    assert any(item.kind == "unmodeled" for item in state.requirements.items)
    with pytest.raises(ProposalRejected):
        prepare_proposal(state,session_id="synthetic",turn_id="detail",interaction_revision=1)


def test_v29_notes_and_equivalent_values_preserve_bound_objects_and_message_identity(state):
    old, match, price = s1(state)
    review(state, "目前仍80席，预计以后30席，先别改。",
        group("same", op("set", "目前仍80席", field="paid_seats", value=80)),
        group("background", op("note", "预计以后30席，先别改。", speech_act="background", certainty="planned")))
    assert state.requirements is old
    assert state.last_match is match
    assert state._selection_context.pricing is price
    assert state._selection_context.result_message_id == "synthetic-old-message"
    assert state.review_preserve_results
    assert len(state.source_notes) == 2
    with pytest.raises(ProposalRejected):
        prepare_proposal(state,session_id="synthetic",turn_id="note",interaction_revision=2)


def test_v31_explicit_prepare_can_create_new_object_but_does_not_save(state):
    s1(state)
    review(state, "保存这个", group("prepare", op("request_proposal", "保存这个", speech_act="instruction")))
    assert state.review_requested_execution == ("prepare",)
    assert not state.review_preserve_results
    proposal = prepare_proposal(state,session_id="synthetic",turn_id="prepare",interaction_revision=2)
    assert proposal.pricing.base_price_minor == 7_680_000


def test_v39_unique_original_clarification_removes_ambiguity_without_delete_instruction(state):
    message = "必须对接现有系统，购买80席一年，请算价格。"
    state.apply_patch({"intent":"selection", "add":[feature(None,"必须对接现有系统","ambiguous")],
        "parameters":[{"name":"paid_seats","value":80,"user_quote":"购买80席"},{"name":"months","value":12,"user_quote":"一年"}],
        "pricing_quote":"请算价格"},message)
    old_id=state.requirements.items[0].requirement_id
    message="我说的对接就是产品API，不是另加一项要求。"
    review(state,message,group("clarify",op("clarify",message,requirement_id=old_id,
        requirement=feature("api_access","产品API"))))
    assert len(state.requirements.items)==1
    assert state.requirements.items[0].requirement_id==old_id
    assert state.requirements.items[0].kind=="feature"
    assert state.match().selected_plan_id=="team"


def test_v40_conditional_purchase_group_does_not_publish_numbers(state):
    message="必须SSO，如果兼容才买50席订6个月，目前兼容不确定。"
    review(state,message,
        group("sso",op("add","必须SSO",requirement=feature("saml_sso","必须SSO"))),
        group("condition",op("set","目前兼容不确定",field="saml_sso_compatible_idp",value="unknown",certainty="uncertain")),
        group("purchase",op("set","如果兼容才买50席订6个月",field="paid_seats",value=50,certainty="conditional"),
              op("set","如果兼容才买50席订6个月",field="months",value=6,certainty="conditional")))
    assert state.requirements.parameters==()
    assert state.requirements.condition_assertions[0].value=="unknown"


def test_review_failure_and_later_patch_cannot_mutate_state(state):
    old,_,_=s1(state)
    state.begin_review_turn()
    with pytest.raises(ValidationError):
        state.apply_review({"intent":"selection","coverage_complete":True,"illegal_amount":10},"改50席",{})
    with pytest.raises(InputRejected):
        state.apply_patch({"intent":"selection","parameters":[{"name":"paid_seats","value":50,"user_quote":"改50席"}]},"改50席")
    assert state.requirements is old


def test_followup_update_and_accept_price_plan_cannot_bypass_review(state):
    s1(state)
    review(state,"改50席",group("seats",op("set","改50席",field="paid_seats",value=50)))
    state.apply_patch({"intent":"selection","parameters":[{"name":"paid_seats","value":50,"user_quote":"改50席"}]},"改50席")
    with pytest.raises(InputRejected):
        state.apply_patch({"intent":"selection","add":[feature("audit_logs","另需审计")]},"另需审计")
    with pytest.raises(InputRejected):
        state.accept_price_plan("business","Business")
    assert [item.feature_id for item in state.requirements.items]==["api_access"]


def test_duplicate_quote_requires_exact_location_and_unique_quote_can_repair_offsets(state):
    message="以前50席，现在50席。"
    review(state,message,group("seats",op("set","50席",field="paid_seats",value=50)))
    assert state.operation_feedback[0]["issue"]=="reference_missing"
    review(state,message,group("seats",{**op("set","50席",field="paid_seats",value=50),
        "span":{"quote":"50席","start":message.rindex("50席"),"end":message.rindex("50席")+3}}))
    assert state.requirements.parameters[0].value==50
    review(state,"现在40席",group("seats",{**op("set","现在40席",field="paid_seats",value=40),
        "span":{"quote":"现在40席","start":99,"end":100}}))
    assert state.operation_feedback[0]["spans"][0]["start"]==0


def test_multi_quote_ids_are_limited_to_this_actual_message(state):
    review(state,"按编号读取Q-A和Q-B。",kind="none",intent="quote_query",quote_ids=["Q-A","Q-B","Q-C"])
    assert state.review_quote_ids==("Q-A","Q-B")


def test_initial_empty_new_identity_requires_no_new_task_keyword(state):
    review(state,"必须API",group("api",op("add","必须API",requirement=feature("api_access","必须API"))),
        target={"kind":"selection","relation":"new"})
    assert state.review_complete
    assert state.match().selected_plan_id=="team"


def test_missing_feature_in_shared_parallel_quote_is_not_candidate_disagreement(state):
    message="必须API，也必须数据导出。"
    review(state,message,
        group("api",op("add",message,requirement=feature("api_access",message))),
        group("export",op("add","也必须数据导出",requirement=feature("data_export","也必须数据导出"))),
        candidate={"add":[feature("api_access",message)]})
    assert {item.feature_id for item in state.requirements.items}=={"api_access","data_export"}
    assert all(entry["status"]=="accepted" for entry in state.operation_feedback)


@pytest.mark.parametrize("quote,value",[("订半年",6),("期限一年",12),("期限为六个月",6)])
def test_v03_duration_normalization_is_bound_to_original_phrase(state,quote,value):
    review(state,quote,group("term",op("set",quote,field="months",value=value)))
    assert state.requirements.parameters[0].value==value


@pytest.mark.parametrize("quote",["付费席位降到50","付费席位设为五十","设为五十"])
def test_reviewed_reordered_numeric_semantics_is_not_a_postfix_wordlist(state,quote):
    s1(state)
    review(state,quote,group("seats",op("set",quote,field="paid_seats",value=50)))
    assert next(p.value for p in state.requirements.parameters if p.name=="paid_seats")==50


def test_automatic_minimum_match_plan_can_be_consumed_without_explicit_selection(state):
    s1(state)
    review(state,"改50席",group("seats",op("set","改50席",field="paid_seats",value=50)))
    assert state.requirements.selected_plan_id is None
    state.match()
    state.accept_price_plan("team","改50席")
    state.validate_price("team",50,12)
    with pytest.raises(InputRejected):
        state.accept_price_plan("business","改50席")


def test_select_plan_must_be_named_and_have_positive_direction(state):
    s1(state)
    review(state,"请选最低档",group("plan",op("select_plan","请选最低档",value="basic")))
    assert state.operation_feedback[0]["issue"]=="unknown_identity"
    assert state.requirements.selected_plan_id is None
    review(state,"不要Team，选Business",group("plan",op("select_plan","不要Team，选Business",value="team")))
    assert state.operation_feedback[0]["issue"]=="negative"
    assert state.requirements.selected_plan_id is None
    review(state,"不要Team，选Business",group("plan",op("select_plan","选Business",value="business")))
    assert state.requirements.selected_plan_id=="business"


def test_failed_dependency_and_cycles_cannot_release_part_of_group(state):
    s1(state)
    old=state.requirements
    review(state,"数量约50，订6个月",
        group("seats",op("set","数量约50",field="paid_seats",value=50),verdict="reject",reason="uncertain"),
        group("term",op("set","订6个月",field="months",value=6),depends_on=["seats"]))
    assert state.requirements is old
    assert state.operation_feedback[1]["status"]=="blocked"
    review(state,"改50席订6个月",
        group("seats",op("set","改50席",field="paid_seats",value=50),depends_on=["term"]),
        group("term",op("set","订6个月",field="months",value=6),depends_on=["seats"]))
    assert state.requirements is old
    assert all(entry["status"]=="blocked" for entry in state.operation_feedback)


def test_incomplete_hard_detail_coverage_blocks_full_assessment_even_with_valid_other_groups(state):
    review(state,"必须API，购买50席",group("api",op("add","必须API",requirement=feature("api_access","必须API"))),
        group("seats",op("set","购买50席",field="paid_seats",value=50)),coverage_complete=False)
    assert len(state.requirements.items)==1
    assert "detail_uncovered" in state.review_blockers
    with pytest.raises(InputRejected):
        state.match()


@pytest.mark.parametrize("answer,value",[("是","yes"),("否","no"),("不确定","unknown")])
def test_v11_unique_current_condition_short_answers(state,answer,value):
    state.apply_patch({"intent":"selection","add":[feature("saml_sso","必须SSO")]},"必须SSO")
    state.pending=("condition:saml_sso_compatible_idp",)
    review(state,answer,group("answer",op("set",answer,field="saml_sso_compatible_idp",value=value,
        certainty="uncertain" if value=="unknown" else "certain")))
    assert state.requirements.condition_assertions[0].value==value


@pytest.mark.parametrize("quote",["我们有公司账号","我们支持SAML","兼容性还不确定"])
def test_v14_known_insufficient_self_report_cannot_become_yes_by_two_wrong_candidates(state,quote):
    review(state,quote,group("idp",op("set",quote,field="saml_sso_compatible_idp",value="yes")),
        candidate={"conditions":[{"condition_id":"saml_sso_compatible_idp","value":"yes","user_quote":quote}]})
    assert state.requirements.condition_assertions==()
    assert state.operation_feedback[0]["issue"] in {"ambiguous","uncertain"}


def test_multiple_pending_generic_assent_does_not_choose_a_condition(state):
    state.pending=("paid_seats","condition:saml_sso_compatible_idp")
    review(state,"好的",group("idp",op("set","好的",field="saml_sso_compatible_idp",value="yes")))
    assert state.requirements.condition_assertions==()


@pytest.mark.parametrize("quote,action",[("不需要API","add"),("支持API？","add"),("不再需要API吗？","remove"),("不需要取消API，原要求保留","remove")])
def test_v17_known_negative_and_question_scope_cannot_become_requirements_by_wrong_facets(state,quote,action):
    old,_,_=s1(state)
    values={"requirement":feature("api_access",quote)} if action=="add" else {"requirement_id":old.items[0].requirement_id}
    review(state,quote,group("requirement",op(action,quote,**values)))
    assert state.requirements is old
    assert state.operation_feedback[0]["issue"] in {"negative","question"}


def test_v04_negated_sso_is_not_an_idp_statement_and_other_groups_can_complete(state):
    message="买50席但不需要SSO，订阅6个月，必须有API，请算价。"
    review(state,message,
        group("seats",op("set","买50席",field="paid_seats",value=50)),
        group("months",op("set","订阅6个月",field="months",value=6)),
        group("idp",op("set","不需要SSO",field="saml_sso_compatible_idp",value="no")),
        group("api",op("add","必须有API",requirement=feature("api_access","必须有API"))),
        group("price",op("request_pricing","请算价",speech_act="instruction")))
    assert state.requirements.condition_assertions==()
    assert state.review_blockers==()
    assert state.operation_feedback[2]["issue"]=="negative"
    state.match()
    state.validate_price("team",50,6)


def test_v21_false_negative_add_is_not_a_relevant_gap_but_true_detail_group_is(state):
    message="必须API，不需要SSO，买50席，订6个月，请算价。"
    common=[group("api",op("add","必须API",requirement=feature("api_access","必须API"))),
        group("seats",op("set","买50席",field="paid_seats",value=50)),
        group("months",op("set","订6个月",field="months",value=6)),
        group("price",op("request_pricing","请算价",speech_act="instruction"))]
    review(state,message,*common,
        group("false-sso",op("add","不需要SSO",requirement=feature("saml_sso","不需要SSO"))))
    assert state.review_blockers==()
    state.match()
    state.validate_price("team",50,6)
    message="不需要SSO，但必须CSV导出"
    review(state,message,group("mixed",op("add","不需要SSO",requirement=feature("saml_sso","不需要SSO")),
        op("add","必须CSV导出",requirement=feature(None,"必须CSV导出","unmodeled"))))
    assert "requirements" in state.review_blockers


def test_false_negative_add_of_existing_requirement_remains_relevant(state):
    state.apply_patch({"intent":"selection","add":[feature("saml_sso","必须SSO")]},"必须SSO")
    review(state,"不需要SSO",group("false-add",op("add","不需要SSO",requirement=feature("saml_sso","不需要SSO"))))
    assert "requirements" in state.review_blockers
    assert state.requirements.items[0].feature_id=="saml_sso"


def test_v06_qualifier_is_not_copied_to_an_independent_month_group(state):
    review(state,"预计购买50席，订阅6个月",
        group("seats",op("set","预计购买50席",field="paid_seats",value=50,certainty="planned",time_scope="future")),
        group("months",op("set","订阅6个月",field="months",value=6)))
    assert {p.name:p.value for p in state.requirements.parameters}=={"months":6}


@pytest.mark.parametrize("quote,value,issue",[("实际购买50.5席",50,"invalid_type"),("实际购买501席",501,"invalid_range")])
def test_v10_invalid_seat_group_keeps_independent_valid_months(state,quote,value,issue):
    review(state,quote+"，订阅6个月，Team",
        group("seats",op("set",quote,field="paid_seats",value=value)),
        group("months",op("set","订阅6个月",field="months",value=6)),
        group("plan",op("select_plan","Team",value="team")))
    assert {p.name:p.value for p in state.requirements.parameters}=={"months":6}
    assert state.operation_feedback[0]["issue"]==issue


def test_rejected_review_groups_still_validate_and_keep_source_positions(state):
    review(state,"约70席，期限6个月",
        group("uncertain",op("set","约70席",field="paid_seats",value=70),verdict="reject",reason="uncertain"),
        group("bad-source",op("set","凭空10席",field="paid_seats",value=10),verdict="reject",reason="uncertain"))
    assert state.operation_feedback[0]["spans"]==[{"quote":"约70席","start":0,"end":4}]
    assert state.operation_feedback[1]["issue"]=="reference_missing"
    assert state.operation_feedback[1]["spans"]==[]


def test_v27_v28_preparing_a_specific_proposal_retains_price_goal_after_detail_withdrawal(state):
    message="必须CSV导出，买50席，订12个月，请准备内部草案"
    review(state,message,
        group("export",op("add","必须CSV导出",requirement=feature("data_export","必须CSV导出")),
            op("add","必须CSV导出",requirement=feature(None,"CSV导出","unmodeled"))),
        group("seats",op("set","买50席",field="paid_seats",value=50)),
        group("months",op("set","订12个月",field="months",value=12)),
        group("prepare",op("request_proposal","请准备内部草案",speech_act="instruction")))
    assert state.wants_price
    assert state.match().status=="blocked"
    detail=next(item for item in state.requirements.items if item.kind=="unmodeled")
    review(state,"CSV不是硬要求，只要一般导出，继续核对",
        group("remove-detail",op("remove","CSV不是硬要求",requirement_id=detail.requirement_id)),
        group("evaluate",op("request_evaluation","继续核对",speech_act="instruction")))
    assert state.wants_price
    assert state.match().selected_plan_id=="basic"
    state.validate_price("basic",50,12)
    price=calculate_subscription_price("basic",50,12)
    state.record_pricing_result(price)
    state.remember_result(state.last_match,price,(),())
    assert prepare_proposal(state,session_id="synthetic",turn_id="after-detail",interaction_revision=2).pricing.base_price_minor==2_400_000


def test_prepare_and_explicit_cancel_price_conflict_is_not_resolved_by_order(state):
    s1(state)
    review(state,"不算价，但准备草案",
        group("cancel",op("cancel_pricing","不算价",speech_act="instruction")),
        group("prepare",op("request_proposal","准备草案",speech_act="instruction")))
    assert state.wants_price
    assert state.review_preserve_results
    assert all(entry["issue"]=="conflict" for entry in state.operation_feedback)


@pytest.mark.parametrize("verdict",["accept","reject"])
def test_v10_wire_actual_decimal_can_be_rejected_per_group_without_losing_months(state,verdict):
    review(state,"实际购买50.5席，订阅6个月",
        group("seats",op("set","实际购买50.5席",field="paid_seats",value=50.5),
            verdict=verdict,reason="invalid_type" if verdict=="reject" else "confirmed"),
        group("months",op("set","订阅6个月",field="months",value=6)))
    assert state.review_complete
    assert {p.name:p.value for p in state.requirements.parameters}=={"months":6}
    assert state.operation_feedback[0]["issue"]=="invalid_type"
    assert state.operation_feedback[0]["spans"]==[{"quote":"实际购买50.5席","start":0,"end":9}]


def test_wire_approximate_rejection_reason_does_not_invalidate_independent_groups(state):
    review(state,"公司约80人，订阅一年，需要API",
        group("population",op("set","公司约80人",field="paid_seats",value=80),verdict="reject",reason="approximate"),
        group("months",op("set","订阅一年",field="months",value=12)),
        group("api",op("add","需要API",requirement=feature("api_access","需要API"))))
    assert state.review_complete
    assert {p.name:p.value for p in state.requirements.parameters}=={"months":12}
    assert state.requirements.items[0].feature_id=="api_access"
    assert state.operation_feedback[0]["issue"]=="approximate"


def test_review_float_representation_of_original_integer_is_system_failure_not_user_decimal(state):
    review(state,"实际购买50席，订阅6个月",
        group("seats",op("set","实际购买50席",field="paid_seats",value=50.0)),
        group("months",op("set","订阅6个月",field="months",value=6)))
    assert {p.name:p.value for p in state.requirements.parameters}=={"months":6}
    assert state.operation_feedback[0]["issue"]=="extraction_failed"
    assert "实际输入不是所需整数" not in state.operation_feedback[0]["message"]


def test_v02_numeric_replace_wire_uses_the_complete_existing_acceptance_boundary(state):
    old,_,_=s1(state)
    message="不是80席而是50席，其余不变，重新计算订阅基价。"
    review(state,message,
        group("seats",op("replace","不是80席而是50席",field="paid_seats",value=50)),
        group("price",op("request_pricing","重新计算订阅基价",speech_act="instruction")),
        candidate={"parameters":[{"name":"paid_seats","value":50,"user_quote":"不是80席而是50席"}]})
    assert state.requirements.requirement_id==old.requirement_id
    assert state.requirements.revision==old.revision+1
    assert {p.name:p.value for p in state.requirements.parameters}=={"paid_seats":50,"months":12}
    assert state.match().selected_plan_id=="team"
    state.validate_price("team",50,12)


@pytest.mark.parametrize("candidate",[{}, {"parameters":[{"name":"paid_seats","value":50,"user_quote":"不是80席而是50席"}]}])
def test_wrong_numeric_replace_old_value_is_not_selected_from_its_true_source(state,candidate):
    old,match,price=s1(state)
    message="不是80席而是50席，其余不变。"
    review(state,message,group("seats",op("replace","不是80席而是50席",field="paid_seats",value=80)),candidate=candidate)
    assert state.requirements is old
    assert state.last_match is match
    assert state._selection_context.pricing is price
    assert state.operation_feedback[0]["issue"] in {"negative","candidate_disagreement"}


def test_numeric_replace_does_not_absorb_requirement_identity_or_bypass_type_and_dependency(state):
    old,_,_=s1(state)
    review(state,"改50席",group("invalid-object",op("replace","改50席",field="paid_seats",value=50,
        requirement_id=old.items[0].requirement_id)))
    assert state.requirements is old
    assert state.operation_feedback[0]["issue"]=="reference_missing"
    review(state,"实际购买50.5席，订6个月",
        group("decimal",op("replace","实际购买50.5席",field="paid_seats",value=50.5)),
        group("dependent",op("replace","订6个月",field="months",value=6),depends_on=["decimal"]))
    assert state.requirements is old
    assert state.operation_feedback[0]["issue"]=="invalid_type"
    assert state.operation_feedback[1]["status"]=="blocked"


@pytest.mark.parametrize("field,value,quote", [("paid_seats", 50, "改50席"), ("months", 6, "订6个月")])
def test_numeric_replace_empty_feature_metadata_is_not_a_requirement_change(state,field,value,quote):
    old,_,_=s1(state)
    review(state,quote,group("numeric",op("replace",quote,field=field,value=value,
        requirement=feature(None,quote))))
    assert dict((p.name,p.value) for p in state.requirements.parameters)[field]==value
    assert state.requirements.items==old.items
    assert state.operation_feedback[0]["status"]=="accepted"


@pytest.mark.parametrize("metadata", [feature("api_access","改50席"),feature(None,"改50席","unmodeled"),
    feature(None,"另一个来源")])
def test_numeric_replace_cannot_discard_an_identified_requirement_or_unknown_detail(state,metadata):
    old,match,price=s1(state)
    review(state,"改50席",group("mixed",op("replace","改50席",field="paid_seats",value=50,requirement=metadata)))
    assert state.requirements is old
    assert state.last_match is match
    assert state._selection_context.pricing is price
    assert state.operation_feedback[0]["status"]=="rejected"


def test_equivalent_clarification_preserves_clear_feature_and_bound_result(state):
    old,match,price=s1(state)
    context=state._selection_context
    original_result=(context.result_turn_id,context.result_message_id)
    message="原来的API要求保持不变。"
    review(state,message,group("source",op("clarify",message,requirement_id=old.items[0].requirement_id,
        requirement=feature("api_access","API要求保持不变"))))
    assert state.requirements is old
    assert state.last_match is match
    assert state._selection_context.pricing is price
    assert (state._selection_context.result_turn_id,state._selection_context.result_message_id)==original_result
    assert state.review_blockers==()
    assert state.source_notes[-1]["kind"]=="equivalent_source"
    assert state.operation_feedback[0]["status"]=="accepted"


@pytest.mark.parametrize("replacement,identity", [(feature("audit_logs","审计"),"original"),
    (feature("api_access","API"),"missing"),(feature(None,"API","unmodeled"),"original")])
def test_clear_feature_clarification_cannot_change_its_meaning_or_guess_target(state,replacement,identity):
    old,match,price=s1(state)
    quote=replacement["user_quote"]
    review(state,quote,group("unsafe",op("clarify",quote,
        requirement_id=old.items[0].requirement_id if identity=="original" else "missing-object",
        requirement=replacement)))
    assert state.requirements is old
    assert state.last_match is match
    assert state._selection_context.pricing is price
    assert state.operation_feedback[0]["issue"]=="unknown_identity"
