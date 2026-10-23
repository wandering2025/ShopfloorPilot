"""Strict contracts for the synthetic maintenance corpus and lexical retrieval."""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Literal, Self

from pydantic import Field, model_validator

from shopfloor_pilot.domain.base import Identifier, Sha256, StrictModel
from shopfloor_pilot.domain.models import Capability


def _require_unique(values: tuple[object, ...], label: str) -> None:
    if len(values) != len(set(values)):
        raise ValueError(f"{label} values must be unique")


class KnowledgeNoteChunk(StrictModel):
    """One author-maintained chunk before source provenance is attached."""

    chunk_id: Identifier
    evidence_excerpt: str = Field(min_length=1, max_length=2_000)
    observable_symptoms: tuple[str, ...] = Field(default=(), max_length=16)
    observable_signal_fields: tuple[Identifier, ...] = Field(default=(), max_length=16)
    possible_explanation: str | None = Field(default=None, min_length=1, max_length=500)
    suggested_checks: tuple[str, ...] = Field(default=(), max_length=16)
    additional_signal_fields: tuple[Identifier, ...] = Field(default=(), max_length=16)
    limitations: tuple[str, ...] = Field(min_length=1, max_length=16)

    @model_validator(mode="after")
    def validate_lists(self) -> Self:
        for values, label in (
            (self.observable_symptoms, "observable symptom"),
            (self.observable_signal_fields, "observable signal field"),
            (self.suggested_checks, "suggested check"),
            (self.additional_signal_fields, "additional signal field"),
            (self.limitations, "limitation"),
        ):
            _require_unique(values, label)
        return self


class KnowledgeNote(StrictModel):
    """The exact JSON schema accepted for an original synthetic maintenance note."""

    schema_version: Literal[1] = 1
    document_id: Identifier
    version: Identifier
    title: str = Field(min_length=1, max_length=200)
    provenance: Literal["original_synthetic"]
    provenance_detail: str = Field(min_length=1, max_length=500)
    applicable_capabilities: tuple[Capability, ...] = Field(default=(), max_length=16)
    chunks: tuple[KnowledgeNoteChunk, ...] = Field(min_length=1, max_length=32)

    @model_validator(mode="after")
    def validate_note(self) -> Self:
        _require_unique(self.applicable_capabilities, "applicable capability")
        _require_unique(tuple(item.chunk_id for item in self.chunks), "chunk_id")
        return self


class CorpusManifestEntry(StrictModel):
    document_id: Identifier
    version: Identifier
    source_path: str = Field(min_length=1, max_length=300)
    source_sha256: Sha256
    chunk_ids: tuple[Identifier, ...] = Field(min_length=1, max_length=32)

    @model_validator(mode="after")
    def validate_chunk_ids(self) -> Self:
        _require_unique(self.chunk_ids, "manifest chunk_id")
        return self


class CorpusManifest(StrictModel):
    schema_version: Literal[1] = 1
    corpus_id: Identifier
    entries: tuple[CorpusManifestEntry, ...] = Field(min_length=1, max_length=128)

    @model_validator(mode="after")
    def validate_entries(self) -> Self:
        _require_unique(tuple(item.document_id for item in self.entries), "document_id")
        _require_unique(tuple(item.source_path for item in self.entries), "source_path")
        all_chunk_ids = tuple(chunk_id for entry in self.entries for chunk_id in entry.chunk_ids)
        _require_unique(all_chunk_ids, "global chunk_id")
        return self


class KnowledgeChunk(StrictModel):
    """One verified, immutable retrieval unit with citation metadata."""

    document_id: Identifier
    document_version: Identifier
    source_path: str = Field(min_length=1, max_length=300)
    source_sha256: Sha256
    chunk_id: Identifier
    chunk_hash: Sha256
    title: str = Field(min_length=1, max_length=200)
    provenance: Literal["original_synthetic"]
    provenance_detail: str = Field(min_length=1, max_length=500)
    applicable_capabilities: tuple[Capability, ...] = Field(default=(), max_length=16)
    evidence_excerpt: str = Field(min_length=1, max_length=2_000)
    observable_symptoms: tuple[str, ...] = Field(default=(), max_length=16)
    observable_signal_fields: tuple[Identifier, ...] = Field(default=(), max_length=16)
    possible_explanation: str | None = Field(default=None, min_length=1, max_length=500)
    suggested_checks: tuple[str, ...] = Field(default=(), max_length=16)
    additional_signal_fields: tuple[Identifier, ...] = Field(default=(), max_length=16)
    limitations: tuple[str, ...] = Field(min_length=1, max_length=16)


