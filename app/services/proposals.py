"""Assemble server-owned facts consumed unchanged by persistence transactions.

Authority is the actual WorkState context and its issuing KnowledgeService.
Serialized model/client fact packages are never accepted by this interface.
The calling Orchestrator owns ordering and page/interaction validity.
"""

from datetime import datetime, timezone
import secrets
from uuid import uuid4

from app.schemas.proposal import (AdoptedSource, PlanLabel, Proposal,
                                  ProposalCondition, ProposalPresentation)
from app.services.catalog import get_catalog
from app.services.matching import match_requirements
from app.services.pricing import calculate_subscription_price


class ProposalRejected(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__("方案尚不可确认：" + code)


def selection_facts(state):
    """Check a retained selection without parsing text or issuing a query."""
    if getattr(state, "review_locked", False) and (not state.review_complete or state.review_blockers):
        raise ProposalRejected("semantic_review_incomplete")
    state.current_task()  # Revalidates current Match using existing evidence IDs.
    context = state._selection_context
    if context is None or not context.wants_price or context.pending:
        raise ProposalRejected("selection_incomplete")
    requirements, match, price = context.requirements, context.match, context.pricing
    if not requirements.items or any(i.kind != "feature" for i in requirements.items):
        raise ProposalRejected("hard_requirements_unresolved")
    if match is None or price is None:
        raise ProposalRejected("match_or_price_missing")
    if context.pricing_requirements != requirements or context.pricing_match != match:
        raise ProposalRejected("pricing_requirement_revision_mismatch")
    catalog = get_catalog()
    if (match.requirements != requirements or match.catalog_version != catalog.version
            or price.catalog_version != catalog.version):
        raise ProposalRejected("input_or_version_mismatch")
    if match.evidence_origin == "test_fixture" or match.test_evidence:
        raise ProposalRejected("test_evidence_forbidden")
    if (match.status != "matched" or match.blocking_reasons or not match.lowest_plan_id
            or match.selected_plan_id not in match.eligible_plan_ids):
        raise ProposalRejected("selection_not_satisfied")
    ids = tuple(e.retrieval_id for e in match.retrieval_evidence)
    if any(e.chain_id != state.knowledge.chain_id or state.knowledge.evidence(e.retrieval_id) != e
           or e.scope != "formal" for e in match.retrieval_evidence):
        raise ProposalRejected("evidence_not_owned")
    checked = match_requirements(requirements, catalog=catalog,
        knowledge=state.knowledge, retrieval_ids=ids)
    if checked != match:
        raise ProposalRejected("match_no_longer_current")
    selected = next(p for p in match.plans if p.plan_id == match.selected_plan_id)
    if (not selected.satisfies_all or selected.blocking_reasons
            or any(row.status != "supported" for row in selected.items)):
        raise ProposalRejected("selected_plan_incomplete")
    parameters = {p.name: p.value for p in requirements.parameters}
    if (set(parameters) != {"paid_seats", "months"} or price.plan_id != match.selected_plan_id
            or (price.paid_seats, price.months) != (parameters["paid_seats"], parameters["months"])):
        raise ProposalRejected("pricing_operands_mismatch")
    # Recalculate only for comparison. The snapshot keeps the actual adopted
    # result, never a substituted current amount or arbitrary supplied price.
    if calculate_subscription_price(price.plan_id, price.paid_seats, price.months) != price:
        raise ProposalRejected("pricing_not_catalog_result")
    conditions, adopted = [], []
    adopted_ids = set()
    for row in selected.items:
        if row.condition_id is None:
            continue
        assertion = next((a for a in requirements.condition_assertions if a.condition_id == row.condition_id), None)
        condition = next(c for c in catalog.conditions if c.condition_id == row.condition_id)
        if (row.condition_evidence_status != "valid" or row.condition_assertion_status != "yes"
                or assertion is None or assertion.value != "yes"
                or row.condition_user_quote != assertion.user_quote or not row.evidence_source_ids):
            raise ProposalRejected("condition_incomplete")
        conditions.append(ProposalCondition(condition_id=condition.condition_id,
            name=condition.name, statement=condition.statement,
            user_quote=assertion.user_quote, source_ids=row.evidence_source_ids))
        adopted_ids.update(row.evidence_source_ids)
    for evidence in match.retrieval_evidence:
        used = tuple(h.source.source_id for h in evidence.hits if h.source.source_id in adopted_ids)
        if not used:
            continue
        sources = state.knowledge.validate_citations(evidence.retrieval_id, used)
        current_index = state.knowledge._validate_current(evidence)
        if (evidence.dataset_version != catalog.version or evidence.product_id != catalog.product.product_id
                or evidence.index_build_id != current_index.build_id or not evidence.index_inputs_sha256):
            raise ProposalRejected("adopted_source_version_mismatch")
        for source in sources:
            if source.dataset_version != catalog.version:
                raise ProposalRejected("adopted_source_version_mismatch")
            if any(s.source.source_id == source.source_id for s in adopted):
                continue
            adopted.append(AdoptedSource(retrieval_id=evidence.retrieval_id,
                chain_id=evidence.chain_id, dataset_version=evidence.dataset_version,
                index_build_id=evidence.index_build_id,
                index_inputs_sha256=evidence.index_inputs_sha256, source=source))
    if adopted_ids != {s.source.source_id for s in adopted}:
        raise ProposalRejected("adopted_sources_missing")
    return requirements, match, price, tuple(conditions), tuple(adopted), catalog


def presentation_facts(catalog, requirements):
    used = {item.feature_id for item in requirements.items}
    return ProposalPresentation(product_name=catalog.product.name,
        plans=tuple(PlanLabel(plan_id=p.plan_id, name=p.name) for p in catalog.plans),
        features=tuple(f for f in catalog.features if f.feature_id in used))


def prepare_proposal(state, *, session_id: str, turn_id: str,
                     interaction_revision: int, view_id: str | None = None) -> Proposal:
    if getattr(state, "review_locked", False) and state.review_preserve_results:
        raise ProposalRejected("no_new_proposal_requested")
    if state.intent != "selection":
        raise ProposalRejected("intent_not_selection")
    req, match, price, conditions, adopted, catalog = selection_facts(state)
    names = {p.plan_id: p.name for p in catalog.plans}
    return Proposal(proposal_id=uuid4().hex, session_id=session_id, turn_id=turn_id,
        view_id=view_id, interaction_revision=interaction_revision,
        created_at=datetime.now(timezone.utc).isoformat(), catalog_version=catalog.version,
        presentation=presentation_facts(catalog, req),
        requirements=req, plans=match.plans, eligible_plan_ids=match.eligible_plan_ids,
        lowest_plan_id=match.lowest_plan_id, lowest_plan_name=names[match.lowest_plan_id],
        selected_plan_id=match.selected_plan_id, selected_plan_name=names[match.selected_plan_id],
        selection_reason=match.selection_reason, pricing=price,
        conditions=conditions, adopted_sources=adopted, action_ref=secrets.token_urlsafe(32))


def validate_proposal(proposal: Proposal, state) -> None:
    req, match, price, conditions, adopted, catalog = selection_facts(state)
    names = {p.plan_id: p.name for p in catalog.plans}
    if (proposal.catalog_version != catalog.version or proposal.requirements != req
            or proposal.presentation != presentation_facts(catalog, req)
            or proposal.plans != match.plans or proposal.eligible_plan_ids != match.eligible_plan_ids
            or proposal.lowest_plan_id != match.lowest_plan_id
            or proposal.lowest_plan_name != names[match.lowest_plan_id]
            or proposal.selected_plan_id != match.selected_plan_id
            or proposal.selected_plan_name != names[match.selected_plan_id]
            or proposal.selection_reason != match.selection_reason or proposal.pricing != price
            or proposal.conditions != conditions or proposal.adopted_sources != adopted):
        raise ProposalRejected("proposal_facts_changed")
