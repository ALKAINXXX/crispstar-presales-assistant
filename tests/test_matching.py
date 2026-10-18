"""Business acceptance cases C01-C14 and requirement-preservation checks.

These are local, synthetic inputs. TestConditionEvidence proves only the
condition-matching branch; it is not a retrieval result or customer validation.
"""

import pytest

from app.schemas.matching import (
    ConditionAssertion,
    DeclaredParameter,
    HardRequirement,
    RequirementSet,
    TestConditionEvidence,
)
from app.services.matching import _match_with_test_evidence, match_requirements


CATALOG_VERSION = "chengxing-demo-v1"
SSO_CONDITION = "saml_sso_compatible_idp"
PLAN_IDS = ("basic", "team", "business")


def feature_requirement(feature_id, *, requirement_id=None, quote=None):
    return HardRequirement(
        requirement_id=requirement_id or f"need_{feature_id}",
        statement=f"必须支持 {feature_id}",
        user_quote=quote or f"我们必须使用 {feature_id}",
        feature_id=feature_id,
    )


def requirements_for(*feature_ids, **overrides):
    values = {
        "requirement_id": "customer-needs",
        "items": tuple(feature_requirement(item) for item in feature_ids),
    }
    values.update(overrides)
    return RequirementSet(**values)


def sso_assertion(value):
    quotes = {
        "yes": "我们现在已经具备兼容的身份提供方。",
        "no": "我们现在没有兼容的身份提供方。",
        "unknown": "以后可能会准备，目前还不确定。",
    }
    return ConditionAssertion(
        condition_id=SSO_CONDITION, value=value, user_quote=quotes[value]
    )


def test_evidence(**overrides):
    values = {
        "condition_id": SSO_CONDITION,
        "catalog_version": CATALOG_VERSION,
        "source_id": "synthetic-sso-condition",
        "quote": "使用 SAML SSO 时，客户须明确确认已具备兼容身份提供方。",
        "purpose": "test_fixture",
    }
    values.update(overrides)
    return TestConditionEvidence(**values)


# This helper is not a pytest test; the evidence is explicitly local test data.
test_evidence.__test__ = False


def plan_result(result, plan_id):
    return next(plan for plan in result.plans if plan.plan_id == plan_id)


def item_result(result, plan_id, feature_id):
    return next(
        item
        for item in plan_result(result, plan_id).items
        if item.feature_id == feature_id
    )


def assert_complete_assessment(result, requirements):
    """Every plan accounts for every hard requirement, with stable identities."""
    assert result.requirements == requirements
    assert result.catalog_version == CATALOG_VERSION
    assert tuple(plan.plan_id for plan in result.plans) == PLAN_IDS
    expected_ids = tuple(item.requirement_id for item in requirements.items)
    for plan in result.plans:
        assert tuple(item.requirement_id for item in plan.items) == expected_ids


def assert_no_satisfying_plan(result):
    assert result.status == "blocked"
    assert result.eligible_plan_ids == ()
    assert result.lowest_plan_id is None
    assert result.selected_plan_id is None
    assert result.selection_reason == "no_satisfying_plan"
    assert not any(plan.satisfies_all for plan in result.plans)


def test_C01_collaboration_and_export_choose_basic_and_keep_all_eligible():
    requirements = requirements_for("project_collaboration", "data_export")

    result = match_requirements(requirements)

    assert_complete_assessment(result, requirements)
    assert result.eligible_plan_ids == PLAN_IDS
    assert result.lowest_plan_id == result.selected_plan_id == "basic"
    assert result.status == "matched"
    assert result.selection_reason == "lowest_satisfying"
    assert result.evidence_origin == "none"
    assert all(plan.satisfies_all for plan in result.plans)
    assert all(item.status == "supported" for plan in result.plans for item in plan.items)


def test_C02_api_requires_team_and_explains_basic_gap():
    requirements = requirements_for("project_collaboration", "data_export", "api_access")

    result = match_requirements(requirements)

    assert_complete_assessment(result, requirements)
    assert result.eligible_plan_ids == ("team", "business")
    assert result.lowest_plan_id == result.selected_plan_id == "team"
    assert result.status == "matched"
    basic_api = item_result(result, "basic", "api_access")
    assert basic_api.status == "unsupported"
    assert "catalog_unsupported" in basic_api.reason_codes
    assert item_result(result, "basic", "data_export").status == "supported"
    assert not plan_result(result, "basic").satisfies_all


