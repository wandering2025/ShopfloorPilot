"""Independent hand-built operational stream used by S02 replay acceptance tests."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from shopfloor_pilot.domain.models import (
    AttemptStatus,
    Capability,
    EventType,
    Machine,
    Observation,
    Operation,
    OperationalSnapshot,
    OperationAttempt,
    OperationStatus,
    Order,
    OrderStatus,
    ResourceKind,
    TelemetryValue,
    TimeWindow,
)
from shopfloor_pilot.events import (
    EventDraft,
    EventEnvelope,
    MachineDownPayload,
    MachineRestoredPayload,
    ObservationRecordedPayload,
    OperationCompletedPayload,
    OperationInterruptedPayload,
    OperationStartedPayload,
    OrderRegisteredPayload,
    PlanAppliedPayload,
    SnapshotCheckpoint,
    TimeAdvancedPayload,
    materialize_event_group,
    reduce_event_group,
)


@dataclass(frozen=True)
class HandBuiltReplay:
    initial: SnapshotCheckpoint
    envelopes: tuple[EventEnvelope, ...]
    expected_snapshot: OperationalSnapshot
    group_end_offsets: tuple[int, ...]


def _machines(*, cnc02_windows: tuple[TimeWindow, ...] | None = None) -> tuple[Machine, ...]:
    full_window = (TimeWindow(start_seconds=0, end_seconds=100),)
    return (
        Machine(
            machine_id="CNC01",
            display_name="CNC 01",
            kind=ResourceKind.PROCESSING,
            capabilities=(Capability.DRILLING, Capability.TAPPING),
            availability_windows=full_window,
        ),
        Machine(
            machine_id="CNC02",
            display_name="CNC 02",
            kind=ResourceKind.PROCESSING,
            capabilities=(Capability.MILLING, Capability.SLOTTING),
            availability_windows=cnc02_windows or full_window,
        ),
        Machine(
            machine_id="CNC03",
            display_name="CNC 03",
            kind=ResourceKind.PROCESSING,
            capabilities=(Capability.DRILLING, Capability.MILLING),
            availability_windows=full_window,
        ),
        Machine(
            machine_id="QC01",
            display_name="QC 01",
            kind=ResourceKind.INSPECTION,
            capabilities=(Capability.INSPECTION,),
            availability_windows=full_window,
        ),
    )


def _draft(event_type: EventType, at: int, payload: object) -> EventDraft:
    assert hasattr(payload, "model_dump")
    return EventDraft(
        simulated_time_seconds=at,
        event_type=event_type,
        payload=payload.model_dump(mode="json"),  # type: ignore[union-attr]
    )


@pytest.fixture
def hand_built_replay() -> HandBuiltReplay:
    main_order = Order(
        order_id="order-replay-main",
        product_id="prod_aluminum_mounting_plate",
        quantity=1,
        release_time_seconds=0,
        due_time_seconds=80,
        priority_weight=3,
        status=OrderStatus.PENDING,
    )
    urgent_order = Order(
        order_id="order-replay-urgent",
        product_id="prod_aluminum_mounting_plate",
        quantity=1,
        release_time_seconds=15,
        due_time_seconds=60,
        priority_weight=10,
        status=OrderStatus.PENDING,
    )
    first_ready = Operation(
        operation_id="op-replay-first",
        order_id=main_order.order_id,
        unit_index=1,
        operation_template_id="template-replay-first",
        sequence_index=0,
        required_capability=Capability.MILLING,
        duration_seconds=10,
        status=OperationStatus.READY,
    )
    second_pending = Operation(
        operation_id="op-replay-second",
        order_id=main_order.order_id,
        unit_index=1,
        operation_template_id="template-replay-second",
        sequence_index=1,
        required_capability=Capability.MILLING,
        duration_seconds=20,
        predecessor_operation_ids=(first_ready.operation_id,),
        status=OperationStatus.PENDING,
    )
    first_scheduled = OperationAttempt(
        attempt_id="attempt-replay-first-1",
        operation_id=first_ready.operation_id,
        attempt_number=1,
        machine_id="CNC02",
        planned_duration_seconds=10,
        status=AttemptStatus.SCHEDULED,
        scheduled_window=TimeWindow(start_seconds=0, end_seconds=10),
    )
    second_scheduled = OperationAttempt(
        attempt_id="attempt-replay-second-1",
        operation_id=second_pending.operation_id,
        attempt_number=1,
        machine_id="CNC02",
        planned_duration_seconds=20,
        status=AttemptStatus.SCHEDULED,
        scheduled_window=TimeWindow(start_seconds=10, end_seconds=30),
    )
    initial_snapshot = OperationalSnapshot(
        schema_version=1,
        run_id="run-replay-hand-built",
        state_version=0,
        plan_revision=0,
        simulated_time_seconds=0,
        hard_horizon_seconds=100,
        machines=_machines(),
        orders=(main_order,),
        operations=(),
        attempts=(),
    )
    checkpoint = SnapshotCheckpoint.create(initial_snapshot)
    all_envelopes: list[EventEnvelope] = []
    group_ends: list[int] = []

    def apply(mutation_id: str, *drafts: EventDraft) -> None:
        nonlocal checkpoint
        group = materialize_event_group(checkpoint, mutation_id, drafts)
        all_envelopes.extend(group)
        checkpoint = reduce_event_group(checkpoint, group)
        group_ends.append(len(all_envelopes))

    apply(
        "mutation-plan-initial",
        _draft(
            EventType.PLAN_APPLIED,
            0,
            PlanAppliedPayload(
                plan_revision=1,
                operations=(first_ready, second_pending),
                attempts=(first_scheduled, second_scheduled),
            ),
        ),
    )
    apply(
        "mutation-start-first",
        _draft(
            EventType.OPERATION_STARTED,
            0,
            OperationStartedPayload(attempt_id=first_scheduled.attempt_id),
        ),
    )
    # Deliberately unsorted drafts prove materialization uses the S01 priority table.
    apply(
        "mutation-at-ten",
        _draft(EventType.TIME_ADVANCED, 10, TimeAdvancedPayload(target_time_seconds=10)),
        _draft(
            EventType.OPERATION_STARTED,
            10,
            OperationStartedPayload(attempt_id=second_scheduled.attempt_id),
        ),
        _draft(
            EventType.OPERATION_COMPLETED,
            10,
            OperationCompletedPayload(attempt_id=first_scheduled.attempt_id),
        ),
    )
    observation = Observation(
        schema_version=1,
        observation_id="observation-replay-cnc02",
        run_id=initial_snapshot.run_id,
        machine_id="CNC02",
        observed_at_seconds=15,
        signals=(TelemetryValue(name="temperature", value=71000, unit="mdegC"),),
        summary="Synthetic elevated temperature at interruption.",
    )
    apply(
        "mutation-fault-and-order",
        _draft(EventType.TIME_ADVANCED, 15, TimeAdvancedPayload(target_time_seconds=15)),
        _draft(
            EventType.OBSERVATION_RECORDED,
            15,
            ObservationRecordedPayload(observation=observation),
        ),
        _draft(
            EventType.ORDER_REGISTERED,
            15,
            OrderRegisteredPayload(order=urgent_order),
        ),
        _draft(
            EventType.OPERATION_INTERRUPTED,
            15,
            OperationInterruptedPayload(attempt_id=second_scheduled.attempt_id),
        ),
        _draft(
            EventType.MACHINE_DOWN,
            15,
            MachineDownPayload(
                machine_id="CNC02",
                status="down",
                availability_windows=(TimeWindow(start_seconds=0, end_seconds=15),),
            ),
        ),
    )

    completed_first = next(
        item
        for item in checkpoint.snapshot.operations
        if item.operation_id == first_ready.operation_id
    )
    interrupted_second = next(
        item
        for item in checkpoint.snapshot.operations
        if item.operation_id == second_pending.operation_id
    )
    completed_first_attempt = next(
        item
        for item in checkpoint.snapshot.attempts
        if item.attempt_id == first_scheduled.attempt_id
    )
    interrupted_second_attempt = next(
        item
        for item in checkpoint.snapshot.attempts
        if item.attempt_id == second_scheduled.attempt_id
    )
    second_ready = Operation(
        operation_id=interrupted_second.operation_id,
        order_id=interrupted_second.order_id,
        unit_index=interrupted_second.unit_index,
        operation_template_id=interrupted_second.operation_template_id,
        sequence_index=interrupted_second.sequence_index,
        required_capability=interrupted_second.required_capability,
        duration_seconds=interrupted_second.duration_seconds,
        predecessor_operation_ids=interrupted_second.predecessor_operation_ids,
        status=OperationStatus.READY,
    )
    restarted = OperationAttempt(
        attempt_id="attempt-replay-second-2",
        operation_id=second_ready.operation_id,
        attempt_number=2,
        machine_id="CNC03",
        planned_duration_seconds=20,
        status=AttemptStatus.SCHEDULED,
        scheduled_window=TimeWindow(start_seconds=16, end_seconds=36),
        restart_of_attempt_id=interrupted_second_attempt.attempt_id,
    )
    apply(
        "mutation-restore-and-replan",
        _draft(EventType.TIME_ADVANCED, 16, TimeAdvancedPayload(target_time_seconds=16)),
        _draft(
            EventType.OPERATION_STARTED,
            16,
            OperationStartedPayload(attempt_id=restarted.attempt_id),
        ),
        _draft(
            EventType.PLAN_APPLIED,
            16,
            PlanAppliedPayload(
                plan_revision=2,
                operations=(completed_first, second_ready),
                attempts=(completed_first_attempt, interrupted_second_attempt, restarted),
            ),
        ),
        _draft(
            EventType.MACHINE_RESTORED,
            16,
            MachineRestoredPayload(
                machine_id="CNC02",
                status="available",
                availability_windows=(TimeWindow(start_seconds=0, end_seconds=100),),
            ),
        ),
    )
    apply(
        "mutation-complete-restart",
        _draft(EventType.TIME_ADVANCED, 36, TimeAdvancedPayload(target_time_seconds=36)),
        _draft(
            EventType.OPERATION_COMPLETED,
            36,
            OperationCompletedPayload(attempt_id=restarted.attempt_id),
        ),
    )

    final_main_order = Order(
        order_id=main_order.order_id,
        product_id=main_order.product_id,
        quantity=main_order.quantity,
        release_time_seconds=main_order.release_time_seconds,
        due_time_seconds=main_order.due_time_seconds,
        priority_weight=main_order.priority_weight,
        status=OrderStatus.COMPLETED,
    )
    final_second = Operation(
        operation_id=second_ready.operation_id,
        order_id=second_ready.order_id,
        unit_index=second_ready.unit_index,
        operation_template_id=second_ready.operation_template_id,
        sequence_index=second_ready.sequence_index,
        required_capability=second_ready.required_capability,
        duration_seconds=second_ready.duration_seconds,
        predecessor_operation_ids=second_ready.predecessor_operation_ids,
        status=OperationStatus.COMPLETED,
    )
    completed_restart = OperationAttempt(
        attempt_id=restarted.attempt_id,
        operation_id=restarted.operation_id,
        attempt_number=restarted.attempt_number,
        machine_id=restarted.machine_id,
        planned_duration_seconds=restarted.planned_duration_seconds,
        status=AttemptStatus.COMPLETED,
        scheduled_window=restarted.scheduled_window,
        actual_start_seconds=16,
        actual_end_seconds=36,
        consumed_seconds=20,
        restart_of_attempt_id=restarted.restart_of_attempt_id,
    )
    expected = OperationalSnapshot(
        schema_version=1,
        run_id=initial_snapshot.run_id,
        state_version=6,
        plan_revision=2,
        simulated_time_seconds=36,
        hard_horizon_seconds=100,
        machines=_machines(),
        orders=(final_main_order, urgent_order),
        operations=(completed_first, final_second),
        attempts=(completed_first_attempt, interrupted_second_attempt, completed_restart),
    )
    assert checkpoint.snapshot == expected
    return HandBuiltReplay(
        initial=SnapshotCheckpoint.create(initial_snapshot),
        envelopes=tuple(all_envelopes),
        expected_snapshot=expected,
        group_end_offsets=tuple(group_ends),
    )
