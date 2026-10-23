"""Pure KPI calculation from a version-zero checkpoint and operational events."""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from datetime import datetime
from typing import Any

from pydantic import BaseModel

from shopfloor_pilot.domain.models import (
    AttemptStatus,
    EventType,
    MachineStatus,
    Operation,
    OperationalSnapshot,
    OperationAttempt,
    Order,
    OrderStatus,
    TimeWindow,
)
from shopfloor_pilot.events import (
    EventEnvelope,
    MachineDownPayload,
    MachineRestoredPayload,
    SnapshotCheckpoint,
    replay_events,
)

from .errors import MetricsInputError
from .models import (
    DecisionLatency,
    MachineMetrics,
    MetricBasis,
    MetricWindow,
    OrderMetrics,
    RecoveryMetrics,
    RunMetrics,
)


def _json_payload[ModelT: BaseModel](model_type: type[ModelT], payload: Any) -> ModelT:
    return model_type.model_validate_json(  # type: ignore[attr-defined,no-any-return]
        json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    )


def _clip_duration(start: int, end: int, window_start: int, window_end: int) -> int:
    return max(0, min(end, window_end) - max(start, window_start))


def _merge_intervals(intervals: Iterable[tuple[int, int]]) -> tuple[tuple[int, int], ...]:
    ordered = sorted((start, end) for start, end in intervals if end > start)
    merged: list[tuple[int, int]] = []
    for start, end in ordered:
        if not merged or start > merged[-1][1]:
            merged.append((start, end))
            continue
        old_start, old_end = merged[-1]
        merged[-1] = (old_start, max(old_end, end))
    return tuple(merged)


def _measure(intervals: Iterable[tuple[int, int]]) -> int:
    return sum(end - start for start, end in _merge_intervals(intervals))


def _intersection_measure(left: Iterable[tuple[int, int]], right: Iterable[tuple[int, int]]) -> int:
    first = _merge_intervals(left)
    second = _merge_intervals(right)
    result = 0
    i = 0
    j = 0
    while i < len(first) and j < len(second):
        start = max(first[i][0], second[j][0])
        end = min(first[i][1], second[j][1])
        if end > start:
            result += end - start
        if first[i][1] <= second[j][1]:
            i += 1
        else:
            j += 1
    return result


def _calendar_intervals(
    windows: Sequence[TimeWindow], segment_start: int, segment_end: int
) -> tuple[tuple[int, int], ...]:
    return tuple(
        (max(window.start_seconds, segment_start), min(window.end_seconds, segment_end))
        for window in windows
        if window.start_seconds < segment_end and segment_start < window.end_seconds
    )


def _attempt_actual_interval(
    attempt: OperationAttempt, as_of_seconds: int
) -> tuple[int, int] | None:
    if attempt.status in (AttemptStatus.COMPLETED, AttemptStatus.INTERRUPTED):
        if attempt.actual_start_seconds is None or attempt.actual_end_seconds is None:
            raise MetricsInputError(f"fixed attempt {attempt.attempt_id} lacks actual bounds")
        return attempt.actual_start_seconds, attempt.actual_end_seconds
    if attempt.status is AttemptStatus.RUNNING:
        if attempt.actual_start_seconds is None:
            raise MetricsInputError(f"running attempt {attempt.attempt_id} lacks an actual start")
        return attempt.actual_start_seconds, as_of_seconds
    return None


