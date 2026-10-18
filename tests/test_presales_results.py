"""Deterministic receipt/coverage contracts; no live model success is claimed."""

import json

import pytest

from app.schemas.business import ConsultationItem, SourceSpan
from app.schemas.chat import BusinessResponse
from app.schemas.matching import ConditionAssertion, RequirementSet
from app.schemas.pricing import UnitPriceResult
from app.schemas.retrieval import RetrievalEvidence, RetrievalFailure, RetrievalHit
from app.services.catalog import get_catalog
from app.services.chat_render import organize_business, render_business
from app.services.knowledge import prepare_corpus
from app.services.presales import TurnConsultationResults
from app.services.pricing import get_unit_price as query_unit_price


def item(identity="prices", aspect="unit_price", plans=("basic", "team", "business"), feature=None, topic="订阅单价"):
    return ConsultationItem(item_id=identity, aspect=aspect, plan_ids=plans,
        feature_id=feature, topic=topic, span=SourceSpan(quote=topic))


def response(results, coverage, *, legacy=None, evidence=(), answers=(), errors=(), intent="unit_price"):
    return BusinessResponse(turn_id="turn", intent=intent,
        requirements=RequirementSet(requirement_id="unchanged"), unit_price=legacy,
        unit_prices=results.scoped_unit_prices(coverage.items) if coverage.items else results.unit_prices,
        unit_price_observations=results.observations,
        consultation_coverage=coverage, retrieval_evidence=list(evidence),
        knowledge_answers=list(answers), errors=list(errors))


def observation(section="integration_api", status="retrieved"):
    sources = prepare_corpus().sources
    source = next(source for source in sources if source.section_id == section)
    if status == "unavailable":
        return RetrievalEvidence(retrieval_id="search", chain_id="isolated", query="本次具体问题",
            retrieved_at="2026-10-08T00:00:00+00:00", options={}, status=status,
            error=RetrievalFailure(step="retrieval", code="controlled_failure", message="资料查询失败", retry_action="稍后重试"))
    return RetrievalEvidence(retrieval_id="search", chain_id="isolated", query="本次具体问题",
        retrieved_at="2026-10-08T00:00:00+00:00", options={}, status=status,
        hits=(RetrievalHit(rank=1, score=0.8, relevance="related", source=source),))


def search_record(evidence, identities):
    return {"tool_call_id": "search-call", "name": "search_knowledge",
        "arguments": json.dumps({"query": evidence.query, "consultation_item_ids": identities}),
        "result": {"ok": True, "data": evidence.model_dump(mode="json")}}


def answer(evidence, identities, quote=None, answerability="supported"):
    return {"retrieval_id": evidence.retrieval_id, "query": evidence.query,
        "consultation_item_ids": identities, "answerability": answerability,
        "citations": [{"source_id": evidence.hits[0].source.source_id,
                       "quote": quote or evidence.hits[0].source.raw_text}] if evidence.hits else []}


def test_multi_price_order_duplicate_and_last_single_legacy_are_separate():
    results = TurnConsultationResults()
    for identity in ("basic", "team", "business", "team"):
        results.add_unit(query_unit_price(identity), identity + "-" + str(len(results.observations)))
    coverage = results.coverage([item(plans=("business", "basic", "team", "team"))])
    assert coverage.status == "completed"
    assert [price.plan_id for price in coverage.items[0].unit_prices] == ["business", "basic", "team"]
    assert len(results.unit_prices) == 3
    legacy = query_unit_price("team")
    business = response(results, coverage, legacy=legacy)
    presentation, formal = organize_business(business, "completed")
    assert business.unit_price == legacy
    assert presentation.answer_markdown.index("Business") < presentation.answer_markdown.index("Basic") < presentation.answer_markdown.index("Team")
    assert all(presentation.answer_markdown.count(plan.name) == 1 for plan in get_catalog().plans)
    assert "120.00" in formal and "40.00" in formal and "80.00" in formal
    assert formal == render_business(business, "completed")


