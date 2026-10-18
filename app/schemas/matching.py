"""Normalized requirements and explainable results, without language parsing."""

from typing import Literal

from pydantic import Field, StrictInt, model_validator

from app.schemas.catalog import ReadOnlyModel
from app.schemas.retrieval import RetrievalEvidence


class HardRequirement(ReadOnlyModel):
    requirement_id: str = Field(min_length=1)
    statement: str = Field(min_length=1)
    user_quote: str = Field(min_length=1)
    feature_id: str | None = None
    kind: Literal["feature", "unmodeled", "ambiguous"] = "feature"

    @model_validator(mode="after")
    def check_kind(self):
        if self.kind == "feature" and self.feature_id is None:
            raise ValueError("A feature requirement needs a feature_id")
        if self.kind == "unmodeled" and self.feature_id is not None:
            raise ValueError("An unmodeled requirement must not claim a feature_id")
        return self


class ConditionAssertion(ReadOnlyModel):
    condition_id: str = Field(min_length=1)
    value: Literal["yes", "no", "unknown"]
    user_quote: str = Field(min_length=1)


class DeclaredParameter(ReadOnlyModel):
    # Preserve explicit input and its basis. The pricing service checks ranges.
    name: Literal["paid_seats", "months"]
    value: StrictInt
    user_quote: str = Field(min_length=1)


class RequirementSet(ReadOnlyModel):
    requirement_id: str = Field(min_length=1)
    revision: StrictInt = Field(default=1, ge=1)
    previous_revision: StrictInt | None = Field(default=None, ge=1)
    items: tuple[HardRequirement, ...] = ()
    selected_plan_id: str | None = None
    parameters: tuple[DeclaredParameter, ...] = ()
    context_quotes: tuple[str, ...] = ()
    condition_assertions: tuple[ConditionAssertion, ...] = ()

    @model_validator(mode="after")
    def check_identity_and_revision(self):
        groups = (
            ("requirement", tuple(item.requirement_id for item in self.items)),
            ("parameter", tuple(item.name for item in self.parameters)),
            ("condition assertion", tuple(item.condition_id for item in self.condition_assertions)),
        )
        for label, identities in groups:
            if len(identities) != len(set(identities)):
                raise ValueError(f"Duplicate {label} identity")
        if self.previous_revision is not None and self.previous_revision >= self.revision:
            raise ValueError("previous_revision must precede revision")
        if any(not quote.strip() for quote in self.context_quotes):
            raise ValueError("context_quotes must not contain blank text")
        return self


class TestConditionEvidence(ReadOnlyModel):
    """Explicit test fixture only; not a retrieval result or runtime authority."""

    __test__ = False
    condition_id: str = Field(min_length=1)
    catalog_version: str = Field(min_length=1)
    source_id: str = Field(min_length=1)
    quote: str = Field(min_length=1)
    conflict: bool = False
    purpose: Literal["test_fixture"] = "test_fixture"


class RequirementMatch(ReadOnlyModel):
    requirement_id: str
    feature_id: str | None
    status: Literal["supported", "unsupported", "unknown", "pending_condition"]
    reason_codes: tuple[str, ...] = ()
    condition_id: str | None = None
    evidence_source_ids: tuple[str, ...] = ()
    condition_user_quote: str | None = None
    condition_evidence_status: Literal["valid", "missing", "conflict", "unavailable", "invalid"] | None = None
    condition_assertion_status: Literal["yes", "no", "unknown"] | None = None


class PlanMatch(ReadOnlyModel):
    plan_id: str
    items: tuple[RequirementMatch, ...]
    satisfies_all: bool
    blocking_reasons: tuple[str, ...] = ()


class MatchResult(ReadOnlyModel):
    requirements: RequirementSet
    catalog_version: str
    plans: tuple[PlanMatch, ...]
    eligible_plan_ids: tuple[str, ...]
    lowest_plan_id: str | None
    selected_plan_id: str | None
    selection_reason: Literal[
        "lowest_satisfying", "user_selected", "no_satisfying_plan",
        "selected_plan_not_satisfying", "unknown_selected_plan", "empty_requirements",
    ]
    status: Literal["matched", "blocked", "comparison_only"]
    evidence_origin: Literal["none", "test_fixture", "retrieval"] = "none"
    test_evidence: tuple[TestConditionEvidence, ...] = ()
    retrieval_evidence: tuple[RetrievalEvidence, ...] = ()
    blocking_reasons: tuple[str, ...] = ()