def _machine_capacity_and_downtime(
    initial: OperationalSnapshot,
    events: Sequence[EventEnvelope],
    machine_id: str,
    window_start: int,
    window_end: int,
) -> tuple[int, int, int, tuple[tuple[int, int], ...], tuple[tuple[int, int], ...]]:
    try:
        machine = next(item for item in initial.machines if item.machine_id == machine_id)
    except StopIteration as exc:
        raise MetricsInputError(f"final snapshot contains unknown machine {machine_id}") from exc
    status = machine.status
    windows = machine.availability_windows
    cursor = window_start
    capacity_intervals: list[tuple[int, int]] = []
    down_intervals: list[tuple[int, int]] = []
    down_capacity_intervals: list[tuple[int, int]] = []

    transitions = [
        envelope.event
        for envelope in events
        if envelope.event.event_type in (EventType.MACHINE_DOWN, EventType.MACHINE_RESTORED)
        and envelope.event.payload.get("machine_id") == machine_id
        and window_start <= envelope.event.simulated_time_seconds <= window_end
    ]
    for event in transitions:
        at = event.simulated_time_seconds
        segment_end = max(cursor, min(at, window_end))
        calendar = _calendar_intervals(windows, cursor, segment_end)
        capacity_intervals.extend(calendar)
        if status in (MachineStatus.DOWN, MachineStatus.MAINTENANCE):
            down_intervals.append((cursor, segment_end))
            down_capacity_intervals.extend(calendar)
        if event.event_type is EventType.MACHINE_DOWN:
            payload = _json_payload(MachineDownPayload, event.payload)
            status = MachineStatus.DOWN
        else:
            payload = _json_payload(MachineRestoredPayload, event.payload)
            status = MachineStatus.AVAILABLE
        if payload.availability_windows is not None:
            windows = payload.availability_windows
        cursor = segment_end

    calendar = _calendar_intervals(windows, cursor, window_end)
    capacity_intervals.extend(calendar)
    if status in (MachineStatus.DOWN, MachineStatus.MAINTENANCE):
        down_intervals.append((cursor, window_end))
        down_capacity_intervals.extend(calendar)
    return (
        _measure(capacity_intervals),
        _measure(down_intervals),
        _measure(down_capacity_intervals),
        _merge_intervals(capacity_intervals),
        _merge_intervals(down_intervals),
    )


def _machine_metrics(
    initial: OperationalSnapshot,
    snapshot: OperationalSnapshot,
    events: Sequence[EventEnvelope],
    window: MetricWindow,
) -> tuple[MachineMetrics, ...]:
    result: list[MachineMetrics] = []
    for machine in sorted(snapshot.machines, key=lambda item: item.machine_id):
        capacity, downtime, lost, capacity_intervals, down_intervals = (
            _machine_capacity_and_downtime(
                initial,
                events,
                machine.machine_id,
                window.start_seconds,
                window.end_seconds,
            )
        )
        productive_intervals: list[tuple[int, int]] = []
        wasted_intervals: list[tuple[int, int]] = []
        for attempt in snapshot.attempts:
            if attempt.machine_id != machine.machine_id:
                continue
            actual = _attempt_actual_interval(attempt, window.end_seconds)
            if actual is None:
                continue
            clipped = (
                max(actual[0], window.start_seconds),
                min(actual[1], window.end_seconds),
            )
            if clipped[1] <= clipped[0]:
                continue
            if attempt.status is AttemptStatus.INTERRUPTED:
                wasted_intervals.append(clipped)
            else:
                productive_intervals.append(clipped)

        all_busy = (*productive_intervals, *wasted_intervals)
        raw_busy = sum(end - start for start, end in all_busy)
        busy = _measure(all_busy)
        if raw_busy != busy:
            raise MetricsInputError(f"actual attempts overlap on machine {machine.machine_id}")
        productive = _measure(productive_intervals)
        wasted = _measure(wasted_intervals)
        if productive + wasted != busy:
            raise MetricsInputError(
                f"productive/wasted intervals overlap on machine {machine.machine_id}"
            )
        if _intersection_measure(all_busy, down_intervals):
            raise MetricsInputError(f"machine {machine.machine_id} is busy while down")
        busy_in_capacity = _intersection_measure(all_busy, capacity_intervals)
        if busy_in_capacity != busy:
            raise MetricsInputError(
                f"machine {machine.machine_id} has actual processing outside its calendar"
            )
        idle = capacity - busy - lost
        if idle < 0:
            raise MetricsInputError(
                f"machine {machine.machine_id} time accounting exceeds calendar capacity"
            )
        utilization = None if capacity == 0 else busy / capacity
        productive_utilization = None if capacity == 0 else productive / capacity
        result.append(
            MachineMetrics(
                machine_id=machine.machine_id,
                window_duration_seconds=window.duration_seconds,
                scheduled_capacity_seconds=capacity,
                off_calendar_seconds=window.duration_seconds - capacity,
                busy_processing_seconds=busy,
                productive_processing_seconds=productive,
                wasted_processing_seconds=wasted,
                downtime_seconds=downtime,
                capacity_lost_to_downtime_seconds=lost,
                idle_seconds=idle,
                utilization_numerator_seconds=busy,
                utilization_denominator_seconds=capacity,
                utilization_ratio=utilization,
                productive_utilization_ratio=productive_utilization,
            )
        )
    return tuple(result)


