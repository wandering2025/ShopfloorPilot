"""Hand-calculated event history for S06 KPI and portable-replay tests."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from shopfloor_pilot.domain.models import (
    AttemptStatus,
    Capability,
    EventType,
    Machine,
    Operation,
    OperationalSnapshot,
    OperationAttempt,
    OperationStatus,
    Order,
    ResourceKind,
    TimeWindow,
)
from shopfloor_pilot.events import (
    EventDraft,
    EventEnvelope,
    MachineDownPayload,
    MachineRestoredPayload,
    OperationCompletedPayload,
    OperationInterruptedPayload,
    OperationStartedPayload,
    PlanAppliedPayload,
    SnapshotCheckpoint,
    TimeAdvancedPayload,
    materialize_event_group,
    reduce_event_group,
)


@dataclass(frozen=True)
class MetricTimeline:
    initial: SnapshotCheckpoint
    at_fault: SnapshotCheckpoint
    at_twenty: SnapshotCheckpoint
    complete: SnapshotCheckpoint
    events_at_fault: tuple[EventEnvelope, ...]
    events_at_twenty: tuple[EventEnvelope, ...]
    events_complete: tuple[EventEnvelope, ...]


def _draft(event_type: EventType, at: int, payload: object) -> EventDraft:
    assert hasattr(payload, "model_dump")
    return EventDraft(
        simulated_time_seconds=at,
        event_type=event_type,
        payload=payload.model_dump(mode="json"),  # type: ignore[union-attr]
    )


def _validated_update[ModelT](model: ModelT, **updates: Any) -> ModelT:
    raw = model.model_dump(mode="python")  # type: ignore[attr-defined]
    raw.update(updates)
    return type(model).model_validate(raw)


@pytest.fixture
def metric_timeline() -> MetricTimeline:
    """One machine, a completed job, then a failed and restarted job.

    The oracle deliberately contains idle, productive, downtime, and wasted intervals:
    ``[0,10)`` productive, ``[10,12)`` idle, ``[12,15)`` wasted,
    ``[15,18)`` down, and ``[18,23)`` productive recovery.
    """

    machine = Machine(
        machine_id="mill",
        display_name="Mill",
        kind=ResourceKind.PROCESSING,
        capabilities=(Capability.MILLING,),
        availability_windows=(TimeWindow(start_seconds=0, end_seconds=30),),
    )
    order_a = Order(
        order_id="order-a",
        product_id="part-a",
        quantity=1,
        release_time_seconds=0,
        due_time_seconds=8,
        priority_weight=2,
    )
    order_b = Order(
        order_id="order-b",
        product_id="part-b",
        quantity=1,
        release_time_seconds=0,
        due_time_seconds=18,
        priority_weight=3,
    )
    operation_a = Operation(
        operation_id="operation-a",
        order_id=order_a.order_id,
        unit_index=1,
        operation_template_id="template-a",
        sequence_index=0,
        required_capability=Capability.MILLING,
        duration_seconds=10,
        status=OperationStatus.READY,
    )
    operation_b = Operation(
        operation_id="operation-b",
        order_id=order_b.order_id,
        unit_index=1,
        operation_template_id="template-b",
        sequence_index=0,
        required_capability=Capability.MILLING,
        duration_seconds=5,
        status=OperationStatus.READY,
    )
    attempt_a = OperationAttempt(
        attempt_id="attempt-a-1",
        operation_id=operation_a.operation_id,
        attempt_number=1,
        machine_id=machine.machine_id,
        planned_duration_seconds=10,
        status=AttemptStatus.SCHEDULED,
        scheduled_window=TimeWindow(start_seconds=0, end_seconds=10),
    )
    attempt_b = OperationAttempt(
        attempt_id="attempt-b-1",
        operation_id=operation_b.operation_id,
        attempt_number=1,
        machine_id=machine.machine_id,
        planned_duration_seconds=5,
        status=AttemptStatus.SCHEDULED,
        scheduled_window=TimeWindow(start_seconds=12, end_seconds=17),
    )
    initial_snapshot = OperationalSnapshot(
        schema_version=1,
        run_id="run-s06-metrics",
        state_version=0,
        plan_revision=0,
        simulated_time_seconds=0,
        hard_horizon_seconds=30,
        machines=(machine,),
        orders=(order_a, order_b),
        operations=(),
        attempts=(),
    )
    initial = SnapshotCheckpoint.create(initial_snapshot)
    checkpoint = initial
    envelopes: list[EventEnvelope] = []

    def apply(mutation_id: str, *drafts: EventDraft) -> None:
        nonlocal checkpoint
        group = materialize_event_group(checkpoint, mutation_id, drafts)
        checkpoint = reduce_event_group(checkpoint, group)
        envelopes.extend(group)

    apply(
        "mutation-initial-plan-and-start",
        _draft(
            EventType.PLAN_APPLIED,
            0,
            PlanAppliedPayload(
                plan_revision=1,
                operations=(operation_a, operation_b),
                attempts=(attempt_a, attempt_b),
            ),
        ),
        _draft(
            EventType.OPERATION_STARTED,
            0,
            OperationStartedPayload(attempt_id=attempt_a.attempt_id),
        ),
    )
    apply(
        "mutation-complete-a",
        _draft(
            EventType.OPERATION_COMPLETED,
            10,
            OperationCompletedPayload(attempt_id=attempt_a.attempt_id),
        ),
        _draft(EventType.TIME_ADVANCED, 10, TimeAdvancedPayload(target_time_seconds=10)),
    )
    apply(
        "mutation-start-b",
        _draft(
            EventType.OPERATION_STARTED,
            12,
            OperationStartedPayload(attempt_id=attempt_b.attempt_id),
        ),
        _draft(EventType.TIME_ADVANCED, 12, TimeAdvancedPayload(target_time_seconds=12)),
    )
    apply(
        "mutation-interrupt-b",
        _draft(
            EventType.MACHINE_DOWN,
            15,
            MachineDownPayload(machine_id=machine.machine_id, status="down"),
        ),
        _draft(
            EventType.OPERATION_INTERRUPTED,
            15,
            OperationInterruptedPayload(attempt_id=attempt_b.attempt_id),
        ),
        _draft(EventType.TIME_ADVANCED, 15, TimeAdvancedPayload(target_time_seconds=15)),
    )
    at_fault = checkpoint
    events_at_fault = tuple(envelopes)

    completed_a = next(
        item for item in checkpoint.snapshot.operations if item.operation_id == "operation-a"
    )
    interrupted_b = next(
        item for item in checkpoint.snapshot.operations if item.operation_id == "operation-b"
    )
    completed_attempt_a = next(
        item for item in checkpoint.snapshot.attempts if item.attempt_id == "attempt-a-1"
    )
    interrupted_attempt_b = next(
        item for item in checkpoint.snapshot.attempts if item.attempt_id == "attempt-b-1"
    )
    ready_b = _validated_update(interrupted_b, status=OperationStatus.READY)
    restart_b = OperationAttempt(
        attempt_id="attempt-b-2",
        operation_id=operation_b.operation_id,
        attempt_number=2,
        machine_id=machine.machine_id,
        planned_duration_seconds=5,
        status=AttemptStatus.SCHEDULED,
        scheduled_window=TimeWindow(start_seconds=18, end_seconds=23),
        restart_of_attempt_id=interrupted_attempt_b.attempt_id,
    )
    apply(
        "mutation-restore-replan-and-start",
        _draft(
            EventType.MACHINE_RESTORED,
            18,
            MachineRestoredPayload(machine_id=machine.machine_id, status="available"),
        ),
        _draft(
            EventType.PLAN_APPLIED,
            18,
            PlanAppliedPayload(
                plan_revision=2,
                operations=(completed_a, ready_b),
                attempts=(completed_attempt_a, interrupted_attempt_b, restart_b),
            ),
        ),
        _draft(
            EventType.OPERATION_STARTED,
            18,
            OperationStartedPayload(attempt_id=restart_b.attempt_id),
        ),
        _draft(EventType.TIME_ADVANCED, 18, TimeAdvancedPayload(target_time_seconds=18)),
    )
    apply(
        "mutation-observe-running-restart",
        _draft(EventType.TIME_ADVANCED, 20, TimeAdvancedPayload(target_time_seconds=20)),
    )
    at_twenty = checkpoint
    events_at_twenty = tuple(envelopes)

    apply(
        "mutation-complete-restart",
        _draft(
            EventType.OPERATION_COMPLETED,
            23,
            OperationCompletedPayload(attempt_id=restart_b.attempt_id),
        ),
        _draft(EventType.TIME_ADVANCED, 23, TimeAdvancedPayload(target_time_seconds=23)),
    )
    return MetricTimeline(
        initial=initial,
        at_fault=at_fault,
        at_twenty=at_twenty,
        complete=checkpoint,
        events_at_fault=events_at_fault,
        events_at_twenty=events_at_twenty,
        events_complete=tuple(envelopes),
    )
