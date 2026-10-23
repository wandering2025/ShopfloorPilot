"""S05-A05/A06/A08/A09/A11 interruption, replanning, and rejection acceptance."""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from typing import Any

import pytest

from shopfloor_pilot.domain.fixtures import InterventionKind, ScheduledIntervention
from shopfloor_pilot.domain.models import (
    AttemptStatus,
    CandidateProposal,
    EventType,
    MachineStatus,
    OperationStatus,
    OrderStatus,
    ScheduleAssignment,
    TimeWindow,
)
from shopfloor_pilot.events import (
    EventDraft,
    MachineDownPayload,
    MachineRestoredPayload,
    TimeAdvancedPayload,
)
from shopfloor_pilot.scheduling import SolveOutcome, SolverConfig, solve_schedule
from shopfloor_pilot.simulation import (
    BoundaryClosedError,
    InvalidPlanError,
    SimulationRuntime,
    StalePlanError,
)
from shopfloor_pilot.storage import SQLiteStateRepository


def _fault(intervention_id: str, at: int, machine_id: str) -> ScheduledIntervention:
    return ScheduledIntervention(
        intervention_id=intervention_id,
        at_seconds=at,
        kind=InterventionKind.MACHINE_FAULT,
        target_machine_id=machine_id,
        injected_status=MachineStatus.DOWN,
        private_truth_ref="truth-private-sentinel",
    )


def _restore(intervention_id: str, at: int, machine_id: str) -> ScheduledIntervention:
    return ScheduledIntervention(
        intervention_id=intervention_id,
        at_seconds=at,
        kind=InterventionKind.MACHINE_RESTORED,
        target_machine_id=machine_id,
        injected_status=MachineStatus.AVAILABLE,
    )


def _event_rows(repository: SQLiteStateRepository, run_id: str) -> tuple[Any, ...]:
    return tuple(item.event for item in repository.list_events(run_id))


def _attempt_for_operation(snapshot: Any, operation_id: str) -> Any:
    return next(item for item in snapshot.attempts if item.operation_id == operation_id)


def _draft(at_seconds: int, event_type: EventType, payload: Any) -> EventDraft:
    return EventDraft(
        simulated_time_seconds=at_seconds,
        event_type=event_type,
        payload=payload.model_dump(mode="json"),
    )


