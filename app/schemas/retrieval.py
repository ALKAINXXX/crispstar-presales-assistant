"""Actual retrieval observations, separate from semantic support."""

from typing import Literal

from pydantic import Field, StrictInt, model_validator

from app.schemas.catalog import ReadOnlyModel
from app.schemas.knowledge import Digest, EmbeddingProfile, Source


class RetrievalOptions(ReadOnlyModel):
    top_k: StrictInt = Field(default=4, ge=2, le=5)
    min_score: float = Field(default=0.35, ge=-1, le=1, allow_inf_nan=False)
    max_source_chars: StrictInt = Field(default=420, ge=100, le=1000)
    max_total_chars: StrictInt = Field(default=1400, ge=100, le=5000)


class RetrievalHit(ReadOnlyModel):
    rank: StrictInt = Field(ge=1)
    score: float = Field(ge=-1, le=1, allow_inf_nan=False)
    relevance: Literal["related", "low"]
    source: Source


class RetrievalFailure(ReadOnlyModel):
    step: Literal["index_validation", "embedding_initialization", "query_embedding", "retrieval"]
    code: str
    message: str
    retry_action: str


class RecognizedConflict(ReadOnlyModel):
    condition_id: str
    source_ids: tuple[str, ...] = Field(min_length=1)
    reason: str
    origin: Literal["server_review", "controlled_test"]


class SemanticReview(ReadOnlyModel):
    verdict: Literal["supported", "insufficient"]
    conclusion: str
    source_ids: tuple[str, ...] = Field(min_length=1)
    rationale: str
    origin: Literal["server_review", "controlled_test"]


class RetrievalEvidence(ReadOnlyModel):
    retrieval_id: str
    chain_id: str
    query: str
    retrieved_at: str
    options: RetrievalOptions
    status: Literal["retrieved", "low_relevance", "empty", "unavailable"]
    # A score/Source identity cannot decide whether prose answers a question.
    semantic_support: Literal["not_assessed", "supported", "insufficient"] = "not_assessed"
    semantic_review: SemanticReview | None = None
    hits: tuple[RetrievalHit, ...] = ()
    reason_codes: tuple[str, ...] = ()
    excluded_source_ids: tuple[str, ...] = ()
    product_id: str | None = None
    dataset_version: str | None = None
    index_build_id: str | None = None
    index_inputs_sha256: Digest | None = None
    embedding: EmbeddingProfile | None = None
    error: RetrievalFailure | None = None
    conflicts: tuple[RecognizedConflict, ...] = ()
    scope: Literal["formal", "negative_experiment"] = "formal"

    @model_validator(mode="after")
    def check_observation(self):
        if self.status == "unavailable":
            if self.hits or self.error is None or self.conflicts or self.semantic_review:
                raise ValueError("Unavailable retrieval must have an error and no usable evidence")
        elif self.error is not None:
            raise ValueError("Completed retrieval must not carry an error")
        if self.status == "empty" and self.hits:
            raise ValueError("Empty retrieval cannot have hits")
        if self.status == "retrieved" and not any(h.relevance == "related" for h in self.hits):
            raise ValueError("Retrieved status requires a relevant hit")
        if self.status == "low_relevance" and (not self.hits or any(h.relevance != "low" for h in self.hits)):
            raise ValueError("Low relevance status requires only low hits")
        if any((h.score >= self.options.min_score) != (h.relevance == "related") for h in self.hits):
            raise ValueError("Hit score and relevance disagree")
        if len(self.hits) > self.options.top_k or len({h.source.source_id for h in self.hits}) != len(self.hits):
            raise ValueError("Invalid hit count or duplicate sources")
        if len({h.rank for h in self.hits}) != len(self.hits) or any(h.rank > self.options.top_k for h in self.hits):
            raise ValueError("Invalid ranks")
        returned = {h.source.source_id for h in self.hits}
        if any(not set(c.source_ids) <= returned for c in self.conflicts):
            raise ValueError("Conflict references a source not returned")
        if self.semantic_review is None:
            if self.semantic_support != "not_assessed":
                raise ValueError("Semantic support needs an explicit review")
        elif self.semantic_support != self.semantic_review.verdict or not set(self.semantic_review.source_ids) <= returned:
            raise ValueError("Semantic review verdict or citations disagree")
        return self


class ConditionEvidenceCheck(ReadOnlyModel):
    condition_id: str
    status: Literal["valid", "missing", "conflict", "unavailable", "invalid"]
    reason_codes: tuple[str, ...] = ()
    source_ids: tuple[str, ...] = ()
    retrieval_ids: tuple[str, ...] = ()
