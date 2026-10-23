"""S03-A03 through A06 hand-built schedule and recovery counterexamples."""

from __future__ import annotations

from typing import Any

from shopfloor_pilot.domain.canonical import canonical_hash
from shopfloor_pilot.domain.models import (
    AttemptStatus,
    Capability,
    Machine,
    MachineStatus,
    Operation,
    OperationalSnapshot,
    OperationAttempt,
    OperationStatus,
    ScheduleAssignment,
    TimeWindow,
)
from shopfloor_pilot.validation import ViolationCode, validate_candidate, validate_schedule


def _copy[ModelT](model: ModelT, **updates: Any) -> ModelT:
    data = model.model_dump(mode="python")  # type: ignore[attr-defined]
    data.update(updates)
    return type(model).model_validate(data)


def _codes(report: Any) -> tuple[ViolationCode, ...]:
    return tuple(item.code for item in report.violations)


def _operation(
    operations: tuple[Operation, ...], unit_index: int, sequence_index: int
) -> Operation:
    return next(
        item
        for item in operations
        if item.unit_index == unit_index and item.sequence_index == sequence_index
    )


def _assignment(
    assignments: tuple[ScheduleAssignment, ...], operation_id: str
) -> ScheduleAssignment:
    return next(item for item in assignments if item.operation_id == operation_id)


def _replace_assignment(
    assignments: tuple[ScheduleAssignment, ...],
    operation_id: str,
    **updates: Any,
) -> tuple[ScheduleAssignment, ...]:
    return tuple(
        _copy(item, **updates) if item.operation_id == operation_id else item
        for item in assignments
    )


def test_a03_hand_calculated_adjacent_tardy_schedule_is_valid(legal_case: Any) -> None:
    snapshot_before = canonical_hash(legal_case.snapshot)
    candidate_before = canonical_hash(legal_case.candidate)
    report = validate_candidate(
        snapshot=legal_case.snapshot,
        catalog=legal_case.catalog,
        candidate=legal_case.candidate,
    )

    assert report.is_valid
    assert report.violations == ()
    assert report.total_tardiness_seconds == 400
    assert report.makespan_seconds == 2400
    assert len(report.projected_operations) == 6
    assert len(report.projected_attempts) == 6
    first_mill = _operation(legal_case.operations, 1, 0)
    second_mill = _operation(legal_case.operations, 2, 0)
    first = _assignment(legal_case.assignments, first_mill.operation_id)
    second = _assignment(legal_case.assignments, second_mill.operation_id)
    assert first.window.end_seconds == second.window.start_seconds == 900
    assert not first.window.overlaps(second.window)
    assert canonical_hash(legal_case.snapshot) == snapshot_before
    assert canonical_hash(legal_case.candidate) == candidate_before


def test_a04_missing_duplicate_unknown_extra_and_attempt_duplicates_are_identified(
    legal_case: Any,
) -> None:
    first = legal_case.assignments[0]
    missing = validate_schedule(
        snapshot=legal_case.snapshot,
        catalog=legal_case.catalog,
        assignments=legal_case.assignments[1:],
    )
    duplicate = validate_schedule(
        snapshot=legal_case.snapshot,
        catalog=legal_case.catalog,
        assignments=(*legal_case.assignments, first),
    )
    unknown_assignment = _copy(first, operation_id="op-unknown-s03")
    unknown = validate_schedule(
        snapshot=legal_case.snapshot,
        catalog=legal_case.catalog,
        assignments=(unknown_assignment, *legal_case.assignments[1:]),
    )

    rogue_operation = Operation(
        operation_id="op-unapproved-extra-s03",
        order_id=legal_case.snapshot.orders[0].order_id,
        unit_index=1,
        operation_template_id="unapproved-extra-template",
        sequence_index=99,
        required_capability=first_machine_capability(legal_case.snapshot, "CNC02"),
        duration_seconds=60,
    )
    snapshot_with_extra = _copy(
        legal_case.snapshot,
        operations=(rogue_operation,),
    )
    extra_assignment = ScheduleAssignment(
        operation_id=rogue_operation.operation_id,
        attempt_id="attempt-unapproved-extra-s03",
        machine_id="CNC02",
        window=TimeWindow(start_seconds=2400, end_seconds=2460),
    )
    extra = validate_schedule(
        snapshot=snapshot_with_extra,
        catalog=legal_case.catalog,
        assignments=(*legal_case.assignments, extra_assignment),
    )

    second = legal_case.assignments[1]
    reused_attempt = _copy(second, attempt_id=first.attempt_id)
    duplicate_attempt = validate_schedule(
        snapshot=legal_case.snapshot,
        catalog=legal_case.catalog,
        assignments=(first, reused_attempt, *legal_case.assignments[2:]),
    )

    assert ViolationCode.MISSING_OPERATION in _codes(missing)
    assert first.operation_id in next(
        item.operation_ids
        for item in missing.violations
        if item.code is ViolationCode.MISSING_OPERATION
    )
    assert ViolationCode.DUPLICATE_OPERATION in _codes(duplicate)
    assert ViolationCode.UNKNOWN_OPERATION in _codes(unknown)
    assert unknown_assignment.operation_id in next(
        item.operation_ids
        for item in unknown.violations
        if item.code is ViolationCode.UNKNOWN_OPERATION
    )
    assert ViolationCode.EXTRA_OPERATION in _codes(extra)
    assert rogue_operation.operation_id in next(
        item.operation_ids
        for item in extra.violations
        if item.code is ViolationCode.EXTRA_OPERATION
    )
    assert ViolationCode.DUPLICATE_ATTEMPT in _codes(duplicate_attempt)


