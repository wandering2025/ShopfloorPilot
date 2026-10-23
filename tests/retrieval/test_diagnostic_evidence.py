"""S08 evidence grounding, ambiguity, conflict, and citation acceptance."""

from __future__ import annotations

from collections.abc import Callable

from shopfloor_pilot.diagnostics import (
    CitationKind,
    CitationValidationRequest,
    CitationValidationStatus,
    DiagnosticRequest,
    DiagnosticResult,
    DiagnosticService,
    DiagnosticStatus,
    MissingEvidenceReason,
    QualitativeConfidence,
)
from shopfloor_pilot.domain.models import Observation, TelemetryValue
from shopfloor_pilot.retrieval import RetrievalStatus


def _observation(
    *,
    run_id: str,
    observation_id: str,
    signals: tuple[TelemetryValue, ...],
    summary: str,
    at_seconds: int = 2_700,
) -> Observation:
    return Observation(
        schema_version=1,
        observation_id=observation_id,
        run_id=run_id,
        machine_id="CNC02",
        observed_at_seconds=at_seconds,
        signals=signals,
        summary=summary,
    )


def _temperature_vibration_observation(run_id: str) -> Observation:
    return _observation(
        run_id=run_id,
        observation_id="observation-s08-temperature-vibration",
        signals=(
            TelemetryValue(
                name="temperature_millicelsius",
                value=71_008,
                unit="mdegC",
            ),
            TelemetryValue(
                name="vibration_micrometers_per_second",
                value=8_877,
                unit="um_per_s",
            ),
        ),
        summary=(
            "Synthetic CNC02 observation with concurrent elevated temperature and vibration alarms."
        ),
    )


def _validated_citations(
    service: DiagnosticService,
    result: DiagnosticResult,
) -> CitationValidationRequest:
    request = CitationValidationRequest.from_diagnostic(result)
    validation = service.validate_citations(request)
    assert validation.status is CitationValidationStatus.VALID
    assert validation.valid
    return request


def test_a04_ambiguous_hypotheses_have_two_exact_citation_types_and_tampering_fails(
    diagnostic_harness_factory: Callable[..., object],
) -> None:
    harness = diagnostic_harness_factory()
    harness.persist((_temperature_vibration_observation(harness.run_id),))

    result = harness.service.generate_hypotheses(DiagnosticRequest(run_id=harness.run_id))

    assert result.status is DiagnosticStatus.AMBIGUOUS
    assert result.ambiguous
    assert len({item.possible_explanation for item in result.hypotheses}) >= 2
    assert result.retrieval is not None
    assert result.retrieval.status is RetrievalStatus.MATCHES
    assert not result.confidence_is_calibrated
    assert not result.confirmed_root_cause
    assert not result.maintenance_completed
    for hypothesis in result.hypotheses:
        assert hypothesis.observation_citations
        assert hypothesis.guidance_citations
        assert hypothesis.confidence_basis == "uncalibrated_rule_derived"
        assert all(
            citation.event_id in {item.event_id for item in result.summary.observations}
            for citation in hypothesis.observation_citations
        )
        assert all(
            citation.chunk_id in {item.chunk_id for item in result.retrieval.hits}
            for citation in hypothesis.guidance_citations
        )

    request = _validated_citations(harness.service, result)

    invented_observation = request.observation_citations[0].model_copy(
        update={"event_id": "event-invented"}
    )
    stale_guidance = request.guidance_citations[0].model_copy(
        update={"document_version": "v-stale"}
    )
    tampered = request.model_copy(
        update={
            "snapshot_hash": "f" * 64,
            "observation_citations": (
                invented_observation,
                *request.observation_citations[1:],
            ),
            "guidance_citations": (
                stale_guidance,
                *request.guidance_citations[1:],
            ),
        }
    )
    invalid = harness.service.validate_citations(tampered)
    assert invalid.status is CitationValidationStatus.INVALID
    assert not invalid.valid
    assert {item.kind for item in invalid.issues} == {
        CitationKind.SNAPSHOT,
        CitationKind.OBSERVATION,
        CitationKind.GUIDANCE,
    }


def test_a05_incomplete_observation_stays_limited_and_requests_specific_checks(
    diagnostic_harness_factory: Callable[..., object],
) -> None:
    harness = diagnostic_harness_factory(run_id="run-s08-incomplete")
    observation = _observation(
        run_id=harness.run_id,
        observation_id="observation-s08-incomplete",
        signals=(
            TelemetryValue(
                name="vibration_micrometers_per_second",
                value=8_400,
                unit="um_per_s",
            ),
        ),
        summary="Synthetic vibration alarm observed during cutting.",
    )
    harness.persist((observation,))

    result = harness.service.generate_hypotheses(
        DiagnosticRequest(
            run_id=harness.run_id,
            query="vibration alarm tool holder seating debris runout cutting load",
        )
    )

    assert result.status in {DiagnosticStatus.HYPOTHESES, DiagnosticStatus.AMBIGUOUS}
    assert result.hypotheses
    assert all(item.confidence is QualitativeConfidence.LIMITED for item in result.hypotheses)
    assert any(
        item.reason is MissingEvidenceReason.ADDITIONAL_SIGNAL_REQUIRED
        for item in result.missing_evidence
    )
    assert result.suggested_checks
    actual_signals = {signal.name for signal in observation.signals}
    assert all(
        set(citation.signal_fields) <= actual_signals
        for hypothesis in result.hypotheses
        for citation in hypothesis.observation_citations
    )
    assert not result.confirmed_root_cause
    assert not result.maintenance_completed
    _validated_citations(harness.service, result)