def test_a05_fault_invalidates_old_callback_and_restart_consumes_full_duration(
    tiny_cell_factory: Callable[..., Any],
    candidate_factory: Callable[..., CandidateProposal],
    repository_factory: Callable[..., SQLiteStateRepository],
) -> None:
    cell = tiny_cell_factory(
        quantity=1,
        milling_seconds=10,
        inspection_seconds=2,
        horizon=25,
        second_mill=True,
    )
    repository = repository_factory(cell.snapshot, "a05")
    runtime = SimulationRuntime(repository, cell.snapshot.run_id, cell.catalog, seed=101)
    initial = candidate_factory(
        cell.snapshot,
        (
            (cell.operation(1, 0), "mill-a", 0, 10),
            (cell.operation(1, 1), "qc", 10, 12),
        ),
        proposal_id="proposal-a05-initial",
        makespan=12,
    )
    runtime.activate_plan(initial, idempotency_key="activate-a05-initial")

    fault_result = runtime.advance_to(
        4,
        advance_id="advance-a05-fault",
        interventions=(_fault("fault-a05", 4, "mill-a"),),
    )
    interrupted = _attempt_for_operation(fault_result.snapshot, cell.operation(1, 0).operation_id)
    assert interrupted.status is AttemptStatus.INTERRUPTED
    assert (interrupted.actual_start_seconds, interrupted.actual_end_seconds) == (0, 4)
    assert interrupted.consumed_seconds == 4
    assert fault_result.snapshot.orders[0].status is OrderStatus.BLOCKED

    old_qc = _attempt_for_operation(fault_result.snapshot, cell.operation(1, 1).operation_id)
    recovery = candidate_factory(
        fault_result.snapshot,
        (
            ScheduleAssignment(
                operation_id=cell.operation(1, 0).operation_id,
                attempt_id="attempt-a05-restart-2",
                machine_id="mill-b",
                window=TimeWindow(start_seconds=5, end_seconds=15),
            ),
            ScheduleAssignment(
                operation_id=cell.operation(1, 1).operation_id,
                attempt_id=old_qc.attempt_id,
                machine_id="qc",
                window=TimeWindow(start_seconds=15, end_seconds=17),
            ),
        ),
        proposal_id="proposal-a05-recovery",
        makespan=17,
    )
    activated = runtime.activate_plan(
        recovery,
        idempotency_key="activate-a05-recovery",
        effective_time_seconds=5,
    )
    restarted = next(
        item for item in activated.snapshot.attempts if item.attempt_id == "attempt-a05-restart-2"
    )
    assert restarted.status is AttemptStatus.SCHEDULED
    assert restarted.attempt_number == 2
    assert restarted.restart_of_attempt_id == interrupted.attempt_id
    assert restarted.actual_start_seconds is None

    at_restart = runtime.advance_to(5, advance_id="advance-a05-restart")
    restarted = next(
        item for item in at_restart.snapshot.attempts if item.attempt_id == "attempt-a05-restart-2"
    )
    assert restarted.status is AttemptStatus.RUNNING
    assert restarted.actual_start_seconds == 5

    # The old completion at 10 and old downstream start at 10 are both obsolete.
    after_old_callbacks = runtime.advance_to(11, advance_id="advance-a05-past-old")
    restarted = next(
        item
        for item in after_old_callbacks.snapshot.attempts
        if item.attempt_id == "attempt-a05-restart-2"
    )
    moved_qc = next(
        item
        for item in after_old_callbacks.snapshot.attempts
        if item.operation_id == cell.operation(1, 1).operation_id
    )
    assert restarted.status is AttemptStatus.RUNNING
    assert moved_qc.status is AttemptStatus.SCHEDULED
    assert moved_qc.actual_start_seconds is None

    at_new_completion = runtime.advance_to(15, advance_id="advance-a05-new-completion")
    restarted = next(
        item
        for item in at_new_completion.snapshot.attempts
        if item.attempt_id == "attempt-a05-restart-2"
    )
    assert restarted.status is AttemptStatus.COMPLETED
    assert (restarted.actual_start_seconds, restarted.actual_end_seconds) == (5, 15)
    assert restarted.consumed_seconds == 10
    assert (
        _attempt_for_operation(
            at_new_completion.snapshot, cell.operation(1, 1).operation_id
        ).actual_start_seconds
        == 15
    )
    final = runtime.advance_to(17, advance_id="advance-a05-final").snapshot
    assert final.orders[0].status is OrderStatus.COMPLETED

    events = _event_rows(repository, cell.snapshot.run_id)
    interruption_ids = [
        event.payload["attempt_id"]
        for event in events
        if event.event_type is EventType.OPERATION_INTERRUPTED
    ]
    completion_ids = [
        event.payload["attempt_id"]
        for event in events
        if event.event_type is EventType.OPERATION_COMPLETED
    ]
    assert interruption_ids == [interrupted.attempt_id]
    assert completion_ids.count(interrupted.attempt_id) == 0
    assert completion_ids.count("attempt-a05-restart-2") == 1


