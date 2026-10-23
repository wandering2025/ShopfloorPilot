"""S05-A01..A04 deterministic plan execution and explicit-time acceptance."""

from __future__ import annotations

import time
from collections.abc import Callable
from copy import deepcopy
from typing import Any

import pytest

from shopfloor_pilot.domain.canonical import canonical_hash
from shopfloor_pilot.domain.fixtures import load_scenario
from shopfloor_pilot.domain.models import (
    AttemptStatus,
    CandidateProposal,
    EventType,
    MachineStatus,
    OperationalSnapshot,
    OperationStatus,
    OrderStatus,
)
from shopfloor_pilot.events import SnapshotCheckpoint, replay_events
from shopfloor_pilot.scheduling import SolverConfig, solve_schedule
from shopfloor_pilot.simulation import SimulationRuntime
from shopfloor_pilot.storage import SQLiteStateRepository


def _domain_events(repository: SQLiteStateRepository, run_id: str) -> tuple[Any, ...]:
    return tuple(envelope.event for envelope in repository.list_events(run_id))


def _semantic_snapshot(snapshot: OperationalSnapshot) -> dict[str, Any]:
    value = snapshot.model_dump(mode="json", exclude={"state_version", "recorded_at_utc"})
    value["run_id"] = "<run>"
    return value


def _semantic_events(
    repository: SQLiteStateRepository,
    run_id: str,
    *,
    exclude_time_advanced: bool = False,
) -> tuple[dict[str, Any], ...]:
    projected: list[dict[str, Any]] = []
    for event in _domain_events(repository, run_id):
        if exclude_time_advanced and event.event_type is EventType.TIME_ADVANCED:
            continue
        payload = deepcopy(event.payload)
        # Observation payloads carry the independently generated run identity.
        if isinstance(payload.get("observation"), dict):
            payload["observation"]["run_id"] = "<run>"
            payload["observation"]["observation_id"] = "<observation>"
        projected.append(
            {
                "simulated_time_seconds": event.simulated_time_seconds,
                "event_type": event.event_type.value,
                "payload": payload,
            }
        )
    return tuple(projected)


def _quantity_two_assignments(cell: Any) -> tuple[tuple[Any, str, int, int], ...]:
    """The oracle is independent of both the solver and simulator."""

    return (
        (cell.operation(1, 0), "mill-a", 0, 3),
        (cell.operation(1, 1), "qc", 3, 5),
        (cell.operation(2, 0), "mill-a", 3, 6),
        (cell.operation(2, 1), "qc", 6, 8),
    )


def test_a01_quantity_two_exact_windows_statuses_ordering_and_inclusive_boundaries(
    tiny_cell_factory: Callable[..., Any],
    candidate_factory: Callable[..., CandidateProposal],
    repository_factory: Callable[..., SQLiteStateRepository],
) -> None:
    cell = tiny_cell_factory()
    repository = repository_factory(cell.snapshot, "a01")
    runtime = SimulationRuntime(repository, cell.snapshot.run_id, cell.catalog, seed=17)
    candidate = candidate_factory(
        cell.snapshot,
        _quantity_two_assignments(cell),
        proposal_id="proposal-a01",
        makespan=8,
    )

    activation = runtime.activate_plan(candidate, idempotency_key="activate-a01")
    assert activation.snapshot.plan_revision == 1
    assert activation.snapshot.simulated_time_seconds == 0

    # T is inclusive: the completion and both newly eligible starts at exactly 3 run now.
    at_three = runtime.advance_to(3, advance_id="advance-a01-3").snapshot
    first_mill = next(
        item for item in at_three.attempts if item.operation_id == cell.operation(1, 0).operation_id
    )
    second_mill = next(
        item for item in at_three.attempts if item.operation_id == cell.operation(2, 0).operation_id
    )
    first_qc = next(
        item for item in at_three.attempts if item.operation_id == cell.operation(1, 1).operation_id
    )
    assert first_mill.status is AttemptStatus.COMPLETED
    assert (first_mill.actual_start_seconds, first_mill.actual_end_seconds) == (0, 3)
    assert second_mill.status is AttemptStatus.RUNNING
    assert second_mill.actual_start_seconds == 3
    assert first_qc.status is AttemptStatus.RUNNING
    assert first_qc.actual_start_seconds == 3
    assert at_three.simulated_time_seconds == 3

    final = runtime.advance_to(8, advance_id="advance-a01-8").snapshot
    by_operation = {item.operation_id: item for item in final.attempts}
    assert {
        operation_id: (
            attempt.machine_id,
            attempt.actual_start_seconds,
            attempt.actual_end_seconds,
            attempt.status,
        )
        for operation_id, attempt in by_operation.items()
    } == {
        cell.operation(1, 0).operation_id: ("mill-a", 0, 3, AttemptStatus.COMPLETED),
        cell.operation(1, 1).operation_id: ("qc", 3, 5, AttemptStatus.COMPLETED),
        cell.operation(2, 0).operation_id: ("mill-a", 3, 6, AttemptStatus.COMPLETED),
        cell.operation(2, 1).operation_id: ("qc", 6, 8, AttemptStatus.COMPLETED),
    }
    assert all(item.status is OperationStatus.COMPLETED for item in final.operations)
    assert final.orders[0].status is OrderStatus.COMPLETED
    assert all(item.status is MachineStatus.AVAILABLE for item in final.machines)

    events = _domain_events(repository, cell.snapshot.run_id)
    at_three_events = tuple(item.event_type for item in events if item.simulated_time_seconds == 3)
    assert at_three_events == (
        EventType.OPERATION_COMPLETED,
        EventType.OPERATION_STARTED,
        EventType.OPERATION_STARTED,
        EventType.OBSERVATION_RECORDED,
        EventType.OBSERVATION_RECORDED,
        EventType.TIME_ADVANCED,
    )
    assert tuple(item.sequence for item in events) == tuple(range(1, len(events) + 1))
    for previous, current in zip(events, events[1:], strict=False):
        assert previous.ordering_key < current.ordering_key