class KnowledgeDocument(StrictModel):
    schema_version: Literal[1] = 1
    document_id: Identifier
    version: Identifier
    title: str = Field(min_length=1, max_length=200)
    provenance: Literal["original_synthetic"]
    provenance_detail: str = Field(min_length=1, max_length=500)
    applicable_capabilities: tuple[Capability, ...] = Field(default=(), max_length=16)
    source_path: str = Field(min_length=1, max_length=300)
    source_sha256: Sha256
    chunks: tuple[KnowledgeChunk, ...] = Field(min_length=1, max_length=32)


class KnowledgeCorpus(StrictModel):
    schema_version: Literal[1] = 1
    corpus_id: Identifier
    manifest_path: Path
    manifest_sha256: Sha256
    documents: tuple[KnowledgeDocument, ...] = Field(min_length=1, max_length=128)
    chunks: tuple[KnowledgeChunk, ...] = Field(min_length=1, max_length=4_096)

    @model_validator(mode="after")
    def validate_corpus(self) -> Self:
        _require_unique(tuple(item.document_id for item in self.documents), "document_id")
        _require_unique(tuple(item.chunk_id for item in self.chunks), "chunk_id")
        document_ids = {item.document_id for item in self.documents}
        if any(item.document_id not in document_ids for item in self.chunks):
            raise ValueError("corpus chunk references an unknown document")
        expected = tuple(
            sorted(
                (chunk for document in self.documents for chunk in document.chunks),
                key=lambda item: item.chunk_id,
            )
        )
        if self.chunks != expected:
            raise ValueError("corpus chunks must equal document chunks sorted by chunk_id")
        return self


class RetrievalConfig(StrictModel):
    """Inspectable scoring, threshold, and request bounds."""

    schema_version: Literal[1] = 1
    algorithm: Literal["bm25_okapi"] = "bm25_okapi"
    tokenizer: Literal["nfkc_casefold_identifier_v1"] = "nfkc_casefold_identifier_v1"
    k1: float = Field(default=1.2, gt=0.0, le=10.0, allow_inf_nan=False)
    b: float = Field(default=0.75, ge=0.0, le=1.0, allow_inf_nan=False)
    score_precision: int = Field(default=6, ge=0, le=9)
    min_score: float = Field(default=1.0, ge=0.0, allow_inf_nan=False)
    min_distinct_terms: int = Field(default=2, ge=1, le=64)
    max_query_characters: int = Field(default=500, ge=1, le=500)
    max_query_tokens: int = Field(default=64, ge=1, le=64)
    default_top_k: int = Field(default=3, ge=1, le=10)
    max_top_k: int = Field(default=10, ge=1, le=10)

    @model_validator(mode="after")
    def validate_bounds(self) -> Self:
        if self.default_top_k > self.max_top_k:
            raise ValueError("default_top_k cannot exceed max_top_k")
        if self.min_distinct_terms > self.max_query_tokens:
            raise ValueError("min_distinct_terms cannot exceed max_query_tokens")
        return self


class RetrievalRequest(StrictModel):
    query: str = Field(max_length=500)
    top_k: int = Field(default=3, ge=1, le=10)
    capability_filters: tuple[Capability, ...] = Field(default=(), max_length=16)

    @model_validator(mode="after")
    def validate_filters(self) -> Self:
        _require_unique(self.capability_filters, "capability filter")
        return self


class RetrievalStatus(StrEnum):
    MATCHES = "matches"
    NO_EVIDENCE = "no_evidence"
    INVALID_REQUEST = "invalid_request"


class RetrievalIssueCode(StrEnum):
    EMPTY_QUERY = "empty_query"
    QUERY_TOO_LONG = "query_too_long"
    TOO_MANY_QUERY_TOKENS = "too_many_query_tokens"
    INVALID_TOP_K = "invalid_top_k"
    MALFORMED_FILTER = "malformed_filter"
    INVALID_REQUEST = "invalid_request"


class RetrievalIssue(StrictModel):
    code: RetrievalIssueCode
    field: str = Field(min_length=1, max_length=80)
    message: str = Field(min_length=1, max_length=300)


