"""Run-bound, read-only observation and diagnostic services."""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol

from pydantic import TypeAdapter, ValidationError

from shopfloor_pilot.domain.base import Identifier, StrictModel
from shopfloor_pilot.domain.models import Capability, EventType, Observation
from shopfloor_pilot.events import EventEnvelope, ObservationRecordedPayload, SnapshotCheckpoint
from shopfloor_pilot.retrieval import (
    KnowledgeCitation,
    LexicalRetriever,
    RetrievalHit,
    RetrievalRequest,
    RetrievalResult,
    RetrievalStatus,
    tokenize,
)

from .models import (
    MAX_OBSERVATION_SIGNALS,
    BoundaryStatus,
    CitationKind,
    CitationValidationIssue,
    CitationValidationRequest,
    CitationValidationResult,
    CitationValidationStatus,
    DiagnosticHypothesis,
    DiagnosticIssue,
    DiagnosticIssueCode,
    DiagnosticRequest,
    DiagnosticResult,
    DiagnosticRetrievalRequest,
    DiagnosticRetrievalResult,
    DiagnosticStatus,
    EvidenceConflict,
    MissingEvidence,
    MissingEvidenceReason,
    ObservationCitation,
    ObservationEvidence,
    ObservationSummary,
    ObservationSummaryRequest,
    ObservationSummaryResult,
    QualitativeConfidence,
    SignalReading,
    SuggestedCheck,
)

_IDENTIFIER_ADAPTER = TypeAdapter(Identifier)
_MAX_DIAGNOSTIC_QUERY_CHARACTERS = 500
_MAX_DIAGNOSTIC_QUERY_TOKENS = 64


class CheckpointReader(Protocol):
    def __call__(
        self,
        run_id: Identifier,
        state_version: int | None = None,
    ) -> SnapshotCheckpoint: ...


class EventReader(Protocol):
    def __call__(
        self,
        run_id: Identifier,
        after_sequence: int = 0,
    ) -> tuple[EventEnvelope, ...]: ...


@dataclass
class _HypothesisAccumulator:
    machine_id: Identifier
    possible_explanation: str
    hits: list[RetrievalHit] = field(default_factory=list)
    supporting_fields: set[str] = field(default_factory=set)


def _json_model[ModelT: StrictModel](
    model_type: type[ModelT],
    value: ModelT | Mapping[str, object],
) -> ModelT:
    if isinstance(value, model_type):
        value = _raw_model_data(value)
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise TypeError("request must be a typed model or string-keyed mapping")
    payload = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    )
    return model_type.model_validate_json(payload)


def _raw_model_data(model: StrictModel) -> dict[str, object]:
    """Expose forged runtime values to strict JSON validation without coercion."""

    raw = vars(model)
    expected_fields = set(type(model).model_fields)
    if set(raw) != expected_fields:
        raise ValueError("typed request fields do not exactly match its schema")
    return {name: _raw_json_value(raw[name]) for name in type(model).model_fields}


def _raw_json_value(value: object) -> object:
    if isinstance(value, StrictModel):
        return _raw_model_data(value)
    if isinstance(value, tuple | list):
        return [_raw_json_value(item) for item in value]
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise TypeError("nested request mappings require string keys")
        return {key: _raw_json_value(item) for key, item in value.items()}
    return value


def _parse_observation(envelope: EventEnvelope) -> Observation:
    payload = json.dumps(
        envelope.event.payload,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    )
    return ObservationRecordedPayload.model_validate_json(payload).observation


def _invalid_request_issue() -> DiagnosticIssue:
    return DiagnosticIssue(
        code=DiagnosticIssueCode.INVALID_REQUEST,
        message="Request did not match the strict bounded schema.",
    )


def _wrong_run_issue() -> DiagnosticIssue:
    return DiagnosticIssue(
        code=DiagnosticIssueCode.WRONG_RUN,
        message="Request is outside this diagnostic service's authorized run.",
    )


def _read_failure_issue() -> DiagnosticIssue:
    return DiagnosticIssue(
        code=DiagnosticIssueCode.RUN_UNAVAILABLE,
        message="Authorized run evidence could not be read or validated.",
    )