def test_a02_same_seed_semantics_exact_replay_and_negative_sensitivity(
    tiny_cell_factory: Callable[..., Any],
    candidate_factory: Callable[..., CandidateProposal],
    repository_factory: Callable[..., SQLiteStateRepository],
) -> None:
    executions: list[
        tuple[Any, SQLiteStateRepository, OperationalSnapshot, SnapshotCheckpoint]
    ] = []
    for suffix in ("one", "two"):
        cell = tiny_cell_factory(run_id=f"run-s05-a02-{suffix}")
        repository = repository_factory(cell.snapshot, f"a02-{suffix}")
        runtime = SimulationRuntime(repository, cell.snapshot.run_id, cell.catalog, seed=739)
        candidate = candidate_factory(
            cell.snapshot,
            _quantity_two_assignments(cell),
            proposal_id=f"proposal-a02-{suffix}",
            makespan=8,
        )
        runtime.activate_plan(candidate, idempotency_key="activate-normal")
        final = runtime.advance_to(8, advance_id="advance-normal").snapshot
        persisted = repository.get_checkpoint(cell.snapshot.run_id)
        replayed = replay_events(
            SnapshotCheckpoint.create(cell.snapshot),
            repository.list_events(cell.snapshot.run_id),
        )
        assert replayed == persisted
        assert replayed.snapshot == final
        assert (
            replay_events(
                SnapshotCheckpoint.create(cell.snapshot),
                repository.list_events(cell.snapshot.run_id),
            )
            == replayed
        )
        executions.append((cell, repository, final, replayed))

    first_cell, first_repo, first_final, _ = executions[0]
    second_cell, second_repo, second_final, _ = executions[1]
    assert _semantic_snapshot(first_final) == _semantic_snapshot(second_final)
    assert _semantic_events(first_repo, first_cell.snapshot.run_id) == _semantic_events(
        second_repo, second_cell.snapshot.run_id
    )

    changed = tiny_cell_factory(run_id="run-s05-a02-changed")
    changed_repo = repository_factory(changed.snapshot, "a02-changed")
    changed_runtime = SimulationRuntime(
        changed_repo,
        changed.snapshot.run_id,
        changed.catalog,
        seed=739,
    )
    changed_assignments = (
        (changed.operation(2, 0), "mill-a", 0, 3),
        (changed.operation(2, 1), "qc", 3, 5),
        (changed.operation(1, 0), "mill-a", 3, 6),
        (changed.operation(1, 1), "qc", 6, 8),
    )
    changed_candidate = candidate_factory(
        changed.snapshot,
        changed_assignments,
        proposal_id="proposal-a02-changed",
        makespan=8,
    )
    changed_runtime.activate_plan(changed_candidate, idempotency_key="activate-normal")
    changed_runtime.advance_to(8, advance_id="advance-normal")
    assert _semantic_events(first_repo, first_cell.snapshot.run_id) != _semantic_events(
        changed_repo, changed.snapshot.run_id
    )