def test_full_catalog_actual_result_does_not_fabricate_legacy_or_expand_scope():
    results = TurnConsultationResults()
    results.add_catalog(get_catalog(), "catalog-call")
    coverage = results.coverage([item(plans=("team",))])
    business = response(results, coverage)
    assert business.unit_price is None
    assert [price.plan_id for price in business.unit_prices] == ["team"]
    assert [entry.price.plan_id for entry in business.unit_price_observations] == ["basic", "team", "business"]
    assert coverage.items[0].tool_call_ids == ["catalog-call"]
    text = organize_business(business, "completed")[0].answer_markdown
    assert "Team" in text and "Basic" not in text and "Business" not in text


def test_price_partial_failure_retains_success_and_names_missing_object():
    results = TurnConsultationResults()
    results.add_unit(query_unit_price("basic"), "basic")
    results.add_unit(query_unit_price("business"), "business")
    coverage = results.coverage([item()])
    assert coverage.status == "incomplete"
    assert coverage.items[0].missing_plan_ids == ["team"]
    text = organize_business(response(results, coverage, legacy=query_unit_price("business")), "incomplete")[0].answer_markdown
    assert "40.00" in text and "120.00" in text and "团队版 Team的订阅单价尚未取得" in text
    assert text.count("120.00") == 1


@pytest.mark.parametrize("changed", [{"unit_price_minor": 8001}, {"catalog_version": "another-version"}, {"product_id": "other-product"}])
def test_conflicting_actual_values_have_no_winning_normal_answer(changed):
    results = TurnConsultationResults()
    original = query_unit_price("team")
    results.add_unit(original, "first")
    candidate = UnitPriceResult.model_validate_json(json.dumps(original.model_dump(mode="json") | changed))
    results.add_unit(candidate, "second")
    coverage = results.coverage([item(plans=("team",))])
    assert results.unit_prices == []
    assert results.observations[0].price == original and results.conflicts[0]["tool_call_id"] == "second"
    assert coverage.status == "incomplete" and coverage.items[0].stage == "result_conflict"
    text = organize_business(response(results, coverage, legacy=original), "incomplete")[0].answer_markdown
    assert "冲突" in text and "80.00" not in text


def test_different_plan_versions_cannot_form_one_normal_price_set():
    results = TurnConsultationResults()
    results.add_unit(query_unit_price("basic"), "basic")
    price = query_unit_price("business").model_dump(mode="json") | {"catalog_version": "another-version"}
    results.add_unit(price, "business")
    coverage = results.coverage([item(plans=("basic", "business"))])
    assert coverage.status == "incomplete" and coverage.items[0].stage == "result_conflict"
    assert not coverage.items[0].unit_prices and len(results.observations) == 2


@pytest.mark.parametrize("four_relations", [False, True])
def test_support_keeps_two_actual_relations_or_four_and_preserves_sources(four_relations):
    results = TurnConsultationResults()
    results.add_catalog(get_catalog(), "catalog")
    plans = ("team", "basic") if four_relations else ("team",)
    inquiries = [item("api", "support", plans, "api_access", "产品 API"),
                 item("hook", "support", ("team", "basic") if four_relations else ("basic",), "webhook_notifications", "Webhook 通知")]
    evidence = observation()
    accepted = answer(evidence, ["api", "hook"])
    coverage = results.coverage(inquiries, [accepted], [evidence], [search_record(evidence, ["api", "hook"])])
    assert sum(len(entry.support) for entry in coverage.items) == (4 if four_relations else 2)
    assert coverage.status == "completed"
    business = response(results, coverage, evidence=[evidence], answers=[accepted], intent="knowledge")
    assert not business.unit_prices and len(business.unit_price_observations) == 3
    presentation, formal = organize_business(business, "completed")
    assert "团队版 Team支持产品 API" in presentation.answer_markdown
    assert "基础版 Basic不支持Webhook 通知" in presentation.answer_markdown
    assert ("基础版 Basic不支持产品 API" in presentation.answer_markdown) == four_relations
    assert "03_api_webhook.md" in formal and presentation.used_source_ids == [evidence.hits[0].source.source_id]
    assert "单价口径" not in formal and "订阅单价" not in presentation.answer_markdown


