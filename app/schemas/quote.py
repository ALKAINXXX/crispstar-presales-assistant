"""Real persisted business facts and model-independent action receipts."""

from typing import Literal

from pydantic import model_validator

from app.schemas.catalog import ReadOnlyModel
from app.schemas.proposal import (AdoptedSource, Proposal, ProposalCondition,
                                  ProposalPresentation)
from app.schemas.matching import PlanMatch, RequirementSet
from app.schemas.pricing import PricingResult


class Quote(ReadOnlyModel):
    quote_id: str
    saved_at: str
    snapshot: Proposal
    snapshot_version: Literal[1] = 1
    source_conversation_status: Literal["active", "trash", "permanently_deleted", "not_found"] = "active"


class QuoteSnapshotFacts(ReadOnlyModel):
    """Saved business facts only: no action, view or submission authority."""
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
    conditions: tuple[ProposalCondition, ...]
    adopted_sources: tuple[AdoptedSource, ...]


class QuoteFacts(ReadOnlyModel):
    quote_id: str
    saved_at: str
    snapshot_version: Literal[1] = 1
    snapshot: QuoteSnapshotFacts
    source_conversation_status: Literal["active", "trash", "permanently_deleted", "not_found"] = "active"


def quote_facts(quote: Quote) -> QuoteFacts:
    fields = QuoteSnapshotFacts.model_fields
    return QuoteFacts(quote_id=quote.quote_id, saved_at=quote.saved_at,
        snapshot_version=quote.snapshot_version,
        source_conversation_status=quote.source_conversation_status,
        snapshot=QuoteSnapshotFacts(**{name: getattr(quote.snapshot, name) for name in fields}))


class QuoteActionResult(ReadOnlyModel):
    status: Literal["saved", "not_saved", "unknown"]
    code: str
    message: str
    quote: Quote | None = None
    quote_id: str | None = None
    saved_at: str | None = None
    current_status: Literal["active", "trash", "permanently_deleted"] | None = None

    @model_validator(mode="after")
    def check_receipt(self):
        deleted_receipt = (self.quote is None and self.quote_id is not None
                           and self.saved_at is not None
                           and self.current_status in {"trash", "permanently_deleted"})
        if self.status == "saved":
            if self.quote is None and not deleted_receipt:
                raise ValueError("Saved persistence requires its snapshot or deleted-object identity")
        elif (self.quote is not None or self.quote_id is not None or self.saved_at is not None
              or self.current_status is not None):
            raise ValueError("Only confirmed persistence may carry a Quote receipt")
        return self