def test_a02_actual_normal_scenario_is_semantically_repeatable_with_same_seed(
    repository_factory: Callable[..., SQLiteStateRepository],
) -> None:
    bundle = load_scenario("normal")
    first_initial = OperationalSnapshot(
        schema_version=1,
        run_id="run-s05-a02-normal-one",
        state_version=0,
        plan_revision=0,
        simulated_time_seconds=0,
        hard_horizon_seconds=bundle.scenario.hard_horizon_seconds,
        machines=bundle.catalog.machines,
        orders=bundle.scenario.orders,
        operations=(),
        attempts=(),
    )
    solved = solve_schedule(
        first_initial,
        bundle.catalog,
        proposal_id="proposal-s05-a02-normal-solved",
        workflow_id="workflow-s05-a02-normal",
        config=SolverConfig(max_time_seconds=5.0, random_seed=0),
    )
    assert solved.candidate is not None

    runs: list[tuple[OperationalSnapshot, SQLiteStateRepository, OperationalSnapshot]] = []
    for suffix in ("one", "two"):
        initial = first_initial.model_copy(update={"run_id": f"run-s05-a02-normal-{suffix}"})
        candidate = CandidateProposal.create(
            schema_version=1,
            proposal_id=f"proposal-s05-a02-normal-{suffix}",
            run_id=initial.run_id,
            workflow_id=f"workflow-s05-a02-normal-{suffix}",
            base_state_version=0,
            base_snapshot_hash=canonical_hash(initial),
            proposed_plan_revision=1,
            assignments=solved.candidate.assignments,
            objective=solved.candidate.objective,
        )
        repository = repository_factory(initial, f"a02-normal-{suffix}")
        runtime = SimulationRuntime(repository, initial.run_id, bundle.catalog, seed=20261020)
        runtime.activate_plan(candidate, idempotency_key="activate-a02-normal")
        final = runtime.advance_to(
            candidate.objective.makespan_seconds,
            advance_id="advance-a02-normal",
        ).snapshot
        exact = replay_events(
            SnapshotCheckpoint.create(initial),
            repository.list_events(initial.run_id),
        )
        assert exact == repository.get_checkpoint(initial.run_id)
        assert exact.snapshot == final
        runs.append((initial, repository, final))

    first_initial, first_repository, first_final = runs[0]
    second_initial, second_repository, second_final = runs[1]
    assert _semantic_snapshot(first_final) == _semantic_snapshot(second_final)
    assert _semantic_events(first_repository, first_initial.run_id) == _semantic_events(
        second_repository, second_initial.run_id
    )


def test_a02_identical_activation_race_reports_idempotent_without_duplicate_plan(
    monkeypatch: pytest.MonkeyPatch,
    tiny_cell_factory: Callable[..., Any],
    candidate_factory: Callable[..., CandidateProposal],
    repository_factory: Callable[..., SQLiteStateRepository],
) -> None:
    cell = tiny_cell_factory(quantity=1, run_id="run-s05-a02-activation-race")
    repository = repository_factory(cell.snapshot, "a02-activation-race")
    runtime = SimulationRuntime(repository, cell.snapshot.run_id, cell.catalog, seed=739)
    candidate = candidate_factory(
        cell.snapshot,
        (
            (cell.operation(1, 0), "mill-a", 5, 8),
            (cell.operation(1, 1), "qc", 8, 10),
        ),
        proposal_id="proposal-a02-activation-race",
        makespan=10,
    )
    original_apply = repository.apply_mutation
    inserted_results: list[Any] = []

    def insert_identical_writer_before_original_result(**kwargs: Any) -> Any:
        # SimulationRuntime already completed its initial key scan and synchronization.
        # Commit the exact request as a competing writer, then let this invocation
        # receive SQLite's idempotent replay of that stored result.
        committed = original_apply(**kwargs)
        replayed = original_apply(**kwargs)
        assert not committed.replayed_idempotent
        assert replayed.replayed_idempotent
        inserted_results.extend((committed, replayed))
        return replayed

    monkeypatch.setattr(
        repository, "apply_mutation", insert_identical_writer_before_original_result
    )
    result = runtime.activate_plan(
        candidate,
        idempotency_key="activate-a02-racing-identical",
    )

    assert len(inserted_results) == 2
    assert result.idempotent
    assert not result.applied
    assert result.mutation is None
    assert result.snapshot.state_version == 1
    assert result.snapshot.plan_revision == 1
    plan_events = tuple(
        item.event
        for item in repository.list_events(cell.snapshot.run_id)
        if item.event.event_type is EventType.PLAN_APPLIED
    )
    assert len(plan_events) == 1
    assert repository.get_checkpoint(cell.snapshot.run_id).snapshot == result.snapshot