def test_sso_full_decisive_quote_is_required_and_rule_does_not_assert_customer_condition():
    results = TurnConsultationResults()
    inquiry = item("sso", "prerequisite", ("business",), "saml_sso", "SSO 的使用前提")
    evidence = observation("sso_prerequisite")
    statement = get_catalog().conditions[0].statement
    accepted = answer(evidence, ["sso"], statement)
    coverage = results.coverage([inquiry], [accepted], [evidence], [search_record(evidence, ["sso"])])
    assert coverage.status == "completed"
    business = response(results, coverage, evidence=[evidence], answers=[accepted], intent="knowledge")
    text = organize_business(business, "completed")[0].answer_markdown
    assert statement in text and "企业版 Business的 SAML SSO 使用前提" in text
    assert not business.requirements.condition_assertions and "你们已具备" not in text
    incomplete = results.coverage([inquiry], [answer(evidence, ["sso"], "SAML SSO")], [evidence])
    assert incomplete.status == "incomplete" and incomplete.items[0].stage == "explanation_rejected"


@pytest.mark.parametrize("stage", ["retrieved", "unavailable"])
def test_failure_stage_and_history_do_not_turn_acquired_evidence_into_missing_material(stage):
    results = TurnConsultationResults()
    inquiry = item("api", "detail", ("team",), "api_access", "API 限流")
    evidence = observation(status=stage)
    coverage = results.coverage([inquiry], (), [evidence], [search_record(evidence, ["api"])])
    assert coverage.items[0].stage == ("explanation_pending" if stage == "retrieved" else "query_failed")
    business = response(results, coverage, evidence=[evidence], intent="knowledge",
                        errors=[{"code": "model_unavailable", "message": "模型不可用"}])
    presentation, formal = organize_business(business, "incomplete")
    assert presentation.answer_markdown in formal
    assert "资料不足" not in formal and "资料未说明" not in formal
    assert ("已取得“API 限流”的检索原文" in formal) == (stage == "retrieved")


def test_unmodeled_detail_has_specific_limitation_without_catalog_support_claim():
    results = TurnConsultationResults()
    evidence = observation()
    inquiry = item("limit", "detail", ("team",), "api_access", "API 每分钟调用限额")
    accepted = answer(evidence, ["limit"], answerability="insufficient")
    coverage = results.coverage([inquiry], [accepted], [evidence])
    assert coverage.status == "completed" and coverage.items[0].stage == "evidence_insufficient"
    text = organize_business(response(results, coverage, evidence=[evidence], answers=[accepted], intent="knowledge"), "completed")[0].answer_markdown
    assert "API 每分钟调用限额" in text and "不能据此认定产品不支持" in text
    assert "Team支持API 每分钟" not in text


def test_legacy_response_without_collection_uses_old_single_price_body():
    business = BusinessResponse(turn_id="legacy", intent="unit_price",
        requirements=RequirementSet(requirement_id="legacy"), unit_price=query_unit_price("team"))
    assert organize_business(business, "completed")[0].answer_markdown == "团队版 Team的订阅单价为 **80.00 元／席／月**。"


def test_unestablished_consultation_scope_preserves_actual_success_without_completion():
    results = TurnConsultationResults()
    results.add_unit(query_unit_price("team"), "actual")
    coverage = results.coverage([], scope_complete=False)
    assert coverage.status == "incomplete"
    business = response(results, coverage, legacy=query_unit_price("team"))
    text = organize_business(business, "incomplete")[0].answer_markdown
    assert "80.00" not in text and "咨询范围尚未完整核定" in text
    assert business.unit_price == query_unit_price("team")
    assert business.unit_price_observations[0].tool_call_ids == ["actual"]


