"""Immutable source records and local index metadata."""

import math
from typing import Annotated, Literal

from pydantic import Field, StrictInt, model_validator

from app.schemas.catalog import ReadOnlyModel


Identity = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_-]*$")]
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class SectionSpec(ReadOnlyModel):
    section_id: Identity
    title: str
    plan_ids: tuple[str, ...] = ()
    feature_ids: tuple[str, ...] = ()
    condition_ids: tuple[str, ...] = ()


class DocumentSpec(ReadOnlyModel):
    document_id: Identity
    file: Annotated[str, Field(pattern=r"^[a-zA-Z0-9_-]+\.md$")]
    title: str
    sections: tuple[SectionSpec, ...] = Field(min_length=1)


class CorpusManifest(ReadOnlyModel):
    schema_version: Literal[1]
    version: str
    product_id: str
    documents: tuple[DocumentSpec, ...] = Field(min_length=4, max_length=8)


class FileDigest(ReadOnlyModel):
    file: str
    sha256: Digest


class LibraryVersion(ReadOnlyModel):
    name: str
    version: str


class EmbeddingProfile(ReadOnlyModel):
    provider: str
    model: str
    repository: str
    revision: str
    files: tuple[FileDigest, ...]
    dimension: StrictInt = Field(gt=0)
    max_tokens: StrictInt = Field(gt=0)
    pooling: str
    normalization: str
    document_prefix: str | None
    query_prefix: str | None
    execution_providers: tuple[str, ...]
    threads: StrictInt = Field(gt=0)
    batch_size: StrictInt = Field(gt=0)
    parallel: StrictInt | None
    libraries: tuple[LibraryVersion, ...]

    @classmethod
    def from_configuration(cls, values: dict) -> "EmbeddingProfile":
        fields = dict(values)
        fields["files"] = tuple(FileDigest(file=k, sha256=v) for k, v in sorted(fields.pop("file_sha256").items()))
        fields["libraries"] = tuple(LibraryVersion(name=k, version=v) for k, v in sorted(fields.pop("versions").items()))
        fields["execution_providers"] = tuple(fields["execution_providers"])
        for name in ("document_prefix", "query_prefix"):
            fields[name] = fields[name] or None
        return cls(**fields)


class ChunkingPolicy(ReadOnlyModel):
    strategy: Literal["markdown_h2_whole_section_v1"] = "markdown_h2_whole_section_v1"
    max_embedding_chars: StrictInt = Field(default=420, ge=100, le=480)
    include_document_title: Literal[True] = True


class Source(ReadOnlyModel):
    source_id: str
    document_id: Identity
    document_title: str
    relative_path: str
    dataset_version: str
    section_id: Identity
    section_title: str
    line_start: StrictInt = Field(ge=1)
    line_end: StrictInt = Field(ge=1)
    raw_text: str
    document_sha256: Digest
    text_sha256: Digest
    plan_ids: tuple[str, ...] = ()
    feature_ids: tuple[str, ...] = ()
    condition_ids: tuple[str, ...] = ()

    @model_validator(mode="after")
    def check_location(self):
        if self.line_end < self.line_start:
            raise ValueError("Source line_end precedes line_start")
        return self

    def embedding_text(self) -> str:
        # raw_text includes its original section heading and complete body.
        return self.document_title + "\n" + self.raw_text


class IndexInputs(ReadOnlyModel):
    dataset_version: str
    product_id: str
    catalog_sha256: Digest
    manifest_sha256: Digest
    documents: tuple[FileDigest, ...]
    chunking: ChunkingPolicy
    embedding: EmbeddingProfile
    sources: tuple[Source, ...] = Field(min_length=1)


class KnowledgeIndex(ReadOnlyModel):
    schema_version: Literal[1] = 1
    scope: Literal["w04_local_index", "w05_negative_experiment"] = "w04_local_index"
    build_id: str
    built_at: str
    inputs_sha256: Digest
    inputs: IndexInputs
    vectors: tuple[tuple[float, ...], ...]

    @model_validator(mode="after")
    def check_vectors(self):
        if len(self.vectors) != len(self.inputs.sources):
            raise ValueError("Vector and Source counts differ")
        for row in self.vectors:
            if len(row) != self.inputs.embedding.dimension:
                raise ValueError("Vector dimension mismatch")
            if not all(math.isfinite(x) for x in row):
                raise ValueError("Vector contains a non-finite value")
            if not math.isclose(math.sqrt(sum(x * x for x in row)), 1.0, abs_tol=1e-5):
                raise ValueError("Vector is not L2 normalized")
        identities = [source.source_id for source in self.inputs.sources]
        if len(set(identities)) != len(identities):
            raise ValueError("Source identities are not unique")
        return self


class IndexFile(ReadOnlyModel):
    payload_sha256: Digest
    payload: KnowledgeIndex
