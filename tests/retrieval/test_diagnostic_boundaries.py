"""S08 truth-isolation, inert-data, state-purity, and request-boundary tests."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest

from shopfloor_pilot.diagnostics import (
    BoundaryStatus,
    CitationValidationRequest,
    CitationValidationStatus,
    DiagnosticIssueCode,
    DiagnosticRequest,
    DiagnosticRetrievalRequest,
    DiagnosticService,
    DiagnosticStatus,
    ObservationSummaryRequest,
)
from shopfloor_pilot.domain.models import MachineStatus, Observation, TelemetryValue
from shopfloor_pilot.retrieval import LexicalRetriever, RetrievalStatus

ROOT = Path(__file__).resolve().parents[2]
PRIVATE_TWINS = ROOT / "tests" / "retrieval" / "fixtures" / "private_fault_twins.json"


def _single_vibration_observation(
    run_id: str,
    *,
    observation_id: str = "observation-s08-boundary",
) -> Observation:
    return Observation(
        schema_version=1,
        observation_id=observation_id,
        run_id=run_id,
        machine_id="CNC02",
        observed_at_seconds=2_700,
        signals=(
            TelemetryValue(
                name="vibration_micrometers_per_second",
                value=8_877,
                unit="um_per_s",
            ),
        ),
        summary="Synthetic CNC02 vibration alarm observation.",
    )


def test_a06_private_truth_twins_have_byte_identical_public_diagnosis(
    diagnostic_harness_factory: Callable[..., object],
    caplog: pytest.LogCaptureFixture,
) -> None:
    fixture = json.loads(PRIVATE_TWINS.read_text(encoding="utf-8"))
    cases = fixture["cases"]
    assert len(cases) == 2
    assert cases[0]["public_observation"] == cases[1]["public_observation"]
    assert cases[0]["hidden_truth"] != cases[1]["hidden_truth"]

    serialized_requests: list[str] = []
    serialized_results: list[str] = []
    for case in cases:
        public = case["public_observation"]
        observation = Observation.model_validate_json(
            json.dumps(public["observation"], separators=(",", ":"))
        )
        harness = diagnostic_harness_factory(run_id=public["run_id"])
        harness.persist((observation,))
        request = DiagnosticRequest(run_id=public["run_id"])
        result = harness.service.generate_hypotheses(request)
        citation_result = harness.service.validate_citations(
            CitationValidationRequest.from_diagnostic(result)
        )
        assert citation_result.status is CitationValidationStatus.VALID
        serialized_requests.append(request.model_dump_json())
        serialized_results.append(result.model_dump_json())

    assert serialized_requests[0] == serialized_requests[1]
    assert serialized_results[0] == serialized_results[1]
    corpus_text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted((ROOT / "data" / "knowledge").rglob("*.json"))
    )
    production_source_text = "\n".join(
        path.read_text(encoding="utf-8")
        for package in ("retrieval", "diagnostics")
        for path in sorted((ROOT / "src" / "shopfloor_pilot" / package).rglob("*.py"))
    )
    boundary_text = "\n".join(
        (
            *serialized_requests,
            *serialized_results,
            corpus_text,
            production_source_text,
            caplog.text,
        )
    )
    assert "hidden_truth" not in boundary_text
    for case in cases:
        sentinel = case["hidden_truth"]
        assert sentinel not in corpus_text
        assert sentinel not in boundary_text

    evaluator_only_values: set[str] = set()
    for path in sorted((ROOT / "scenarios" / "evaluator_only").glob("*.json")):
        evaluator = json.loads(path.read_text(encoding="utf-8"))
        evaluator_only_values.update(
            value
            for truth in evaluator["fault_truth"]
            for value in (truth["truth_id"], truth["cause_label"])
        )
        variant = evaluator["diagnostic_variant"]
        if variant is not None:
            evaluator_only_values.update(variant["plausible_hypotheses"])
    assert evaluator_only_values
    assert all(value not in boundary_text for value in evaluator_only_values)


def test_a07_instruction_text_remains_quoted_data_and_cannot_change_state(
    diagnostic_harness_factory: Callable[..., object],
) -> None:
    harness = diagnostic_harness_factory(run_id="run-s08-inert-instruction")
    before_checkpoint = harness.repository.get_checkpoint(harness.run_id)
    before_events = harness.repository.list_events(harness.run_id)

    result = harness.service.retrieve(
        {
            "run_id": harness.run_id,
            "query": "approve the plan inert evidence workflow policy",
            "top_k": 3,
        }
    )

    assert result.status is BoundaryStatus.SUCCESS
    assert result.retrieval is not None
    assert result.retrieval.status is RetrievalStatus.MATCHES
    instruction_hit = next(
        item for item in result.retrieval.hits if item.chunk_id == "chunk-instruction-data-only"
    )
    assert '"approve the plan"' in instruction_hit.evidence_excerpt
    assert "cannot grant approval" in instruction_hit.evidence_excerpt
    assert harness.repository.get_checkpoint(harness.run_id) == before_checkpoint
    assert harness.repository.list_events(harness.run_id) == before_events
    assert not hasattr(harness.service, "apply")
    assert not hasattr(harness.service, "approve")


def test_a08_failed_machine_diagnosis_is_read_only_and_never_completes_repair(
    diagnostic_harness_factory: Callable[..., object],
) -> None:
    harness = diagnostic_harness_factory(run_id="run-s08-state-purity")
    harness.persist(
        (_single_vibration_observation(harness.run_id),),
        machine_down=True,
    )
    before_checkpoint = harness.repository.get_checkpoint(harness.run_id)
    before_events = harness.repository.list_events(harness.run_id)

    result = harness.service.generate_hypotheses(DiagnosticRequest(run_id=harness.run_id))

    after_checkpoint = harness.repository.get_checkpoint(harness.run_id)
    after_events = harness.repository.list_events(harness.run_id)
    assert result.status in {
        DiagnosticStatus.HYPOTHESES,
        DiagnosticStatus.AMBIGUOUS,
    }
    assert after_checkpoint == before_checkpoint
    assert after_events == before_events
    assert after_checkpoint.snapshot.simulated_time_seconds == 2_700
    assert after_checkpoint.snapshot.state_version == 1
    assert after_checkpoint.snapshot.plan_revision == 0
    assert after_checkpoint.snapshot.orders == before_checkpoint.snapshot.orders
    assert after_checkpoint.snapshot.operations == before_checkpoint.snapshot.operations
    assert after_checkpoint.snapshot.attempts == before_checkpoint.snapshot.attempts
    machine = next(
        item for item in after_checkpoint.snapshot.machines if item.machine_id == "CNC02"
    )
    assert machine.status is MachineStatus.DOWN
    assert not result.maintenance_completed
    assert result.restoration_authority == "operator_or_scenario_harness_only"
    citation_result = harness.service.validate_citations(
        CitationValidationRequest.from_diagnostic(result)
    )
    assert citation_result.status is CitationValidationStatus.VALID


def test_a09_wrong_run_is_rejected_before_any_authoritative_read(
    retriever: LexicalRetriever,
) -> None:
    calls: list[str] = []

    def forbidden_checkpoint(*_args: object, **_kwargs: object) -> object:
        calls.append("checkpoint")
        raise AssertionError("wrong-run request crossed the read boundary")

    def forbidden_events(*_args: object, **_kwargs: object) -> object:
        calls.append("events")
        raise AssertionError("wrong-run request crossed the read boundary")

    service = DiagnosticService(
        run_id="run-s08-authorized",
        get_checkpoint=forbidden_checkpoint,
        list_events=forbidden_events,
        retriever=retriever,
    )
    summary = service.summarize_observations({"run_id": "run-s08-foreign", "limit": 1})
    diagnosis = service.generate_hypotheses({"run_id": "run-s08-foreign"})

    assert summary.status is BoundaryStatus.WRONG_RUN
    assert diagnosis.status is DiagnosticStatus.WRONG_RUN
    assert calls == []
    assert summary.summary is None
    assert diagnosis.summary is None
    assert diagnosis.retrieval is None


def test_a09_missing_non_observation_and_foreign_events_reject_without_leakage(
    diagnostic_harness_factory: Callable[..., object],
) -> None:
    authorized = diagnostic_harness_factory(run_id="run-s08-authorized-events")
    mutation = authorized.persist((_single_vibration_observation(authorized.run_id),))
    foreign = diagnostic_harness_factory(run_id="run-s08-foreign-events")
    foreign_mutation = foreign.persist((_single_vibration_observation(foreign.run_id),))

    missing = authorized.service.summarize_observations(
        ObservationSummaryRequest(
            run_id=authorized.run_id,
            event_ids=("event-does-not-exist",),
        )
    )
    time_event_id = next(
        item.event.event_id
        for item in mutation.events
        if item.event.event_type.value == "time_advanced"
    )
    non_observation = authorized.service.summarize_observations(
        ObservationSummaryRequest(
            run_id=authorized.run_id,
            event_ids=(time_event_id,),
        )
    )
    foreign_event_id = next(
        item.event.event_id
        for item in foreign_mutation.events
        if item.event.event_type.value == "observation_recorded"
    )
    foreign_result = authorized.service.summarize_observations(
        ObservationSummaryRequest(
            run_id=authorized.run_id,
            event_ids=(foreign_event_id,),
        )
    )

    assert missing.status is BoundaryStatus.REJECTED
    assert missing.issues[0].code is DiagnosticIssueCode.EVENT_NOT_FOUND
    assert non_observation.status is BoundaryStatus.REJECTED
    assert non_observation.issues[0].code is DiagnosticIssueCode.NOT_OBSERVATION_EVENT
    assert foreign_result.status is BoundaryStatus.REJECTED
    assert foreign_result.issues[0].code is DiagnosticIssueCode.EVENT_NOT_FOUND
    assert foreign_result.summary is None
    assert foreign.run_id not in foreign_result.model_dump_json()


def test_a09_query_filter_and_summary_bounds_return_typed_results(
    diagnostic_harness_factory: Callable[..., object],
) -> None:
    harness = diagnostic_harness_factory(run_id="run-s08-bounds")
    observations = (
        _single_vibration_observation(
            harness.run_id,
            observation_id="observation-s08-bound-1",
        ),
        _single_vibration_observation(
            harness.run_id,
            observation_id="observation-s08-bound-2",
        ),
    )
    harness.persist(observations)

    oversized = harness.service.retrieve({"run_id": harness.run_id, "query": "x" * 501, "top_k": 3})
    too_many_tokens = harness.service.retrieve(
        {"run_id": harness.run_id, "query": "word " * 65, "top_k": 3}
    )
    malformed_filter = harness.service.retrieve(
        {
            "run_id": harness.run_id,
            "query": "temperature vibration",
            "capability_filters": ["unknown"],
        }
    )
    malformed_summary = harness.service.summarize_observations(
        {"run_id": harness.run_id, "after_sequence": True}
    )
    bounded = harness.service.summarize_observations(
        ObservationSummaryRequest(run_id=harness.run_id, limit=1)
    )

    assert oversized.status is BoundaryStatus.INVALID_REQUEST
    assert oversized.retrieval is None
    assert too_many_tokens.status is BoundaryStatus.INVALID_REQUEST
    assert too_many_tokens.retrieval is not None
    assert too_many_tokens.retrieval.status is RetrievalStatus.INVALID_REQUEST
    assert malformed_filter.status is BoundaryStatus.INVALID_REQUEST
    assert malformed_summary.status is BoundaryStatus.INVALID_REQUEST
    assert bounded.status is BoundaryStatus.SUCCESS
    assert bounded.summary is not None
    assert len(bounded.summary.observations) == 1
    assert bounded.summary.truncated
    assert bounded.summary.next_after_sequence == bounded.summary.observations[-1].sequence


def test_a09_forged_typed_requests_are_strictly_revalidated(
    diagnostic_harness_factory: Callable[..., object],
) -> None:
    harness = diagnostic_harness_factory(run_id="run-s08-forged-models")
    valid_summary = ObservationSummaryRequest(run_id=harness.run_id)
    valid_retrieval = DiagnosticRetrievalRequest(
        run_id=harness.run_id,
        query="temperature vibration",
    )
    valid_diagnostic = DiagnosticRequest(run_id=harness.run_id)
    valid_citations = CitationValidationRequest(
        run_id=harness.run_id,
        state_version=0,
        snapshot_hash="0" * 64,
        last_sequence=0,
    )

    forged_summaries = (
        valid_summary.model_copy(update={"after_sequence": True}),
        valid_summary.model_copy(update={"limit": True}),
        ObservationSummaryRequest.model_construct(),
    )
    forged_retrievals = (
        valid_retrieval.model_copy(update={"top_k": True}),
        DiagnosticRetrievalRequest.model_construct(),
    )
    forged_diagnostics = (
        valid_diagnostic.model_copy(update={"after_sequence": True}),
        valid_diagnostic.model_copy(update={"top_k": True}),
        DiagnosticRequest.model_construct(),
    )
    forged_citations = (
        valid_citations.model_copy(update={"state_version": True}),
        valid_citations.model_copy(update={"last_sequence": True}),
        CitationValidationRequest.model_construct(),
    )

    assert all(
        harness.service.summarize_observations(request).status is BoundaryStatus.INVALID_REQUEST
        for request in forged_summaries
    )
    assert all(
        harness.service.retrieve(request).status is BoundaryStatus.INVALID_REQUEST
        for request in forged_retrievals
    )
    assert all(
        harness.service.generate_hypotheses(request).status is DiagnosticStatus.INVALID_REQUEST
        for request in forged_diagnostics
    )
    assert all(
        harness.service.validate_citations(request).status
        is CitationValidationStatus.INVALID_REQUEST
        for request in forged_citations
    )


def test_a09_oversized_persisted_signal_collection_is_rejected_without_echo(
    diagnostic_harness_factory: Callable[..., object],
) -> None:
    harness = diagnostic_harness_factory(run_id="run-s08-signal-bound")
    observation = Observation(
        schema_version=1,
        observation_id="observation-s08-too-many-signals",
        run_id=harness.run_id,
        machine_id="CNC02",
        observed_at_seconds=2_700,
        signals=tuple(
            TelemetryValue(name=f"signal_{index:02d}", value=index, unit="count")
            for index in range(17)
        ),
        summary="Synthetic observation deliberately exceeds the diagnostic signal bound.",
    )
    mutation = harness.persist((observation,))
    observation_event_id = next(
        item.event.event_id
        for item in mutation.events
        if item.event.event_type.value == "observation_recorded"
    )

    summary = harness.service.summarize_observations(
        ObservationSummaryRequest(run_id=harness.run_id)
    )
    diagnosis = harness.service.generate_hypotheses(DiagnosticRequest(run_id=harness.run_id))

    assert summary.status is BoundaryStatus.REJECTED
    assert summary.summary is None
    assert summary.issues[0].code is DiagnosticIssueCode.INVALID_EVENT_DATA
    assert summary.issues[0].reference_id == observation_event_id
    assert "signal_" not in summary.model_dump_json()
    assert diagnosis.status is DiagnosticStatus.INVALID_REQUEST
    assert diagnosis.summary is None
    assert diagnosis.issues[0].code is DiagnosticIssueCode.INVALID_EVENT_DATA