def test_a05_scheduler_not_before_recovery_activates_and_executes(
    tiny_cell_factory: Callable[..., Any],
    candidate_factory: Callable[..., CandidateProposal],
    repository_factory: Callable[..., SQLiteStateRepository],
) -> None:
    """Exercise the real S04 recovery boundary without replacing the hand-built oracle."""

    cell = tiny_cell_factory(
        quantity=1,
        milling_seconds=10,
        inspection_seconds=2,
        horizon=25,
        second_mill=True,
        run_id="run-s05-a05-solver",
    )
    repository = repository_factory(cell.snapshot, "a05-solver")
    runtime = SimulationRuntime(repository, cell.snapshot.run_id, cell.catalog, seed=101)
    initial = candidate_factory(
        cell.snapshot,
        (
            (cell.operation(1, 0), "mill-a", 0, 10),
            (cell.operation(1, 1), "qc", 10, 12),
        ),
        proposal_id="proposal-a05-solver-initial",
        makespan=12,
    )
    runtime.activate_plan(initial, idempotency_key="activate-a05-solver-initial")
    fault_snapshot = runtime.advance_to(
        4,
        advance_id="advance-a05-solver-fault",
        interventions=(_fault("fault-a05-solver", 4, "mill-a"),),
    ).snapshot

    recovery = solve_schedule(
        fault_snapshot,
        cell.catalog,
        proposal_id="proposal-a05-solver-recovery",
        workflow_id="workflow-a05-solver-recovery",
        not_before_seconds=5,
        config=SolverConfig(max_time_seconds=1.0, random_seed=0),
    )
    assert recovery.outcome is SolveOutcome.OPTIMAL
    assert recovery.report.not_before_seconds == 5
    assert recovery.candidate is not None
    assert tuple(
        (item.machine_id, item.window.start_seconds, item.window.end_seconds)
        for item in recovery.candidate.assignments
    ) == (("mill-b", 5, 15), ("qc", 15, 17))

    activated = runtime.activate_plan(
        recovery.candidate,
        idempotency_key="activate-a05-solver-recovery",
        effective_time_seconds=5,
    )
    assert activated.snapshot.simulated_time_seconds == 4
    restart = next(
        item
        for item in activated.snapshot.attempts
        if item.operation_id == cell.operation(1, 0).operation_id
        and item.status is AttemptStatus.SCHEDULED
    )
    assert restart.machine_id == "mill-b"
    assert restart.attempt_number == 2
    assert restart.scheduled_window == TimeWindow(start_seconds=5, end_seconds=15)

    final = runtime.advance_to(17, advance_id="advance-a05-solver-final").snapshot
    restarted = next(item for item in final.attempts if item.attempt_number == 2)
    assert restarted.status is AttemptStatus.COMPLETED
    assert (restarted.actual_start_seconds, restarted.actual_end_seconds) == (5, 15)
    assert restarted.consumed_seconds == 10
    assert final.orders[0].status is OrderStatus.COMPLETED