def test_rejected_scope_does_not_deny_an_actual_catalog_query_or_adopt_its_facts():
    results = TurnConsultationResults()
    results.add_catalog(get_catalog(), "actual-catalog")
    coverage = results.coverage([], feedback=[{"item_id": "bad-scope", "aspect": "unit_price",
        "status": "rejected", "topic": "尚未核定的单价范围", "plan_ids": ["team"]}], scope_complete=False)
    business = response(results, coverage)
    # Assembly retains actual receipts when no inquiry scope was accepted.
    business.unit_prices = results.unit_prices
    presentation, formal = organize_business(business, "incomplete")
    assert coverage.status == "incomplete" and not coverage.items[0].unit_prices
    assert [entry.price.plan_id for entry in results.observations] == ["basic", "team", "business"]
    assert "范围核对未完成" in presentation.answer_markdown
    assert "尚未执行" not in formal
    assert all(value not in presentation.answer_markdown for value in ("40.00", "80.00", "120.00"))


def test_blocked_dependent_inquiry_is_visible_and_not_answered_from_an_old_result():
    results = TurnConsultationResults()
    feedback = [{"item_id": "new-total", "aspect": "pricing", "status": "blocked",
                 "topic": "改为50席后的金额", "plan_ids": ["team"], "depends_on": ["seat-edit"]}]
    coverage = results.coverage([], feedback=feedback, scope_complete=False)
    assert coverage.status == "incomplete" and coverage.items[0].stage == "dependency_unresolved"
    text = organize_business(response(results, coverage, intent="selection"), "incomplete")[0].answer_markdown
    assert "改为50席后的金额" in text and "不能用此前结果代答" in text


def test_answered_other_aspect_cannot_complete_sso_without_decisive_rule():
    results = TurnConsultationResults()
    results.add_catalog(get_catalog(), "catalog")
    sso = item("sso", "prerequisite", ("business",), "saml_sso", "SSO 前提")
    api = item("api", "support", ("team",), "api_access", "产品 API")
    sso_evidence = observation("sso_prerequisite")
    api_evidence = observation()
    api_evidence = api_evidence.model_copy(update={"retrieval_id": "api-search"})
    coverage = results.coverage([sso, api], [answer(sso_evidence, ["sso"], answerability="insufficient"),
        answer(api_evidence, ["api"])], [sso_evidence, api_evidence])
    assert coverage.items[0].status == "incomplete" and coverage.items[1].status == "completed"
    assert coverage.status == "incomplete"


def test_conflicting_explanation_verdicts_do_not_choose_latest_as_normal_answer():
    results = TurnConsultationResults()
    evidence = observation()
    inquiry = item("detail", "detail", ("team",), "api_access", "接口技术细节")
    coverage = results.coverage([inquiry], [answer(evidence, ["detail"]),
        answer(evidence, ["detail"], answerability="insufficient")], [evidence])
    assert coverage.status == "incomplete" and coverage.items[0].stage == "explanation_rejected"


def test_full_catalog_formal_prices_follow_only_reviewed_reverse_scope():
    results = TurnConsultationResults()
    results.add_catalog(get_catalog(), "full-query")
    scope = [item("first", plans=("business", "basic")), item("repeat", plans=("business",))]
    assert [price.plan_id for price in results.scoped_unit_prices(scope)] == ["business", "basic"]
    assert [entry.price.plan_id for entry in results.observations] == ["basic", "team", "business"]
    assert results.scoped_unit_prices([item("support", "support", ("team",), "api_access", "产品 API")]) == []


@pytest.mark.parametrize("identity", ["basic", "team", "business"])
def test_sso_prerequisite_is_bound_to_actual_plan_feature_condition_relationship(identity):
    results = TurnConsultationResults()
    inquiry = item("sso", "prerequisite", (identity,), "saml_sso", identity + "的 SSO 前提")
    evidence = observation("sso_prerequisite")
    accepted = answer(evidence, ["sso"])
    coverage = results.coverage([inquiry], [accepted], [evidence])
    result = coverage.items[0]
    assert coverage.status == "completed"
    business = response(results, coverage, evidence=[evidence], answers=[accepted], intent="knowledge")
    presentation, formal = organize_business(business, "completed")
    if identity == "business":
        assert not result.support and [rule.plan_id for rule in result.prerequisites] == ["business"]
        rule = result.prerequisites[0]
        assert rule.condition_id == "saml_sso_compatible_idp" and rule.catalog_version == get_catalog().version
        assert rule.retrieval_id == evidence.retrieval_id and rule.source_id == evidence.hits[0].source.source_id
        assert rule.statement == get_catalog().conditions[0].statement
        assert "企业版 Business的 SAML SSO 使用前提" in presentation.answer_markdown
    else:
        assert not result.prerequisites and len(result.support) == 1 and not result.support[0].supported
        assert result.support[0].plan_id == identity and result.support[0].condition_id is None
        assert result.support[0].source_ids == [evidence.hits[0].source.source_id]
        assert ("基础版 Basic" if identity == "basic" else "团队版 Team") + "不支持SAML SSO" in presentation.answer_markdown
        assert "的 SAML SSO 使用前提" not in presentation.answer_markdown
    assert not business.questions and not business.requirements.condition_assertions
    assert "你目前是否" not in formal and "04_saml_sso.md" in formal