def first_machine_capability(snapshot: OperationalSnapshot, machine_id: str):
    return next(item for item in snapshot.machines if item.machine_id == machine_id).capabilities[0]


def test_a05_capability_duration_overlap_and_precedence_each_have_a_counterexample(
    legal_case: Any,
) -> None:
    first_mill = _operation(legal_case.operations, 1, 0)
    first_drill = _operation(legal_case.operations, 1, 1)
    second_mill = _operation(legal_case.operations, 2, 0)

    wrong_machine = _replace_assignment(
        legal_case.assignments,
        first_mill.operation_id,
        machine_id="CNC01",
    )
    short_window = _replace_assignment(
        legal_case.assignments,
        first_mill.operation_id,
        window=TimeWindow(start_seconds=0, end_seconds=899),
    )
    overlapping = _replace_assignment(
        legal_case.assignments,
        second_mill.operation_id,
        window=TimeWindow(start_seconds=899, end_seconds=1799),
    )
    reversed_precedence = _replace_assignment(
        legal_case.assignments,
        first_drill.operation_id,
        window=TimeWindow(start_seconds=899, end_seconds=1319),
    )

    reports = (
        (
            validate_schedule(
                snapshot=legal_case.snapshot,
                catalog=legal_case.catalog,
                assignments=wrong_machine,
            ),
            ViolationCode.CAPABILITY_MISMATCH,
        ),
        (
            validate_schedule(
                snapshot=legal_case.snapshot,
                catalog=legal_case.catalog,
                assignments=short_window,
            ),
            ViolationCode.DURATION_MISMATCH,
        ),
        (
            validate_schedule(
                snapshot=legal_case.snapshot,
                catalog=legal_case.catalog,
                assignments=overlapping,
            ),
            ViolationCode.MACHINE_OVERLAP,
        ),
        (
            validate_schedule(
                snapshot=legal_case.snapshot,
                catalog=legal_case.catalog,
                assignments=reversed_precedence,
            ),
            ViolationCode.PRECEDENCE_VIOLATION,
        ),
    )
    for report, code in reports:
        assert not report.is_valid
        assert code in _codes(report)