def test_a06_successive_plans_move_one_scheduled_callback_without_duplicate_work(
    tiny_cell_factory: Callable[..., Any],
    candidate_factory: Callable[..., CandidateProposal],
    repository_factory: Callable[..., SQLiteStateRepository],
) -> None:
    cell = tiny_cell_factory(
        quantity=2,
        milling_seconds=10,
        inspection_seconds=2,
        horizon=30,
        second_mill=True,
    )
    repository = repository_factory(cell.snapshot, "a06")
    runtime = SimulationRuntime(repository, cell.snapshot.run_id, cell.catalog, seed=33)
    first = candidate_factory(
        cell.snapshot,
        (
            (cell.operation(1, 0), "mill-a", 0, 10),
            (cell.operation(1, 1), "qc", 10, 12),
            (cell.operation(2, 0), "mill-b", 5, 15),
            (cell.operation(2, 1), "qc", 15, 17),
        ),
        proposal_id="proposal-a06-r1",
        makespan=17,
    )
    first_result = runtime.activate_plan(first, idempotency_key="activate-a06-r1")
    first_by_operation = {item.operation_id: item for item in first_result.snapshot.attempts}
    unaffected_id = first_by_operation[cell.operation(1, 0).operation_id].attempt_id
    movable_id = first_by_operation[cell.operation(2, 0).operation_id].attempt_id
    unit_one_qc_id = first_by_operation[cell.operation(1, 1).operation_id].attempt_id
    unit_two_qc_id = first_by_operation[cell.operation(2, 1).operation_id].attempt_id
    assert first_by_operation[cell.operation(2, 0).operation_id].scheduled_window == TimeWindow(
        start_seconds=5, end_seconds=15
    )

    def moved_candidate(start: int, base: Any, revision: int) -> CandidateProposal:
        return candidate_factory(
            base,
            (
                ScheduleAssignment(
                    operation_id=cell.operation(1, 1).operation_id,
                    attempt_id=unit_one_qc_id,
                    machine_id="qc",
                    window=TimeWindow(start_seconds=10, end_seconds=12),
                ),
                ScheduleAssignment(
                    operation_id=cell.operation(2, 0).operation_id,
                    attempt_id=movable_id,
                    machine_id="mill-b",
                    window=TimeWindow(start_seconds=start, end_seconds=start + 10),
                ),
                ScheduleAssignment(
                    operation_id=cell.operation(2, 1).operation_id,
                    attempt_id=unit_two_qc_id,
                    machine_id="qc",
                    window=TimeWindow(start_seconds=start + 10, end_seconds=start + 12),
                ),
            ),
            proposal_id=f"proposal-a06-r{revision}",
            makespan=start + 12,
        )

    second = moved_candidate(6, first_result.snapshot, 2)
    second_result = runtime.activate_plan(
        second,
        idempotency_key="activate-a06-r2",
        effective_time_seconds=1,
    )
    second_by_operation = {item.operation_id: item for item in second_result.snapshot.attempts}
    assert second_result.snapshot.simulated_time_seconds == 0
    assert second_by_operation[cell.operation(2, 0).operation_id].attempt_id == movable_id
    assert second_by_operation[cell.operation(2, 0).operation_id].scheduled_window == TimeWindow(
        start_seconds=6, end_seconds=16
    )
    assert (
        second_by_operation[cell.operation(1, 0).operation_id]
        == first_by_operation[cell.operation(1, 0).operation_id]
    )
    second_boundary = runtime.advance_to(1, advance_id="advance-a06-r2-boundary")
    assert second_boundary.snapshot.simulated_time_seconds == 1
    third = moved_candidate(7, second_boundary.snapshot, 3)
    third_result = runtime.activate_plan(
        third,
        idempotency_key="activate-a06-r3",
        effective_time_seconds=2,
    )
    third_by_operation = {item.operation_id: item for item in third_result.snapshot.attempts}
    assert third_result.snapshot.simulated_time_seconds == 1
    assert third_by_operation[cell.operation(2, 0).operation_id].attempt_id == movable_id
    assert third_by_operation[cell.operation(2, 0).operation_id].scheduled_window == TimeWindow(
        start_seconds=7, end_seconds=17
    )
    assert (
        third_by_operation[cell.operation(1, 0).operation_id]
        == first_by_operation[cell.operation(1, 0).operation_id]
    )
    third_boundary = runtime.advance_to(2, advance_id="advance-a06-r3-boundary")
    assert third_boundary.snapshot.simulated_time_seconds == 2
    final = runtime.advance_to(20, advance_id="advance-a06-final").snapshot

    attempts_by_operation: dict[str, list[Any]] = {}
    for attempt in final.attempts:
        attempts_by_operation.setdefault(attempt.operation_id, []).append(attempt)
    assert all(len(items) == 1 for items in attempts_by_operation.values())
    unaffected = attempts_by_operation[cell.operation(1, 0).operation_id][0]
    moved = attempts_by_operation[cell.operation(2, 0).operation_id][0]
    assert unaffected.attempt_id == unaffected_id
    assert (unaffected.actual_start_seconds, unaffected.actual_end_seconds) == (0, 10)
    assert moved.attempt_id == movable_id
    assert (moved.actual_start_seconds, moved.actual_end_seconds) == (7, 17)

    actual_windows = sorted(
        (
            attempt.machine_id,
            attempt.actual_start_seconds,
            attempt.actual_end_seconds,
        )
        for attempt in final.attempts
    )
    assert actual_windows == [
        ("mill-a", 0, 10),
        ("mill-b", 7, 17),
        ("qc", 10, 12),
        ("qc", 17, 19),
    ]
    events = _event_rows(repository, cell.snapshot.run_id)
    starts = Counter(
        event.payload["attempt_id"]
        for event in events
        if event.event_type is EventType.OPERATION_STARTED
    )
    completions = Counter(
        event.payload["attempt_id"]
        for event in events
        if event.event_type is EventType.OPERATION_COMPLETED
    )
    assert set(starts.values()) == {1}
    assert set(completions.values()) == {1}
    assert set(starts) == set(completions) == {item.attempt_id for item in final.attempts}