class RetrievalHit(StrictModel):
    document_id: Identifier
    document_version: Identifier
    chunk_id: Identifier
    chunk_hash: Sha256
    source_path: str = Field(min_length=1, max_length=300)
    source_sha256: Sha256
    score: float = Field(ge=0.0, allow_inf_nan=False)
    matched_terms: tuple[str, ...] = Field(min_length=1, max_length=64)
    evidence_excerpt: str = Field(min_length=1, max_length=2_000)
    title: str = Field(min_length=1, max_length=200)
    applicable_capabilities: tuple[Capability, ...] = Field(default=(), max_length=16)
    observable_symptoms: tuple[str, ...] = Field(default=(), max_length=16)
    observable_signal_fields: tuple[Identifier, ...] = Field(default=(), max_length=16)
    possible_explanation: str | None = Field(default=None, min_length=1, max_length=500)
    suggested_checks: tuple[str, ...] = Field(default=(), max_length=16)
    additional_signal_fields: tuple[Identifier, ...] = Field(default=(), max_length=16)
    limitations: tuple[str, ...] = Field(min_length=1, max_length=16)


class KnowledgeCitation(StrictModel):
    document_id: Identifier
    document_version: Identifier
    chunk_id: Identifier
    chunk_hash: Sha256
    source_path: str = Field(min_length=1, max_length=300)
    source_sha256: Sha256
    evidence_excerpt: str = Field(min_length=1, max_length=2_000)

    @classmethod
    def from_hit(cls, hit: RetrievalHit) -> KnowledgeCitation:
        return cls(
            document_id=hit.document_id,
            document_version=hit.document_version,
            chunk_id=hit.chunk_id,
            chunk_hash=hit.chunk_hash,
            source_path=hit.source_path,
            source_sha256=hit.source_sha256,
            evidence_excerpt=hit.evidence_excerpt,
        )


class RetrievalResult(StrictModel):
    """Bounded result; empty input is no-evidence plus an explanatory issue.

    Issues on ``NO_EVIDENCE`` are therefore informational, whereas
    ``INVALID_REQUEST`` always carries a rejecting issue.
    """

    status: RetrievalStatus
    query: str = Field(max_length=500)
    hits: tuple[RetrievalHit, ...] = Field(default=(), max_length=10)
    issues: tuple[RetrievalIssue, ...] = Field(default=(), max_length=8)
    config: RetrievalConfig

    @model_validator(mode="after")
    def validate_result(self) -> Self:
        if self.status is RetrievalStatus.MATCHES:
            if not self.hits or self.issues:
                raise ValueError("matching retrieval results require hits and no issues")
        elif self.hits:
            raise ValueError("non-matching retrieval results cannot contain hits")
        if self.status is RetrievalStatus.INVALID_REQUEST and not self.issues:
            raise ValueError("invalid retrieval results require at least one issue")
        return self


class RetrievalEvaluationQueryResult(StrictModel):
    query_id: Identifier
    query: str = Field(max_length=500)
    relevant_chunk_ids: tuple[Identifier, ...] = ()
    expect_no_evidence: bool = False
    status: RetrievalStatus
    ranking: tuple[RetrievalHit, ...] = Field(default=(), max_length=10)
    answerable_hit_in_top_k: bool | None = None
    no_evidence_abstained: bool | None = None


class RetrievalScoringDefinition(StrictModel):
    top_k: int = Field(ge=1, le=10)
    answerable_query_count: int = Field(ge=0)
    no_evidence_query_count: int = Field(ge=0)
    minimum_answerable_top3_hits: int = Field(ge=0)
    required_no_evidence_abstentions: int = Field(ge=0)
    answerable_success_definition: str = Field(min_length=1, max_length=500)
    no_evidence_success_definition: str = Field(min_length=1, max_length=500)


class RetrievalEvaluationSummary(StrictModel):
    total_queries: int = Field(ge=0)
    answerable_queries: int = Field(ge=0)
    answerable_hits_in_top_k: int = Field(ge=0)
    no_evidence_queries: int = Field(ge=0)
    no_evidence_abstentions: int = Field(ge=0)
    answerable_acceptance_passed: bool
    no_evidence_acceptance_passed: bool


class RetrievalEvaluationReport(StrictModel):
    schema_version: Literal[1] = 1
    fixture_id: Identifier
    fixture_path: Path
    corpus_id: Identifier
    config: RetrievalConfig
    scoring: RetrievalScoringDefinition
    queries: tuple[RetrievalEvaluationQueryResult, ...]
    summary: RetrievalEvaluationSummary