def _terminal_operations(order: Order, operations: Sequence[Operation]) -> tuple[Operation, ...]:
    matches = [item for item in operations if item.order_id == order.order_id]
    if not matches:
        return ()
    final_sequence = max(item.sequence_index for item in matches)
    return tuple(item for item in matches if item.sequence_index == final_sequence)


def _completed_attempt_end(
    operation: Operation, attempts: Sequence[OperationAttempt]
) -> int | None:
    completed = [
        item
        for item in attempts
        if item.operation_id == operation.operation_id
        and item.status is AttemptStatus.COMPLETED
        and item.actual_end_seconds is not None
    ]
    if len(completed) > 1:
        raise MetricsInputError(f"operation {operation.operation_id} completed more than once")
    return completed[0].actual_end_seconds if completed else None


def _predicted_attempt_end(
    operation: Operation, attempts: Sequence[OperationAttempt]
) -> int | None:
    candidates = sorted(
        (item for item in attempts if item.operation_id == operation.operation_id),
        key=lambda item: (item.attempt_number, item.attempt_id),
        reverse=True,
    )
    for attempt in candidates:
        if attempt.status is AttemptStatus.COMPLETED:
            return attempt.actual_end_seconds
        if attempt.status is AttemptStatus.RUNNING and attempt.actual_start_seconds is not None:
            return attempt.actual_start_seconds + attempt.planned_duration_seconds
        if attempt.status is AttemptStatus.SCHEDULED and attempt.scheduled_window is not None:
            return attempt.scheduled_window.end_seconds
        if attempt.status is AttemptStatus.INTERRUPTED:
            return None
    return None


def _order_metrics(snapshot: OperationalSnapshot) -> tuple[OrderMetrics, ...]:
    results: list[OrderMetrics] = []
    for order in sorted(snapshot.orders, key=lambda item: item.order_id):
        terminals = _terminal_operations(order, snapshot.operations)
        actual_ends = [
            _completed_attempt_end(operation, snapshot.attempts) for operation in terminals
        ]
        actual_completion = (
            max(value for value in actual_ends if value is not None)
            if terminals and all(value is not None for value in actual_ends)
            else None
        )
        if order.status is OrderStatus.COMPLETED and actual_completion is None:
            raise MetricsInputError(
                f"completed order {order.order_id} lacks completed terminal attempts"
            )
        predicted_ends = [
            _predicted_attempt_end(operation, snapshot.attempts) for operation in terminals
        ]
        predicted_completion = (
            max(value for value in predicted_ends if value is not None)
            if terminals and all(value is not None for value in predicted_ends)
            else None
        )
        if actual_completion is not None:
            actual_tardiness = (
                max(0, actual_completion - order.due_time_seconds) * order.priority_weight
            )
            results.append(
                OrderMetrics(
                    order_id=order.order_id,
                    release_time_seconds=order.release_time_seconds,
                    due_time_seconds=order.due_time_seconds,
                    priority_weight=order.priority_weight,
                    quantity=order.quantity,
                    actual_basis=MetricBasis.ACTUAL,
                    actual_completion_seconds=actual_completion,
                    actual_weighted_tardiness_seconds=actual_tardiness,
                    weighted_tardiness_lower_bound_seconds=actual_tardiness,
                    prediction_basis=MetricBasis.ACTUAL,
                    predicted_completion_seconds=actual_completion,
                    predicted_weighted_tardiness_seconds=actual_tardiness,
                )
            )
            continue

        lower_bound = (
            max(0, snapshot.simulated_time_seconds - order.due_time_seconds) * order.priority_weight
        )
        if predicted_completion is None:
            prediction_basis = MetricBasis.UNAVAILABLE
            predicted_tardiness = None
        else:
            prediction_basis = MetricBasis.PREDICTED
            predicted_tardiness = (
                max(0, predicted_completion - order.due_time_seconds) * order.priority_weight
            )
        results.append(
            OrderMetrics(
                order_id=order.order_id,
                release_time_seconds=order.release_time_seconds,
                due_time_seconds=order.due_time_seconds,
                priority_weight=order.priority_weight,
                quantity=order.quantity,
                actual_basis=MetricBasis.CENSORED,
                censored_at_seconds=snapshot.simulated_time_seconds,
                weighted_tardiness_lower_bound_seconds=lower_bound,
                prediction_basis=prediction_basis,
                predicted_completion_seconds=predicted_completion,
                predicted_weighted_tardiness_seconds=predicted_tardiness,
            )
        )
    return tuple(results)