def test_C03_audit_logs_choose_business_without_unrelated_sso_precondition():
    requirements = requirements_for("audit_logs")

    result = match_requirements(requirements)

    assert_complete_assessment(result, requirements)
    assert result.eligible_plan_ids == ("business",)
    assert result.lowest_plan_id == result.selected_plan_id == "business"
    assert result.status == "matched"
    audit = item_result(result, "business", "audit_logs")
    assert audit.status == "supported"
    assert audit.condition_id is None
    assert audit.reason_codes == ()
    assert result.evidence_origin == "none"


def test_C04_private_deployment_is_explicitly_unsupported_in_every_plan():
    requirements = requirements_for("private_deployment")

    result = match_requirements(requirements)

    assert_complete_assessment(result, requirements)
    assert_no_satisfying_plan(result)
    for plan_id in PLAN_IDS:
        item = item_result(result, plan_id, "private_deployment")
        assert item.status == "unsupported"
        assert "catalog_unsupported" in item.reason_codes


def test_C05_scim_is_retained_as_unknown_instead_of_inheriting_sso_support():
    scim = HardRequirement(
        requirement_id="scim", statement="必须有 SCIM 自动开通", user_quote="我们必须自动开通账号。",
        kind="unmodeled",
    )
    requirements = RequirementSet(requirement_id="scim-needs", items=(scim,))

    result = match_requirements(requirements)

    assert_complete_assessment(result, requirements)
    assert_no_satisfying_plan(result)
    for plan in result.plans:
        assert plan.items[0].status == "unknown"
        assert "unmodeled_requirement" in plan.items[0].reason_codes
        assert plan.items[0].condition_id is None


@pytest.mark.parametrize("statement", ["必须符合指定地域合规要求", "订阅预算每月不得超过 500 元"])
def test_C06_unmodeled_hard_constraint_blocks_otherwise_supported_needs(statement):
    unknown = HardRequirement(
        requirement_id="hard-constraint", statement=statement, user_quote=statement,
        kind="unmodeled",
    )
    requirements = requirements_for(
        items=(feature_requirement("project_collaboration"), unknown)
    )

    result = match_requirements(requirements)

    assert_complete_assessment(result, requirements)
    assert_no_satisfying_plan(result)
    for plan in result.plans:
        assert plan.items[0].status == "supported"
        assert plan.items[1].status == "unknown"
        assert "unmodeled_requirement" in plan.items[1].reason_codes


def test_C07_user_assertion_cannot_replace_missing_sso_evidence():
    requirements = requirements_for("saml_sso", condition_assertions=(sso_assertion("yes"),))

    # Explicit pre-retrieval assessment; the default entry retrieves.
    result = match_requirements(requirements, retrieval_ids=())

    assert_complete_assessment(result, requirements)
    assert_no_satisfying_plan(result)
    assert result.evidence_origin == "none"
    for plan_id in ("basic", "team"):
        item = item_result(result, plan_id, "saml_sso")
        assert item.status == "unsupported"
        assert item.reason_codes == ("catalog_unsupported",)
    business_sso = item_result(result, "business", "saml_sso")
    assert business_sso.status not in ("supported", "unsupported")
    assert "condition_evidence_missing" in business_sso.reason_codes
    assert business_sso.condition_id == SSO_CONDITION
    assert business_sso.evidence_source_ids == ()


@pytest.mark.parametrize("assertions", [(), (sso_assertion("unknown"),)])
def test_C08_valid_test_evidence_and_unknown_customer_fact_require_confirmation(assertions):
    requirements = requirements_for("saml_sso", condition_assertions=assertions)
    evidence = test_evidence()

    result = _match_with_test_evidence(requirements, evidence=(evidence,))

    assert_complete_assessment(result, requirements)
    assert_no_satisfying_plan(result)
    assert result.evidence_origin == "test_fixture"
    business_sso = item_result(result, "business", "saml_sso")
    assert business_sso.status == "pending_condition"
    assert "condition_unknown" in business_sso.reason_codes
    assert business_sso.evidence_source_ids == (evidence.source_id,)


def test_C09_valid_test_evidence_and_customer_no_make_sso_unsatisfied():
    assertion = sso_assertion("no")
    requirements = requirements_for("saml_sso", condition_assertions=(assertion,))

    result = _match_with_test_evidence(requirements, evidence=(test_evidence(),))

    assert_complete_assessment(result, requirements)
    assert_no_satisfying_plan(result)
    business_sso = item_result(result, "business", "saml_sso")
    assert business_sso.status == "unsupported"
    assert "condition_not_met" in business_sso.reason_codes
    assert business_sso.condition_user_quote == assertion.user_quote


