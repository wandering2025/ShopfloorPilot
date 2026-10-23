"""Strict, non-authoritative contracts for evidence-based diagnostic hypotheses.

These models intentionally describe observations, possible explanations, missing
evidence, and citations.  They contain no repair command, resource delta, approval,
or maintenance-restoration capability.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal, Self

from pydantic import Field, model_validator

from shopfloor_pilot.domain.base import Identifier, Seconds, Sha256, StrictModel
from shopfloor_pilot.domain.models import Capability, Observation, TelemetryValue
from shopfloor_pilot.retrieval import KnowledgeCitation, RetrievalResult, RetrievalStatus

MAX_OBSERVATION_SIGNALS = 16


class BoundaryStatus(StrEnum):
    """Outcome of a run-bound diagnostic service call."""

    SUCCESS = "success"
    INVALID_REQUEST = "invalid_request"
    WRONG_RUN = "wrong_run"
    READ_FAILURE = "read_failure"
    REJECTED = "rejected"


class DiagnosticIssueCode(StrEnum):
    INVALID_REQUEST = "invalid_request"
    WRONG_RUN = "wrong_run"
    RUN_UNAVAILABLE = "run_unavailable"
    EVENT_NOT_FOUND = "event_not_found"
    MACHINE_NOT_FOUND = "machine_not_found"
    NOT_OBSERVATION_EVENT = "not_observation_event"
    FILTER_MISMATCH = "filter_mismatch"
    INVALID_EVENT_DATA = "invalid_event_data"
    RETRIEVAL_INVALID = "retrieval_invalid"
    STALE_SNAPSHOT = "stale_snapshot"
    OBSERVATION_CITATION_INVALID = "observation_citation_invalid"
    GUIDANCE_CITATION_INVALID = "guidance_citation_invalid"


class DiagnosticIssue(StrictModel):
    code: DiagnosticIssueCode
    message: str = Field(min_length=1, max_length=300)
    reference_id: Identifier | None = None


class ObservationSummaryRequest(StrictModel):
    """Bounded filters over persisted observation events in one authorized run."""

    schema_version: Literal[1] = 1
    run_id: Identifier
    event_ids: tuple[Identifier, ...] = Field(default=(), max_length=32)
    machine_ids: tuple[Identifier, ...] = Field(default=(), max_length=16)
    after_sequence: int = Field(default=0, strict=True, ge=0)
    limit: int = Field(default=20, strict=True, ge=1, le=50)

    @model_validator(mode="after")
    def validate_filters(self) -> Self:
        if len(set(self.event_ids)) != len(self.event_ids):
            raise ValueError("event_ids must be unique")
        if len(set(self.machine_ids)) != len(self.machine_ids):
            raise ValueError("machine_ids must be unique")
        if self.event_ids and len(self.event_ids) > self.limit:
            raise ValueError("limit must cover every explicitly requested event_id")
        return self


class ObservationEvidence(StrictModel):
    """One typed observation bound to its exact persisted event identity."""

    schema_version: Literal[1] = 1
    event_id: Identifier
    sequence: int = Field(strict=True, ge=1)
    resulting_state_version: int = Field(strict=True, ge=1)
    observation: Observation

    @model_validator(mode="after")
    def validate_observation_bound(self) -> Self:
        if len(self.observation.signals) > MAX_OBSERVATION_SIGNALS:
            raise ValueError(
                f"observation evidence cannot exceed {MAX_OBSERVATION_SIGNALS} signals"
            )
        return self


class ObservationSummary(StrictModel):
    """A bounded observation view pinned to an immutable checkpoint prefix."""

    schema_version: Literal[1] = 1
    run_id: Identifier
    state_version: int = Field(strict=True, ge=0)
    snapshot_hash: Sha256
    last_sequence: int = Field(strict=True, ge=0)
    simulated_time_seconds: Seconds
    observations: tuple[ObservationEvidence, ...] = Field(default=(), max_length=50)
    truncated: bool = False
    next_after_sequence: int | None = Field(default=None, strict=True, ge=1)

    @model_validator(mode="after")
    def validate_summary(self) -> Self:
        event_ids = [item.event_id for item in self.observations]
        if len(event_ids) != len(set(event_ids)):
            raise ValueError("observation evidence event IDs must be unique")
        sequences = [item.sequence for item in self.observations]
        if sequences != sorted(sequences):
            raise ValueError("observation evidence must use event-stream order")
        for item in self.observations:
            if item.sequence > self.last_sequence:
                raise ValueError("observation evidence exceeds the pinned event prefix")
            if item.resulting_state_version > self.state_version:
                raise ValueError("observation evidence exceeds the pinned state version")
            if item.observation.run_id != self.run_id:
                raise ValueError("observation evidence belongs to another run")
        if self.truncated:
            if not self.observations or self.next_after_sequence != self.observations[-1].sequence:
                raise ValueError("a truncated summary requires a continuation sequence")
        elif self.next_after_sequence is not None:
            raise ValueError("an untruncated summary cannot have a continuation sequence")
        return self


class ObservationSummaryResult(StrictModel):
    schema_version: Literal[1] = 1
    status: BoundaryStatus
    summary: ObservationSummary | None = None
    issues: tuple[DiagnosticIssue, ...] = Field(default=(), max_length=32)

    @model_validator(mode="after")
    def validate_result(self) -> Self:
        if self.status is BoundaryStatus.SUCCESS:
            if self.summary is None or self.issues:
                raise ValueError("a successful summary result requires only a summary")
        elif self.summary is not None or not self.issues:
            raise ValueError("a rejected summary result requires issues and no summary")
        return self


class DiagnosticRetrievalRequest(StrictModel):
    """Run-authorized wrapper around the corpus-only retrieval request."""

    schema_version: Literal[1] = 1
    run_id: Identifier
    query: str = Field(max_length=500)
    top_k: int = Field(default=5, strict=True, ge=1, le=10)
    capability_filters: tuple[Capability, ...] = Field(default=(), max_length=5)

    @model_validator(mode="after")
    def validate_capabilities(self) -> Self:
        if len(set(self.capability_filters)) != len(self.capability_filters):
            raise ValueError("capability_filters must be unique")
        return self


class DiagnosticRetrievalResult(StrictModel):
    schema_version: Literal[1] = 1
    status: BoundaryStatus
    retrieval: RetrievalResult | None = None
    issues: tuple[DiagnosticIssue, ...] = Field(default=(), max_length=32)

    @model_validator(mode="after")
    def validate_result(self) -> Self:
        if self.status is BoundaryStatus.SUCCESS:
            if self.retrieval is None or self.issues:
                raise ValueError("successful retrieval requires only a retrieval result")
            if self.retrieval.status is RetrievalStatus.INVALID_REQUEST:
                raise ValueError("invalid retrieval cannot have a successful boundary status")
        elif self.status is BoundaryStatus.INVALID_REQUEST and self.retrieval is not None:
            if self.retrieval.status is not RetrievalStatus.INVALID_REQUEST or not self.issues:
                raise ValueError("nested invalid retrieval requires an outer typed rejection")
        elif self.retrieval is not None or not self.issues:
            raise ValueError("rejected retrieval requires issues and no retrieval result")
        return self


class DiagnosticRequest(StrictModel):
    schema_version: Literal[1] = 1
    run_id: Identifier
    query: str = Field(default="", max_length=500)
    event_ids: tuple[Identifier, ...] = Field(default=(), max_length=32)
    machine_ids: tuple[Identifier, ...] = Field(default=(), max_length=16)
    after_sequence: int = Field(default=0, strict=True, ge=0)
    observation_limit: int = Field(default=20, strict=True, ge=1, le=50)
    top_k: int = Field(default=5, strict=True, ge=1, le=10)
    capability_filters: tuple[Capability, ...] = Field(default=(), max_length=5)

    @model_validator(mode="after")
    def validate_filters(self) -> Self:
        if len(set(self.event_ids)) != len(self.event_ids):
            raise ValueError("event_ids must be unique")
        if len(set(self.machine_ids)) != len(self.machine_ids):
            raise ValueError("machine_ids must be unique")
        if len(set(self.capability_filters)) != len(self.capability_filters):
            raise ValueError("capability_filters must be unique")
        if self.event_ids and len(self.event_ids) > self.observation_limit:
            raise ValueError("observation_limit must cover every requested event_id")
        return self


class QualitativeConfidence(StrEnum):
    """Uncalibrated rule-derived support, never a probability."""

    LIMITED = "limited"
    MODERATE = "moderate"


class ObservationCitation(StrictModel):
    event_id: Identifier
    observation_id: Identifier
    machine_id: Identifier
    observed_at_seconds: Seconds
    signal_fields: tuple[Identifier, ...] = Field(
        min_length=1,
        max_length=MAX_OBSERVATION_SIGNALS,
    )
    signals: tuple[TelemetryValue, ...] = Field(
        min_length=1,
        max_length=MAX_OBSERVATION_SIGNALS,
    )

    @model_validator(mode="after")
    def validate_signals(self) -> Self:
        names = tuple(item.name for item in self.signals)
        if names != self.signal_fields:
            raise ValueError("signal_fields must exactly match the cited signal names")
        if len(set(names)) != len(names):
            raise ValueError("cited signal fields must be unique")
        return self


class SignalReading(StrictModel):
    value: int = Field(strict=True)
    unit: str = Field(min_length=1, max_length=40)
    event_ids: tuple[Identifier, ...] = Field(min_length=1, max_length=50)


class EvidenceConflict(StrictModel):
    machine_id: Identifier
    observed_at_seconds: Seconds
    signal_field: Identifier
    readings: tuple[SignalReading, ...] = Field(min_length=2, max_length=50)

    @model_validator(mode="after")
    def validate_distinct_readings(self) -> Self:
        values = [(item.value, item.unit) for item in self.readings]
        if len(values) != len(set(values)):
            raise ValueError("conflict readings must have distinct value/unit pairs")
        return self


class MissingEvidenceReason(StrEnum):
    NO_OBSERVATIONS = "no_observations"
    NO_RETRIEVED_GUIDANCE = "no_retrieved_guidance"
    NO_SUPPORTED_EXPLANATION = "no_supported_explanation"
    NO_APPLICABLE_GUIDANCE = "no_applicable_guidance"
    NO_SIGNAL_OVERLAP = "no_signal_overlap"
    ADDITIONAL_SIGNAL_REQUIRED = "additional_signal_required"
    OBSERVATIONS_TRUNCATED = "observations_truncated"


class MissingEvidence(StrictModel):
    reason: MissingEvidenceReason
    description: str = Field(min_length=1, max_length=500)
    machine_id: Identifier | None = None
    signal_field: Identifier | None = None
    guidance_chunk_id: Identifier | None = None


class SuggestedCheck(StrictModel):
    guidance_chunk_id: Identifier
    text: str = Field(min_length=1, max_length=500)


class DiagnosticHypothesis(StrictModel):
    """A possible explanation grounded in both persisted and retrieved evidence."""

    machine_id: Identifier
    possible_explanation: str = Field(min_length=1, max_length=500)
    confidence: QualitativeConfidence
    confidence_basis: Literal["uncalibrated_rule_derived"] = "uncalibrated_rule_derived"
    supporting_signal_fields: tuple[Identifier, ...] = Field(min_length=1, max_length=16)
    observation_citations: tuple[ObservationCitation, ...] = Field(min_length=1, max_length=50)
    guidance_citations: tuple[KnowledgeCitation, ...] = Field(min_length=1, max_length=10)
    missing_evidence: tuple[MissingEvidence, ...] = Field(default=(), max_length=50)
    limitations: tuple[str, ...] = Field(default=(), max_length=20)

    @model_validator(mode="after")
    def validate_hypothesis(self) -> Self:
        if len(set(self.supporting_signal_fields)) != len(self.supporting_signal_fields):
            raise ValueError("supporting_signal_fields must be unique")
        if any(item.machine_id != self.machine_id for item in self.observation_citations):
            raise ValueError("all observation citations must belong to the hypothesis machine")
        cited_fields = {
            field for citation in self.observation_citations for field in citation.signal_fields
        }
        if not set(self.supporting_signal_fields) <= cited_fields:
            raise ValueError("every supporting signal field must be cited")
        return self


class DiagnosticStatus(StrEnum):
    HYPOTHESES = "hypotheses"
    AMBIGUOUS = "ambiguous"
    CONFLICTING_EVIDENCE = "conflicting_evidence"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    INVALID_REQUEST = "invalid_request"
    WRONG_RUN = "wrong_run"
    READ_FAILURE = "read_failure"
    RETRIEVAL_FAILURE = "retrieval_failure"


class DiagnosticResult(StrictModel):
    schema_version: Literal[1] = 1
    status: DiagnosticStatus
    run_id: Identifier
    summary: ObservationSummary | None = None
    retrieval: RetrievalResult | None = None
    hypotheses: tuple[DiagnosticHypothesis, ...] = Field(default=(), max_length=20)
    conflicts: tuple[EvidenceConflict, ...] = Field(default=(), max_length=50)
    missing_evidence: tuple[MissingEvidence, ...] = Field(default=(), max_length=100)
    suggested_checks: tuple[SuggestedCheck, ...] = Field(default=(), max_length=100)
    ambiguous: bool = False
    confidence_is_calibrated: Literal[False] = False
    confirmed_root_cause: Literal[False] = False
    maintenance_completed: Literal[False] = False
    restoration_authority: Literal["operator_or_scenario_harness_only"] = (
        "operator_or_scenario_harness_only"
    )
    issues: tuple[DiagnosticIssue, ...] = Field(default=(), max_length=32)

    @model_validator(mode="after")
    def validate_result(self) -> Self:
        evidence_statuses = {
            DiagnosticStatus.HYPOTHESES,
            DiagnosticStatus.AMBIGUOUS,
            DiagnosticStatus.CONFLICTING_EVIDENCE,
        }
        if self.hypotheses and self.status not in evidence_statuses:
            raise ValueError("hypotheses require an evidence-bearing status")
        if not self.hypotheses and self.status in {
            DiagnosticStatus.HYPOTHESES,
            DiagnosticStatus.AMBIGUOUS,
        }:
            raise ValueError("the selected status requires at least one hypothesis")
        if self.status is DiagnosticStatus.AMBIGUOUS and not self.ambiguous:
            raise ValueError("ambiguous status requires ambiguous=true")
        distinct_explanations = {item.possible_explanation for item in self.hypotheses}
        if self.ambiguous and len(distinct_explanations) < 2:
            raise ValueError("ambiguity requires multiple distinct possible explanations")
        if self.ambiguous and self.status not in {
            DiagnosticStatus.AMBIGUOUS,
            DiagnosticStatus.CONFLICTING_EVIDENCE,
        }:
            raise ValueError("ambiguity requires an ambiguous or conflicting-evidence status")
        if self.hypotheses or self.suggested_checks:
            if self.retrieval is None:
                raise ValueError("evidence-backed outputs require their retrieval result")
            available_citations = tuple(
                KnowledgeCitation.from_hit(hit) for hit in self.retrieval.hits
            )
            for hypothesis in self.hypotheses:
                if any(
                    citation not in available_citations
                    for citation in hypothesis.guidance_citations
                ):
                    raise ValueError("hypothesis guidance must resolve within the retrieval result")
            checks_by_chunk = {
                hit.chunk_id: set(hit.suggested_checks) for hit in self.retrieval.hits
            }
            if any(
                check.text not in checks_by_chunk.get(check.guidance_chunk_id, set())
                for check in self.suggested_checks
            ):
                raise ValueError("suggested checks must be exact retrieved guidance text")
        unique_observation_citations: list[ObservationCitation] = []
        for hypothesis in self.hypotheses:
            for citation in hypothesis.observation_citations:
                if citation not in unique_observation_citations:
                    unique_observation_citations.append(citation)
        if len(unique_observation_citations) > 320:
            raise ValueError("diagnostic result exceeds the citation-validation bound")
        failure_statuses = {
            DiagnosticStatus.INVALID_REQUEST,
            DiagnosticStatus.WRONG_RUN,
            DiagnosticStatus.READ_FAILURE,
            DiagnosticStatus.RETRIEVAL_FAILURE,
        }
        if self.status in failure_statuses and not self.issues:
            raise ValueError("failure statuses require a typed issue")
        return self


class CitationValidationRequest(StrictModel):
    schema_version: Literal[1] = 1
    run_id: Identifier
    state_version: int = Field(strict=True, ge=0)
    snapshot_hash: Sha256
    last_sequence: int = Field(strict=True, ge=0)
    observation_citations: tuple[ObservationCitation, ...] = Field(default=(), max_length=320)
    guidance_citations: tuple[KnowledgeCitation, ...] = Field(default=(), max_length=100)

    @classmethod
    def from_diagnostic(cls, result: DiagnosticResult) -> CitationValidationRequest:
        if result.summary is None:
            raise ValueError("a diagnostic result without a summary has no pinned citations")
        observation_citations: list[ObservationCitation] = []
        guidance_citations: list[KnowledgeCitation] = []
        for hypothesis in result.hypotheses:
            for citation in hypothesis.observation_citations:
                if citation not in observation_citations:
                    observation_citations.append(citation)
            for citation in hypothesis.guidance_citations:
                if citation not in guidance_citations:
                    guidance_citations.append(citation)
        if result.retrieval is not None:
            check_chunk_ids = {item.guidance_chunk_id for item in result.suggested_checks}
            for hit in result.retrieval.hits:
                if hit.chunk_id not in check_chunk_ids:
                    continue
                citation = KnowledgeCitation.from_hit(hit)
                if citation not in guidance_citations:
                    guidance_citations.append(citation)
        return cls(
            run_id=result.run_id,
            state_version=result.summary.state_version,
            snapshot_hash=result.summary.snapshot_hash,
            last_sequence=result.summary.last_sequence,
            observation_citations=tuple(observation_citations),
            guidance_citations=tuple(guidance_citations),
        )


class CitationKind(StrEnum):
    SNAPSHOT = "snapshot"
    OBSERVATION = "observation"
    GUIDANCE = "guidance"


class CitationValidationIssue(StrictModel):
    kind: CitationKind
    code: DiagnosticIssueCode
    message: str = Field(min_length=1, max_length=300)
    citation_index: int | None = Field(default=None, strict=True, ge=0)
    reference_id: Identifier | None = None


class CitationValidationStatus(StrEnum):
    VALID = "valid"
    INVALID_REQUEST = "invalid_request"
    WRONG_RUN = "wrong_run"
    READ_FAILURE = "read_failure"
    INVALID = "invalid"


class CitationValidationResult(StrictModel):
    schema_version: Literal[1] = 1
    status: CitationValidationStatus
    valid: bool
    issues: tuple[CitationValidationIssue, ...] = Field(default=(), max_length=421)

    @model_validator(mode="after")
    def validate_result(self) -> Self:
        if self.valid != (self.status is CitationValidationStatus.VALID):
            raise ValueError("valid must match validation status")
        if self.valid and self.issues:
            raise ValueError("valid citations cannot include issues")
        if not self.valid and not self.issues:
            raise ValueError("invalid citations require issues")
        return self
