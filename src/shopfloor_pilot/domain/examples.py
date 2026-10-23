"""Small executable examples for round-trip and interruption contracts."""

from __future__ import annotations

from dataclasses import dataclass

from .canonical import canonical_hash
from .fixtures import ScenarioBundle
from .models import (
    ApprovalDecision,
    ApprovalRecord,
    AttemptStatus,
    CandidateProposal,
    Capability,
    DomainEvent,
    EventType,
    MachineStatus,
    Observation,
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
    ScheduleObjective,
    TelemetryValue,
    TimeWindow,
)


@dataclass(frozen=True)
class RepresentativeContracts:
    snapshot: OperationalSnapshot
    candidate: CandidateProposal
    approval: ApprovalRecord
    observation: Observation
    event: DomainEvent


def build_representative_contracts(bundle: ScenarioBundle) -> RepresentativeContracts:
    """Build a completed + interrupted + full-restart example.

    The returned Pydantic records are frozen. Approval records are deliberately
    separate from snapshots/candidates and do not assert that a plan was applied.
    """

    base_order = bundle.scenario.orders[0]
    completed_operation = Operation(
        operation_id="op-example-completed",
        order_id=base_order.order_id,
        unit_index=1,
        operation_template_id="plate-drill",
        sequence_index=0,
        required_capability=Capability.DRILLING,
        duration_seconds=420,
        status=OperationStatus.COMPLETED,
    )
    interrupted_operation = Operation(
        operation_id="op-example-interrupted",
        order_id=base_order.order_id,
        unit_index=1,
        operation_template_id="plate-mill",
        sequence_index=1,
        required_capability=Capability.MILLING,
        duration_seconds=900,
        predecessor_operation_ids=(completed_operation.operation_id,),
        status=OperationStatus.INTERRUPTED,
    )
    completed_attempt = OperationAttempt(
        attempt_id="attempt-example-completed-1",
        operation_id=completed_operation.operation_id,
        attempt_number=1,
        machine_id="CNC01",
        planned_duration_seconds=420,
        status=AttemptStatus.COMPLETED,
        scheduled_window=TimeWindow(start_seconds=0, end_seconds=420),
        actual_start_seconds=0,
        actual_end_seconds=420,
        consumed_seconds=420,
    )
    running_operation = Operation(
        operation_id="op-example-running",
        order_id=base_order.order_id,
        unit_index=1,
        operation_template_id="base-tap",
        sequence_index=1,
        required_capability=Capability.TAPPING,
        duration_seconds=480,
        predecessor_operation_ids=(completed_operation.operation_id,),
        status=OperationStatus.RUNNING,
    )
    running_attempt = OperationAttempt(
        attempt_id="attempt-example-running-1",
        operation_id=running_operation.operation_id,
        attempt_number=1,
        machine_id="CNC01",
        planned_duration_seconds=480,
        status=AttemptStatus.RUNNING,
        scheduled_window=TimeWindow(start_seconds=420, end_seconds=900),
        actual_start_seconds=420,
        consumed_seconds=180,
    )
    interrupted_attempt = OperationAttempt(
        attempt_id="attempt-example-interrupted-1",
        operation_id=interrupted_operation.operation_id,
        attempt_number=1,
        machine_id="CNC02",
        planned_duration_seconds=900,
        status=AttemptStatus.INTERRUPTED,
        scheduled_window=TimeWindow(start_seconds=420, end_seconds=1320),
        actual_start_seconds=420,
        actual_end_seconds=600,
        consumed_seconds=180,
    )
    successor_attempt = OperationAttempt(
        attempt_id="attempt-example-interrupted-2",
        operation_id=interrupted_operation.operation_id,
        attempt_number=2,
        machine_id="CNC03",
        planned_duration_seconds=900,
        status=AttemptStatus.SCHEDULED,
        scheduled_window=TimeWindow(start_seconds=600, end_seconds=1500),
        consumed_seconds=0,
        restart_of_attempt_id=interrupted_attempt.attempt_id,
    )
    snapshot = OperationalSnapshot(
        schema_version=1,
        run_id=bundle.scenario.run_id,
        state_version=7,
        plan_revision=2,
        simulated_time_seconds=600,
        hard_horizon_seconds=bundle.scenario.hard_horizon_seconds,
        machines=bundle.catalog.machines,
        orders=bundle.scenario.orders,
        operations=(completed_operation, running_operation, interrupted_operation),
        attempts=(
            completed_attempt,
            running_attempt,
            interrupted_attempt,
            successor_attempt,
        ),
        recorded_at_utc="2026-10-20T15:00:00Z",
    )
    urgent_order = Order(
        order_id="order-example-urgent",
        product_id="prod_aluminum_mounting_plate",
        quantity=1,
        release_time_seconds=600,
        due_time_seconds=2400,
        priority_weight=10,
        status=OrderStatus.PENDING,
    )
    candidate = CandidateProposal.create(
        schema_version=1,
        proposal_id="proposal-example-recovery",
        run_id=snapshot.run_id,
        workflow_id="workflow-example-recovery",
        base_state_version=snapshot.state_version,
        base_snapshot_hash=canonical_hash(snapshot),
        proposed_plan_revision=3,
        assignments=(
            ScheduleAssignment(
                operation_id=interrupted_operation.operation_id,
                attempt_id=successor_attempt.attempt_id,
                machine_id="CNC03",
                window=TimeWindow(start_seconds=600, end_seconds=1500),
            ),
        ),
        objective=ScheduleObjective(
            weighted_total_tardiness_seconds=0,
            makespan_seconds=1500,
        ),
        proposed_order_deltas=(OrderDelta(kind=OrderDeltaKind.ADD, order=urgent_order),),
        proposed_resource_deltas=(
            ResourceDelta(
                kind=ResourceDeltaKind.MARK_DOWN,
                machine_id="CNC02",
                effective_time_seconds=600,
                new_status=MachineStatus.DOWN,
            ),
        ),
        created_at_utc="2026-10-20T15:00:01Z",
    )
    approval = ApprovalRecord(
        schema_version=1,
        approval_id="approval-example-recovery",
        proposal_id=candidate.proposal_id,
        proposal_hash=candidate.proposal_hash,
        run_id=candidate.run_id,
        workflow_id=candidate.workflow_id,
        base_state_version=candidate.base_state_version,
        decision=ApprovalDecision.APPROVED,
        actor="fixture-operator",
        decided_at_utc="2026-10-20T15:00:02Z",
        reason="Example approval; application remains a separate future action.",
    )
    observation = Observation(
        schema_version=1,
        observation_id="observation-example-cnc02",
        run_id=snapshot.run_id,
        machine_id="CNC02",
        observed_at_seconds=600,
        signals=(
            TelemetryValue(name="temperature_millicelsius", value=71400, unit="mdegC"),
            TelemetryValue(
                name="vibration_micrometers_per_second",
                value=8900,
                unit="um_per_s",
            ),
        ),
        summary="Elevated spindle temperature and vibration; cause is not asserted.",
        recorded_at_utc="2026-10-20T15:00:00Z",
    )
    event = DomainEvent(
        schema_version=1,
        event_id="event-example-machine-down",
        run_id=snapshot.run_id,
        sequence=12,
        simulated_time_seconds=600,
        event_type=EventType.MACHINE_DOWN,
        payload={"machine_id": "CNC02", "status": "down"},
        recorded_at_utc="2026-10-20T15:00:00Z",
    )
    return RepresentativeContracts(
        snapshot=snapshot,
        candidate=candidate,
        approval=approval,
        observation=observation,
        event=event,
    )