def test_C10_valid_test_evidence_and_customer_yes_allow_business_in_fixture_only():
    assertion = sso_assertion("yes")
    evidence = test_evidence()
    requirements = requirements_for("saml_sso", "audit_logs", condition_assertions=(assertion,))

    result = _match_with_test_evidence(requirements, evidence=(evidence,))

    assert_complete_assessment(result, requirements)
    assert result.status == "matched"
    assert result.eligible_plan_ids == ("business",)
    assert result.lowest_plan_id == result.selected_plan_id == "business"
    assert result.evidence_origin == "test_fixture"
    business_sso = item_result(result, "business", "saml_sso")
    assert business_sso.status == "supported"
    assert business_sso.condition_user_quote == assertion.user_quote
    assert business_sso.evidence_source_ids == (evidence.source_id,)
    assert item_result(result, "business", "audit_logs").status == "supported"

    # A fixture does not establish evidence in an empty real business chain.
    runtime_result = match_requirements(requirements, retrieval_ids=())
    assert_no_satisfying_plan(runtime_result)
    assert runtime_result.evidence_origin == "none"
    assert "condition_evidence_missing" in item_result(runtime_result, "business", "saml_sso").reason_codes


def test_C11_customer_can_choose_satisfying_team_while_lowest_remains_basic():
    requirements = requirements_for(
        "project_collaboration", "data_export", selected_plan_id="team"
    )

    result = match_requirements(requirements)

    assert_complete_assessment(result, requirements)
    assert result.status == "matched"
    assert result.eligible_plan_ids == PLAN_IDS
    assert result.lowest_plan_id == "basic"
    assert result.selected_plan_id == "team"
    assert result.selection_reason == "user_selected"


def test_C12_invalid_basic_choice_does_not_delete_api_or_hide_real_satisfying_plans():
    requirements = requirements_for(
        "project_collaboration", "data_export", "api_access", selected_plan_id="basic"
    )

    result = match_requirements(requirements)

    assert_complete_assessment(result, requirements)
    assert result.status == "blocked"
    assert result.eligible_plan_ids == ("team", "business")
    assert result.lowest_plan_id == "team"
    assert result.selected_plan_id is None
    assert result.selection_reason == "selected_plan_not_satisfying"
    assert item_result(result, "basic", "api_access").status == "unsupported"
    assert len(result.requirements.items) == 3
    assert result.requirements.selected_plan_id == "basic"


@pytest.mark.parametrize("selected_plan_id", [None, "team"])
def test_C13_empty_requirements_allow_comparison_but_never_complete_selection(selected_plan_id):
    requirements = requirements_for(selected_plan_id=selected_plan_id)

    result = match_requirements(requirements)

    assert_complete_assessment(result, requirements)
    assert result.status == "comparison_only"
    assert result.selection_reason == "empty_requirements"
    assert result.lowest_plan_id is None
    assert result.selected_plan_id is None
    assert all(plan.items == () for plan in result.plans)


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"catalog_version": "outdated-demo-v0"}, "condition_evidence_version_mismatch"),
        ({"condition_id": "unrelated_condition"}, "condition_evidence_association_mismatch"),
        ({"conflict": True}, "condition_evidence_conflict"),
    ],
)
def test_C14_invalid_or_conflicting_sso_evidence_preserves_specific_blocker(overrides, reason):
    requirements = requirements_for("saml_sso", condition_assertions=(sso_assertion("yes"),))

    result = _match_with_test_evidence(requirements, evidence=(test_evidence(**overrides),))

    assert_complete_assessment(result, requirements)
    assert_no_satisfying_plan(result)
    business_sso = item_result(result, "business", "saml_sso")
    assert business_sso.status not in ("supported", "unsupported")
    assert reason in business_sso.reason_codes


def test_ambiguous_integration_request_is_not_silently_promoted_to_api_need():
    ambiguous = HardRequirement(
        requirement_id="integration-unclear", statement="需要对接我们的系统",
        user_quote="我们希望对接自己的系统。", kind="ambiguous", feature_id="api_access",
    )
    requirements = RequirementSet(requirement_id="unclear-needs", items=(ambiguous,))

    result = match_requirements(requirements)

    assert_complete_assessment(result, requirements)
    assert_no_satisfying_plan(result)
    assert all(plan.items[0].status == "unknown" for plan in result.plans)
    assert all("ambiguous_requirement" in plan.items[0].reason_codes for plan in result.plans)


def test_unrecognized_feature_id_stays_unknown_in_every_plan():
    requirements = requirements_for("scim_provisioning")

    result = match_requirements(requirements)

    assert_complete_assessment(result, requirements)
    assert_no_satisfying_plan(result)
    assert all(plan.items[0].status == "unknown" for plan in result.plans)