def test_sso_mixed_plan_prerequisites_preserve_true_applicability_and_order():
    results = TurnConsultationResults()
    results.add_catalog(get_catalog(), "actual-catalog")
    inquiry = item("sso", "prerequisite", ("team", "business", "basic"), "saml_sso", "这三档的SSO前提")
    evidence = observation("sso_prerequisite")
    accepted = answer(evidence, ["sso"])
    coverage = results.coverage([inquiry], [accepted], [evidence])
    result = coverage.items[0]
    assert coverage.status == "completed" and result.plan_ids == ["team", "business", "basic"]
    assert [row.plan_id for row in result.support] == ["team", "basic"]
    assert [rule.plan_id for rule in result.prerequisites] == ["business"]
    assert result.prerequisites[0].tool_call_ids == ["actual-catalog"]
    presentation, formal = organize_business(response(results, coverage, evidence=[evidence], answers=[accepted], intent="knowledge"), "completed")
    assert "团队版 Team不支持SAML SSO" in presentation.answer_markdown
    assert "基础版 Basic不支持SAML SSO" in presentation.answer_markdown
    assert "企业版 Business的 SAML SSO 使用前提" in presentation.answer_markdown
    assert presentation.answer_markdown.index("团队版 Team不支持SAML SSO") < presentation.answer_markdown.index("企业版 Business的 SAML SSO 使用前提") < presentation.answer_markdown.index("基础版 Basic不支持SAML SSO")
    assert "团队版 Team的 SAML SSO 使用前提" not in formal and "基础版 Basic的 SAML SSO 使用前提" not in formal


def test_generic_sso_rule_names_only_actual_condition_plan_and_preserves_unknown_customer_fact():
    results = TurnConsultationResults()
    inquiry = item("sso", "prerequisite", (), "saml_sso", "SSO的前提是什么")
    evidence = observation("sso_prerequisite")
    accepted = answer(evidence, ["sso"], get_catalog().conditions[0].statement)
    coverage = results.coverage([inquiry], [accepted], [evidence])
    assert coverage.status == "completed" and coverage.items[0].plan_ids == []
    assert [rule.plan_id for rule in coverage.items[0].prerequisites] == ["business"]
    business = response(results, coverage, evidence=[evidence], answers=[accepted], intent="knowledge")
    unknown = ConditionAssertion(condition_id="saml_sso_compatible_idp", value="unknown", user_quote="目前不确定兼容情况")
    requirements = RequirementSet(requirement_id="existing", condition_assertions=(unknown,))
    business.requirements = requirements
    presentation, formal = organize_business(business, "completed")
    assert "企业版 Business的 SAML SSO 使用前提" in presentation.answer_markdown
    assert "基础版 Basic的 SAML SSO 使用前提" not in formal and "团队版 Team的 SAML SSO 使用前提" not in formal
    assert business.requirements == requirements and business.requirements.condition_assertions == (unknown,)
    assert business.requirements.selected_plan_id is None and not business.questions
    assert "你们已具备" not in formal and "你目前是否" not in formal


