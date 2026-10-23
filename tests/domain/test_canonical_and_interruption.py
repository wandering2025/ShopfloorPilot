"""S01-A04 and A06 serialization, provenance, and interruption acceptance."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from shopfloor_pilot.domain.canonical import (
    canonical_hash,
    canonical_json_bytes,
    semantic_hash,
)
from shopfloor_pilot.domain.examples import build_representative_contracts
from shopfloor_pilot.domain.fixtures import ScenarioBundle
from shopfloor_pilot.domain.models import (
    CandidateProposal,
    DomainEvent,
    EventType,
    Observation,
    OperationalSnapshot,
)


def test_a04_representative_types_round_trip_twice(
    bundles: dict[str, ScenarioBundle],
) -> None:
    contracts = build_representative_contracts(bundles["normal"])
    for value, model_type in (
        (contracts.snapshot, OperationalSnapshot),
        (contracts.candidate, CandidateProposal),
        (contracts.observation, Observation),
        (contracts.event, DomainEvent),
    ):
        first = model_type.model_validate_json(value.model_dump_json())
        second = model_type.model_validate_json(first.model_dump_json())
        assert first == value
        assert second == value
        assert canonical_json_bytes(first) == canonical_json_bytes(second)


def test_a04_canonical_hash_ignores_dictionary_order_and_wall_clock(
    bundles: dict[str, ScenarioBundle],
) -> None:
    contracts = build_representative_contracts(bundles["normal"])
    assert canonical_hash({"b": 2, "a": 1}) == canonical_hash({"a": 1, "b": 2})

    raw = contracts.snapshot.model_dump(mode="json")
    raw["recorded_at_utc"] = "2026-10-20T15:59:59Z"
    later = OperationalSnapshot.model_validate_json(json.dumps(raw))
    assert later != contracts.snapshot
    assert canonical_hash(later) == canonical_hash(contracts.snapshot)


def test_a04_exact_replay_and_semantic_cross_run_views_are_distinct() -> None:
    first = DomainEvent(
        schema_version=1,
        event_id="event-generated-a",
        run_id="run-generated-a",
        sequence=1,
        simulated_time_seconds=50,
        event_type=EventType.OPERATION_STARTED,
        payload={"machine_id": "CNC01", "operation_id": "order-001-unit-1-drill"},
        recorded_at_utc="2026-10-20T15:00:00Z",
    )
    second = DomainEvent(
        schema_version=1,
        event_id="event-generated-b",
        run_id="run-generated-b",
        sequence=87,
        simulated_time_seconds=50,
        event_type=EventType.OPERATION_STARTED,
        payload={"operation_id": "order-001-unit-1-drill", "machine_id": "CNC01"},
        recorded_at_utc="2026-10-20T16:00:00Z",
    )
    assert canonical_hash(first) != canonical_hash(second)
    assert semantic_hash(first) == semantic_hash(second)


def test_a04_candidate_hash_detects_content_tampering(
    bundles: dict[str, ScenarioBundle],
) -> None:
    candidate = build_representative_contracts(bundles["normal"]).candidate
    raw = candidate.model_dump(mode="json")
    raw["base_state_version"] += 1
    with pytest.raises(ValidationError, match="proposal_hash does not match"):
        CandidateProposal.model_validate_json(json.dumps(raw))


def test_a06_interruption_keeps_history_and_restarts_full_duration(
    bundles: dict[str, ScenarioBundle],
) -> None:
    contracts = build_representative_contracts(bundles["normal"])
    attempts = {item.attempt_id: item for item in contracts.snapshot.attempts}
    interrupted = attempts["attempt-example-interrupted-1"]
    successor = attempts["attempt-example-interrupted-2"]
    completed = attempts["attempt-example-completed-1"]
    running = attempts["attempt-example-running-1"]

    assert interrupted.consumed_seconds == 180
    assert interrupted.planned_duration_seconds == 900
    assert successor.restart_of_attempt_id == interrupted.attempt_id
    assert successor.planned_duration_seconds == interrupted.planned_duration_seconds == 900
    assert successor.scheduled_window is not None
    assert successor.scheduled_window.duration_seconds == 900
    assert completed.consumed_seconds == completed.planned_duration_seconds == 420
    assert running.actual_start_seconds == 420
    assert running.actual_end_seconds is None
    assert running.consumed_seconds == 180
    with pytest.raises(ValidationError, match="frozen"):
        completed.consumed_seconds = 0  # type: ignore[misc]


def test_a06_candidate_declares_provenance_deltas_and_approval_is_not_apply(
    bundles: dict[str, ScenarioBundle],
) -> None:
    contracts = build_representative_contracts(bundles["normal"])
    snapshot_before = canonical_hash(contracts.snapshot)
    candidate = contracts.candidate

    assert candidate.run_id == contracts.snapshot.run_id
    assert candidate.base_state_version == contracts.snapshot.state_version
    assert candidate.base_snapshot_hash == canonical_hash(contracts.snapshot)
    assert candidate.proposed_plan_revision == contracts.snapshot.plan_revision + 1
    assert candidate.objective.ordering == "lexicographic_weighted_tardiness_then_makespan"
    assert candidate.proposed_order_deltas[0].order.order_id == "order-example-urgent"
    assert candidate.proposed_resource_deltas[0].machine_id == "CNC02"
    assert candidate.assignments[0].machine_id == "CNC03"
    assert all(
        assignment.operation_id != "op-example-running" for assignment in candidate.assignments
    )
    assert contracts.approval.proposal_hash == candidate.proposal_hash
    assert "applied" not in type(contracts.approval).model_fields
    assert "application" not in type(contracts.approval).model_fields
    assert canonical_hash(contracts.snapshot) == snapshot_before