def test_a05_release_current_time_calendar_and_horizon_each_have_a_counterexample(
    legal_case: Any,
) -> None:
    order = legal_case.snapshot.orders[0]
    released_later = _copy(order, release_time_seconds=100)
    release_snapshot = _copy(legal_case.snapshot, orders=(released_later,))
    past_snapshot = _copy(legal_case.snapshot, simulated_time_seconds=100)

    split_machines: list[Machine] = []
    for machine in legal_case.snapshot.machines:
        if machine.machine_id == "CNC02":
            split_machines.append(
                _copy(
                    machine,
                    availability_windows=(
                        TimeWindow(start_seconds=0, end_seconds=400),
                        TimeWindow(start_seconds=500, end_seconds=3000),
                    ),
                )
            )
        else:
            split_machines.append(machine)
    calendar_snapshot = _copy(legal_case.snapshot, machines=tuple(split_machines))
    horizon_snapshot = _copy(legal_case.snapshot, hard_horizon_seconds=2399)

    reports = (
        (
            validate_schedule(
                snapshot=release_snapshot,
                catalog=legal_case.catalog,
                assignments=legal_case.assignments,
            ),
            ViolationCode.RELEASE_TIME_VIOLATION,
        ),
        (
            validate_schedule(
                snapshot=past_snapshot,
                catalog=legal_case.catalog,
                assignments=legal_case.assignments,
            ),
            ViolationCode.START_BEFORE_SNAPSHOT,
        ),
        (
            validate_schedule(
                snapshot=calendar_snapshot,
                catalog=legal_case.catalog,
                assignments=legal_case.assignments,
            ),
            ViolationCode.CALENDAR_VIOLATION,
        ),
        (
            validate_schedule(
                snapshot=horizon_snapshot,
                catalog=legal_case.catalog,
                assignments=legal_case.assignments,
            ),
            ViolationCode.HARD_HORIZON_VIOLATION,
        ),
    )
    for report, code in reports:
        assert not report.is_valid
        assert code in _codes(report)


def test_a05_down_machine_is_unavailable_even_with_stale_nominal_windows(
    legal_case: Any,
) -> None:
    machines = tuple(
        _copy(machine, status=MachineStatus.DOWN) if machine.machine_id == "CNC02" else machine
        for machine in legal_case.snapshot.machines
    )
    snapshot = _copy(legal_case.snapshot, machines=machines)
    report = validate_schedule(
        snapshot=snapshot,
        catalog=legal_case.catalog,
        assignments=legal_case.assignments,
    )
    assert ViolationCode.CALENDAR_VIOLATION in _codes(report)


def test_a05_snapshot_capabilities_are_authoritative_not_only_catalog_capabilities(
    legal_case: Any,
) -> None:
    machines = tuple(
        _copy(machine, capabilities=(Capability.SLOTTING,))
        if machine.machine_id == "CNC02"
        else machine
        for machine in legal_case.snapshot.machines
    )
    snapshot = _copy(legal_case.snapshot, machines=machines)
    report = validate_schedule(
        snapshot=snapshot,
        catalog=legal_case.catalog,
        assignments=legal_case.assignments,
    )
    assert ViolationCode.CAPABILITY_MISMATCH in _codes(report)


def test_a06_completed_and_unaffected_running_operations_cannot_be_rescheduled(
    history_case: Any,
) -> None:
    valid = validate_candidate(
        snapshot=history_case.snapshot,
        catalog=history_case.catalog,
        candidate=history_case.candidate,
    )
    completed_operation = _operation(history_case.operations, 1, 0)
    running_operation = _operation(history_case.operations, 1, 1)
    completed_move = ScheduleAssignment(
        operation_id=completed_operation.operation_id,
        attempt_id="attempt-illegal-completed-move",
        machine_id="CNC03",
        window=TimeWindow(start_seconds=1000, end_seconds=1900),
    )
    running_move = ScheduleAssignment(
        operation_id=running_operation.operation_id,
        attempt_id="attempt-illegal-running-move",
        machine_id="CNC03",
        window=TimeWindow(start_seconds=1000, end_seconds=1420),
    )
    moved_completed = validate_schedule(
        snapshot=history_case.snapshot,
        catalog=history_case.catalog,
        assignments=(*history_case.assignments, completed_move),
    )
    moved_running = validate_schedule(
        snapshot=history_case.snapshot,
        catalog=history_case.catalog,
        assignments=(*history_case.assignments, running_move),
    )

    assert valid.is_valid
    assert ViolationCode.COMPLETED_OPERATION_RESCHEDULED in _codes(moved_completed)
    assert ViolationCode.RUNNING_OPERATION_RESCHEDULED in _codes(moved_running)