def _recoveries(
    snapshot: OperationalSnapshot, events: Sequence[EventEnvelope]
) -> tuple[RecoveryMetrics, ...]:
    successors: dict[str, list[OperationAttempt]] = {}
    for attempt in snapshot.attempts:
        if attempt.restart_of_attempt_id is not None:
            successors.setdefault(attempt.restart_of_attempt_id, []).append(attempt)
    down_events: dict[tuple[str, int], str] = {}
    for envelope in events:
        event = envelope.event
        if event.event_type is EventType.MACHINE_DOWN:
            machine_id = str(event.payload["machine_id"])
            down_events[(machine_id, event.simulated_time_seconds)] = event.event_id

    result: list[RecoveryMetrics] = []
    interrupted = sorted(
        (item for item in snapshot.attempts if item.status is AttemptStatus.INTERRUPTED),
        key=lambda item: (item.actual_end_seconds or 0, item.attempt_id),
    )
    for attempt in interrupted:
        if attempt.actual_end_seconds is None:
            raise MetricsInputError(f"interrupted attempt {attempt.attempt_id} has no end")
        matches = successors.get(attempt.attempt_id, [])
        if len(matches) > 1:
            raise MetricsInputError(f"interrupted attempt {attempt.attempt_id} has many restarts")
        successor = matches[0] if matches else None
        common: dict[str, Any] = {
            "interruption_attempt_id": attempt.attempt_id,
            "operation_id": attempt.operation_id,
            "disrupted_machine_id": attempt.machine_id,
            "disruption_event_id": down_events.get(
                (attempt.machine_id, attempt.actual_end_seconds)
            ),
            "disruption_seconds": attempt.actual_end_seconds,
            "resumed_attempt_id": successor.attempt_id if successor is not None else None,
            "resumed_machine_id": successor.machine_id if successor is not None else None,
        }
        if successor is not None and successor.actual_start_seconds is not None:
            result.append(
                RecoveryMetrics(
                    **common,
                    actual_basis=MetricBasis.ACTUAL,
                    actual_resumption_seconds=successor.actual_start_seconds,
                    actual_recovery_duration_seconds=(
                        successor.actual_start_seconds - attempt.actual_end_seconds
                    ),
                )
            )
            continue
        predicted_start = (
            successor.scheduled_window.start_seconds
            if successor is not None and successor.scheduled_window is not None
            else None
        )
        result.append(
            RecoveryMetrics(
                **common,
                actual_basis=MetricBasis.CENSORED,
                censored_at_seconds=snapshot.simulated_time_seconds,
                censored_lower_bound_seconds=(
                    snapshot.simulated_time_seconds - attempt.actual_end_seconds
                ),
                predicted_resumption_seconds=predicted_start,
                predicted_recovery_duration_seconds=(
                    predicted_start - attempt.actual_end_seconds
                    if predicted_start is not None
                    else None
                ),
            )
        )
    return tuple(result)


