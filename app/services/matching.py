"""Catalog matching with actual, chain-owned condition evidence."""

from app.schemas.catalog import Catalog, Plan
from app.schemas.matching import (
    HardRequirement,
    MatchResult,
    PlanMatch,
    RequirementMatch,
    RequirementSet,
    TestConditionEvidence,
)
from app.services.catalog import get_catalog, validate_catalog
from app.schemas.retrieval import RetrievalEvidence
from app.services.retrieval import KnowledgeService


def match_requirements(
    requirements: RequirementSet, *, catalog: Catalog | None = None,
    knowledge: KnowledgeService | None = None,
    retrieval_ids: tuple[str, ...] | None = None,
) -> MatchResult:
    """Normal entry: retrieve the accepted individual conditional requirement.

    An owning KnowledgeService plus IDs reuses its actual observations.
    Explicit retrieval_ids=() performs the pre-retrieval/missing-evidence
    assessment. Arbitrary serialized Evidence or booleans are never accepted.
    Unconditional requirements do not start the embedding worker.
    """
    if not isinstance(requirements, RequirementSet):
        raise TypeError("requirements must be a normalized RequirementSet")
    requirements = RequirementSet.model_validate(requirements)
    catalog = get_catalog() if catalog is None else validate_catalog(catalog)
    if any(a.condition_id not in {c.condition_id for c in catalog.conditions}
           for a in requirements.condition_assertions):
        raise ValueError("Unknown condition assertion identity")
    if knowledge is not None and not isinstance(knowledge, KnowledgeService):
        raise TypeError("knowledge must be the chain's KnowledgeService")
    if retrieval_ids is not None and (not isinstance(retrieval_ids, tuple)
                                    or any(not isinstance(i, str) for i in retrieval_ids)):
        raise TypeError("retrieval_ids must be a tuple of actual retrieval IDs")
    conditional = tuple(c for c in catalog.conditions if any(
        item.kind == "feature" and item.feature_id == c.feature_id for item in requirements.items))
    if conditional and retrieval_ids is None:
        knowledge = knowledge if knowledge is not None else KnowledgeService()
        # Query the already accepted individual statement. A shared user quote
        # can contain several needs and bury this condition in unrelated hits.
        # No extra terms, fixed answer mapping or query expansion are added.
        queries = dict.fromkeys(item.statement for item in requirements.items
                                if item.kind == "feature" and item.feature_id in {c.feature_id for c in conditional})
        retrieval_ids = tuple(knowledge.retrieve(query).retrieval_id for query in queries)
    retrieval_ids = () if retrieval_ids is None else retrieval_ids
    if retrieval_ids and knowledge is None:
        raise ValueError("retrieval IDs require their owning KnowledgeService")
    if knowledge is not None and conditional:
        retrieval_ids = tuple(dict.fromkeys(retrieval_ids + knowledge.condition_conflict_ids(
            tuple(c.condition_id for c in conditional))))
    checks = {}
    actual = ()
    if knowledge is not None and retrieval_ids:
        # Retain actual failures and conflicts in Match; never take client data.
        # Unknown/expired IDs produce an explicit blocker rather than a fake hit.
        from app.services.knowledge import KnowledgeError
        observed = []
        for identity in retrieval_ids:
            try:
                observed.append(knowledge.evidence(identity))
            except KnowledgeError:
                pass
        actual = tuple(observed)
        for condition in conditional:
            for plan in catalog.plans:
                if any(f.condition_id == condition.condition_id for f in plan.features):
                    checks[(plan.plan_id, condition.condition_id)] = knowledge.check_condition(
                        condition.condition_id, plan_id=plan.plan_id, catalog=catalog, retrieval_ids=retrieval_ids)
    return _match(requirements, catalog=catalog, evidence=(),
                  evidence_origin="retrieval" if retrieval_ids else "none",
                  condition_checks=checks, retrieval_evidence=actual)


def _match_with_test_evidence(
    requirements: RequirementSet,
    *,
    evidence: tuple[TestConditionEvidence, ...],
    catalog: Catalog | None = None,
) -> MatchResult:
    """Controlled condition-evidence test seam; never wired to app or tools.

    The result permanently identifies these inputs as test fixtures. They
    provide no proof of retrieval or suitability in a real customer system.
    """
    if not isinstance(evidence, tuple) or any(
        not isinstance(item, TestConditionEvidence) for item in evidence
    ):
        raise TypeError("evidence must be a tuple of TestConditionEvidence fixtures")
    checked = tuple(TestConditionEvidence.model_validate(item) for item in evidence)
    return _match(
        requirements, catalog=catalog, evidence=checked, evidence_origin="test_fixture"
    )