def test_business_condition_quote_cannot_complete_basic_or_team_explanation():
    results = TurnConsultationResults()
    evidence = observation("sso_prerequisite")
    inquiry = item("sso", "prerequisite", ("basic", "team"), "saml_sso", "两档的SSO前提")
    accepted = answer(evidence, ["sso"], get_catalog().conditions[0].statement)
    coverage = results.coverage([inquiry], [accepted], [evidence])
    assert coverage.status == "incomplete" and coverage.items[0].stage == "explanation_rejected"
    assert not coverage.items[0].prerequisites and not coverage.items[0].support
    assert coverage.items[0].missing_plan_ids == ["basic", "team"]
    text = organize_business(response(results, coverage, evidence=[evidence], answers=[accepted], intent="knowledge"), "incomplete")[0].answer_markdown
    assert "基础版 Basic的 SAML SSO 使用前提" not in text and "团队版 Team的 SAML SSO 使用前提" not in text


def test_sso_actual_catalog_conflict_rejects_applicability_even_with_valid_rule_quote():
    results = TurnConsultationResults()
    changed = get_catalog().model_dump(mode="json") | {"version": "another-version"}
    results.add_catalog(changed, "conflicting-catalog")
    evidence = observation("sso_prerequisite")
    accepted = answer(evidence, ["sso"])
    coverage = results.coverage([item("sso", "prerequisite", ("business",), "saml_sso", "SSO前提")], [accepted], [evidence])
    assert coverage.status == "incomplete" and coverage.items[0].stage == "result_conflict"
    assert not coverage.items[0].prerequisites and not coverage.items[0].support


def test_sso_conflicting_explanation_verdicts_do_not_adopt_one_rule():
    results = TurnConsultationResults()
    evidence = observation("sso_prerequisite")
    inquiry = item("sso", "prerequisite", ("business",), "saml_sso", "SSO前提")
    coverage = results.coverage([inquiry], [answer(evidence, ["sso"]),
        answer(evidence, ["sso"], answerability="insufficient")], [evidence])
    assert coverage.status == "incomplete" and coverage.items[0].stage == "explanation_rejected"
    assert not coverage.items[0].prerequisites and not coverage.items[0].support


def test_one_supported_explanation_keeps_three_same_source_quotes_for_rule_and_unknown_customer():
    results = TurnConsultationResults()
    evidence = observation("sso_prerequisite")
    source = evidence.hits[0].source
    quotes = ["本节适用于企业版 Business 的 SAML SSO 需求；Basic、Team 不支持该能力。",
              get_catalog().conditions[0].statement,
              "情况未知或答复有歧义时，前提待确认；未来计划准备不能当作当前具备。"]
    assert all(quote in source.raw_text for quote in quotes)
    accepted = {"retrieval_id": evidence.retrieval_id, "query": evidence.query,
        "consultation_item_ids": ["rule", "unknown"], "answerability": "supported",
        "citations": [{"source_id": source.source_id, "quote": quote} for quote in quotes]}
    inquiries = [item("rule", "prerequisite", ("business",), "saml_sso", "SSO前提"),
                 item("unknown", "detail", ("business",), "saml_sso", "目前不知道IdP兼容性时如何判断")]
    coverage = results.coverage(inquiries, [accepted], [evidence], [search_record(evidence, ["rule", "unknown"])])
    assert coverage.status == "completed" and all(result.status == "completed" for result in coverage.items)
    assert all(result.source_ids == [source.source_id] and result.retrieval_ids == [evidence.retrieval_id]
               for result in coverage.items)
    assert len(coverage.items[0].prerequisites) == 1 and not coverage.items[1].prerequisites
    business = response(results, coverage, evidence=[evidence], answers=[accepted], intent="knowledge")
    unknown = ConditionAssertion(condition_id="saml_sso_compatible_idp", value="unknown", user_quote="目前不知道IdP是否兼容")
    business.requirements = RequirementSet(requirement_id="customer-context", condition_assertions=(unknown,))
    presentation, formal = organize_business(business, "completed")
    assert all("> " + quote in presentation.answer_markdown for quote in quotes)
    assert presentation.used_source_ids == [source.source_id] and formal.count("04_saml_sso.md") == 1
    assert "企业版 Business的 SAML SSO 使用前提" in presentation.answer_markdown
    assert business.requirements.condition_assertions == (unknown,)
    assert business.requirements.selected_plan_id is None and not business.questions
    assert "你们已具备" not in formal and "当前具备：yes" not in formal