def forecast_order_outcomes(snapshot: OperationalSnapshot) -> tuple[OrderMetrics, ...]:
    """Per-order actual/censored outcome and plan forecast at ``snapshot`` time.

    This is the same per-order calculation used by :func:`calculate_run_metrics`,
    exposed for prediction-only proposal review (for example soft due-date misses
    of a candidate's projected snapshot). It reads no events and observes nothing.
    """

    return _order_metrics(snapshot)


def forecast_recoveries(snapshot: OperationalSnapshot) -> tuple[RecoveryMetrics, ...]:
    """Interruption-to-restart episodes of one snapshot, without reading events.

    Same definition as the ``recoveries`` of :func:`calculate_run_metrics`; the
    disruption event identity is unknown here. Used for prediction-only review of a
    candidate's projected snapshot.
    """

    return _recoveries(snapshot, ())


def calculate_run_metrics(
    initial_checkpoint: SnapshotCheckpoint,
    events: Sequence[EventEnvelope],
) -> RunMetrics:
    """Replay ``events`` and calculate deterministic operational KPIs.

    Actual duration accounting uses ``[initial_time, current_time)``. State and
    completion counts observe the replayed boundary after all events at current time,
    so an operation executing ``[0, 10)`` and completing at 10 contributes ten busy
    seconds and is counted complete in a report as of 10.
    """

    event_tuple = tuple(events)
    try:
        checkpoint = replay_events(initial_checkpoint, event_tuple)
    except Exception as exc:
        raise MetricsInputError(f"cannot replay metric input: {exc}") from exc
    initial = initial_checkpoint.snapshot
    snapshot = checkpoint.snapshot
    if initial.state_version != 0 or initial_checkpoint.last_sequence != 0:
        raise MetricsInputError("run metrics require the version-zero run checkpoint")
    if snapshot.simulated_time_seconds < initial.simulated_time_seconds:
        raise MetricsInputError("replayed time precedes the run origin")
    window = MetricWindow(
        start_seconds=initial.simulated_time_seconds,
        end_seconds=snapshot.simulated_time_seconds,
    )
    orders = _order_metrics(snapshot)
    completed = tuple(item for item in orders if item.actual_basis is MetricBasis.ACTUAL)
    unfinished = tuple(item for item in orders if item.actual_basis is MetricBasis.CENSORED)
    completed_tardiness = sum(item.actual_weighted_tardiness_seconds or 0 for item in completed)
    tardiness_lower_bound = sum(item.weighted_tardiness_lower_bound_seconds for item in orders)
    actual_completion_values = tuple(
        item.actual_completion_seconds
        for item in completed
        if item.actual_completion_seconds is not None
    )
    if unfinished:
        actual_basis = MetricBasis.CENSORED
        actual_makespan = None
        actual_tardiness = None
        makespan_lower_bound = max((snapshot.simulated_time_seconds, *actual_completion_values))
    else:
        actual_basis = MetricBasis.ACTUAL
        actual_makespan = max(
            actual_completion_values,
            default=initial.simulated_time_seconds,
        )
        actual_tardiness = completed_tardiness
        makespan_lower_bound = actual_makespan

    if orders and all(item.prediction_basis is not MetricBasis.UNAVAILABLE for item in orders):
        predicted_makespan = max(
            item.predicted_completion_seconds
            for item in orders
            if item.predicted_completion_seconds is not None
        )
        predicted_tardiness = sum(item.predicted_weighted_tardiness_seconds or 0 for item in orders)
        prediction_basis = MetricBasis.ACTUAL if not unfinished else MetricBasis.PREDICTED
    elif not orders:
        predicted_makespan = initial.simulated_time_seconds
        predicted_tardiness = 0
        prediction_basis = MetricBasis.ACTUAL
    else:
        predicted_makespan = None
        predicted_tardiness = None
        prediction_basis = MetricBasis.UNAVAILABLE

    machine_metrics = _machine_metrics(initial, snapshot, event_tuple, window)
    released_unfinished = [
        order
        for order in snapshot.orders
        if order.release_time_seconds <= snapshot.simulated_time_seconds
        and order.status not in (OrderStatus.COMPLETED, OrderStatus.REJECTED)
    ]
    throughput = len(completed)
    throughput_rate = (
        None if window.duration_seconds == 0 else throughput * 3600 / window.duration_seconds
    )
    return RunMetrics(
        run_id=snapshot.run_id,
        state_version=snapshot.state_version,
        plan_revision=snapshot.plan_revision,
        as_of_seconds=snapshot.simulated_time_seconds,
        hard_horizon_seconds=snapshot.hard_horizon_seconds,
        actual_window=window,
        prediction_horizon_seconds=snapshot.hard_horizon_seconds,
        included_order_ids=tuple(item.order_id for item in orders),
        orders=orders,
        actual_outcome_basis=actual_basis,
        actual_makespan_seconds=actual_makespan,
        actual_makespan_lower_bound_seconds=makespan_lower_bound,
        actual_weighted_tardiness_seconds=actual_tardiness,
        completed_order_weighted_tardiness_seconds=completed_tardiness,
        weighted_tardiness_lower_bound_seconds=tardiness_lower_bound,
        prediction_basis=prediction_basis,
        predicted_makespan_seconds=predicted_makespan,
        predicted_weighted_tardiness_seconds=predicted_tardiness,
        completed_order_count=len(completed),
        unfinished_order_count=len(unfinished),
        throughput_completed_orders=throughput,
        throughput_window_seconds=window.duration_seconds,
        throughput_orders_per_hour=throughput_rate,
        wip_order_count=len(released_unfinished),
        wip_unit_count=sum(item.quantity for item in released_unfinished),
        machines=machine_metrics,
        wasted_processing_seconds=sum(item.wasted_processing_seconds for item in machine_metrics),
        recoveries=_recoveries(snapshot, event_tuple),
    )


