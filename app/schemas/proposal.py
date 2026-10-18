"""Immutable confirmation facts persisted as an exact server snapshot."""

from typing import Literal

from pydantic import Field, StrictInt

from app.schemas.catalog import Feature, ReadOnlyModel
from app.schemas.knowledge import Digest, Source
from app.schemas.matching import PlanMatch, RequirementSet
from app.schemas.pricing import PricingResult


class ProposalCondition(ReadOnlyModel):
    condition_id: str
    name: str
    statement: str
    user_quote: str
    source_ids: tuple[str, ...] = Field(min_length=1)


class AdoptedSource(ReadOnlyModel):
    """Only a source actually used by the selected Match, with its issuer."""
    retrieval_id: str
    chain_id: str
    dataset_version: str
    index_build_id: str
    index_inputs_sha256: Digest
    source: Source


class PlanLabel(ReadOnlyModel):
    plan_id: str
    name: str


class ProposalPresentation(ReadOnlyModel):
    """Names needed to render history without consulting a newer catalog."""
    product_name: str
    plans: tuple[PlanLabel, ...]
    features: tuple[Feature, ...]
    purpose: Literal["internal_draft"] = "internal_draft"
    amount_basis: Literal["subscription_base_price"] = "subscription_base_price"


class Proposal(ReadOnlyModel):
    proposal_id: str
    session_id: str
    turn_id: str
    view_id: str | None = None
    interaction_revision: StrictInt = Field(ge=1)
    created_at: str
    catalog_version: str
    presentation: ProposalPresentation
    requirements: RequirementSet
    plans: tuple[PlanMatch, ...]
    eligible_plan_ids: tuple[str, ...]
    lowest_plan_id: str
    lowest_plan_name: str
    selected_plan_id: str
    selected_plan_name: str
    selection_reason: Literal["lowest_satisfying", "user_selected"]
    pricing: PricingResult
    conditions: tuple[ProposalCondition, ...] = ()
    adopted_sources: tuple[AdoptedSource, ...] = ()
    action_ref: str
    status: Literal["pending_confirmation"] = "pending_confirmation"
    # Legacy snapshots retain false and their historical notice. A newly
    # prepared object exposes the real page action; server checks still
    # decide whether that permission remains current at submission time.
    save_available: bool = True
    notice: str = "方案已准备，尚未保存；请核对后点击“确认并保存草案”。"