def question(unit_id, order, plan, *, question_id="price-question", aspect="unit_price",
             feature=None, sources=("reviewed",), status="accepted"):
    """Collector inputs are certified service units, not language candidates."""
    return {"unit_id": unit_id, "question_id": question_id, "order": order,
        "aspect": aspect, "plan_ids": [plan] if plan else [], "feature_id": feature,
        "topic": "本轮明确问题", "source_item_ids": list(sources), "status": status}


def test_normalized_units_override_interleaved_candidate_blocks_and_tool_order():
    results = TurnConsultationResults()
    for identity in ("business", "basic", "team"):
        results.add_unit(query_unit_price(identity), identity)
    inquiries = [item("outer", plans=("team", "business")), item("middle", plans=("basic",))]
    units = [question("business-unit", 2, "business", sources=("outer",)),
             question("team-unit", 0, "team", sources=("outer",)),
             question("basic-unit", 1, "basic", sources=("middle",))]
    coverage = results.coverage(inquiries, questions=units)
    assert [entry.item_id for entry in coverage.items] == ["team-unit", "basic-unit", "business-unit"]
    assert [price.plan_id for price in results.scoped_unit_prices(coverage.items)] == ["team", "basic", "business"]
    text = organize_business(response(results, coverage), "completed")[0].answer_markdown
    assert text.index("Team") < text.index("Basic") < text.index("Business")
    assert [entry.price.plan_id for entry in results.observations] == ["business", "basic", "team"]


def test_missing_normalized_question_cannot_be_completed_by_extra_real_return():
    results = TurnConsultationResults()
    results.add_catalog(get_catalog(), "real-full-catalog")
    units = [question("team-unit", 0, "team"),
             question("omitted-basic", 1, "basic", sources=(), status="missing")]
    coverage = results.coverage([item(plans=("team",))], questions=units, scope_complete=False)
    assert coverage.status == "incomplete"
    assert coverage.items[0].status == "completed"
    assert coverage.items[1].stage == "extraction_incomplete" and not coverage.items[1].unit_prices
    assert [price.plan_id for price in results.scoped_unit_prices(coverage.items)] == ["team"]
    assert len(results.observations) == 3


def test_empty_scope_completes_without_tool_and_never_projects_extra_prices():
    results = TurnConsultationResults()
    units = [question("excluded-all", 0, None, sources=("all-excluded",), status="empty_scope")]
    coverage = results.coverage([], questions=units)
    assert coverage.status == "completed" and coverage.items[0].stage == "empty_scope"
    assert not coverage.items[0].missing_plan_ids and not coverage.items[0].tool_call_ids
    results.add_catalog(get_catalog(), "unnecessary-but-real")
    coverage = results.coverage([], questions=units)
    assert coverage.status == "completed" and not results.scoped_unit_prices(coverage.items)
    assert len(results.observations) == 3
    text = organize_business(response(results, coverage, legacy=query_unit_price("team")), "completed")[0].answer_markdown
    assert "没有剩余查询对象" in text and "80.00" not in text


def test_normalized_explanation_subset_cannot_expand_through_shared_original_identity():
    results = TurnConsultationResults()
    results.add_catalog(get_catalog(), "actual")
    evidence = observation()
    units = [question("team-api", 0, "team", aspect="support", feature="api_access"),
             question("basic-api", 1, "basic", aspect="support", feature="api_access")]
    partial_answer = answer(evidence, ["reviewed"])
    partial_answer["consultation_unit_ids"] = ["team-api"]
    coverage = results.coverage([], [partial_answer], [evidence],
        [search_record(evidence, ["reviewed"])], questions=units)
    assert coverage.items[0].status == "completed"
    assert coverage.items[1].status == "incomplete" and coverage.items[1].stage == "explanation_pending"
    assert coverage.status == "incomplete"