def _unique_strings(values: Sequence[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(values))


class DiagnosticService:
    """Read one authorized run and produce evidence-supported possibilities.

    Only two bound readers are retained.  The service never receives an application
    service, repository, lifecycle facade, simulator, scenario bundle, or mutator.
    """

    __slots__ = ("__get_checkpoint", "__list_events", "__retriever", "__run_id")

    def __init__(
        self,
        *,
        run_id: Identifier,
        get_checkpoint: CheckpointReader,
        list_events: EventReader,
        retriever: LexicalRetriever,
    ) -> None:
        try:
            self.__run_id = _IDENTIFIER_ADAPTER.validate_python(run_id, strict=True)
        except ValidationError as exc:
            raise ValueError("run_id must be a valid Identifier") from exc
        if not callable(get_checkpoint) or not callable(list_events):
            raise TypeError("get_checkpoint and list_events must be callable")
        if not isinstance(retriever, LexicalRetriever):
            raise TypeError("retriever must be a LexicalRetriever")
        self.__get_checkpoint = get_checkpoint
        self.__list_events = list_events
        self.__retriever = retriever

    @property
    def run_id(self) -> Identifier:
        return self.__run_id

    def summarize_observations(
        self,
        request: ObservationSummaryRequest | Mapping[str, object],
    ) -> ObservationSummaryResult:
        """Return bounded observation evidence from one immutable event prefix."""

        try:
            parsed = _json_model(ObservationSummaryRequest, request)
        except Exception:
            return ObservationSummaryResult(
                status=BoundaryStatus.INVALID_REQUEST,
                issues=(_invalid_request_issue(),),
            )
        if parsed.run_id != self.__run_id:
            return ObservationSummaryResult(
                status=BoundaryStatus.WRONG_RUN,
                issues=(_wrong_run_issue(),),
            )
        try:
            result, _ = self._summarize_validated(parsed)
            return result
        except Exception:
            # A reader failure or inconsistent persisted prefix is deliberately
            # collapsed so another run's identity/content cannot cross the boundary.
            return ObservationSummaryResult(
                status=BoundaryStatus.READ_FAILURE,
                issues=(_read_failure_issue(),),
            )

    def retrieve(
        self,
        request: DiagnosticRetrievalRequest | Mapping[str, object],
    ) -> DiagnosticRetrievalResult:
        """Delegate an authorized, bounded request to the immutable lexical index."""

        try:
            parsed = _json_model(DiagnosticRetrievalRequest, request)
        except Exception:
            return DiagnosticRetrievalResult(
                status=BoundaryStatus.INVALID_REQUEST,
                issues=(_invalid_request_issue(),),
            )
        if parsed.run_id != self.__run_id:
            return DiagnosticRetrievalResult(
                status=BoundaryStatus.WRONG_RUN,
                issues=(_wrong_run_issue(),),
            )
        try:
            result = self.__retriever.search(
                RetrievalRequest(
                    query=parsed.query,
                    top_k=parsed.top_k,
                    capability_filters=parsed.capability_filters,
                )
            )
        except Exception:
            return DiagnosticRetrievalResult(
                status=BoundaryStatus.REJECTED,
                issues=(
                    DiagnosticIssue(
                        code=DiagnosticIssueCode.RETRIEVAL_INVALID,
                        message="Local retrieval could not produce a validated result.",
                    ),
                ),
            )
        if result.status is RetrievalStatus.INVALID_REQUEST:
            return DiagnosticRetrievalResult(
                status=BoundaryStatus.INVALID_REQUEST,
                retrieval=result,
                issues=(
                    DiagnosticIssue(
                        code=DiagnosticIssueCode.RETRIEVAL_INVALID,
                        message="Retrieval rejected the bounded query or filter.",
                    ),
                ),
            )
        return DiagnosticRetrievalResult(status=BoundaryStatus.SUCCESS, retrieval=result)

    def generate_hypotheses(
        self,
        request: DiagnosticRequest | Mapping[str, object],
    ) -> DiagnosticResult:
        """Generate uncalibrated possibilities, never a root-cause determination."""

        try:
            parsed = _json_model(DiagnosticRequest, request)
        except Exception:
            return DiagnosticResult(
                status=DiagnosticStatus.INVALID_REQUEST,
                run_id=self.__run_id,
                issues=(_invalid_request_issue(),),
            )
        if parsed.run_id != self.__run_id:
            return DiagnosticResult(
                status=DiagnosticStatus.WRONG_RUN,
                run_id=parsed.run_id,
                issues=(_wrong_run_issue(),),
            )

        summary_request = ObservationSummaryRequest(
            run_id=parsed.run_id,
            event_ids=parsed.event_ids,
            machine_ids=parsed.machine_ids,
            after_sequence=parsed.after_sequence,
            limit=parsed.observation_limit,
        )
        try:
            summary_result, machine_capabilities = self._summarize_validated(summary_request)
        except Exception:
            summary_result = ObservationSummaryResult(
                status=BoundaryStatus.READ_FAILURE,
                issues=(_read_failure_issue(),),
            )
            machine_capabilities = {}
        if summary_result.status is not BoundaryStatus.SUCCESS:
            status = {
                BoundaryStatus.WRONG_RUN: DiagnosticStatus.WRONG_RUN,
                BoundaryStatus.READ_FAILURE: DiagnosticStatus.READ_FAILURE,
            }.get(summary_result.status, DiagnosticStatus.INVALID_REQUEST)
            return DiagnosticResult(
                status=status,
                run_id=parsed.run_id,
                issues=summary_result.issues,
            )
        assert summary_result.summary is not None
        summary = summary_result.summary

        query = self._diagnostic_query(parsed.query, summary)
        try:
            retrieval = self.__retriever.search(
                RetrievalRequest(
                    query=query,
                    top_k=parsed.top_k,
                    capability_filters=parsed.capability_filters,
                )
            )
        except Exception:
            return DiagnosticResult(
                status=DiagnosticStatus.RETRIEVAL_FAILURE,
                run_id=parsed.run_id,
                summary=summary,
                issues=(
                    DiagnosticIssue(
                        code=DiagnosticIssueCode.RETRIEVAL_INVALID,
                        message="Local retrieval could not produce a validated result.",
                    ),
                ),
            )
        if retrieval.status is RetrievalStatus.INVALID_REQUEST:
            return DiagnosticResult(
                status=DiagnosticStatus.INVALID_REQUEST,
                run_id=parsed.run_id,
                summary=summary,
                retrieval=retrieval,
                issues=(
                    DiagnosticIssue(
                        code=DiagnosticIssueCode.RETRIEVAL_INVALID,
                        message="The derived lexical query did not satisfy retrieval bounds.",
                    ),
                ),
            )

        try:
            return self._build_diagnostic(
                parsed.run_id,
                summary,
                retrieval,
                machine_capabilities,
            )
        except Exception:
            return DiagnosticResult(
                status=DiagnosticStatus.RETRIEVAL_FAILURE,
                run_id=parsed.run_id,
                summary=summary,
                retrieval=retrieval,
                issues=(
                    DiagnosticIssue(
                        code=DiagnosticIssueCode.RETRIEVAL_INVALID,
                        message="Retrieved evidence could not form a validated diagnostic result.",
                    ),
                ),
            )

    def validate_citations(
        self,
        request: CitationValidationRequest | Mapping[str, object],
    ) -> CitationValidationResult:
        """Resolve exact observation and current-corpus citations without writes."""

        try:
            parsed = _json_model(CitationValidationRequest, request)
        except Exception:
            return CitationValidationResult(
                status=CitationValidationStatus.INVALID_REQUEST,
                valid=False,
                issues=(
                    CitationValidationIssue(
                        kind=CitationKind.SNAPSHOT,
                        code=DiagnosticIssueCode.INVALID_REQUEST,
                        message="Citation request did not match the strict bounded schema.",
                    ),
                ),
            )
        if parsed.run_id != self.__run_id:
            return CitationValidationResult(
                status=CitationValidationStatus.WRONG_RUN,
                valid=False,
                issues=(
                    CitationValidationIssue(
                        kind=CitationKind.SNAPSHOT,
                        code=DiagnosticIssueCode.WRONG_RUN,
                        message="Citation request is outside the authorized run.",
                    ),
                ),
            )
        try:
            checkpoint, prefix = self._read_pinned_prefix(state_version=parsed.state_version)
        except Exception:
            return CitationValidationResult(
                status=CitationValidationStatus.READ_FAILURE,
                valid=False,
                issues=(
                    CitationValidationIssue(
                        kind=CitationKind.SNAPSHOT,
                        code=DiagnosticIssueCode.RUN_UNAVAILABLE,
                        message="Authorized citation evidence could not be read or validated.",
                    ),
                ),
            )

        issues: list[CitationValidationIssue] = []
        if (
            checkpoint.snapshot_hash != parsed.snapshot_hash
            or checkpoint.last_sequence != parsed.last_sequence
        ):
            issues.append(
                CitationValidationIssue(
                    kind=CitationKind.SNAPSHOT,
                    code=DiagnosticIssueCode.STALE_SNAPSHOT,
                    message="Citation checkpoint identity does not match persisted authority.",
                )
            )

        by_event_id = {item.event.event_id: item for item in prefix}
        for index, citation in enumerate(parsed.observation_citations):
            envelope = by_event_id.get(citation.event_id)
            if not self._observation_citation_matches(envelope, citation):
                issues.append(
                    CitationValidationIssue(
                        kind=CitationKind.OBSERVATION,
                        code=DiagnosticIssueCode.OBSERVATION_CITATION_INVALID,
                        message=(
                            "Observation citation is missing, stale, or does not match its event."
                        ),
                        citation_index=index,
                        reference_id=citation.event_id,
                    )
                )
        for index, citation in enumerate(parsed.guidance_citations):
            try:
                valid = self.__retriever.resolve_citation(citation)
            except Exception:
                valid = False
            if not valid:
                issues.append(
                    CitationValidationIssue(
                        kind=CitationKind.GUIDANCE,
                        code=DiagnosticIssueCode.GUIDANCE_CITATION_INVALID,
                        message=(
                            "Guidance citation is missing, stale, or does not match the corpus."
                        ),
                        citation_index=index,
                        reference_id=citation.chunk_id,
                    )
                )

        if issues:
            return CitationValidationResult(
                status=CitationValidationStatus.INVALID,
                valid=False,
                issues=tuple(issues),
            )
        return CitationValidationResult(status=CitationValidationStatus.VALID, valid=True)

    def _summarize_validated(
        self,
        request: ObservationSummaryRequest,
    ) -> tuple[ObservationSummaryResult, dict[str, frozenset[Capability]]]:
        checkpoint, prefix = self._read_pinned_prefix()
        snapshot = checkpoint.snapshot
        machine_ids = {item.machine_id for item in snapshot.machines}
        missing_machine = next(
            (item for item in request.machine_ids if item not in machine_ids),
            None,
        )
        if missing_machine is not None:
            return (
                ObservationSummaryResult(
                    status=BoundaryStatus.REJECTED,
                    issues=(
                        DiagnosticIssue(
                            code=DiagnosticIssueCode.MACHINE_NOT_FOUND,
                            message="Requested machine is not present in the authorized run.",
                            reference_id=missing_machine,
                        ),
                    ),
                ),
                {},
            )

        by_event_id = {item.event.event_id: item for item in prefix}
        if request.event_ids:
            for event_id in request.event_ids:
                envelope = by_event_id.get(event_id)
                if envelope is None:
                    return (
                        ObservationSummaryResult(
                            status=BoundaryStatus.REJECTED,
                            issues=(
                                DiagnosticIssue(
                                    code=DiagnosticIssueCode.EVENT_NOT_FOUND,
                                    message=(
                                        "Requested event was not found in the authorized run "
                                        "prefix."
                                    ),
                                    reference_id=event_id,
                                ),
                            ),
                        ),
                        {},
                    )
                if envelope.event.event_type is not EventType.OBSERVATION_RECORDED:
                    return (
                        ObservationSummaryResult(
                            status=BoundaryStatus.REJECTED,
                            issues=(
                                DiagnosticIssue(
                                    code=DiagnosticIssueCode.NOT_OBSERVATION_EVENT,
                                    message="Requested event is not an observation event.",
                                    reference_id=event_id,
                                ),
                            ),
                        ),
                        {},
                    )
            requested = set(request.event_ids)
            candidates = tuple(item for item in prefix if item.event.event_id in requested)
        else:
            candidates = tuple(
                item for item in prefix if item.event.event_type is EventType.OBSERVATION_RECORDED
            )

        evidence: list[ObservationEvidence] = []
        machine_filter = set(request.machine_ids)
        for envelope in candidates:
            if envelope.event.sequence <= request.after_sequence:
                if request.event_ids:
                    return self._filter_mismatch(envelope.event.event_id), {}
                continue
            try:
                observation = _parse_observation(envelope)
            except (TypeError, ValueError, ValidationError, OverflowError) as exc:
                raise ValueError("invalid persisted observation payload") from exc
            if (
                observation.run_id != self.__run_id
                or observation.observed_at_seconds != envelope.event.simulated_time_seconds
                or observation.machine_id not in machine_ids
            ):
                raise ValueError("persisted observation does not match its run event")
            if machine_filter and observation.machine_id not in machine_filter:
                if request.event_ids:
                    return self._filter_mismatch(envelope.event.event_id), {}
                continue
            if len(observation.signals) > MAX_OBSERVATION_SIGNALS:
                return (
                    ObservationSummaryResult(
                        status=BoundaryStatus.REJECTED,
                        issues=(
                            DiagnosticIssue(
                                code=DiagnosticIssueCode.INVALID_EVENT_DATA,
                                message=(
                                    "Persisted observation exceeds the diagnostic signal bound."
                                ),
                                reference_id=envelope.event.event_id,
                            ),
                        ),
                    ),
                    {},
                )
            evidence.append(
                ObservationEvidence(
                    event_id=envelope.event.event_id,
                    sequence=envelope.event.sequence,
                    resulting_state_version=envelope.resulting_state_version,
                    observation=observation,
                )
            )

        truncated = len(evidence) > request.limit
        selected = tuple(evidence[: request.limit])
        summary = ObservationSummary(
            run_id=self.__run_id,
            state_version=snapshot.state_version,
            snapshot_hash=checkpoint.snapshot_hash,
            last_sequence=checkpoint.last_sequence,
            simulated_time_seconds=snapshot.simulated_time_seconds,
            observations=selected,
            truncated=truncated,
            next_after_sequence=selected[-1].sequence if truncated else None,
        )
        selected_machine_ids = {item.observation.machine_id for item in selected} or set(
            request.machine_ids
        )
        machine_capabilities = {
            machine.machine_id: frozenset(machine.capabilities)
            for machine in snapshot.machines
            if machine.machine_id in selected_machine_ids
        }
        return (
            ObservationSummaryResult(status=BoundaryStatus.SUCCESS, summary=summary),
            machine_capabilities,
        )

    @staticmethod
    def _filter_mismatch(event_id: str) -> ObservationSummaryResult:
        return ObservationSummaryResult(
            status=BoundaryStatus.REJECTED,
            issues=(
                DiagnosticIssue(
                    code=DiagnosticIssueCode.FILTER_MISMATCH,
                    message="Explicit event does not satisfy the requested bounded filters.",
                    reference_id=event_id,
                ),
            ),
        )

    def _read_pinned_prefix(
        self,
        *,
        state_version: int | None = None,
    ) -> tuple[SnapshotCheckpoint, tuple[EventEnvelope, ...]]:
        if state_version is None:
            checkpoint = self.__get_checkpoint(self.__run_id)
        else:
            checkpoint = self.__get_checkpoint(self.__run_id, state_version=state_version)
        if not isinstance(checkpoint, SnapshotCheckpoint):
            raise TypeError("checkpoint reader returned an unsupported value")
        if checkpoint.snapshot.run_id != self.__run_id:
            raise ValueError("checkpoint reader crossed the run boundary")
        if state_version is not None and checkpoint.snapshot.state_version != state_version:
            raise ValueError("checkpoint reader returned a different state version")

        raw_events = self.__list_events(self.__run_id, after_sequence=0)
        events = tuple(raw_events)
        if any(not isinstance(item, EventEnvelope) for item in events):
            raise TypeError("event reader returned an unsupported value")
        prefix = tuple(item for item in events if item.event.sequence <= checkpoint.last_sequence)
        if tuple(item.event.event_id for item in prefix) != checkpoint.seen_event_ids:
            raise ValueError("event reader does not match the pinned checkpoint prefix")
        if tuple(item.event.sequence for item in prefix) != tuple(
            range(1, checkpoint.last_sequence + 1)
        ):
            raise ValueError("event reader returned a non-contiguous pinned prefix")
        if any(item.event.run_id != self.__run_id for item in prefix):
            raise ValueError("event reader crossed the run boundary")
        return checkpoint, prefix

    @staticmethod
    def _diagnostic_query(explicit_query: str, summary: ObservationSummary) -> str:
        explicit = explicit_query.strip()
        if explicit:
            return explicit

        parts: list[str] = []

        public_parts: list[str] = []
        for evidence in summary.observations:
            public_parts.extend(signal.name for signal in evidence.observation.signals)
        public_parts.extend(
            evidence.observation.summary.strip() for evidence in summary.observations
        )

        for part in _unique_strings(tuple(item for item in public_parts if item)):
            candidate = " ".join((*parts, part))
            if len(candidate) > _MAX_DIAGNOSTIC_QUERY_CHARACTERS:
                continue
            if len(tokenize(candidate)) > _MAX_DIAGNOSTIC_QUERY_TOKENS:
                continue
            parts.append(part)
        return " ".join(parts)

    def _build_diagnostic(
        self,
        run_id: str,
        summary: ObservationSummary,
        retrieval: RetrievalResult,
        capabilities_by_machine: Mapping[str, frozenset[Capability]],
    ) -> DiagnosticResult:
        conflicts = self._detect_conflicts(summary.observations)
        missing: list[MissingEvidence] = []
        if not summary.observations:
            missing.append(
                MissingEvidence(
                    reason=MissingEvidenceReason.NO_OBSERVATIONS,
                    description="No persisted observation event satisfied the requested scope.",
                )
            )
        if summary.truncated:
            missing.append(
                MissingEvidence(
                    reason=MissingEvidenceReason.OBSERVATIONS_TRUNCATED,
                    description=(
                        "Additional persisted observations exist beyond the requested limit."
                    ),
                )
            )
        if retrieval.status is RetrievalStatus.NO_EVIDENCE:
            missing.append(
                MissingEvidence(
                    reason=MissingEvidenceReason.NO_RETRIEVED_GUIDANCE,
                    description="The bounded lexical search found no guidance above its threshold.",
                )
            )

        evidence_by_machine: dict[str, list[ObservationEvidence]] = defaultdict(list)
        fields_by_machine: dict[str, set[str]] = defaultdict(set)
        for evidence in summary.observations:
            machine_id = evidence.observation.machine_id
            evidence_by_machine[machine_id].append(evidence)
            fields_by_machine[machine_id].update(
                signal.name for signal in evidence.observation.signals
            )

        current_hits: list[RetrievalHit] = []
        for hit in retrieval.hits:
            try:
                current = self.__retriever.resolve_citation(KnowledgeCitation.from_hit(hit))
            except Exception:
                current = False
            if current:
                current_hits.append(hit)

        capability_hits: list[RetrievalHit] = []
        applicable_machines_by_chunk: dict[str, tuple[str, ...]] = {}
        for hit in current_hits:
            applicable_capabilities = set(hit.applicable_capabilities)
            applicable_machines = tuple(
                machine_id
                for machine_id in sorted(capabilities_by_machine)
                if capabilities_by_machine[machine_id] & applicable_capabilities
            )
            if capabilities_by_machine and not applicable_machines:
                continue
            capability_hits.append(hit)
            applicable_machines_by_chunk[hit.chunk_id] = applicable_machines

        machines_with_guidance = {
            machine_id
            for machine_ids in applicable_machines_by_chunk.values()
            for machine_id in machine_ids
        }
        if current_hits:
            for machine_id in sorted(set(capabilities_by_machine) - machines_with_guidance):
                missing.append(
                    MissingEvidence(
                        reason=MissingEvidenceReason.NO_APPLICABLE_GUIDANCE,
                        description=(
                            "Retrieved guidance does not apply to this machine's public "
                            "capabilities."
                        ),
                        machine_id=machine_id,
                    )
                )

        advisory_hits = (
            capability_hits
            if summary.observations
            else [hit for hit in capability_hits if hit.possible_explanation is None]
        )

        accumulators: dict[tuple[str, str], _HypothesisAccumulator] = {}
        for hit in capability_hits:
            if hit.possible_explanation is None:
                continue
            guidance_fields = set(hit.observable_signal_fields)
            applicable_machines = set(applicable_machines_by_chunk[hit.chunk_id])
            for machine_id in sorted(fields_by_machine):
                if machine_id not in applicable_machines:
                    continue
                overlap = fields_by_machine[machine_id] & guidance_fields
                if not overlap:
                    continue
                key = (machine_id, hit.possible_explanation)
                accumulator = accumulators.setdefault(
                    key,
                    _HypothesisAccumulator(
                        machine_id=machine_id,
                        possible_explanation=hit.possible_explanation,
                    ),
                )
                accumulator.hits.append(hit)
                accumulator.supporting_fields.update(overlap)

        hypotheses: list[DiagnosticHypothesis] = []
        for accumulator in accumulators.values():
            hypothesis = self._hypothesis_from_accumulator(
                accumulator,
                evidence_by_machine[accumulator.machine_id],
                conflicts,
            )
            hypotheses.append(hypothesis)

        retained_hypotheses = self._select_hypotheses(hypotheses)
        for hypothesis in retained_hypotheses:
            missing.extend(hypothesis.missing_evidence)

        diagnostic_hits = capability_hits if summary.observations else advisory_hits
        if diagnostic_hits and not retained_hypotheses:
            explanation_hits = tuple(
                hit for hit in diagnostic_hits if hit.possible_explanation is not None
            )
            if not explanation_hits:
                missing.append(
                    MissingEvidence(
                        reason=MissingEvidenceReason.NO_SUPPORTED_EXPLANATION,
                        description=(
                            "Retrieved guidance describes observations but does not state a "
                            "supported possible explanation."
                        ),
                    )
                )
            else:
                expected_fields = _unique_strings(
                    tuple(
                        field for hit in explanation_hits for field in hit.observable_signal_fields
                    )
                )
                if expected_fields:
                    description = (
                        "Retrieved guidance expected observable fields that were absent from "
                        "the selected persisted observations: " + ", ".join(expected_fields)
                    )
                else:
                    description = (
                        "Retrieved guidance did not define an observable signal field shared "
                        "with the selected observations."
                    )
                missing.append(
                    MissingEvidence(
                        reason=MissingEvidenceReason.NO_SIGNAL_OVERLAP,
                        description=description[:500],
                    )
                )

        missing.extend(
            self._missing_fields_from_guidance(
                advisory_hits,
                fields_by_machine,
                applicable_machines_by_chunk,
            )
        )

        suggested_checks: list[SuggestedCheck] = []
        seen_checks: set[tuple[str, str]] = set()
        for hit in advisory_hits:
            for check in hit.suggested_checks:
                key = (hit.chunk_id, check)
                if key in seen_checks:
                    continue
                seen_checks.add(key)
                suggested_checks.append(SuggestedCheck(guidance_chunk_id=hit.chunk_id, text=check))

        distinct_explanations = {item.possible_explanation for item in retained_hypotheses}
        ambiguous = len(distinct_explanations) > 1
        if conflicts:
            status = DiagnosticStatus.CONFLICTING_EVIDENCE
        elif not retained_hypotheses:
            status = DiagnosticStatus.INSUFFICIENT_EVIDENCE
        elif ambiguous:
            status = DiagnosticStatus.AMBIGUOUS
        else:
            status = DiagnosticStatus.HYPOTHESES

        return DiagnosticResult(
            status=status,
            run_id=run_id,
            summary=summary,
            retrieval=retrieval,
            hypotheses=retained_hypotheses,
            conflicts=conflicts[:50],
            missing_evidence=tuple(self._unique_missing(missing)[:100]),
            suggested_checks=tuple(suggested_checks[:100]),
            ambiguous=ambiguous,
        )

    @staticmethod
    def _select_hypotheses(
        hypotheses: Sequence[DiagnosticHypothesis],
    ) -> tuple[DiagnosticHypothesis, ...]:
        """Retain one representative per explanation before filling the bound."""

        selected_indexes: list[int] = []
        seen_explanations: set[str] = set()
        for index, hypothesis in enumerate(hypotheses):
            if hypothesis.possible_explanation in seen_explanations:
                continue
            seen_explanations.add(hypothesis.possible_explanation)
            selected_indexes.append(index)
        for index in range(len(hypotheses)):
            if len(selected_indexes) >= 20:
                break
            if index not in selected_indexes:
                selected_indexes.append(index)
        return tuple(hypotheses[index] for index in selected_indexes[:20])

    @staticmethod
    def _missing_fields_from_guidance(
        hits: Sequence[RetrievalHit],
        fields_by_machine: Mapping[str, set[str]],
        applicable_machines_by_chunk: Mapping[str, tuple[str, ...]],
    ) -> tuple[MissingEvidence, ...]:
        missing: list[MissingEvidence] = []
        for hit in hits:
            guidance_fields = set(hit.observable_signal_fields)
            overlapping_machines = tuple(
                machine_id
                for machine_id in applicable_machines_by_chunk[hit.chunk_id]
                if fields_by_machine[machine_id] & guidance_fields
            )
            if overlapping_machines:
                targets: tuple[str | None, ...] = overlapping_machines
            elif applicable_machines_by_chunk[hit.chunk_id]:
                targets = applicable_machines_by_chunk[hit.chunk_id]
            else:
                targets = (None,)
            for signal_field in hit.additional_signal_fields:
                for machine_id in targets:
                    if machine_id is not None and signal_field in fields_by_machine[machine_id]:
                        continue
                    missing.append(
                        MissingEvidence(
                            reason=MissingEvidenceReason.ADDITIONAL_SIGNAL_REQUIRED,
                            description=(
                                "Retrieved guidance identifies an additional observable field "
                                "that has not been recorded"
                                + (
                                    f" for this machine: {signal_field}."
                                    if machine_id is not None
                                    else f": {signal_field}."
                                )
                            ),
                            machine_id=machine_id,
                            signal_field=signal_field,
                            guidance_chunk_id=hit.chunk_id,
                        )
                    )
        return tuple(missing)

    @staticmethod
    def _unique_missing(values: Sequence[MissingEvidence]) -> list[MissingEvidence]:
        unique: list[MissingEvidence] = []
        for value in values:
            if value not in unique:
                unique.append(value)
        return unique

    @staticmethod
    def _hypothesis_from_accumulator(
        accumulator: _HypothesisAccumulator,
        evidence: Sequence[ObservationEvidence],
        conflicts: Sequence[EvidenceConflict],
    ) -> DiagnosticHypothesis:
        supporting_fields = tuple(sorted(accumulator.supporting_fields))[:16]
        uncited_fields = set(supporting_fields)
        citations: list[ObservationCitation] = []
        for item in evidence:
            signals = tuple(
                signal for signal in item.observation.signals if signal.name in uncited_fields
            )
            if not signals:
                continue
            citations.append(
                ObservationCitation(
                    event_id=item.event_id,
                    observation_id=item.observation.observation_id,
                    machine_id=item.observation.machine_id,
                    observed_at_seconds=item.observation.observed_at_seconds,
                    signal_fields=tuple(signal.name for signal in signals),
                    signals=signals,
                )
            )
            uncited_fields.difference_update(signal.name for signal in signals)
            if not uncited_fields:
                break

        guidance_citations: list[KnowledgeCitation] = []
        seen_guidance: set[str] = set()
        limitations: list[str] = []
        missing: list[MissingEvidence] = []
        observed_fields = {signal.name for item in evidence for signal in item.observation.signals}
        for hit in accumulator.hits:
            if hit.chunk_id not in seen_guidance:
                seen_guidance.add(hit.chunk_id)
                guidance_citations.append(KnowledgeCitation.from_hit(hit))
            limitations.extend(hit.limitations)
            for signal_field in hit.additional_signal_fields:
                if signal_field in observed_fields:
                    continue
                missing.append(
                    MissingEvidence(
                        reason=MissingEvidenceReason.ADDITIONAL_SIGNAL_REQUIRED,
                        description=(
                            "Retrieved guidance identifies an additional observable field "
                            f"that has not been recorded for this machine: {signal_field}."
                        ),
                        machine_id=accumulator.machine_id,
                        signal_field=signal_field,
                        guidance_chunk_id=hit.chunk_id,
                    )
                )

        relevant_conflict = any(
            item.machine_id == accumulator.machine_id
            and item.signal_field in accumulator.supporting_fields
            for item in conflicts
        )
        confidence = (
            QualitativeConfidence.MODERATE
            if len(supporting_fields) >= 2 and not relevant_conflict and not missing
            else QualitativeConfidence.LIMITED
        )
        return DiagnosticHypothesis(
            machine_id=accumulator.machine_id,
            possible_explanation=accumulator.possible_explanation,
            confidence=confidence,
            supporting_signal_fields=supporting_fields,
            observation_citations=tuple(citations),
            guidance_citations=tuple(guidance_citations),
            missing_evidence=tuple(missing[:50]),
            limitations=_unique_strings(tuple(limitations))[:20],
        )

    @staticmethod
    def _detect_conflicts(
        evidence: Sequence[ObservationEvidence],
    ) -> tuple[EvidenceConflict, ...]:
        grouped: dict[
            tuple[str, int, str],
            dict[tuple[int, str], list[str]],
        ] = defaultdict(lambda: defaultdict(list))
        for item in evidence:
            observation = item.observation
            for signal in observation.signals:
                grouped[
                    (
                        observation.machine_id,
                        observation.observed_at_seconds,
                        signal.name,
                    )
                ][(signal.value, signal.unit)].append(item.event_id)

        conflicts: list[EvidenceConflict] = []
        for (machine_id, observed_at, signal_field), readings in sorted(grouped.items()):
            if len(readings) < 2:
                continue
            values = tuple(
                SignalReading(
                    value=value,
                    unit=unit,
                    event_ids=tuple(event_ids),
                )
                for (value, unit), event_ids in sorted(
                    readings.items(),
                    key=lambda item: (item[0][0], item[0][1]),
                )
            )
            conflicts.append(
                EvidenceConflict(
                    machine_id=machine_id,
                    observed_at_seconds=observed_at,
                    signal_field=signal_field,
                    readings=values,
                )
            )
        return tuple(conflicts)

    @staticmethod
    def _observation_citation_matches(
        envelope: EventEnvelope | None,
        citation: ObservationCitation,
    ) -> bool:
        if envelope is None or envelope.event.event_type is not EventType.OBSERVATION_RECORDED:
            return False
        try:
            observation = _parse_observation(envelope)
        except (TypeError, ValueError, ValidationError, OverflowError):
            return False
        if len(observation.signals) > MAX_OBSERVATION_SIGNALS:
            return False
        if (
            observation.run_id != envelope.event.run_id
            or observation.observed_at_seconds != envelope.event.simulated_time_seconds
            or observation.observation_id != citation.observation_id
            or observation.machine_id != citation.machine_id
            or observation.observed_at_seconds != citation.observed_at_seconds
        ):
            return False
        actual = {item.name: item for item in observation.signals}
        return all(actual.get(item.name) == item for item in citation.signals)