@pytest.mark.parametrize("rebuild", [False, True], ids=["synchronized", "rebuilt"])
def test_a06_authoritative_calendar_change_invalidates_future_start_callback(
    rebuild: bool,
    tiny_cell_factory: Callable[..., Any],
    candidate_factory: Callable[..., CandidateProposal],
    repository_factory: Callable[..., SQLiteStateRepository],
) -> None:
    """A callback valid at activation must recheck the latest persisted calendar."""

    suffix = "rebuilt" if rebuild else "synchronized"
    cell = tiny_cell_factory(quantity=1, horizon=20, run_id=f"run-s05-a06-calendar-{suffix}")
    repository = repository_factory(cell.snapshot, f"a06-calendar-{suffix}")
    runtime = SimulationRuntime(repository, cell.snapshot.run_id, cell.catalog, seed=61)
    candidate = candidate_factory(
        cell.snapshot,
        (
            (cell.operation(1, 0), "mill-a", 5, 8),
            (cell.operation(1, 1), "qc", 8, 10),
        ),
        proposal_id=f"proposal-a06-calendar-{suffix}",
        makespan=10,
    )
    activated = runtime.activate_plan(
        candidate,
        idempotency_key=f"activate-a06-calendar-{suffix}",
    )
    scheduled = _attempt_for_operation(activated.snapshot, cell.operation(1, 0).operation_id)
    assert scheduled.status is AttemptStatus.SCHEDULED
    assert scheduled.scheduled_window == TimeWindow(start_seconds=5, end_seconds=8)

    # These are independent, legitimate S02 mutations. The restore makes the machine
    # AVAILABLE again but replaces its authoritative calendar with only [0,2).
    at_zero = repository.get_checkpoint(cell.snapshot.run_id)
    down = MachineDownPayload(
        machine_id="mill-a",
        status="down",
        availability_windows=(TimeWindow(start_seconds=0, end_seconds=1),),
    )
    time_one = TimeAdvancedPayload(target_time_seconds=1)
    repository.apply_mutation(
        run_id=cell.snapshot.run_id,
        expected_state_version=at_zero.snapshot.state_version,
        idempotency_key=f"external-a06-down-{suffix}",
        command_name="test_authoritative_machine_down",
        command_payload={"machine_id": "mill-a", "at_seconds": 1},
        events=(
            _draft(1, EventType.MACHINE_DOWN, down),
            _draft(1, EventType.TIME_ADVANCED, time_one),
        ),
    )
    at_one = repository.get_checkpoint(cell.snapshot.run_id)
    restored = MachineRestoredPayload(
        machine_id="mill-a",
        status="available",
        availability_windows=(TimeWindow(start_seconds=0, end_seconds=2),),
    )
    time_two = TimeAdvancedPayload(target_time_seconds=2)
    repository.apply_mutation(
        run_id=cell.snapshot.run_id,
        expected_state_version=at_one.snapshot.state_version,
        idempotency_key=f"external-a06-restore-{suffix}",
        command_name="test_authoritative_machine_restore",
        command_payload={"machine_id": "mill-a", "at_seconds": 2},
        events=(
            _draft(2, EventType.MACHINE_RESTORED, restored),
            _draft(2, EventType.TIME_ADVANCED, time_two),
        ),
    )

    if rebuild:
        runtime = SimulationRuntime.rebuild(
            repository,
            cell.snapshot.run_id,
            cell.catalog,
            seed=61,
        )
    result = runtime.advance_to(5, advance_id=f"advance-a06-calendar-{suffix}")
    attempt = _attempt_for_operation(result.snapshot, cell.operation(1, 0).operation_id)
    assert result.snapshot.simulated_time_seconds == 5
    assert attempt == scheduled
    assert attempt.status is AttemptStatus.SCHEDULED
    assert not any(
        event.event_type is EventType.OPERATION_STARTED
        and event.payload["attempt_id"] == attempt.attempt_id
        for event in _event_rows(repository, cell.snapshot.run_id)
    )
    assert any(
        item.attempt_id == attempt.attempt_id and item.reason.value == "outside_availability_window"
        for item in result.projection.blocked_attempts
    )