def test_a03_one_shot_and_chunked_advances_differ_only_in_control_events(
    tiny_cell_factory: Callable[..., Any],
    candidate_factory: Callable[..., CandidateProposal],
    repository_factory: Callable[..., SQLiteStateRepository],
) -> None:
    runs: list[tuple[Any, SQLiteStateRepository, OperationalSnapshot]] = []
    for suffix, targets in (("one-shot", (8,)), ("chunked", (1, 2, 3, 4, 5, 6, 7, 8))):
        cell = tiny_cell_factory(run_id=f"run-s05-a03-{suffix}")
        repository = repository_factory(cell.snapshot, f"a03-{suffix}")
        runtime = SimulationRuntime(repository, cell.snapshot.run_id, cell.catalog, seed=51)
        candidate = candidate_factory(
            cell.snapshot,
            _quantity_two_assignments(cell),
            proposal_id=f"proposal-a03-{suffix}",
            makespan=8,
        )
        runtime.activate_plan(candidate, idempotency_key="activate-a03")
        for target in targets:
            runtime.advance_to(target, advance_id=f"advance-a03-{target}")
        runs.append((cell, repository, runtime.snapshot))

    one_cell, one_repo, one_final = runs[0]
    chunk_cell, chunk_repo, chunk_final = runs[1]
    assert _semantic_snapshot(one_final) == _semantic_snapshot(chunk_final)
    assert _semantic_events(
        one_repo, one_cell.snapshot.run_id, exclude_time_advanced=True
    ) == _semantic_events(chunk_repo, chunk_cell.snapshot.run_id, exclude_time_advanced=True)
    assert len(
        tuple(
            item
            for item in _domain_events(chunk_repo, chunk_cell.snapshot.run_id)
            if item.event_type is EventType.TIME_ADVANCED
        )
    ) > len(
        tuple(
            item
            for item in _domain_events(one_repo, one_cell.snapshot.run_id)
            if item.event_type is EventType.TIME_ADVANCED
        )
    )


def test_a04_pause_sleep_and_repeated_reads_never_advance_simulated_time(
    tiny_cell_factory: Callable[..., Any],
    candidate_factory: Callable[..., CandidateProposal],
    repository_factory: Callable[..., SQLiteStateRepository],
) -> None:
    cell = tiny_cell_factory(quantity=1)
    repository = repository_factory(cell.snapshot, "a04")
    runtime = SimulationRuntime(repository, cell.snapshot.run_id, cell.catalog, seed=3)
    candidate = candidate_factory(
        cell.snapshot,
        (
            (cell.operation(1, 0), "mill-a", 5, 8),
            (cell.operation(1, 1), "qc", 8, 10),
        ),
        proposal_id="proposal-a04",
        makespan=10,
    )
    runtime.activate_plan(candidate, idempotency_key="activate-a04")
    before = repository.get_checkpoint(cell.snapshot.run_id)
    before_events = repository.list_events(cell.snapshot.run_id)

    for _ in range(5):
        runtime.pause()
        runtime.project_public_state()
        _ = runtime.snapshot
        _ = repository.get_snapshot(cell.snapshot.run_id)
    time.sleep(0.02)
    for _ in range(5):
        runtime.projection()
        _ = repository.list_events(cell.snapshot.run_id)

    assert repository.get_checkpoint(cell.snapshot.run_id) == before
    assert repository.list_events(cell.snapshot.run_id) == before_events
    assert runtime.snapshot.simulated_time_seconds == 0
    assert all(item.status is AttemptStatus.SCHEDULED for item in runtime.snapshot.attempts)