def test_a06_interruption_requires_fresh_full_duration_restart_and_preserves_history(
    recovery_case: Any,
) -> None:
    before = canonical_hash(recovery_case.snapshot)
    valid = validate_candidate(
        snapshot=recovery_case.snapshot,
        catalog=recovery_case.catalog,
        candidate=recovery_case.candidate,
    )
    mill = _operation(recovery_case.operations, 1, 0)
    shortened = _replace_assignment(
        recovery_case.assignments,
        mill.operation_id,
        window=TimeWindow(start_seconds=300, end_seconds=900),
    )
    shortened_report = validate_schedule(
        snapshot=recovery_case.snapshot,
        catalog=recovery_case.catalog,
        assignments=shortened,
    )
    reused = _replace_assignment(
        recovery_case.assignments,
        mill.operation_id,
        attempt_id="attempt-recovery-mill-1",
    )
    reused_report = validate_schedule(
        snapshot=recovery_case.snapshot,
        catalog=recovery_case.catalog,
        assignments=reused,
    )

    assert valid.is_valid
    restart = next(
        item
        for item in valid.projected_attempts
        if item.operation_id == mill.operation_id and item.status.value == "scheduled"
    )
    assert restart.planned_duration_seconds == mill.duration_seconds == 900
    assert restart.attempt_number == 2
    assert restart.restart_of_attempt_id == "attempt-recovery-mill-1"
    assert ViolationCode.RESTART_DURATION_MISMATCH in _codes(shortened_report)
    assert ViolationCode.ATTEMPT_ID_CONFLICT in _codes(reused_report)
    assert canonical_hash(recovery_case.snapshot) == before


def test_a06_running_occupancy_uses_actual_start_plus_full_duration(
    history_case: Any,
) -> None:
    completed, running = history_case.snapshot.attempts
    delayed_running = _copy(
        running,
        actual_start_seconds=950,
        consumed_seconds=50,
    )
    snapshot = _copy(
        history_case.snapshot,
        attempts=(completed, delayed_running),
    )
    report = validate_schedule(
        snapshot=snapshot,
        catalog=history_case.catalog,
        assignments=history_case.assignments,
    )
    assert ViolationCode.PRECEDENCE_VIOLATION in _codes(report)
    violation = next(
        item for item in report.violations if item.code is ViolationCode.PRECEDENCE_VIOLATION
    )
    assert violation.windows[0] == TimeWindow(start_seconds=950, end_seconds=1370)


def test_a06_replaceable_scheduled_attempt_can_be_retained_without_new_generation(
    history_case: Any,
) -> None:
    inspection = _operation(history_case.operations, 1, 2)
    assignment = _assignment(history_case.assignments, inspection.operation_id)
    scheduled = OperationAttempt(
        attempt_id=assignment.attempt_id,
        operation_id=inspection.operation_id,
        attempt_number=1,
        machine_id=assignment.machine_id,
        planned_duration_seconds=inspection.duration_seconds,
        status=AttemptStatus.SCHEDULED,
        scheduled_window=assignment.window,
    )
    snapshot = _copy(
        history_case.snapshot,
        attempts=(*history_case.snapshot.attempts, scheduled),
    )
    report = validate_schedule(
        snapshot=snapshot,
        catalog=history_case.catalog,
        assignments=history_case.assignments,
    )
    assert report.is_valid
    retained = next(
        item for item in report.projected_attempts if item.attempt_id == scheduled.attempt_id
    )
    assert retained.attempt_number == 1


def test_a06_ready_recovery_with_applied_scheduled_restart_is_valid(
    recovery_case: Any,
) -> None:
    mill = _operation(recovery_case.operations, 1, 0)
    restart_assignment = _assignment(recovery_case.assignments, mill.operation_id)
    operations = tuple(
        _copy(operation, status=OperationStatus.READY)
        if operation.operation_id == mill.operation_id
        else operation
        for operation in recovery_case.snapshot.operations
    )
    scheduled_restart = OperationAttempt(
        attempt_id=restart_assignment.attempt_id,
        operation_id=mill.operation_id,
        attempt_number=2,
        machine_id=restart_assignment.machine_id,
        planned_duration_seconds=mill.duration_seconds,
        status=AttemptStatus.SCHEDULED,
        scheduled_window=restart_assignment.window,
        restart_of_attempt_id="attempt-recovery-mill-1",
    )
    snapshot = _copy(
        recovery_case.snapshot,
        operations=operations,
        attempts=(*recovery_case.snapshot.attempts, scheduled_restart),
    )

    report = validate_schedule(
        snapshot=snapshot,
        catalog=recovery_case.catalog,
        assignments=recovery_case.assignments,
    )

    assert report.is_valid
    retained = next(
        item
        for item in report.projected_attempts
        if item.attempt_id == scheduled_restart.attempt_id
    )
    assert retained == scheduled_restart