def test_a08_completion_fault_start_priority_and_duplicate_command_are_idempotent(
    tiny_cell_factory: Callable[..., Any],
    candidate_factory: Callable[..., CandidateProposal],
    repository_factory: Callable[..., SQLiteStateRepository],
) -> None:
    cell = tiny_cell_factory(
        quantity=2,
        milling_seconds=5,
        inspection_seconds=2,
        horizon=15,
    )
    repository = repository_factory(cell.snapshot, "a08")
    runtime = SimulationRuntime(repository, cell.snapshot.run_id, cell.catalog, seed=5)
    candidate = candidate_factory(
        cell.snapshot,
        (
            (cell.operation(1, 0), "mill-a", 0, 5),
            (cell.operation(1, 1), "qc", 5, 7),
            (cell.operation(2, 0), "mill-a", 5, 10),
            (cell.operation(2, 1), "qc", 10, 12),
        ),
        proposal_id="proposal-a08",
        makespan=12,
    )
    runtime.activate_plan(candidate, idempotency_key="activate-a08")
    intervention = _fault("fault-a08-boundary", 5, "mill-a")
    first = runtime.advance_to(
        5,
        advance_id="advance-a08-boundary",
        interventions=(intervention,),
    )
    event_count = len(repository.list_events(cell.snapshot.run_id))
    duplicate = runtime.advance_to(
        5,
        advance_id="advance-a08-boundary",
        interventions=(intervention,),
    )
    assert duplicate.idempotent
    assert duplicate.snapshot == first.snapshot
    assert len(repository.list_events(cell.snapshot.run_id)) == event_count

    at_five = tuple(
        event.event_type
        for event in _event_rows(repository, cell.snapshot.run_id)
        if event.simulated_time_seconds == 5
    )
    assert at_five == (
        EventType.OPERATION_COMPLETED,
        EventType.MACHINE_DOWN,
        EventType.OPERATION_STARTED,
        EventType.OBSERVATION_RECORDED,
        EventType.OBSERVATION_RECORDED,
        EventType.TIME_ADVANCED,
    )
    milling_attempt = _attempt_for_operation(first.snapshot, cell.operation(1, 0).operation_id)
    inspection_attempt = _attempt_for_operation(first.snapshot, cell.operation(1, 1).operation_id)
    next_milling_attempt = _attempt_for_operation(first.snapshot, cell.operation(2, 0).operation_id)
    assert milling_attempt.status is AttemptStatus.COMPLETED
    assert milling_attempt.actual_end_seconds == 5
    assert inspection_attempt.status is AttemptStatus.RUNNING
    assert inspection_attempt.actual_start_seconds == 5
    assert next_milling_attempt.status is AttemptStatus.SCHEDULED
    assert next_milling_attempt.actual_start_seconds is None
    assert not any(
        event.event_type is EventType.OPERATION_INTERRUPTED
        for event in _event_rows(repository, cell.snapshot.run_id)
    )
    assert any(
        item.attempt_id == next_milling_attempt.attempt_id and item.reason.value == "machine_down"
        for item in first.projection.blocked_attempts
    )


def test_a08_new_fault_at_an_already_closed_current_second_is_rejected_atomically(
    tiny_cell_factory: Callable[..., Any],
    candidate_factory: Callable[..., CandidateProposal],
    repository_factory: Callable[..., SQLiteStateRepository],
) -> None:
    cell = tiny_cell_factory(
        quantity=1,
        milling_seconds=10,
        inspection_seconds=2,
        horizon=20,
        run_id="run-s05-a08-closed",
    )
    repository = repository_factory(cell.snapshot, "a08-closed")
    runtime = SimulationRuntime(repository, cell.snapshot.run_id, cell.catalog, seed=5)
    candidate = candidate_factory(
        cell.snapshot,
        (
            (cell.operation(1, 0), "mill-a", 0, 10),
            (cell.operation(1, 1), "qc", 10, 12),
        ),
        proposal_id="proposal-a08-closed",
        makespan=12,
    )
    runtime.activate_plan(candidate, idempotency_key="activate-a08-closed")
    at_five = runtime.advance_to(5, advance_id="advance-a08-close-five")
    running = _attempt_for_operation(at_five.snapshot, cell.operation(1, 0).operation_id)
    assert running.status is AttemptStatus.RUNNING
    assert at_five.snapshot.simulated_time_seconds == 5
    checkpoint_before = repository.get_checkpoint(cell.snapshot.run_id)
    events_before = repository.list_events(cell.snapshot.run_id)

    with pytest.raises(BoundaryClosedError):
        runtime.advance_to(
            5,
            advance_id="advance-a08-late-fault",
            interventions=(_fault("fault-a08-too-late", 5, "mill-a"),),
        )

    assert repository.get_checkpoint(cell.snapshot.run_id) == checkpoint_before
    assert repository.list_events(cell.snapshot.run_id) == events_before
    assert runtime.snapshot == checkpoint_before.snapshot
    still_running = _attempt_for_operation(runtime.snapshot, cell.operation(1, 0).operation_id)
    assert still_running == running


