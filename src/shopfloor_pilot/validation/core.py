"""Pure validation of hand-built schedules and version-bound candidates.

This module deliberately derives the required work from process routes and checks it
without importing a solver, persistence, event-reduction, or application-service code.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass

from shopfloor_pilot.domain.canonical import canonical_hash
from shopfloor_pilot.domain.models import (
    AttemptStatus,
    CandidateProposal,
    DomainCatalog,
    Machine,
    MachineStatus,
    Operation,
    OperationalSnapshot,
    OperationAttempt,
    OperationStatus,
    Order,
    OrderDelta,
    OrderDeltaKind,
    OrderStatus,
    ResourceDelta,
    ResourceDeltaKind,
    ScheduleAssignment,
    TimeWindow,
)
from shopfloor_pilot.planning import expand_process_route

from .models import ScheduleViolation, ValidationReport, ViolationCode


@dataclass(frozen=True)
class _RouteProjection:
    orders: tuple[Order, ...]
    operations: tuple[Operation, ...]
    order_by_id: dict[str, Order]
    operation_by_id: dict[str, Operation]


@dataclass(frozen=True)
class _MachineUse:
    machine_id: str
    operation_id: str
    attempt_id: str
    window: TimeWindow


_FIXED_ATTEMPT_STATUSES = {
    AttemptStatus.RUNNING,
    AttemptStatus.COMPLETED,
    AttemptStatus.INTERRUPTED,
    AttemptStatus.CANCELLED,
}


def _violation(
    code: ViolationCode,
    message: str,
    *,
    operation_ids: tuple[str, ...] = (),
    attempt_ids: tuple[str, ...] = (),
    machine_ids: tuple[str, ...] = (),
    windows: tuple[TimeWindow, ...] = (),
    field: str | None = None,
    references: dict[str, str] | None = None,
) -> ScheduleViolation:
    return ScheduleViolation(
        code=code,
        message=message,
        operation_ids=operation_ids,
        attempt_ids=attempt_ids,
        machine_ids=machine_ids,
        windows=windows,
        field=field,
        references={} if references is None else references,
    )


def _violation_key(item: ScheduleViolation) -> tuple[object, ...]:
    windows = tuple((window.start_seconds, window.end_seconds) for window in item.windows)
    return (
        item.code.value,
        not item.operation_ids,
        item.operation_ids,
        item.attempt_ids,
        item.machine_ids,
        windows,
        item.field or "",
        tuple(sorted(item.references.items())),
        item.message,
    )


def _sorted_violations(
    violations: Sequence[ScheduleViolation],
) -> tuple[ScheduleViolation, ...]:
    return tuple(sorted(violations, key=_violation_key))


def _validate_order_deltas(
    snapshot: OperationalSnapshot,
    catalog: DomainCatalog,
    deltas: tuple[OrderDelta, ...],
    violations: list[ScheduleViolation],
) -> tuple[Order, ...]:
    orders = list(snapshot.orders)
    known_ids = {order.order_id for order in orders}
    delta_counts = Counter(delta.order.order_id for delta in deltas)

    for order_id in sorted(order_id for order_id, count in delta_counts.items() if count > 1):
        violations.append(
            _violation(
                ViolationCode.INVALID_ORDER_DELTA,
                "an order may appear in at most one projected delta",
                field="projected_order_deltas",
                references={"order_id": order_id, "reason": "duplicate_order_delta"},
            )
        )

    for delta in sorted(deltas, key=lambda item: (item.order.order_id, item.kind.value)):
        order = delta.order
        if delta.kind is not OrderDeltaKind.ADD:
            violations.append(
                _violation(
                    ViolationCode.INVALID_ORDER_DELTA,
                    "the finite validator supports order additions only",
                    field="projected_order_deltas",
                    references={"order_id": order.order_id, "kind": delta.kind.value},
                )
            )
            continue
        if delta_counts[order.order_id] > 1:
            continue
        if order.order_id in known_ids:
            violations.append(
                _violation(
                    ViolationCode.INVALID_ORDER_DELTA,
                    "an added order_id must be fresh in the base snapshot",
                    field="projected_order_deltas",
                    references={"order_id": order.order_id, "reason": "order_id_conflict"},
                )
            )
            continue
        if order.status is not OrderStatus.PENDING:
            violations.append(
                _violation(
                    ViolationCode.INVALID_ORDER_DELTA,
                    "an added order must enter the finite workflow with status=pending",
                    field="projected_order_deltas",
                    references={
                        "order_id": order.order_id,
                        "actual_status": order.status.value,
                    },
                )
            )
            continue
        expansion = expand_process_route(order, catalog)
        if expansion.order is None:
            violations.append(
                _violation(
                    ViolationCode.INVALID_ORDER_DELTA,
                    "the added order does not select a supported finite process route",
                    field="projected_order_deltas",
                    references={
                        "order_id": order.order_id,
                        "route_issue_codes": ",".join(
                            issue.code.value for issue in expansion.failures
                        ),
                    },
                )
            )
            continue
        known_ids.add(order.order_id)
        orders.append(order)
    return tuple(orders)


def _validate_resource_deltas(
    snapshot: OperationalSnapshot,
    catalog: DomainCatalog,
    deltas: tuple[ResourceDelta, ...],
    violations: list[ScheduleViolation],
) -> dict[str, int]:
    snapshot_machines = {machine.machine_id: machine for machine in snapshot.machines}
    catalog_machine_ids = {machine.machine_id for machine in catalog.machines}
    delta_counts = Counter(delta.machine_id for delta in deltas)
    down_from: dict[str, int] = {}

    for machine_id in sorted(machine_id for machine_id, count in delta_counts.items() if count > 1):
        violations.append(
            _violation(
                ViolationCode.INVALID_RESOURCE_DELTA,
                "a machine may appear in at most one projected resource delta",
                machine_ids=(machine_id,),
                field="projected_resource_deltas",
                references={"reason": "duplicate_resource_delta"},
            )
        )

    for delta in sorted(deltas, key=lambda item: (item.machine_id, item.kind.value)):
        machine_id = delta.machine_id
        if delta.kind is ResourceDeltaKind.RESTORE:
            violations.append(
                _violation(
                    ViolationCode.RESOURCE_RESTORE_FORBIDDEN,
                    "a proposal cannot assert that a machine has been repaired",
                    machine_ids=(machine_id,),
                    field="projected_resource_deltas",
                )
            )
            continue
        if delta.kind is not ResourceDeltaKind.MARK_DOWN:
            violations.append(
                _violation(
                    ViolationCode.INVALID_RESOURCE_DELTA,
                    "standalone availability replacement is outside the finite delta contract",
                    machine_ids=(machine_id,),
                    field="projected_resource_deltas",
                    references={"kind": delta.kind.value},
                )
            )
            continue
        if delta_counts[machine_id] > 1:
            continue
        machine = snapshot_machines.get(machine_id)
        if machine is None or machine_id not in catalog_machine_ids:
            violations.append(
                _violation(
                    ViolationCode.INVALID_RESOURCE_DELTA,
                    "mark_down references a machine outside the supplied snapshot/catalog",
                    machine_ids=(machine_id,),
                    field="projected_resource_deltas",
                    references={"reason": "unknown_machine"},
                )
            )
            continue
        if machine.status is not MachineStatus.AVAILABLE:
            violations.append(
                _violation(
                    ViolationCode.INVALID_RESOURCE_DELTA,
                    "mark_down requires a currently available machine",
                    machine_ids=(machine_id,),
                    field="projected_resource_deltas",
                    references={"current_status": machine.status.value},
                )
            )
            continue
        if delta.new_status is not MachineStatus.DOWN or delta.availability_windows is not None:
            violations.append(
                _violation(
                    ViolationCode.INVALID_RESOURCE_DELTA,
                    "mark_down must set status=down and cannot replace availability windows",
                    machine_ids=(machine_id,),
                    field="projected_resource_deltas",
                )
            )
            continue
        if delta.effective_time_seconds != snapshot.simulated_time_seconds:
            violations.append(
                _violation(
                    ViolationCode.INVALID_RESOURCE_DELTA,
                    "mark_down must take effect at the current snapshot event boundary",
                    machine_ids=(machine_id,),
                    field="projected_resource_deltas",
                    references={
                        "effective_time_seconds": str(delta.effective_time_seconds),
                        "snapshot_time_seconds": str(snapshot.simulated_time_seconds),
                    },
                )
            )
            continue
        down_from[machine_id] = delta.effective_time_seconds
    return down_from


def _derive_routes(
    orders: tuple[Order, ...],
    catalog: DomainCatalog,
    violations: list[ScheduleViolation],
) -> _RouteProjection:
    operations: list[Operation] = []
    valid_orders: list[Order] = []
    for order in sorted(orders, key=lambda item: item.order_id):
        if order.status is OrderStatus.REJECTED:
            continue
        expansion = expand_process_route(order, catalog)
        if expansion.order is None:
            violations.append(
                _violation(
                    ViolationCode.EXTRA_OPERATION,
                    "snapshot order cannot be represented by a supported process route",
                    field="snapshot.orders",
                    references={
                        "order_id": order.order_id,
                        "route_issue_codes": ",".join(
                            issue.code.value for issue in expansion.failures
                        ),
                    },
                )
            )
            continue
        valid_orders.append(order)
        operations.extend(expansion.operations)
    ordered_operations = tuple(
        sorted(
            operations,
            key=lambda item: (
                item.order_id,
                item.unit_index,
                item.sequence_index,
                item.operation_id,
            ),
        )
    )
    return _RouteProjection(
        orders=tuple(valid_orders),
        operations=ordered_operations,
        order_by_id={order.order_id: order for order in valid_orders},
        operation_by_id={operation.operation_id: operation for operation in ordered_operations},
    )


def _validate_existing_operations(
    snapshot: OperationalSnapshot,
    projection: _RouteProjection,
    violations: list[ScheduleViolation],
) -> dict[str, Operation]:
    existing_by_id = {operation.operation_id: operation for operation in snapshot.operations}
    for operation in sorted(snapshot.operations, key=lambda item: item.operation_id):
        expected = projection.operation_by_id.get(operation.operation_id)
        if expected is None:
            violations.append(
                _violation(
                    ViolationCode.EXTRA_OPERATION,
                    "snapshot contains an operation not derived from an approved order route",
                    operation_ids=(operation.operation_id,),
                    field="snapshot.operations",
                    references={"order_id": operation.order_id},
                )
            )
            continue
        identity_fields = (
            "order_id",
            "unit_index",
            "operation_template_id",
            "sequence_index",
        )
        mismatched = [
            field
            for field in identity_fields
            if getattr(operation, field) != getattr(expected, field)
        ]
        if mismatched:
            violations.append(
                _violation(
                    ViolationCode.EXTRA_OPERATION,
                    "snapshot operation identity fields do not match its deterministic route",
                    operation_ids=(operation.operation_id,),
                    field="snapshot.operations",
                    references={"mismatched_fields": ",".join(mismatched)},
                )
            )
        if operation.required_capability is not expected.required_capability:
            violations.append(
                _violation(
                    ViolationCode.CAPABILITY_MISMATCH,
                    "snapshot operation capability does not match the predefined route",
                    operation_ids=(operation.operation_id,),
                    field="snapshot.operations.required_capability",
                    references={
                        "expected": expected.required_capability.value,
                        "actual": operation.required_capability.value,
                    },
                )
            )
        if operation.duration_seconds != expected.duration_seconds:
            violations.append(
                _violation(
                    ViolationCode.DURATION_MISMATCH,
                    "snapshot operation duration does not match the predefined route",
                    operation_ids=(operation.operation_id,),
                    field="snapshot.operations.duration_seconds",
                    references={
                        "expected_seconds": str(expected.duration_seconds),
                        "actual_seconds": str(operation.duration_seconds),
                    },
                )
            )
        if operation.predecessor_operation_ids != expected.predecessor_operation_ids:
            violations.append(
                _violation(
                    ViolationCode.PRECEDENCE_VIOLATION,
                    "snapshot operation predecessors do not match the predefined route",
                    operation_ids=(operation.operation_id, *operation.predecessor_operation_ids),
                    field="snapshot.operations.predecessor_operation_ids",
                    references={
                        "expected_predecessors": ",".join(expected.predecessor_operation_ids),
                        "actual_predecessors": ",".join(operation.predecessor_operation_ids),
                    },
                )
            )
    return existing_by_id


def _validate_order_lifecycle(
    projection: _RouteProjection,
    existing_by_id: dict[str, Operation],
    violations: list[ScheduleViolation],
) -> None:
    for order in projection.orders:
        expected = [
            operation for operation in projection.operations if operation.order_id == order.order_id
        ]
        existing = [
            existing_by_id[operation.operation_id]
            for operation in expected
            if operation.operation_id in existing_by_id
        ]
        route_is_materialized_and_complete = len(existing) == len(expected) and all(
            operation.status is OperationStatus.COMPLETED for operation in existing
        )
        reasons: list[str] = []
        if order.status is OrderStatus.COMPLETED and not route_is_materialized_and_complete:
            reasons.append("completed_order_has_unfinished_or_missing_route_operations")
        if order.status is not OrderStatus.COMPLETED and route_is_materialized_and_complete:
            reasons.append("completed_route_has_noncompleted_order_status")
        if not reasons:
            continue
        violations.append(
            _violation(
                ViolationCode.FIXED_HISTORY_MISMATCH,
                "order lifecycle status and its route-operation history disagree",
                operation_ids=tuple(operation.operation_id for operation in expected),
                field="snapshot.orders.status",
                references={
                    "order_id": order.order_id,
                    "order_status": order.status.value,
                    "reasons": ",".join(reasons),
                },
            )
        )


def _fixed_attempt_window(attempt: OperationAttempt) -> TimeWindow | None:
    if attempt.actual_start_seconds is not None and attempt.actual_end_seconds is not None:
        return TimeWindow(
            start_seconds=attempt.actual_start_seconds,
            end_seconds=attempt.actual_end_seconds,
        )
    if attempt.status is AttemptStatus.RUNNING:
        if attempt.actual_start_seconds is not None:
            return TimeWindow(
                start_seconds=attempt.actual_start_seconds,
                end_seconds=attempt.actual_start_seconds + attempt.planned_duration_seconds,
            )
    return None


def _fixed_uses(snapshot: OperationalSnapshot) -> tuple[_MachineUse, ...]:
    uses: list[_MachineUse] = []
    for attempt in snapshot.attempts:
        if attempt.status not in _FIXED_ATTEMPT_STATUSES:
            continue
        window = _fixed_attempt_window(attempt)
        if window is None:
            continue
        uses.append(
            _MachineUse(
                machine_id=attempt.machine_id,
                operation_id=attempt.operation_id,
                attempt_id=attempt.attempt_id,
                window=window,
            )
        )
    return tuple(uses)


def _validate_fixed_history(
    snapshot: OperationalSnapshot,
    projection: _RouteProjection,
    existing_by_id: dict[str, Operation],
    violations: list[ScheduleViolation],
) -> None:
    attempts_by_operation: dict[str, list[OperationAttempt]] = {}
    for attempt in snapshot.attempts:
        attempts_by_operation.setdefault(attempt.operation_id, []).append(attempt)

    for expected in projection.operations:
        existing = existing_by_id.get(expected.operation_id)
        if existing is None:
            continue
        all_attempts = sorted(
            attempts_by_operation.get(existing.operation_id, []),
            key=lambda item: (item.attempt_number, item.attempt_id),
        )
        completed = [
            attempt for attempt in all_attempts if attempt.status is AttemptStatus.COMPLETED
        ]
        running = [attempt for attempt in all_attempts if attempt.status is AttemptStatus.RUNNING]
        interrupted = [
            attempt for attempt in all_attempts if attempt.status is AttemptStatus.INTERRUPTED
        ]
        scheduled = [
            attempt for attempt in all_attempts if attempt.status is AttemptStatus.SCHEDULED
        ]
        fixed = [
            attempt for attempt in all_attempts if attempt.status is not AttemptStatus.SCHEDULED
        ]
        execution_history = [
            attempt
            for attempt in fixed
            if attempt.status
            in (AttemptStatus.COMPLETED, AttemptStatus.RUNNING, AttemptStatus.INTERRUPTED)
        ]
        reasons: list[str] = []
        attempt_by_id = {attempt.attempt_id: attempt for attempt in all_attempts}
        attempt_number_counts = Counter(attempt.attempt_number for attempt in all_attempts)
        if any(count > 1 for count in attempt_number_counts.values()):
            reasons.append("duplicate_attempt_number")
        attempt_numbers = sorted(attempt_number_counts)
        if attempt_numbers and attempt_numbers != list(range(1, attempt_numbers[-1] + 1)):
            reasons.append("noncontiguous_attempt_numbers")
        if len(scheduled) > 1:
            reasons.append("multiple_scheduled_attempts")

        for attempt in all_attempts:
            prior_interrupted = [
                predecessor
                for predecessor in interrupted
                if predecessor.attempt_number < attempt.attempt_number
            ]
            expected_restart = (
                max(
                    prior_interrupted,
                    key=lambda item: (item.attempt_number, item.attempt_id),
                ).attempt_id
                if prior_interrupted
                else None
            )
            if attempt.restart_of_attempt_id != expected_restart:
                reasons.append("attempt_restart_lineage_mismatch")
            if attempt.restart_of_attempt_id is not None:
                restart_target = attempt_by_id.get(attempt.restart_of_attempt_id)
                successor_start = attempt.actual_start_seconds
                if successor_start is None and attempt.scheduled_window is not None:
                    successor_start = attempt.scheduled_window.start_seconds
                if (
                    restart_target is not None
                    and restart_target.actual_end_seconds is not None
                    and successor_start is not None
                    and successor_start < restart_target.actual_end_seconds
                ):
                    reasons.append("restart_precedes_interruption_end")

        execution_windows = sorted(
            (
                (attempt, window)
                for attempt in execution_history
                if (window := _fixed_attempt_window(attempt)) is not None
            ),
            key=lambda item: (
                item[1].start_seconds,
                item[1].end_seconds,
                item[0].attempt_id,
            ),
        )
        for index, (_, left_window) in enumerate(execution_windows):
            if any(
                left_window.overlaps(right_window)
                for _, right_window in execution_windows[index + 1 :]
            ):
                reasons.append("overlapping_operation_generations")
                break

        expected_status = {
            OperationStatus.COMPLETED: AttemptStatus.COMPLETED,
            OperationStatus.RUNNING: AttemptStatus.RUNNING,
            OperationStatus.INTERRUPTED: AttemptStatus.INTERRUPTED,
        }.get(existing.status)
        if expected_status is not None:
            matching = [
                attempt for attempt in execution_history if attempt.status is expected_status
            ]
            if not matching:
                reasons.append(f"missing_{expected_status.value}_attempt")
            if (
                expected_status in (AttemptStatus.COMPLETED, AttemptStatus.RUNNING)
                and len(matching) != 1
            ):
                reasons.append(f"invalid_{expected_status.value}_attempt_count")
            if execution_history and execution_history[-1].status is not expected_status:
                reasons.append("latest_execution_status_mismatch")
        if existing.status is not OperationStatus.COMPLETED and completed:
            reasons.append("completed_attempt_for_noncompleted_operation")
        if existing.status is not OperationStatus.RUNNING and running:
            reasons.append("running_attempt_for_nonrunning_operation")
        fixed_max = max((attempt.attempt_number for attempt in fixed), default=0)
        latest_interrupted = (
            max(interrupted, key=lambda item: (item.attempt_number, item.attempt_id))
            if interrupted
            else None
        )
        has_valid_scheduled_restart = (
            existing.status in (OperationStatus.PENDING, OperationStatus.READY)
            and len(scheduled) == 1
            and latest_interrupted is not None
            and scheduled[0].attempt_number == fixed_max + 1
            and scheduled[0].restart_of_attempt_id == latest_interrupted.attempt_id
        )
        if (
            existing.status is not OperationStatus.INTERRUPTED
            and execution_history
            and execution_history[-1].status is AttemptStatus.INTERRUPTED
            and not has_valid_scheduled_restart
        ):
            reasons.append("latest_interruption_for_noninterrupted_operation")
        if existing.status in (OperationStatus.COMPLETED, OperationStatus.RUNNING) and scheduled:
            reasons.append("scheduled_attempt_for_fixed_operation")

        for attempt in scheduled:
            if attempt.attempt_number != fixed_max + 1:
                reasons.append("scheduled_attempt_number_not_next_generation")
            expected_restart = (
                latest_interrupted.attempt_id if latest_interrupted is not None else None
            )
            if attempt.restart_of_attempt_id != expected_restart:
                reasons.append("scheduled_restart_lineage_mismatch")
        if not reasons:
            continue
        violations.append(
            _violation(
                ViolationCode.FIXED_HISTORY_MISMATCH,
                "operation lifecycle and attempt-generation history disagree",
                operation_ids=(existing.operation_id,),
                attempt_ids=tuple(attempt.attempt_id for attempt in all_attempts),
                machine_ids=tuple(sorted({attempt.machine_id for attempt in all_attempts})),
                windows=tuple(
                    window
                    for window in (_fixed_attempt_window(attempt) for attempt in all_attempts)
                    if window is not None
                ),
                field="snapshot.attempts",
                references={
                    "operation_status": existing.status.value,
                    "reasons": ",".join(sorted(set(reasons))),
                },
            )
        )

    running_by_machine: dict[str, list[OperationAttempt]] = {}
    for attempt in snapshot.attempts:
        if attempt.status is AttemptStatus.RUNNING:
            running_by_machine.setdefault(attempt.machine_id, []).append(attempt)
    for machine in sorted(snapshot.machines, key=lambda item: item.machine_id):
        running = sorted(
            running_by_machine.get(machine.machine_id, []),
            key=lambda item: (item.attempt_number, item.attempt_id),
        )
        expected_count = 1 if machine.status is MachineStatus.RUNNING else 0
        if len(running) == expected_count:
            continue
        violations.append(
            _violation(
                ViolationCode.FIXED_HISTORY_MISMATCH,
                "machine running status and fixed running-attempt occupancy disagree",
                operation_ids=tuple(attempt.operation_id for attempt in running),
                attempt_ids=tuple(attempt.attempt_id for attempt in running),
                machine_ids=(machine.machine_id,),
                windows=tuple(
                    window
                    for window in (_fixed_attempt_window(attempt) for attempt in running)
                    if window is not None
                ),
                field="snapshot.machines.status",
                references={
                    "machine_status": machine.status.value,
                    "expected_running_attempt_count": str(expected_count),
                    "actual_running_attempt_count": str(len(running)),
                },
            )
        )


def _validate_fixed_constraints(
    snapshot: OperationalSnapshot,
    catalog: DomainCatalog,
    projection: _RouteProjection,
    violations: list[ScheduleViolation],
) -> None:
    snapshot_machines = {machine.machine_id: machine for machine in snapshot.machines}
    catalog_machines = {machine.machine_id: machine for machine in catalog.machines}
    for attempt in sorted(snapshot.attempts, key=lambda item: item.attempt_id):
        if attempt.status not in _FIXED_ATTEMPT_STATUSES:
            continue
        window = _fixed_attempt_window(attempt)
        if window is None:
            continue
        operation = projection.operation_by_id.get(attempt.operation_id)
        if operation is None:
            continue
        order = projection.order_by_id[operation.order_id]
        machine = snapshot_machines.get(attempt.machine_id)
        catalog_machine = catalog_machines.get(attempt.machine_id)
        if machine is None or catalog_machine is None:
            violations.append(
                _violation(
                    ViolationCode.UNKNOWN_MACHINE,
                    "fixed attempt references a machine outside the supplied snapshot/catalog",
                    operation_ids=(operation.operation_id,),
                    attempt_ids=(attempt.attempt_id,),
                    machine_ids=(attempt.machine_id,),
                    windows=(window,),
                    field="snapshot.attempts.machine_id",
                )
            )
            continue
        if (
            operation.required_capability not in machine.capabilities
            or operation.required_capability not in catalog_machine.capabilities
        ):
            violations.append(
                _violation(
                    ViolationCode.CAPABILITY_MISMATCH,
                    "fixed attempt machine lacks the route-required capability",
                    operation_ids=(operation.operation_id,),
                    attempt_ids=(attempt.attempt_id,),
                    machine_ids=(attempt.machine_id,),
                    windows=(window,),
                    field="snapshot.attempts.machine_id",
                    references={
                        "required_capability": operation.required_capability.value,
                        "snapshot_capabilities": ",".join(
                            item.value for item in machine.capabilities
                        ),
                        "catalog_capabilities": ",".join(
                            item.value for item in catalog_machine.capabilities
                        ),
                    },
                )
            )
        if not _is_in_calendar(machine, window):
            violations.append(
                _violation(
                    ViolationCode.CALENDAR_VIOLATION,
                    "fixed attempt is outside the explicit machine availability calendar",
                    operation_ids=(operation.operation_id,),
                    attempt_ids=(attempt.attempt_id,),
                    machine_ids=(attempt.machine_id,),
                    windows=(window,),
                    field="snapshot.attempts",
                )
            )
        if window.start_seconds < order.release_time_seconds:
            violations.append(
                _violation(
                    ViolationCode.RELEASE_TIME_VIOLATION,
                    "fixed attempt starts before its order release time",
                    operation_ids=(operation.operation_id,),
                    attempt_ids=(attempt.attempt_id,),
                    machine_ids=(attempt.machine_id,),
                    windows=(window,),
                    field="snapshot.attempts",
                    references={"release_time_seconds": str(order.release_time_seconds)},
                )
            )
        if window.end_seconds > snapshot.hard_horizon_seconds:
            violations.append(
                _violation(
                    ViolationCode.HARD_HORIZON_VIOLATION,
                    "fixed attempt extends beyond the hard planning horizon",
                    operation_ids=(operation.operation_id,),
                    attempt_ids=(attempt.attempt_id,),
                    machine_ids=(attempt.machine_id,),
                    windows=(window,),
                    field="snapshot.attempts",
                    references={"hard_horizon_seconds": str(snapshot.hard_horizon_seconds)},
                )
            )
        history_after_snapshot = (
            attempt.status in (AttemptStatus.COMPLETED, AttemptStatus.INTERRUPTED)
            and window.end_seconds > snapshot.simulated_time_seconds
        )
        running_outside_present = attempt.status is AttemptStatus.RUNNING and not (
            window.start_seconds <= snapshot.simulated_time_seconds < window.end_seconds
        )
        if history_after_snapshot or running_outside_present:
            violations.append(
                _violation(
                    ViolationCode.FIXED_HISTORY_MISMATCH,
                    "fixed attempt timing is inconsistent with the snapshot time",
                    operation_ids=(operation.operation_id,),
                    attempt_ids=(attempt.attempt_id,),
                    machine_ids=(attempt.machine_id,),
                    windows=(window,),
                    field="snapshot.simulated_time_seconds",
                    references={
                        "attempt_status": attempt.status.value,
                        "snapshot_time_seconds": str(snapshot.simulated_time_seconds),
                    },
                )
            )


def _validate_assignment_identity(
    snapshot: OperationalSnapshot,
    assignments: tuple[ScheduleAssignment, ...],
    projection: _RouteProjection,
    existing_by_id: dict[str, Operation],
    violations: list[ScheduleViolation],
) -> dict[str, ScheduleAssignment]:
    operation_counts = Counter(assignment.operation_id for assignment in assignments)
    attempt_counts = Counter(assignment.attempt_id for assignment in assignments)
    snapshot_attempt_by_id = {attempt.attempt_id: attempt for attempt in snapshot.attempts}

    for operation_id in sorted(
        operation_id for operation_id, count in operation_counts.items() if count > 1
    ):
        matches = [item for item in assignments if item.operation_id == operation_id]
        violations.append(
            _violation(
                ViolationCode.DUPLICATE_OPERATION,
                "a future operation must have exactly one assignment",
                operation_ids=(operation_id,),
                attempt_ids=tuple(sorted(item.attempt_id for item in matches)),
                windows=tuple(
                    item.window
                    for item in sorted(
                        matches,
                        key=lambda item: (
                            item.window.start_seconds,
                            item.window.end_seconds,
                            item.attempt_id,
                        ),
                    )
                ),
                field="assignments",
            )
        )
    for attempt_id in sorted(
        attempt_id for attempt_id, count in attempt_counts.items() if count > 1
    ):
        matches = [item for item in assignments if item.attempt_id == attempt_id]
        violations.append(
            _violation(
                ViolationCode.DUPLICATE_ATTEMPT,
                "a candidate attempt_id must identify exactly one future assignment",
                operation_ids=tuple(sorted(item.operation_id for item in matches)),
                attempt_ids=(attempt_id,),
                machine_ids=tuple(sorted({item.machine_id for item in matches})),
                windows=tuple(
                    item.window
                    for item in sorted(
                        matches,
                        key=lambda item: (
                            item.window.start_seconds,
                            item.window.end_seconds,
                            item.operation_id,
                        ),
                    )
                ),
                field="assignments",
            )
        )
    for attempt_id in sorted(set(attempt_counts) & set(snapshot_attempt_by_id)):
        matches = [item for item in assignments if item.attempt_id == attempt_id]
        existing_attempt = snapshot_attempt_by_id[attempt_id]
        if existing_attempt.status is AttemptStatus.SCHEDULED and all(
            item.operation_id == existing_attempt.operation_id for item in matches
        ):
            continue
        violations.append(
            _violation(
                ViolationCode.ATTEMPT_ID_CONFLICT,
                "a candidate attempt_id may only be fresh or retain the same replaceable "
                "scheduled attempt",
                operation_ids=tuple(sorted({item.operation_id for item in matches})),
                attempt_ids=(attempt_id,),
                machine_ids=tuple(sorted({item.machine_id for item in matches})),
                windows=tuple(
                    item.window
                    for item in sorted(
                        matches,
                        key=lambda item: (
                            item.window.start_seconds,
                            item.window.end_seconds,
                            item.operation_id,
                        ),
                    )
                ),
                field="assignments.attempt_id",
                references={
                    "existing_status": existing_attempt.status.value,
                    "existing_operation_id": existing_attempt.operation_id,
                },
            )
        )

    assignment_by_operation: dict[str, ScheduleAssignment] = {}
    for assignment in sorted(
        assignments,
        key=lambda item: (item.operation_id, item.attempt_id, item.machine_id),
    ):
        expected = projection.operation_by_id.get(assignment.operation_id)
        if expected is None:
            violations.append(
                _violation(
                    ViolationCode.UNKNOWN_OPERATION,
                    "assignment references an operation outside the route-derived work set",
                    operation_ids=(assignment.operation_id,),
                    attempt_ids=(assignment.attempt_id,),
                    machine_ids=(assignment.machine_id,),
                    windows=(assignment.window,),
                    field="assignments.operation_id",
                )
            )
            continue
        existing = existing_by_id.get(expected.operation_id)
        if existing is not None and existing.status is OperationStatus.COMPLETED:
            violations.append(
                _violation(
                    ViolationCode.COMPLETED_OPERATION_RESCHEDULED,
                    "completed operation history cannot be rescheduled",
                    operation_ids=(assignment.operation_id,),
                    attempt_ids=(assignment.attempt_id,),
                    machine_ids=(assignment.machine_id,),
                    windows=(assignment.window,),
                    field="assignments.operation_id",
                )
            )
            continue
        if existing is not None and existing.status is OperationStatus.RUNNING:
            violations.append(
                _violation(
                    ViolationCode.RUNNING_OPERATION_RESCHEDULED,
                    "an unaffected running operation is fixed and non-preemptive",
                    operation_ids=(assignment.operation_id,),
                    attempt_ids=(assignment.attempt_id,),
                    machine_ids=(assignment.machine_id,),
                    windows=(assignment.window,),
                    field="assignments.operation_id",
                )
            )
            continue
        if operation_counts[assignment.operation_id] == 1:
            assignment_by_operation[assignment.operation_id] = assignment

    for expected in projection.operations:
        existing = existing_by_id.get(expected.operation_id)
        if existing is not None and existing.status in (
            OperationStatus.COMPLETED,
            OperationStatus.RUNNING,
        ):
            continue
        if operation_counts[expected.operation_id] == 0:
            violations.append(
                _violation(
                    ViolationCode.MISSING_OPERATION,
                    "every unfinished non-running route operation requires one assignment",
                    operation_ids=(expected.operation_id,),
                    field="assignments",
                    references={"order_id": expected.order_id},
                )
            )
    return assignment_by_operation


def _is_in_calendar(machine: Machine, window: TimeWindow) -> bool:
    return any(
        availability.start_seconds <= window.start_seconds
        and window.end_seconds <= availability.end_seconds
        for availability in machine.availability_windows
    )


def _validate_assignment_constraints(
    snapshot: OperationalSnapshot,
    catalog: DomainCatalog,
    projection: _RouteProjection,
    existing_by_id: dict[str, Operation],
    assignment_by_operation: dict[str, ScheduleAssignment],
    down_from: dict[str, int],
    violations: list[ScheduleViolation],
) -> tuple[_MachineUse, ...]:
    snapshot_machines = {machine.machine_id: machine for machine in snapshot.machines}
    catalog_machines = {machine.machine_id: machine for machine in catalog.machines}
    interrupted_operation_ids = {
        attempt.operation_id
        for attempt in snapshot.attempts
        if attempt.status is AttemptStatus.INTERRUPTED
    }
    uses: list[_MachineUse] = []
    for operation_id, assignment in sorted(assignment_by_operation.items()):
        expected = projection.operation_by_id[operation_id]
        existing = existing_by_id.get(operation_id)
        machine = snapshot_machines.get(assignment.machine_id)
        catalog_machine = catalog_machines.get(assignment.machine_id)
        if machine is None or catalog_machine is None:
            violations.append(
                _violation(
                    ViolationCode.UNKNOWN_MACHINE,
                    "assignment references a machine outside the supplied snapshot/catalog",
                    operation_ids=(operation_id,),
                    attempt_ids=(assignment.attempt_id,),
                    machine_ids=(assignment.machine_id,),
                    windows=(assignment.window,),
                    field="assignments.machine_id",
                )
            )
        else:
            if (
                expected.required_capability not in machine.capabilities
                or expected.required_capability not in catalog_machine.capabilities
            ):
                violations.append(
                    _violation(
                        ViolationCode.CAPABILITY_MISMATCH,
                        "assigned machine lacks the route-required capability in the explicit "
                        "snapshot/catalog",
                        operation_ids=(operation_id,),
                        attempt_ids=(assignment.attempt_id,),
                        machine_ids=(assignment.machine_id,),
                        windows=(assignment.window,),
                        field="assignments.machine_id",
                        references={
                            "required_capability": expected.required_capability.value,
                            "snapshot_capabilities": ",".join(
                                item.value for item in machine.capabilities
                            ),
                            "catalog_capabilities": ",".join(
                                item.value for item in catalog_machine.capabilities
                            ),
                        },
                    )
                )
            unavailable_reason: str | None = None
            if machine.status in (MachineStatus.DOWN, MachineStatus.MAINTENANCE):
                unavailable_reason = f"machine_status_{machine.status.value}"
            elif not _is_in_calendar(machine, assignment.window):
                unavailable_reason = "outside_availability_windows"
            elif (
                assignment.machine_id in down_from
                and assignment.window.end_seconds > down_from[assignment.machine_id]
            ):
                unavailable_reason = "overlaps_projected_mark_down"
            if unavailable_reason is not None:
                violations.append(
                    _violation(
                        ViolationCode.CALENDAR_VIOLATION,
                        "assignment is not wholly contained in usable machine time",
                        operation_ids=(operation_id,),
                        attempt_ids=(assignment.attempt_id,),
                        machine_ids=(assignment.machine_id,),
                        windows=(assignment.window,),
                        field="assignments.window",
                        references={"reason": unavailable_reason},
                    )
                )
            uses.append(
                _MachineUse(
                    machine_id=assignment.machine_id,
                    operation_id=operation_id,
                    attempt_id=assignment.attempt_id,
                    window=assignment.window,
                )
            )

        order = projection.order_by_id[expected.order_id]
        if assignment.window.start_seconds < order.release_time_seconds:
            violations.append(
                _violation(
                    ViolationCode.RELEASE_TIME_VIOLATION,
                    "operation starts before its order release time",
                    operation_ids=(operation_id,),
                    attempt_ids=(assignment.attempt_id,),
                    machine_ids=(assignment.machine_id,),
                    windows=(assignment.window,),
                    field="assignments.window.start_seconds",
                    references={"release_time_seconds": str(order.release_time_seconds)},
                )
            )
        if assignment.window.start_seconds < snapshot.simulated_time_seconds:
            violations.append(
                _violation(
                    ViolationCode.START_BEFORE_SNAPSHOT,
                    "future work cannot start before the base snapshot time",
                    operation_ids=(operation_id,),
                    attempt_ids=(assignment.attempt_id,),
                    machine_ids=(assignment.machine_id,),
                    windows=(assignment.window,),
                    field="assignments.window.start_seconds",
                    references={"snapshot_time_seconds": str(snapshot.simulated_time_seconds)},
                )
            )
        if assignment.window.end_seconds > snapshot.hard_horizon_seconds:
            violations.append(
                _violation(
                    ViolationCode.HARD_HORIZON_VIOLATION,
                    "assignment ends beyond the finite hard planning horizon",
                    operation_ids=(operation_id,),
                    attempt_ids=(assignment.attempt_id,),
                    machine_ids=(assignment.machine_id,),
                    windows=(assignment.window,),
                    field="assignments.window.end_seconds",
                    references={"hard_horizon_seconds": str(snapshot.hard_horizon_seconds)},
                )
            )

        if existing is not None and (
            existing.status is OperationStatus.INTERRUPTED
            or operation_id in interrupted_operation_ids
        ):
            if assignment.window.duration_seconds != expected.duration_seconds:
                references = {
                    "expected_full_duration_seconds": str(expected.duration_seconds),
                    "actual_duration_seconds": str(assignment.window.duration_seconds),
                }
                violations.append(
                    _violation(
                        ViolationCode.RESTART_DURATION_MISMATCH,
                        "interrupted work must restart in a fresh full-duration attempt",
                        operation_ids=(operation_id,),
                        attempt_ids=(assignment.attempt_id,),
                        machine_ids=(assignment.machine_id,),
                        windows=(assignment.window,),
                        field="assignments.window",
                        references=references,
                    )
                )
        elif assignment.window.duration_seconds != expected.duration_seconds:
            violations.append(
                _violation(
                    ViolationCode.DURATION_MISMATCH,
                    "assignment must reserve the full predefined operation duration",
                    operation_ids=(operation_id,),
                    attempt_ids=(assignment.attempt_id,),
                    machine_ids=(assignment.machine_id,),
                    windows=(assignment.window,),
                    field="assignments.window",
                    references={
                        "expected_seconds": str(expected.duration_seconds),
                        "actual_seconds": str(assignment.window.duration_seconds),
                    },
                )
            )
    return tuple(uses)


def _validate_overlaps(
    fixed_uses: tuple[_MachineUse, ...],
    candidate_uses: tuple[_MachineUse, ...],
    violations: list[ScheduleViolation],
) -> None:
    by_machine: dict[str, list[_MachineUse]] = {}
    for use in (*fixed_uses, *candidate_uses):
        by_machine.setdefault(use.machine_id, []).append(use)
    for machine_id in sorted(by_machine):
        uses = sorted(
            by_machine[machine_id],
            key=lambda item: (
                item.window.start_seconds,
                item.window.end_seconds,
                item.attempt_id,
                item.operation_id,
            ),
        )
        for index, left in enumerate(uses):
            for right in uses[index + 1 :]:
                if right.window.start_seconds >= left.window.end_seconds:
                    break
                if left.window.overlaps(right.window):
                    violations.append(
                        _violation(
                            ViolationCode.MACHINE_OVERLAP,
                            "half-open machine reservations overlap",
                            operation_ids=(left.operation_id, right.operation_id),
                            attempt_ids=(left.attempt_id, right.attempt_id),
                            machine_ids=(machine_id,),
                            windows=(left.window, right.window),
                            field="assignments.window",
                        )
                    )


def _fixed_completion_use(
    operation_id: str,
    attempts: Sequence[OperationAttempt],
) -> _MachineUse | None:
    fixed = [
        attempt
        for attempt in attempts
        if attempt.operation_id == operation_id
        and attempt.status in (AttemptStatus.COMPLETED, AttemptStatus.RUNNING)
    ]
    if not fixed:
        return None
    latest = max(fixed, key=lambda item: (item.attempt_number, item.attempt_id))
    window = _fixed_attempt_window(latest)
    if window is None:
        return None
    return _MachineUse(
        machine_id=latest.machine_id,
        operation_id=operation_id,
        attempt_id=latest.attempt_id,
        window=window,
    )


def _validate_interrupted_history_precedence(
    snapshot: OperationalSnapshot,
    projection: _RouteProjection,
    violations: list[ScheduleViolation],
) -> None:
    attempts_by_operation: dict[str, list[OperationAttempt]] = {}
    for attempt in snapshot.attempts:
        attempts_by_operation.setdefault(attempt.operation_id, []).append(attempt)

    for operation in projection.operations:
        interrupted_attempts = sorted(
            (
                attempt
                for attempt in attempts_by_operation.get(operation.operation_id, ())
                if attempt.status is AttemptStatus.INTERRUPTED
            ),
            key=lambda item: (item.attempt_number, item.attempt_id),
        )
        for successor_attempt in interrupted_attempts:
            successor_window = _fixed_attempt_window(successor_attempt)
            if successor_window is None:
                continue
            for predecessor_id in operation.predecessor_operation_ids:
                completed_predecessors = sorted(
                    (
                        attempt
                        for attempt in attempts_by_operation.get(predecessor_id, ())
                        if attempt.status is AttemptStatus.COMPLETED
                        and attempt.actual_end_seconds is not None
                    ),
                    key=lambda item: (
                        item.actual_end_seconds,
                        item.attempt_number,
                        item.attempt_id,
                    ),
                )
                if any(
                    attempt.actual_end_seconds <= successor_window.start_seconds
                    for attempt in completed_predecessors
                    if attempt.actual_end_seconds is not None
                ):
                    continue
                predecessor = completed_predecessors[0] if completed_predecessors else None
                predecessor_window = (
                    _fixed_attempt_window(predecessor) if predecessor is not None else None
                )
                violations.append(
                    _violation(
                        ViolationCode.PRECEDENCE_VIOLATION,
                        "interrupted fixed history began before its predecessor completed",
                        operation_ids=(predecessor_id, operation.operation_id),
                        attempt_ids=(
                            *((predecessor.attempt_id,) if predecessor is not None else ()),
                            successor_attempt.attempt_id,
                        ),
                        machine_ids=(
                            *((predecessor.machine_id,) if predecessor is not None else ()),
                            successor_attempt.machine_id,
                        ),
                        windows=(
                            *((predecessor_window,) if predecessor_window is not None else ()),
                            successor_window,
                        ),
                        field="snapshot.attempts",
                        references={"reason": "fixed_interruption_precedes_completion"},
                    )
                )


def _validate_precedence(
    snapshot: OperationalSnapshot,
    projection: _RouteProjection,
    assignment_by_operation: dict[str, ScheduleAssignment],
    violations: list[ScheduleViolation],
) -> dict[str, int]:
    execution_by_operation: dict[str, _MachineUse] = {}
    for operation in projection.operations:
        use = _fixed_completion_use(operation.operation_id, snapshot.attempts)
        if use is not None:
            execution_by_operation[operation.operation_id] = use
    for operation_id, assignment in assignment_by_operation.items():
        execution_by_operation[operation_id] = _MachineUse(
            machine_id=assignment.machine_id,
            operation_id=operation_id,
            attempt_id=assignment.attempt_id,
            window=assignment.window,
        )

    for operation in projection.operations:
        successor = execution_by_operation.get(operation.operation_id)
        if successor is None:
            continue
        for predecessor_id in operation.predecessor_operation_ids:
            predecessor = execution_by_operation.get(predecessor_id)
            if predecessor is None:
                violations.append(
                    _violation(
                        ViolationCode.PRECEDENCE_VIOLATION,
                        "scheduled or fixed successor has no completed predecessor interval",
                        operation_ids=(predecessor_id, operation.operation_id),
                        attempt_ids=(successor.attempt_id,),
                        machine_ids=(successor.machine_id,),
                        windows=(successor.window,),
                        field="assignments.window",
                        references={"reason": "missing_predecessor_completion"},
                    )
                )
                continue
            if successor.window.start_seconds < predecessor.window.end_seconds:
                violations.append(
                    _violation(
                        ViolationCode.PRECEDENCE_VIOLATION,
                        "successor starts before its predecessor completes",
                        operation_ids=(predecessor_id, operation.operation_id),
                        attempt_ids=(predecessor.attempt_id, successor.attempt_id),
                        machine_ids=(predecessor.machine_id, successor.machine_id),
                        windows=(predecessor.window, successor.window),
                        field="assignments.window",
                    )
                )
    return {
        operation_id: use.window.end_seconds for operation_id, use in execution_by_operation.items()
    }


def _calculate_metrics(
    snapshot: OperationalSnapshot,
    projection: _RouteProjection,
    completion_by_operation: dict[str, int],
) -> tuple[int, int]:
    total_tardiness = 0
    for order in projection.orders:
        order_operations = [
            operation for operation in projection.operations if operation.order_id == order.order_id
        ]
        if not order_operations:
            continue
        final_sequence = max(operation.sequence_index for operation in order_operations)
        final_ids = [
            operation.operation_id
            for operation in order_operations
            if operation.sequence_index == final_sequence
        ]
        if all(operation_id in completion_by_operation for operation_id in final_ids):
            completion = max(completion_by_operation[operation_id] for operation_id in final_ids)
            total_tardiness += max(0, completion - order.due_time_seconds) * order.priority_weight
    makespan = max(completion_by_operation.values(), default=snapshot.simulated_time_seconds)
    return total_tardiness, makespan


def _project_valid_plan(
    snapshot: OperationalSnapshot,
    projection: _RouteProjection,
    existing_by_id: dict[str, Operation],
    assignment_by_operation: dict[str, ScheduleAssignment],
) -> tuple[tuple[Operation, ...], tuple[OperationAttempt, ...]]:
    operations: list[Operation] = []
    for expected in projection.operations:
        existing = existing_by_id.get(expected.operation_id)
        if existing is None:
            operations.append(expected)
            continue
        projected_status = (
            OperationStatus.PENDING
            if existing.status is OperationStatus.BLOCKED
            else existing.status
        )
        operations.append(expected.model_copy(update={"status": projected_status}))

    fixed_attempts = [
        attempt for attempt in snapshot.attempts if attempt.status in _FIXED_ATTEMPT_STATUSES
    ]
    attempts_by_operation: dict[str, list[OperationAttempt]] = {}
    for attempt in snapshot.attempts:
        attempts_by_operation.setdefault(attempt.operation_id, []).append(attempt)

    scheduled_attempts: list[OperationAttempt] = []
    for operation in operations:
        assignment = assignment_by_operation.get(operation.operation_id)
        if assignment is None:
            continue
        history = attempts_by_operation.get(operation.operation_id, [])
        retained = next(
            (
                attempt
                for attempt in history
                if attempt.attempt_id == assignment.attempt_id
                and attempt.status is AttemptStatus.SCHEDULED
            ),
            None,
        )
        fixed_history = [
            attempt for attempt in history if attempt.status is not AttemptStatus.SCHEDULED
        ]
        attempt_number = (
            retained.attempt_number
            if retained is not None
            else max((attempt.attempt_number for attempt in fixed_history), default=0) + 1
        )
        interrupted = [
            attempt for attempt in history if attempt.status is AttemptStatus.INTERRUPTED
        ]
        restart_of = None
        if retained is not None:
            restart_of = retained.restart_of_attempt_id
        elif interrupted:
            restart_of = max(
                interrupted,
                key=lambda item: (item.attempt_number, item.attempt_id),
            ).attempt_id
        scheduled_attempts.append(
            OperationAttempt(
                attempt_id=assignment.attempt_id,
                operation_id=operation.operation_id,
                attempt_number=attempt_number,
                machine_id=assignment.machine_id,
                planned_duration_seconds=operation.duration_seconds,
                status=AttemptStatus.SCHEDULED,
                scheduled_window=assignment.window,
                consumed_seconds=0,
                restart_of_attempt_id=restart_of,
            )
        )
    return tuple(operations), tuple((*fixed_attempts, *scheduled_attempts))


def validate_schedule(
    snapshot: OperationalSnapshot,
    catalog: DomainCatalog,
    assignments: Sequence[ScheduleAssignment],
    *,
    projected_order_deltas: Sequence[OrderDelta] = (),
    projected_resource_deltas: Sequence[ResourceDelta] = (),
) -> ValidationReport:
    """Validate future assignments against an explicit immutable snapshot.

    Required operations are always re-derived from the supplied finite route catalog.
    Existing completed and running work is fixed history, so callers provide only future
    assignments. The result never writes to storage or mutates any input model.
    """

    assignment_tuple = tuple(assignments)
    order_delta_tuple = tuple(projected_order_deltas)
    resource_delta_tuple = tuple(projected_resource_deltas)
    violations: list[ScheduleViolation] = []

    orders = _validate_order_deltas(snapshot, catalog, order_delta_tuple, violations)
    down_from = _validate_resource_deltas(snapshot, catalog, resource_delta_tuple, violations)
    projection = _derive_routes(orders, catalog, violations)
    existing_by_id = _validate_existing_operations(snapshot, projection, violations)
    _validate_order_lifecycle(projection, existing_by_id, violations)
    _validate_fixed_history(snapshot, projection, existing_by_id, violations)
    _validate_fixed_constraints(snapshot, catalog, projection, violations)
    assignment_by_operation = _validate_assignment_identity(
        snapshot,
        assignment_tuple,
        projection,
        existing_by_id,
        violations,
    )
    candidate_uses = _validate_assignment_constraints(
        snapshot,
        catalog,
        projection,
        existing_by_id,
        assignment_by_operation,
        down_from,
        violations,
    )
    _validate_overlaps(_fixed_uses(snapshot), candidate_uses, violations)
    _validate_interrupted_history_precedence(snapshot, projection, violations)
    completion_by_operation = _validate_precedence(
        snapshot, projection, assignment_by_operation, violations
    )
    total_tardiness, makespan = _calculate_metrics(snapshot, projection, completion_by_operation)
    ordered_violations = _sorted_violations(violations)
    if ordered_violations:
        return ValidationReport(
            is_valid=False,
            violations=ordered_violations,
            total_tardiness_seconds=total_tardiness,
            makespan_seconds=makespan,
        )

    projected_operations, projected_attempts = _project_valid_plan(
        snapshot,
        projection,
        existing_by_id,
        assignment_by_operation,
    )
    return ValidationReport(
        is_valid=True,
        projected_operations=projected_operations,
        projected_attempts=projected_attempts,
        total_tardiness_seconds=total_tardiness,
        makespan_seconds=makespan,
    )


def validate_candidate(
    snapshot: OperationalSnapshot,
    catalog: DomainCatalog,
    candidate: CandidateProposal,
    *,
    projected_order_deltas: Sequence[OrderDelta] = (),
    projected_resource_deltas: Sequence[ResourceDelta] = (),
) -> ValidationReport:
    """Validate proposal provenance, exact external deltas, objective, and schedule."""

    order_delta_tuple = tuple(projected_order_deltas)
    resource_delta_tuple = tuple(projected_resource_deltas)
    violations: list[ScheduleViolation] = []

    if candidate.proposal_hash != canonical_hash(candidate.content_payload()):
        violations.append(
            _violation(
                ViolationCode.PROPOSAL_HASH_MISMATCH,
                "proposal_hash does not match the candidate's immutable content",
                field="proposal_hash",
            )
        )
    if candidate.run_id != snapshot.run_id:
        violations.append(
            _violation(
                ViolationCode.RUN_ID_MISMATCH,
                "candidate run_id does not match the base snapshot",
                field="run_id",
                references={"expected": snapshot.run_id, "actual": candidate.run_id},
            )
        )
    if candidate.base_state_version != snapshot.state_version:
        violations.append(
            _violation(
                ViolationCode.BASE_STATE_VERSION_MISMATCH,
                "candidate base_state_version is stale or belongs to another snapshot",
                field="base_state_version",
                references={
                    "expected": str(snapshot.state_version),
                    "actual": str(candidate.base_state_version),
                },
            )
        )
    expected_snapshot_hash = canonical_hash(snapshot)
    if candidate.base_snapshot_hash != expected_snapshot_hash:
        violations.append(
            _violation(
                ViolationCode.BASE_SNAPSHOT_HASH_MISMATCH,
                "candidate base_snapshot_hash does not match the supplied snapshot",
                field="base_snapshot_hash",
                references={
                    "expected": expected_snapshot_hash,
                    "actual": candidate.base_snapshot_hash,
                },
            )
        )
    expected_revision = snapshot.plan_revision + 1
    if candidate.proposed_plan_revision != expected_revision:
        violations.append(
            _violation(
                ViolationCode.PLAN_REVISION_MISMATCH,
                "candidate must propose exactly the next plan revision",
                field="proposed_plan_revision",
                references={
                    "expected": str(expected_revision),
                    "actual": str(candidate.proposed_plan_revision),
                },
            )
        )
    if candidate.proposed_order_deltas != order_delta_tuple:
        violations.append(
            _violation(
                ViolationCode.ORDER_DELTA_MISMATCH,
                "candidate order deltas must exactly equal the externally projected deltas",
                field="proposed_order_deltas",
                references={
                    "expected_hash": canonical_hash(order_delta_tuple),
                    "actual_hash": canonical_hash(candidate.proposed_order_deltas),
                },
            )
        )
    if candidate.proposed_resource_deltas != resource_delta_tuple:
        violations.append(
            _violation(
                ViolationCode.RESOURCE_DELTA_MISMATCH,
                "candidate resource deltas must exactly equal the externally projected deltas",
                field="proposed_resource_deltas",
                references={
                    "expected_hash": canonical_hash(resource_delta_tuple),
                    "actual_hash": canonical_hash(candidate.proposed_resource_deltas),
                },
            )
        )

    schedule_report = validate_schedule(
        snapshot,
        catalog,
        candidate.assignments,
        projected_order_deltas=order_delta_tuple,
        projected_resource_deltas=resource_delta_tuple,
    )
    violations.extend(schedule_report.violations)
    if (
        candidate.objective.weighted_total_tardiness_seconds
        != schedule_report.total_tardiness_seconds
        or candidate.objective.makespan_seconds != schedule_report.makespan_seconds
    ):
        violations.append(
            _violation(
                ViolationCode.OBJECTIVE_MISMATCH,
                "declared objective does not equal the independently calculated schedule metrics",
                field="objective",
                references={
                    "expected_weighted_total_tardiness_seconds": str(
                        schedule_report.total_tardiness_seconds
                    ),
                    "actual_weighted_total_tardiness_seconds": str(
                        candidate.objective.weighted_total_tardiness_seconds
                    ),
                    "expected_makespan_seconds": str(schedule_report.makespan_seconds),
                    "actual_makespan_seconds": str(candidate.objective.makespan_seconds),
                },
            )
        )

    ordered_violations = _sorted_violations(violations)
    if ordered_violations:
        return ValidationReport(
            is_valid=False,
            violations=ordered_violations,
            total_tardiness_seconds=schedule_report.total_tardiness_seconds,
            makespan_seconds=schedule_report.makespan_seconds,
        )
    return schedule_report