def test_a05_conflicting_readings_are_explicit_and_never_resolved_by_invention(
    diagnostic_harness_factory: Callable[..., object],
) -> None:
    harness = diagnostic_harness_factory(run_id="run-s08-conflict")
    observations = tuple(
        _observation(
            run_id=harness.run_id,
            observation_id=f"observation-s08-conflict-{index}",
            signals=(
                TelemetryValue(
                    name="vibration_micrometers_per_second",
                    value=value,
                    unit="um_per_s",
                ),
            ),
            summary="Synthetic conflicting vibration reading at one timestamp.",
        )
        for index, value in enumerate((4_200, 8_800), start=1)
    )
    mutation = harness.persist(observations)

    result = harness.service.generate_hypotheses(
        DiagnosticRequest(
            run_id=harness.run_id,
            query="same timestamp conflicting vibration readings rotating component",
        )
    )

    assert result.status is DiagnosticStatus.CONFLICTING_EVIDENCE
    assert len(result.conflicts) == 1
    conflict = result.conflicts[0]
    assert conflict.signal_field == "vibration_micrometers_per_second"
    assert {(item.value, item.unit) for item in conflict.readings} == {
        (4_200, "um_per_s"),
        (8_800, "um_per_s"),
    }
    persisted_ids = {
        envelope.event.event_id
        for envelope in mutation.events
        if envelope.event.event_type.value == "observation_recorded"
    }
    assert {
        event_id for reading in conflict.readings for event_id in reading.event_ids
    } == persisted_ids
    assert all(item.confidence is QualitativeConfidence.LIMITED for item in result.hypotheses)
    assert not result.confirmed_root_cause
    _validated_citations(harness.service, result)


def test_a05_machine_capabilities_gate_hypotheses_and_checks(
    diagnostic_harness_factory: Callable[..., object],
) -> None:
    harness = diagnostic_harness_factory(run_id="run-s08-capability-gate")
    inspection_observation = Observation(
        schema_version=1,
        observation_id="observation-s08-qc-vibration",
        run_id=harness.run_id,
        machine_id="QC01",
        observed_at_seconds=2_700,
        signals=(
            TelemetryValue(
                name="vibration_micrometers_per_second",
                value=8_877,
                unit="um_per_s",
            ),
        ),
        summary="Synthetic inspection-resource vibration observation.",
    )
    harness.persist((inspection_observation,))

    result = harness.service.generate_hypotheses(
        DiagnosticRequest(
            run_id=harness.run_id,
            query="tool holder seating debris runout asymmetric cutting load",
            top_k=1,
        )
    )

    assert result.retrieval is not None
    assert tuple(item.chunk_id for item in result.retrieval.hits) == ("chunk-tool-holder-check",)
    assert result.status is DiagnosticStatus.INSUFFICIENT_EVIDENCE
    assert not result.hypotheses
    assert not result.suggested_checks
    assert any(
        item.reason is MissingEvidenceReason.NO_APPLICABLE_GUIDANCE
        for item in result.missing_evidence
    )
    _validated_citations(harness.service, result)


def test_a05_no_observations_and_unrelated_guidance_abstain_with_missing_evidence(
    diagnostic_harness_factory: Callable[..., object],
) -> None:
    no_observation = diagnostic_harness_factory(run_id="run-s08-no-observation")
    no_observation.persist(machine_down=True)
    absent = no_observation.service.generate_hypotheses(
        DiagnosticRequest(
            run_id=no_observation.run_id,
            query="machine unavailable no sensor readings or alarm details",
        )
    )
    assert absent.status is DiagnosticStatus.INSUFFICIENT_EVIDENCE
    assert not absent.hypotheses
    assert any(
        item.reason is MissingEvidenceReason.NO_OBSERVATIONS for item in absent.missing_evidence
    )
    assert absent.suggested_checks
    _validated_citations(no_observation.service, absent)

    unrelated = diagnostic_harness_factory(run_id="run-s08-no-guidance")
    unrelated.persist((_temperature_vibration_observation(unrelated.run_id),))
    no_guidance = unrelated.service.generate_hypotheses(
        DiagnosticRequest(
            run_id=unrelated.run_id,
            query="robot gripper vacuum cup tear",
        )
    )
    assert no_guidance.status is DiagnosticStatus.INSUFFICIENT_EVIDENCE
    assert no_guidance.retrieval is not None
    assert no_guidance.retrieval.status is RetrievalStatus.NO_EVIDENCE
    assert any(
        item.reason is MissingEvidenceReason.NO_RETRIEVED_GUIDANCE
        for item in no_guidance.missing_evidence
    )
    assert not no_guidance.confirmed_root_cause
    _validated_citations(unrelated.service, no_guidance)