def test_a09_down_only_capable_resource_blocks_predecessor_without_invented_repair(
    tiny_cell_factory: Callable[..., Any],
    candidate_factory: Callable[..., CandidateProposal],
    repository_factory: Callable[..., SQLiteStateRepository],
) -> None:
    cell = tiny_cell_factory(
        quantity=1,
        milling_seconds=10,
        inspection_seconds=2,
        horizon=20,
    )
    repository = repository_factory(cell.snapshot, "a09")
    runtime = SimulationRuntime(repository, cell.snapshot.run_id, cell.catalog, seed=19)
    candidate = candidate_factory(
        cell.snapshot,
        (
            (cell.operation(1, 0), "mill-a", 0, 10),
            (cell.operation(1, 1), "qc", 10, 12),
        ),
        proposal_id="proposal-a09",
        makespan=12,
    )
    runtime.activate_plan(candidate, idempotency_key="activate-a09")
    runtime.advance_to(
        4,
        advance_id="advance-a09-fault",
        interventions=(_fault("fault-a09", 4, "mill-a"),),
    )
    result = runtime.advance_to(20, advance_id="advance-a09-horizon")
    final = result.snapshot

    mill_attempt = _attempt_for_operation(final, cell.operation(1, 0).operation_id)
    qc_attempt = _attempt_for_operation(final, cell.operation(1, 1).operation_id)
    qc_operation = next(
        item for item in final.operations if item.operation_id == cell.operation(1, 1).operation_id
    )
    mill_machine = next(item for item in final.machines if item.machine_id == "mill-a")
    assert mill_attempt.status is AttemptStatus.INTERRUPTED
    assert mill_attempt.consumed_seconds == 4
    assert qc_attempt.status is AttemptStatus.SCHEDULED
    assert qc_attempt.actual_start_seconds is None
    assert qc_operation.status in (OperationStatus.PENDING, OperationStatus.BLOCKED)
    assert final.orders[0].status is OrderStatus.BLOCKED
    assert mill_machine.status is MachineStatus.DOWN
    events = _event_rows(repository, cell.snapshot.run_id)
    assert not any(event.event_type is EventType.MACHINE_RESTORED for event in events)
    assert not any(
        event.event_type is EventType.OPERATION_STARTED
        and event.payload["attempt_id"] == qc_attempt.attempt_id
        for event in events
    )
    assert not any(
        event.event_type is EventType.OPERATION_COMPLETED
        and event.payload["attempt_id"] == mill_attempt.attempt_id
        for event in events
    )
    assert any(
        item.attempt_id == qc_attempt.attempt_id and item.reason.value == "predecessor_incomplete"
        for item in result.projection.blocked_attempts
    )


def test_a09_explicit_restoration_preserves_calendar_and_enables_future_start(
    tiny_cell_factory: Callable[..., Any],
    candidate_factory: Callable[..., CandidateProposal],
    repository_factory: Callable[..., SQLiteStateRepository],
) -> None:
    cell = tiny_cell_factory(quantity=1, horizon=15, run_id="run-s05-a09-restore")
    repository = repository_factory(cell.snapshot, "a09-restore")
    runtime = SimulationRuntime(repository, cell.snapshot.run_id, cell.catalog, seed=23)
    candidate = candidate_factory(
        cell.snapshot,
        (
            (cell.operation(1, 0), "mill-a", 5, 8),
            (cell.operation(1, 1), "qc", 8, 10),
        ),
        proposal_id="proposal-a09-restore",
        makespan=10,
    )
    runtime.activate_plan(candidate, idempotency_key="activate-a09-restore")
    original_calendar = next(
        item.availability_windows for item in cell.snapshot.machines if item.machine_id == "mill-a"
    )

    before_restore = runtime.advance_to(
        2,
        advance_id="advance-a09-before-restore",
        interventions=(_fault("fault-a09-restore", 1, "mill-a"),),
    )
    mill = next(item for item in before_restore.snapshot.machines if item.machine_id == "mill-a")
    attempt = _attempt_for_operation(before_restore.snapshot, cell.operation(1, 0).operation_id)
    assert mill.status is MachineStatus.DOWN
    assert mill.availability_windows == original_calendar
    assert attempt.status is AttemptStatus.SCHEDULED
    assert not any(
        event.event_type is EventType.MACHINE_RESTORED
        for event in _event_rows(repository, cell.snapshot.run_id)
    )

    restored = runtime.advance_to(
        3,
        advance_id="advance-a09-explicit-restore",
        interventions=(_restore("restore-a09", 3, "mill-a"),),
    )
    mill = next(item for item in restored.snapshot.machines if item.machine_id == "mill-a")
    assert mill.status is MachineStatus.AVAILABLE
    assert mill.availability_windows == original_calendar
    restoration_events = [
        event
        for event in _event_rows(repository, cell.snapshot.run_id)
        if event.event_type is EventType.MACHINE_RESTORED
    ]
    assert [
        (event.simulated_time_seconds, event.payload["machine_id"]) for event in restoration_events
    ] == [(3, "mill-a")]

    at_start = runtime.advance_to(5, advance_id="advance-a09-restored-start")
    attempt = _attempt_for_operation(at_start.snapshot, cell.operation(1, 0).operation_id)
    assert attempt.status is AttemptStatus.RUNNING
    assert attempt.actual_start_seconds == 5