def test_a06_fixed_lifecycle_requires_backing_attempt_history(history_case: Any) -> None:
    snapshot = _copy(
        history_case.snapshot,
        attempts=(history_case.snapshot.attempts[0],),
    )
    report = validate_schedule(
        snapshot=snapshot,
        catalog=history_case.catalog,
        assignments=history_case.assignments,
    )
    assert ViolationCode.FIXED_HISTORY_MISMATCH in _codes(report)
    running = _operation(history_case.operations, 1, 1)
    assert running.operation_id in next(
        item.operation_ids
        for item in report.violations
        if item.code is ViolationCode.FIXED_HISTORY_MISMATCH
    )


def test_a06_overlapping_fixed_actual_history_fails_closed(history_case: Any) -> None:
    completed, running = history_case.snapshot.attempts
    overlapping_running = _copy(
        running,
        machine_id="CNC02",
        actual_start_seconds=800,
        consumed_seconds=200,
    )
    machines = tuple(
        _copy(
            machine,
            status=MachineStatus.RUNNING,
            capabilities=(Capability.MILLING, Capability.SLOTTING, Capability.DRILLING),
        )
        if machine.machine_id == "CNC02"
        else _copy(machine, status=MachineStatus.AVAILABLE)
        if machine.machine_id == "CNC01"
        else machine
        for machine in history_case.snapshot.machines
    )
    snapshot = _copy(
        history_case.snapshot,
        machines=machines,
        attempts=(completed, overlapping_running),
    )
    report = validate_schedule(
        snapshot=snapshot,
        catalog=history_case.catalog,
        assignments=history_case.assignments,
    )
    assert ViolationCode.MACHINE_OVERLAP in _codes(report)


def test_a06_completed_and_interrupted_history_must_fit_machine_calendars(
    history_case: Any,
    recovery_case: Any,
) -> None:
    completed_machines = tuple(
        _copy(
            machine,
            availability_windows=(TimeWindow(start_seconds=900, end_seconds=3000),),
        )
        if machine.machine_id == "CNC02"
        else machine
        for machine in history_case.snapshot.machines
    )
    completed_outside_calendar = validate_schedule(
        snapshot=_copy(history_case.snapshot, machines=completed_machines),
        catalog=history_case.catalog,
        assignments=history_case.assignments,
    )

    interrupted_machines = tuple(
        _copy(
            machine,
            availability_windows=(TimeWindow(start_seconds=300, end_seconds=3000),),
        )
        if machine.machine_id == "CNC02"
        else machine
        for machine in recovery_case.snapshot.machines
    )
    interrupted_outside_calendar = validate_schedule(
        snapshot=_copy(recovery_case.snapshot, machines=interrupted_machines),
        catalog=recovery_case.catalog,
        assignments=recovery_case.assignments,
    )

    for report, attempt_id in (
        (completed_outside_calendar, "attempt-history-mill-1"),
        (interrupted_outside_calendar, "attempt-recovery-mill-1"),
    ):
        violation = next(
            item
            for item in report.violations
            if item.code is ViolationCode.CALENDAR_VIOLATION and item.attempt_ids == (attempt_id,)
        )
        assert violation.field == "snapshot.attempts"


def test_a06_fixed_predecessor_must_finish_before_fixed_successor(
    history_case: Any,
) -> None:
    completed, running = history_case.snapshot.attempts
    late_predecessor = _copy(
        completed,
        actual_start_seconds=100,
        actual_end_seconds=1000,
    )
    snapshot = _copy(
        history_case.snapshot,
        attempts=(late_predecessor, running),
    )
    report = validate_schedule(
        snapshot=snapshot,
        catalog=history_case.catalog,
        assignments=history_case.assignments,
    )

    assert ViolationCode.PRECEDENCE_VIOLATION in _codes(report)
    violation = next(
        item for item in report.violations if item.code is ViolationCode.PRECEDENCE_VIOLATION
    )
    assert violation.windows[:2] == (
        TimeWindow(start_seconds=100, end_seconds=1000),
        TimeWindow(start_seconds=900, end_seconds=1320),
    )


