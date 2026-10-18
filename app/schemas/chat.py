# Modified from sairam-s0/personal-ai-assistant (Apache-2.0).
# Changes: extended chat models with presales results and scoped session metadata.
# Source and license: see README.md.

from typing import Literal

from pydantic import BaseModel, Field

from app.schemas.catalog import Catalog
from app.schemas.matching import MatchResult, RequirementSet
from app.schemas.pricing import PricingResult, UnitPriceResult
from app.schemas.retrieval import RetrievalEvidence
from app.schemas.proposal import Proposal
from app.schemas.quote import QuoteFacts


class UnitPriceObservation(BaseModel):
    """One actual query result and its call associations for this turn."""
    price: UnitPriceResult
    tool_call_ids: list[str] = Field(default_factory=list)
    query_kinds: list[Literal["single_plan", "full_catalog"]] = Field(default_factory=list)


class ConsultationSupport(BaseModel):
    product_id: str
    catalog_version: str
    plan_id: str
    plan_name: str
    feature_id: str
    feature_name: str
    supported: bool
    condition_id: str | None = None
    tool_call_ids: list[str] = Field(default_factory=list)
    retrieval_ids: list[str] = Field(default_factory=list)
    source_ids: list[str] = Field(default_factory=list)


class ConsultationPrerequisite(BaseModel):
    """A verified product rule for its actual plan, never customer facts."""
    product_id: str
    catalog_version: str
    plan_id: str
    plan_name: str
    feature_id: str
    feature_name: str
    condition_id: str
    statement: str
    quote: str
    retrieval_id: str
    source_id: str
    tool_call_ids: list[str] = Field(default_factory=list)


class ConsultationResult(BaseModel):
    item_id: str
    # A normalized answer unit may retain several equivalent reviewed item
    # identities. These associations are facts, not new presentation controls.
    question_id: str | None = None
    source_item_ids: list[str] = Field(default_factory=list)
    order: int | None = None
    aspect: Literal["unit_price", "support", "meaning", "prerequisite", "detail", "pricing"]
    plan_ids: list[str] = Field(default_factory=list)
    feature_id: str | None = None
    topic: str
    status: Literal["completed", "needs_input", "incomplete"]
    stage: Literal["answered", "not_queried", "query_failed", "evidence_insufficient",
                   "explanation_pending", "explanation_rejected", "result_conflict", "dependency_unresolved",
                   "empty_scope", "extraction_incomplete", "ambiguous_scope"]
    unit_prices: list[UnitPriceResult] = Field(default_factory=list)
    support: list[ConsultationSupport] = Field(default_factory=list)
    prerequisites: list[ConsultationPrerequisite] = Field(default_factory=list)
    missing_plan_ids: list[str] = Field(default_factory=list)
    retrieval_ids: list[str] = Field(default_factory=list)
    source_ids: list[str] = Field(default_factory=list)
    tool_call_ids: list[str] = Field(default_factory=list)
    limitation: str | None = None


class ConsultationCoverage(BaseModel):
    """Accepted inquiry scope compared with actual results, never model prose."""
    scope_complete: bool = True
    status: Literal["completed", "needs_input", "incomplete"] = "completed"
    items: list[ConsultationResult] = Field(default_factory=list)
    conflicts: list[dict] = Field(default_factory=list)


class Message(BaseModel):
    role: Literal["user", "assistant", "system"]
    content: str = Field(min_length=1)
    # Existing SQLite row identity; never a recovered business authority.
    id: str | None = None


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=20_000)
    history: list[Message] = Field(default_factory=list)
    session_id: str | None = None
    # Runtime fence only; never a model-visible input or business authority.
    session_version: int | None = Field(default=None, ge=0)
    # Page nonce closes invalidation races before a first session ID exists.
    view_id: str | None = Field(default=None, min_length=16, max_length=128,
                                pattern=r"^[a-zA-Z0-9_-]+$")
    temperature: float = Field(default=0.2, ge=0.0, le=2.0)
    max_completion_tokens: int | None = Field(default=None, ge=1, le=32_768)


class ChatResponse(BaseModel):
    message: str
    model: str
    session_id: str
    session_version: int = 0
    usage: dict[str, int] | None = None
    tool_results: list[dict] = Field(default_factory=list)
    status: Literal["completed", "needs_input", "incomplete"] = "completed"
    business: "BusinessResponse | None" = None
    message_id: str | None = None


class AnswerPresentation(BaseModel):
    """Server-owned partitions of the same facts used in formal history."""
    answer_markdown: str
    show_comparison: bool = False
    show_current_result: bool = False
    used_source_ids: list[str] = Field(default_factory=list)
    # Complete service-owned receipts for the existing disclosure. Normal
    # acceptance is expressed through business conclusions in the answer;
    # unaccepted input also has a visible impact there.
    feedback: list[dict] = Field(default_factory=list)


class BusinessResponse(BaseModel):
    turn_id: str
    intent: str
    requirements: RequirementSet
    changes: list[str] = Field(default_factory=list)
    pending: list[str] = Field(default_factory=list)
    questions: list[str] = Field(default_factory=list)
    catalog: Catalog | None = None
    # Display identities from the same authority, independent of a tool query.
    # ``catalog`` above continues to mean an actual query_catalog result.
    catalog_context: Catalog | None = None
    match: MatchResult | None = None
    unit_price: UnitPriceResult | None = None
    # The legacy unit_price remains a single-plan compatibility projection.
    # Reviewed unit_prices follows the accepted price inquiry scope/order.
    # Observations retain the full real query scope for receipt associations;
    # their extra rates do not expand the formal answer or its completion.
    unit_prices: list[UnitPriceResult] = Field(default_factory=list)
    unit_price_observations: list[UnitPriceObservation] = Field(default_factory=list)
    consultation_coverage: ConsultationCoverage | None = None
    pricing: PricingResult | None = None
    retrieval_evidence: list[RetrievalEvidence] = Field(default_factory=list)
    knowledge_answers: list[dict] = Field(default_factory=list)
    errors: list[dict] = Field(default_factory=list)
    current_task: "CurrentTask | None" = None
    recovery_notice: str | None = None
    presentation: AnswerPresentation | None = None
    # Presentation relation from accepted before/after facts, never inferred
    # from user prose or an absent candidate parameter.
    selection_unchanged: bool = False
    # Actual before/after adaptation relation supplied by orchestration.
    # None keeps legacy responses renderable without inventing turn history.
    comparison_changed: bool | None = None
    proposal: Proposal | None = None
    queried_quotes: list[QuoteFacts] = Field(default_factory=list)
    semantic_review: dict | None = None
    operation_feedback: list[dict] = Field(default_factory=list)


class CurrentTask(BaseModel):
    """Process-owned current selection, separate from this turn's inquiry."""
    kind: Literal["selection"] = "selection"
    status: Literal["current", "needs_reassessment"]
    last_result: Literal["updated", "not_updated"] = "updated"
    requirements: RequirementSet
    match: MatchResult | None = None
    pricing: PricingResult | None = None
    pending: list[str] = Field(default_factory=list)
    questions: list[str] = Field(default_factory=list)
    # Remain stable during inquiries and rejected inputs. A changed but not
    # yet assessed requirement revision owns a new result location as well.
    result_turn_id: str | None = None
    result_message_id: str | None = None


ChatResponse.model_rebuild()
BusinessResponse.model_rebuild()