def test_a11_invalid_capability_and_stale_plan_leave_checkpoint_and_events_unchanged(
    tiny_cell_factory: Callable[..., Any],
    candidate_factory: Callable[..., CandidateProposal],
    repository_factory: Callable[..., SQLiteStateRepository],
) -> None:
    invalid_cell = tiny_cell_factory(quantity=1, run_id="run-s05-a11-invalid")
    invalid_repo = repository_factory(invalid_cell.snapshot, "a11-invalid")
    invalid_runtime = SimulationRuntime(
        invalid_repo,
        invalid_cell.snapshot.run_id,
        invalid_cell.catalog,
        seed=1,
    )
    invalid = candidate_factory(
        invalid_cell.snapshot,
        (
            (invalid_cell.operation(1, 0), "drill-only", 0, 3),
            (invalid_cell.operation(1, 1), "qc", 3, 5),
        ),
        proposal_id="proposal-a11-invalid",
        makespan=5,
    )
    checkpoint_before = invalid_repo.get_checkpoint(invalid_cell.snapshot.run_id)
    events_before = invalid_repo.list_events(invalid_cell.snapshot.run_id)
    with pytest.raises(InvalidPlanError) as invalid_error:
        invalid_runtime.activate_plan(invalid, idempotency_key="activate-a11-invalid")
    assert any(
        violation.code.value == "capability_mismatch"
        for violation in invalid_error.value.violations
    )
    assert invalid_repo.get_checkpoint(invalid_cell.snapshot.run_id) == checkpoint_before
    assert invalid_repo.list_events(invalid_cell.snapshot.run_id) == events_before

    stale_cell = tiny_cell_factory(quantity=1, run_id="run-s05-a11-stale")
    stale_repo = repository_factory(stale_cell.snapshot, "a11-stale")
    stale_runtime = SimulationRuntime(
        stale_repo,
        stale_cell.snapshot.run_id,
        stale_cell.catalog,
        seed=1,
    )
    stale = candidate_factory(
        stale_cell.snapshot,
        (
            (stale_cell.operation(1, 0), "mill-a", 2, 5),
            (stale_cell.operation(1, 1), "qc", 5, 7),
        ),
        proposal_id="proposal-a11-stale",
        makespan=7,
    )
    stale_runtime.advance_to(1, advance_id="advance-a11-stale")
    checkpoint_before = stale_repo.get_checkpoint(stale_cell.snapshot.run_id)
    events_before = stale_repo.list_events(stale_cell.snapshot.run_id)
    with pytest.raises(StalePlanError) as stale_error:
        stale_runtime.activate_plan(
            stale,
            idempotency_key="activate-a11-stale",
            effective_time_seconds=2,
        )
    assert any(
        violation.code.value in {"base_state_version_mismatch", "base_snapshot_hash_mismatch"}
        for violation in stale_error.value.violations
    )
    assert stale_repo.get_checkpoint(stale_cell.snapshot.run_id) == checkpoint_before
    assert stale_repo.list_events(stale_cell.snapshot.run_id) == events_before