def calculate_decision_latency(
    *,
    start_simulated_seconds: int,
    end_simulated_seconds: int,
    started_at_utc: str,
    completed_at_utc: str,
) -> DecisionLatency:
    """Calculate wall-clock waiting without converting it into simulated time."""

    if isinstance(start_simulated_seconds, bool) or not isinstance(start_simulated_seconds, int):
        raise MetricsInputError("start_simulated_seconds must be an integer")
    if isinstance(end_simulated_seconds, bool) or not isinstance(end_simulated_seconds, int):
        raise MetricsInputError("end_simulated_seconds must be an integer")
    if start_simulated_seconds < 0 or end_simulated_seconds < start_simulated_seconds:
        raise MetricsInputError("simulated decision bounds are invalid")
    try:
        started = datetime.fromisoformat(started_at_utc)
        completed = datetime.fromisoformat(completed_at_utc)
    except ValueError as exc:
        raise MetricsInputError("decision timestamps must be ISO 8601 values") from exc
    if started.tzinfo is None or completed.tzinfo is None:
        raise MetricsInputError("decision timestamps require explicit UTC offsets")
    if completed < started:
        raise MetricsInputError("wall-clock completion precedes decision start")
    elapsed = completed - started
    elapsed_ms = elapsed.days * 86_400_000 + elapsed.seconds * 1_000 + elapsed.microseconds // 1_000
    return DecisionLatency(
        start_simulated_seconds=start_simulated_seconds,
        end_simulated_seconds=end_simulated_seconds,
        simulated_elapsed_seconds=end_simulated_seconds - start_simulated_seconds,
        started_at_utc=started_at_utc,
        completed_at_utc=completed_at_utc,
        wall_clock_elapsed_ms=elapsed_ms,
    )