def test_explicit_parameters_company_context_and_revision_preserve_their_basis():
    api = feature_requirement("api_access", quote="产品 API 是我们的硬需求。")
    original = RequirementSet(
        requirement_id="revision-example", items=(api,),
        parameters=(DeclaredParameter(name="paid_seats", value=80, user_quote="先购买 80 席。"),),
        context_quotes=("我们公司约 100 人。",),
    )
    revised = RequirementSet(
        requirement_id=original.requirement_id, revision=2, previous_revision=1,
        items=original.items,
        parameters=(
            DeclaredParameter(name="paid_seats", value=60, user_quote="席位改成 60 席。"),
            DeclaredParameter(name="months", value=12, user_quote="使用 12 个月。"),
        ),
        context_quotes=original.context_quotes,
    )

    first_result = match_requirements(original)
    revised_result = match_requirements(revised)

    assert_complete_assessment(first_result, original)
    assert_complete_assessment(revised_result, revised)
    assert first_result.lowest_plan_id == revised_result.lowest_plan_id == "team"
    assert revised_result.requirements.items[0].user_quote == "产品 API 是我们的硬需求。"
    assert original.parameters[0].value == 80
    assert revised_result.requirements.parameters[0].value == 60
    assert revised_result.requirements.parameters[1].value == 12
    assert revised_result.requirements.context_quotes == ("我们公司约 100 人。",)
    assert revised_result.requirements.previous_revision == 1


def test_company_size_does_not_create_paid_seats_or_change_plan_eligibility():
    requirements = requirements_for(
        "project_collaboration", context_quotes=("我们公司有 80 人。",)
    )

    result = match_requirements(requirements)

    assert result.lowest_plan_id == "basic"
    assert result.requirements.parameters == ()
    assert result.requirements.context_quotes == requirements.context_quotes


def test_unknown_selected_plan_blocks_and_keeps_the_supported_options():
    requirements = requirements_for("project_collaboration", selected_plan_id="enterprise-plus")

    result = match_requirements(requirements)

    assert_complete_assessment(result, requirements)
    assert result.status == "blocked"
    assert result.selection_reason == "unknown_selected_plan"
    assert result.selected_plan_id is None
    assert result.lowest_plan_id == "basic"
    assert result.eligible_plan_ids == PLAN_IDS


@pytest.mark.parametrize("raw", ["需要 API", {"requirement_id": "raw", "items": []}])
def test_runtime_matcher_requires_normalized_requirement_set(raw):
    with pytest.raises((TypeError, ValueError)):
        match_requirements(raw)


def test_runtime_matcher_does_not_accept_test_evidence_parameter():
    requirements = requirements_for("saml_sso", condition_assertions=(sso_assertion("yes"),))

    with pytest.raises(TypeError):
        match_requirements(requirements, evidence=(test_evidence(),))


@pytest.mark.parametrize("field", ["requirement_id", "statement", "user_quote", "feature_id"])
def test_hard_requirement_rejects_blank_identity_or_basis(field):
    values = {
        "requirement_id": "need-api", "statement": "需要 API", "user_quote": "需要 API",
        "feature_id": "api_access",
    }
    values[field] = "   "

    with pytest.raises(ValueError):
        HardRequirement(**values)


@pytest.mark.parametrize("value", [True, 1.5, "80"])
def test_explicit_parameters_do_not_coerce_bool_decimal_or_text_to_integer(value):
    with pytest.raises(ValueError):
        DeclaredParameter(name="paid_seats", value=value, user_quote="用户明确输入")


@pytest.mark.parametrize("group", ["items", "parameters", "condition_assertions"])
def test_requirement_set_rejects_duplicate_identities(group):
    values = {
        "items": (feature_requirement("api_access"),) * 2,
        "parameters": (DeclaredParameter(name="months", value=12, user_quote="12 个月"),) * 2,
        "condition_assertions": (sso_assertion("yes"),) * 2,
    }

    with pytest.raises(ValueError):
        RequirementSet(requirement_id="duplicate", **{group: values[group]})


@pytest.mark.parametrize("previous_revision", [2, 3])
def test_previous_revision_must_precede_current_revision(previous_revision):
    with pytest.raises(ValueError):
        RequirementSet(requirement_id="revision", revision=2, previous_revision=previous_revision)


def test_claimed_real_evidence_cannot_be_disguised_as_test_fixture():
    with pytest.raises(ValueError):
        test_evidence(purpose="retrieved")