def test_two_distinct_question_sources_cannot_fill_each_others_explanation_gap():
    results = TurnConsultationResults()
    results.add_catalog(get_catalog(), "actual")
    evidence = observation()
    units = [question("q1-api", 0, "team", question_id="q1", aspect="support", feature="api_access", sources=("api",)),
             question("q2-hook", 1, "basic", question_id="q2", aspect="support", feature="webhook_notifications", sources=("hook",))]
    accepted = answer(evidence, ["api"])
    coverage = results.coverage([], [accepted], [evidence],
        [search_record(evidence, ["api", "hook"])], questions=units)
    assert coverage.items[0].status == "completed" and coverage.items[1].status == "incomplete"
    assert [(row.plan_id, row.feature_id) for entry in coverage.items for row in entry.support] == [
        ("team", "api_access"), ("basic", "webhook_notifications")]


def test_explicit_current_empty_contract_does_not_infer_question_from_legacy_items():
    results = TurnConsultationResults()
    results.add_catalog(get_catalog(), "actual")
    coverage = results.coverage([item()], questions=())
    assert coverage.status == "completed" and coverage.items == []


def test_normalized_gaps_keep_extraction_dependency_and_real_ambiguity_distinct():
    results = TurnConsultationResults()
    units = [question("extraction", 0, "team", status="missing", sources=()),
             question("dependency", 1, "team", aspect="pricing", status="blocked", sources=()),
             question("ambiguous", 2, "basic", status="ambiguous", sources=())]
    coverage = results.coverage([], questions=units, scope_complete=False)
    assert [(entry.status, entry.stage) for entry in coverage.items] == [
        ("incomplete", "extraction_incomplete"), ("incomplete", "dependency_unresolved"),
        ("needs_input", "ambiguous_scope")]
    assert coverage.status == "needs_input"


@pytest.mark.parametrize("candidate_status", ["rejected", "blocked", "missing", "ambiguous"])
def test_current_inventory_does_not_add_a_question_from_wrong_candidate_feedback(candidate_status):
    results = TurnConsultationResults()
    results.add_catalog(get_catalog(), "real-return")
    units = [question("original-business", 0, "business", sources=(), status="missing")]
    units[0]["topic"] = "企业版单价"
    feedback = [{"item_id": "raw-wrong-candidate", "aspect": "unit_price",
        "status": candidate_status, "plan_ids": ["basic", "team"],
        "topic": "MODEL_ONLY_ADDITIONAL_QUESTION", "issue": "scope_mismatch"}]
    coverage = results.coverage([], questions=units, feedback=feedback, scope_complete=False)
    assert coverage.status == "incomplete"
    assert [entry.item_id for entry in coverage.items] == ["original-business"]
    assert coverage.items[0].stage == "extraction_incomplete"
    assert not coverage.items[0].unit_prices
    text = organize_business(response(results, coverage), "incomplete")[0].answer_markdown
    assert "企业版单价" in text and "MODEL_ONLY_ADDITIONAL_QUESTION" not in text
    assert all(value not in text for value in ("40.00", "80.00", "120.00"))
    assert feedback[0]["item_id"] == "raw-wrong-candidate" and len(results.observations) == 3


def test_current_empty_inventory_stays_incomplete_without_inventing_candidate_question():
    results = TurnConsultationResults()
    feedback = [{"item_id": "wrong-candidate", "aspect": "support", "status": "rejected",
        "topic": "MODEL_ONLY_QUESTION", "plan_ids": ["team"]}]
    coverage = results.coverage([], questions=(), feedback=feedback, scope_complete=False)
    assert coverage.status == "incomplete" and not coverage.items


def test_legacy_collector_still_projects_feedback_when_inventory_contract_is_absent():
    results = TurnConsultationResults()
    feedback = [{"item_id": "legacy-rejected", "aspect": "unit_price", "status": "rejected",
        "topic": "旧咨询接收失败", "plan_ids": ["team"]}]
    coverage = results.coverage([], questions=None, feedback=feedback, scope_complete=False)
    assert coverage.status == "incomplete"
    assert [entry.item_id for entry in coverage.items] == ["legacy-rejected"]
    assert coverage.items[0].topic == "旧咨询接收失败"
    assert coverage.items[0].stage == "extraction_incomplete" and not coverage.items[0].unit_prices