def _match(
    requirements: RequirementSet,
    *,
    catalog: Catalog | None,
    evidence: tuple[TestConditionEvidence, ...],
    evidence_origin: str,
    condition_checks: dict | None = None,
    retrieval_evidence: tuple[RetrievalEvidence, ...] = (),
) -> MatchResult:
    if not isinstance(requirements, RequirementSet):
        raise TypeError("requirements must be a normalized RequirementSet")
    requirements = RequirementSet.model_validate(requirements)
    catalog = get_catalog() if catalog is None else validate_catalog(catalog)

    condition_ids = {condition.condition_id for condition in catalog.conditions}
    if any(
        assertion.condition_id not in condition_ids
        for assertion in requirements.condition_assertions
    ):
        raise ValueError("Unknown condition assertion identity")

    plans = tuple(
        _match_plan(plan, requirements, catalog, evidence, condition_checks or {}) for plan in catalog.plans
    )
    eligible = tuple(plan.plan_id for plan in plans if plan.satisfies_all)
    lowest = eligible[0] if eligible else None
    requested = requirements.selected_plan_id
    selected = None
    blocking_reasons: tuple[str, ...] = ()

    if not requirements.items:
        status, reason = "comparison_only", "empty_requirements"
    elif requested is not None and requested not in {plan.plan_id for plan in catalog.plans}:
        status, reason = "blocked", "unknown_selected_plan"
        blocking_reasons = (reason,)
    elif requested is not None and requested not in eligible:
        status, reason = "blocked", "selected_plan_not_satisfying"
        blocking_reasons = (reason,)
    elif requested is not None:
        status, reason, selected = "matched", "user_selected", requested
    elif lowest is not None:
        status, reason, selected = "matched", "lowest_satisfying", lowest
    else:
        status, reason = "blocked", "no_satisfying_plan"
        blocking_reasons = tuple(dict.fromkeys(
            code for plan in plans for code in plan.blocking_reasons
        ))

    return MatchResult(
        requirements=requirements,
        catalog_version=catalog.version,
        plans=plans,
        eligible_plan_ids=eligible,
        lowest_plan_id=lowest,
        selected_plan_id=selected,
        selection_reason=reason,
        status=status,
        evidence_origin=evidence_origin,
        test_evidence=evidence,
        retrieval_evidence=retrieval_evidence,
        blocking_reasons=blocking_reasons,
    )


def _match_plan(
    plan: Plan,
    requirements: RequirementSet,
    catalog: Catalog,
    evidence: tuple[TestConditionEvidence, ...],
    condition_checks: dict,
) -> PlanMatch:
    items = tuple(
        _match_item(item, plan, requirements, catalog, evidence, condition_checks)
        for item in requirements.items
    )
    blocking = tuple(dict.fromkeys(
        code for item in items if item.status != "supported" for code in item.reason_codes
    ))
    return PlanMatch(
        plan_id=plan.plan_id,
        items=items,
        satisfies_all=bool(items) and all(item.status == "supported" for item in items),
        blocking_reasons=blocking,
    )


def _match_item(
    item: HardRequirement,
    plan: Plan,
    requirements: RequirementSet,
    catalog: Catalog,
    evidence: tuple[TestConditionEvidence, ...],
    condition_checks: dict,
) -> RequirementMatch:
    base = {"requirement_id": item.requirement_id, "feature_id": item.feature_id}
    if item.kind == "ambiguous":
        return RequirementMatch(
            **base, status="unknown", reason_codes=("ambiguous_requirement",)
        )
    if item.kind == "unmodeled":
        return RequirementMatch(
            **base, status="unknown", reason_codes=("unmodeled_requirement",)
        )

    feature = next((feature for feature in plan.features if feature.feature_id == item.feature_id), None)
    if feature is None:
        return RequirementMatch(
            **base, status="unknown", reason_codes=("unmodeled_requirement",)
        )
    if not feature.supported:
        return RequirementMatch(
            **base, status="unsupported", reason_codes=("catalog_unsupported",)
        )
    if feature.condition_id is None:
        return RequirementMatch(**base, status="supported")

    condition_id = feature.condition_id
    assertion = next((
        assertion for assertion in requirements.condition_assertions
        if assertion.condition_id == condition_id
    ), None)
    check = condition_checks.get((plan.plan_id, condition_id))
    common = {
        **base,
        "condition_id": condition_id,
        "condition_user_quote": assertion.user_quote if assertion else None,
        "condition_assertion_status": assertion.value if assertion else "unknown",
        "evidence_source_ids": check.source_ids if check else tuple(dict.fromkeys(
            item.source_id for item in evidence if item.condition_id == condition_id
        )),
    }
    if check is not None:
        common["condition_evidence_status"] = check.status
        errors = list(check.reason_codes)
        # Evidence and the user fact remain independently visible.
        if assertion is not None and assertion.value == "no":
            return RequirementMatch(**common, status="unsupported",
                                    reason_codes=tuple(errors + ["condition_not_met"]))
        if assertion is None or assertion.value == "unknown":
            errors.append("condition_unknown")
        if check.status != "valid" or errors:
            return RequirementMatch(**common, status="pending_condition", reason_codes=tuple(errors))
        return RequirementMatch(**common, status="supported")
    evidence_errors = []
    if not evidence:
        evidence_errors.append("condition_evidence_missing")
    else:
        # The catalog has exactly one condition. A contradictory or misassociated
        # returned fixture is retained as a blocker, never silently discarded.
        if any(item.condition_id != condition_id for item in evidence):
            evidence_errors.append("condition_evidence_association_mismatch")
        if any(item.catalog_version != catalog.version for item in evidence):
            evidence_errors.append("condition_evidence_version_mismatch")
        if any(item.conflict for item in evidence):
            evidence_errors.append("condition_evidence_conflict")
    if evidence_errors:
        common["condition_evidence_status"] = "missing" if not evidence else (
            "conflict" if "condition_evidence_conflict" in evidence_errors else "invalid")
        if assertion is not None and assertion.value == "no":
            return RequirementMatch(**common, status="unsupported",
                                    reason_codes=tuple(evidence_errors + ["condition_not_met"]))
        if assertion is None or assertion.value == "unknown":
            evidence_errors.append("condition_unknown")
        return RequirementMatch(
            **common, status="pending_condition", reason_codes=tuple(evidence_errors)
        )
    common["condition_evidence_status"] = "valid"
    if assertion is None or assertion.value == "unknown":
        return RequirementMatch(
            **common, status="pending_condition", reason_codes=("condition_unknown",)
        )
    if assertion.value == "no":
        return RequirementMatch(
            **common, status="unsupported", reason_codes=("condition_not_met",)
        )
    return RequirementMatch(**common, status="supported")