def test_a06_interrupted_history_cannot_hide_a_precedence_violation(
    history_case: Any,
) -> None:
    completed, running = history_case.snapshot.attempts
    interrupted = _copy(
        running,
        status=AttemptStatus.INTERRUPTED,
        actual_start_seconds=800,
        actual_end_seconds=850,
        consumed_seconds=50,
    )
    drill = _operation(history_case.operations, 1, 1)
    inspection = _operation(history_case.operations, 1, 2)
    operations = tuple(
        _copy(operation, status=OperationStatus.INTERRUPTED)
        if operation.operation_id == drill.operation_id
        else operation
        for operation in history_case.snapshot.operations
    )
    machines = tuple(
        _copy(machine, status=MachineStatus.DOWN) if machine.machine_id == "CNC01" else machine
        for machine in history_case.snapshot.machines
    )
    restart = ScheduleAssignment(
        operation_id=drill.operation_id,
        attempt_id="attempt-history-drill-2",
        machine_id="CNC03",
        window=TimeWindow(start_seconds=1000, end_seconds=1420),
    )
    inspection_assignment = _copy(
        _assignment(history_case.assignments, inspection.operation_id),
        window=TimeWindow(start_seconds=1420, end_seconds=1600),
    )
    snapshot = _copy(
        history_case.snapshot,
        machines=machines,
        operations=operations,
        attempts=(completed, interrupted),
    )

    report = validate_schedule(
        snapshot=snapshot,
        catalog=history_case.catalog,
        assignments=(restart, inspection_assignment),
    )

    violation = next(
        item
        for item in report.violations
        if item.code is ViolationCode.PRECEDENCE_VIOLATION
        and item.references.get("reason") == "fixed_interruption_precedes_completion"
    )
    assert violation.attempt_ids == (
        "attempt-history-mill-1",
        "attempt-history-drill-1",
    )
    assert violation.windows == (
        TimeWindow(start_seconds=0, end_seconds=900),
        TimeWindow(start_seconds=800, end_seconds=850),
    )


def test_a06_machine_running_state_and_running_attempt_are_bidirectional(
    history_case: Any,
    legal_case: Any,
) -> None:
    available_running_machine = tuple(
        _copy(machine, status=MachineStatus.AVAILABLE) if machine.machine_id == "CNC01" else machine
        for machine in history_case.snapshot.machines
    )
    running_attempt_without_running_machine = validate_schedule(
        snapshot=_copy(history_case.snapshot, machines=available_running_machine),
        catalog=history_case.catalog,
        assignments=history_case.assignments,
    )

    ghost_running_machine = tuple(
        _copy(machine, status=MachineStatus.RUNNING) if machine.machine_id == "CNC03" else machine
        for machine in legal_case.snapshot.machines
    )
    running_machine_without_attempt = validate_schedule(
        snapshot=_copy(legal_case.snapshot, machines=ghost_running_machine),
        catalog=legal_case.catalog,
        assignments=legal_case.assignments,
    )

    for report, machine_id in (
        (running_attempt_without_running_machine, "CNC01"),
        (running_machine_without_attempt, "CNC03"),
    ):
        violation = next(
            item
            for item in report.violations
            if item.code is ViolationCode.FIXED_HISTORY_MISMATCH
            and item.field == "snapshot.machines.status"
        )
        assert violation.machine_ids == (machine_id,)


def test_a06_assigned_blocked_work_is_projected_to_startable_pending_state(
    legal_case: Any,
) -> None:
    first = legal_case.operations[0]
    operations = tuple(
        _copy(operation, status=OperationStatus.BLOCKED)
        if operation.operation_id == first.operation_id
        else operation
        for operation in legal_case.operations
    )
    snapshot = _copy(legal_case.snapshot, operations=operations)
    report = validate_schedule(
        snapshot=snapshot,
        catalog=legal_case.catalog,
        assignments=legal_case.assignments,
    )
    assert report.is_valid
    projected = next(
        item for item in report.projected_operations if item.operation_id == first.operation_id
    )
    assert projected.status is OperationStatus.PENDING
