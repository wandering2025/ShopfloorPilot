"""Typed manufacturing-cell contracts shared by later implementation steps."""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal, Self

from pydantic import Field, JsonValue, model_validator

from .base import (
    Identifier,
    PositiveQuantity,
    PositiveSeconds,
    PriorityWeight,
    Seconds,
    Sha256,
    StrictModel,
    WallClockTimestamp,
)
from .canonical import canonical_hash


class Capability(StrEnum):
    DRILLING = "drilling"
    TAPPING = "tapping"
    MILLING = "milling"
    SLOTTING = "slotting"
    INSPECTION = "inspection"


class ResourceKind(StrEnum):
    PROCESSING = "processing"
    INSPECTION = "inspection"


class MachineStatus(StrEnum):
    AVAILABLE = "available"
    RUNNING = "running"
    DOWN = "down"
    MAINTENANCE = "maintenance"


class OrderStatus(StrEnum):
    PENDING = "pending"
    RELEASED = "released"
    RUNNING = "running"
    COMPLETED = "completed"
    BLOCKED = "blocked"
    REJECTED = "rejected"


class OperationStatus(StrEnum):
    PENDING = "pending"
    READY = "ready"
    RUNNING = "running"
    COMPLETED = "completed"
    INTERRUPTED = "interrupted"
    BLOCKED = "blocked"


class AttemptStatus(StrEnum):
    SCHEDULED = "scheduled"
    RUNNING = "running"
    COMPLETED = "completed"
    INTERRUPTED = "interrupted"
    CANCELLED = "cancelled"


class EventType(StrEnum):
    TIME_ADVANCED = "time_advanced"
    ORDER_REGISTERED = "order_registered"
    PLAN_APPLIED = "plan_applied"
    OPERATION_STARTED = "operation_started"
    OPERATION_COMPLETED = "operation_completed"
    OPERATION_INTERRUPTED = "operation_interrupted"
    MACHINE_DOWN = "machine_down"
    MACHINE_RESTORED = "machine_restored"
    OBSERVATION_RECORDED = "observation_recorded"


EVENT_TYPE_PRIORITY: dict[EventType, int] = {
    EventType.OPERATION_COMPLETED: 10,
    EventType.MACHINE_DOWN: 20,
    EventType.OPERATION_INTERRUPTED: 30,
    EventType.MACHINE_RESTORED: 40,
    EventType.ORDER_REGISTERED: 50,
    EventType.PLAN_APPLIED: 60,
    EventType.OPERATION_STARTED: 70,
    EventType.OBSERVATION_RECORDED: 80,
    EventType.TIME_ADVANCED: 90,
}


class ResultStatus(StrEnum):
    SUCCESS = "success"
    REJECTED = "rejected"
    CLARIFICATION_REQUIRED = "clarification_required"
    CONFLICT = "conflict"
    INFEASIBLE = "infeasible"
    UNKNOWN = "unknown"
    CANCELLED = "cancelled"
    PROVIDER_FAILURE = "provider_failure"


class FailureCode(StrEnum):
    UNSUPPORTED_PRODUCT = "unsupported_product"
    UNKNOWN_ROUTE = "unknown_route"
    INVALID_QUANTITY = "invalid_quantity"
    MISSING_PRODUCT = "missing_product"
    MISSING_QUANTITY = "missing_quantity"
    AMBIGUOUS_TIME = "ambiguous_time"
    UNKNOWN_CAPABILITY = "unknown_capability"
    INVALID_DURATION = "invalid_duration"
    INVALID_WINDOW = "invalid_window"
    DUPLICATE_ID = "duplicate_id"
    STALE_STATE = "stale_state"
    HASH_MISMATCH = "hash_mismatch"
    HARD_HORIZON = "hard_horizon"
    NO_CAPABLE_RESOURCE_WINDOW = "no_capable_resource_window"
    PROVIDER_UNAVAILABLE = "provider_unavailable"


class FeasibilityStatus(StrEnum):
    FEASIBLE_ON_TIME = "feasible_on_time"
    FEASIBLE_TARDY = "feasible_tardy"
    HARD_HORIZON_INFEASIBLE = "hard_horizon_infeasible"


class ApprovalDecision(StrEnum):
    APPROVED = "approved"
    REJECTED = "rejected"


class ResourceDeltaKind(StrEnum):
    MARK_DOWN = "mark_down"
    RESTORE = "restore"
    REPLACE_AVAILABILITY = "replace_availability"


class OrderDeltaKind(StrEnum):
    ADD = "add"
    REPLACE = "replace"


class TimeWindow(StrictModel):
    """A half-open simulated-time interval ``[start_seconds, end_seconds)``."""

    start_seconds: Seconds
    end_seconds: PositiveSeconds

    @model_validator(mode="after")
    def validate_bounds(self) -> Self:
        if self.end_seconds <= self.start_seconds:
            raise ValueError("end_seconds must be greater than start_seconds for [start, end)")
        return self

    @property
    def duration_seconds(self) -> int:
        return self.end_seconds - self.start_seconds

    def overlaps(self, other: TimeWindow) -> bool:
        return self.start_seconds < other.end_seconds and other.start_seconds < self.end_seconds


class Machine(StrictModel):
    machine_id: Identifier
    display_name: str = Field(min_length=1, max_length=80)
    kind: ResourceKind
    capabilities: tuple[Capability, ...] = Field(min_length=1)
    availability_windows: tuple[TimeWindow, ...] = Field(min_length=1)
    status: MachineStatus = MachineStatus.AVAILABLE

    @model_validator(mode="after")
    def validate_machine(self) -> Self:
        if len(set(self.capabilities)) != len(self.capabilities):
            raise ValueError("capabilities must be unique")
        if self.kind is ResourceKind.INSPECTION and self.capabilities != (Capability.INSPECTION,):
            raise ValueError("inspection resources must have exactly the inspection capability")
        if self.kind is ResourceKind.PROCESSING and Capability.INSPECTION in self.capabilities:
            raise ValueError("processing resources cannot claim the inspection capability")
        previous: TimeWindow | None = None
        for window in self.availability_windows:
            if previous and window.start_seconds < previous.end_seconds:
                raise ValueError("availability_windows must be sorted and non-overlapping")
            previous = window
        return self


class OperationTemplate(StrictModel):
    operation_template_id: Identifier
    sequence_index: int = Field(strict=True, ge=0)
    name: str = Field(min_length=1, max_length=80)
    required_capability: Capability
    duration_seconds: PositiveSeconds


class ProductTemplate(StrictModel):
    product_id: Identifier
    canonical_name: str = Field(min_length=1, max_length=100)
    route_id: Identifier
    operations: tuple[OperationTemplate, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_route(self) -> Self:
        operation_ids = [item.operation_template_id for item in self.operations]
        if len(operation_ids) != len(set(operation_ids)):
            raise ValueError("operation_template_id values must be unique within a route")
        indexes = [item.sequence_index for item in self.operations]
        if indexes != list(range(len(self.operations))):
            raise ValueError("operation sequence_index values must be contiguous from zero")
        if self.operations[-1].required_capability is not Capability.INSPECTION:
            raise ValueError("every supported product route must end with inspection")
        return self


class DomainCatalog(StrictModel):
    schema_version: Literal[1]
    machines: tuple[Machine, ...] = Field(min_length=1)
    products: tuple[ProductTemplate, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_catalog(self) -> Self:
        machine_ids = [item.machine_id for item in self.machines]
        product_ids = [item.product_id for item in self.products]
        route_ids = [item.route_id for item in self.products]
        if len(machine_ids) != len(set(machine_ids)):
            raise ValueError("machine_id values must be unique")
        if len(product_ids) != len(set(product_ids)):
            raise ValueError("product_id values must be unique")
        if len(route_ids) != len(set(route_ids)):
            raise ValueError("route_id values must be unique")
        available_capabilities = {
            capability for machine in self.machines for capability in machine.capabilities
        }
        required_capabilities = {
            operation.required_capability
            for product in self.products
            for operation in product.operations
        }
        missing = sorted(item.value for item in required_capabilities - available_capabilities)
        if missing:
            raise ValueError(f"routes reference unsupported capabilities: {missing}")
        return self


class Order(StrictModel):
    order_id: Identifier
    product_id: Identifier
    quantity: PositiveQuantity
    release_time_seconds: Seconds
    due_time_seconds: Seconds
    priority_weight: PriorityWeight
    status: OrderStatus = OrderStatus.PENDING

    @model_validator(mode="after")
    def validate_due_time(self) -> Self:
        if self.due_time_seconds < self.release_time_seconds:
            raise ValueError("due_time_seconds must not precede release_time_seconds")
        return self


class Operation(StrictModel):
    operation_id: Identifier
    order_id: Identifier
    unit_index: PositiveQuantity
    operation_template_id: Identifier
    sequence_index: int = Field(strict=True, ge=0)
    required_capability: Capability
    duration_seconds: PositiveSeconds
    predecessor_operation_ids: tuple[Identifier, ...] = ()
    status: OperationStatus = OperationStatus.PENDING

    @model_validator(mode="after")
    def validate_predecessors(self) -> Self:
        if self.operation_id in self.predecessor_operation_ids:
            raise ValueError("an operation cannot precede itself")
        if len(self.predecessor_operation_ids) != len(set(self.predecessor_operation_ids)):
            raise ValueError("predecessor_operation_ids must be unique")
        return self


class OperationAttempt(StrictModel):
    attempt_id: Identifier
    operation_id: Identifier
    attempt_number: int = Field(strict=True, ge=1)
    machine_id: Identifier
    planned_duration_seconds: PositiveSeconds
    status: AttemptStatus
    scheduled_window: TimeWindow | None = None
    actual_start_seconds: Seconds | None = None
    actual_end_seconds: Seconds | None = None
    consumed_seconds: Seconds = 0
    restart_of_attempt_id: Identifier | None = None

    @model_validator(mode="after")
    def validate_attempt(self) -> Self:
        if self.consumed_seconds > self.planned_duration_seconds:
            raise ValueError("consumed_seconds cannot exceed planned_duration_seconds")
        if self.scheduled_window and (
            self.scheduled_window.duration_seconds != self.planned_duration_seconds
        ):
            raise ValueError("scheduled_window must reserve the full planned duration")
        if self.actual_end_seconds is not None and self.actual_start_seconds is None:
            raise ValueError("actual_end_seconds requires actual_start_seconds")
        elapsed: int | None = None
        if self.actual_start_seconds is not None and self.actual_end_seconds is not None:
            if self.actual_end_seconds <= self.actual_start_seconds:
                raise ValueError("actual_end_seconds must be greater than actual_start_seconds")
            elapsed = self.actual_end_seconds - self.actual_start_seconds
        if self.status is AttemptStatus.COMPLETED:
            if elapsed is None:
                raise ValueError("completed attempts require actual start and end")
            if elapsed != self.planned_duration_seconds:
                raise ValueError("completed attempts must consume their full planned duration")
            if self.consumed_seconds != self.planned_duration_seconds:
                raise ValueError("completed attempts require full consumed_seconds")
        elif self.status is AttemptStatus.INTERRUPTED:
            if elapsed is None:
                raise ValueError("interrupted attempts require actual start and end")
            if elapsed != self.consumed_seconds:
                raise ValueError("interrupted consumed_seconds must equal actual elapsed time")
            if not 0 < self.consumed_seconds < self.planned_duration_seconds:
                raise ValueError("interrupted attempts require partial positive consumed time")
        elif self.status is AttemptStatus.RUNNING:
            if self.actual_start_seconds is None or self.actual_end_seconds is not None:
                raise ValueError("running attempts require a start and no end")
            if self.consumed_seconds >= self.planned_duration_seconds:
                raise ValueError("running attempts cannot have consumed their full duration")
        elif self.status is AttemptStatus.SCHEDULED:
            if self.actual_start_seconds is not None or self.actual_end_seconds is not None:
                raise ValueError("scheduled attempts cannot have actual times")
            if self.consumed_seconds != 0:
                raise ValueError("scheduled attempts cannot have consumed time")
        if self.restart_of_attempt_id is not None and self.attempt_number == 1:
            raise ValueError("a first attempt cannot restart another attempt")
        return self

    @property
    def actual_window(self) -> TimeWindow | None:
        if self.actual_start_seconds is None or self.actual_end_seconds is None:
            return None
        return TimeWindow(
            start_seconds=self.actual_start_seconds,
            end_seconds=self.actual_end_seconds,
        )


class OperationalSnapshot(StrictModel):
    schema_version: Literal[1]
    run_id: Identifier
    state_version: int = Field(strict=True, ge=0)
    plan_revision: int = Field(strict=True, ge=0)
    simulated_time_seconds: Seconds
    hard_horizon_seconds: PositiveSeconds
    machines: tuple[Machine, ...]
    orders: tuple[Order, ...]
    operations: tuple[Operation, ...]
    attempts: tuple[OperationAttempt, ...]
    recorded_at_utc: WallClockTimestamp | None = None

    @model_validator(mode="after")
    def validate_snapshot(self) -> Self:
        if self.simulated_time_seconds > self.hard_horizon_seconds:
            raise ValueError("simulated_time_seconds cannot exceed hard_horizon_seconds")

        def unique(values: list[str], label: str) -> set[str]:
            result = set(values)
            if len(values) != len(result):
                raise ValueError(f"{label} values must be unique")
            return result

        machine_ids = unique([item.machine_id for item in self.machines], "machine_id")
        order_ids = unique([item.order_id for item in self.orders], "order_id")
        operation_ids = unique([item.operation_id for item in self.operations], "operation_id")
        unique([item.attempt_id for item in self.attempts], "attempt_id")
        order_by_id = {item.order_id: item for item in self.orders}
        operation_by_id = {item.operation_id: item for item in self.operations}
        machine_by_id = {item.machine_id: item for item in self.machines}
        attempt_by_id = {item.attempt_id: item for item in self.attempts}

        for operation in self.operations:
            if operation.order_id not in order_ids:
                raise ValueError(f"operation {operation.operation_id} references unknown order")
            if operation.unit_index > order_by_id[operation.order_id].quantity:
                raise ValueError(f"operation {operation.operation_id} unit_index exceeds quantity")
            unknown_predecessors = set(operation.predecessor_operation_ids) - operation_ids
            if unknown_predecessors:
                raise ValueError(
                    f"operation {operation.operation_id} references unknown predecessors"
                )
        for attempt in self.attempts:
            if attempt.operation_id not in operation_ids:
                raise ValueError(f"attempt {attempt.attempt_id} references unknown operation")
            if attempt.machine_id not in machine_ids:
                raise ValueError(f"attempt {attempt.attempt_id} references unknown machine")
            operation = operation_by_id[attempt.operation_id]
            machine = machine_by_id[attempt.machine_id]
            if operation.required_capability not in machine.capabilities:
                raise ValueError(f"attempt {attempt.attempt_id} uses an incompatible machine")
            if attempt.planned_duration_seconds != operation.duration_seconds:
                raise ValueError(f"attempt {attempt.attempt_id} changes operation duration")
            if attempt.restart_of_attempt_id:
                predecessor = attempt_by_id.get(attempt.restart_of_attempt_id)
                if predecessor is None:
                    raise ValueError(f"attempt {attempt.attempt_id} restart target is unknown")
                if predecessor.operation_id != attempt.operation_id:
                    raise ValueError(f"attempt {attempt.attempt_id} restarts another operation")
                if predecessor.status is not AttemptStatus.INTERRUPTED:
                    raise ValueError(f"attempt {attempt.attempt_id} must restart an interruption")
                if attempt.planned_duration_seconds != predecessor.planned_duration_seconds:
                    raise ValueError(f"attempt {attempt.attempt_id} must restart full duration")
        return self


class ScheduleAssignment(StrictModel):
    operation_id: Identifier
    attempt_id: Identifier
    machine_id: Identifier
    window: TimeWindow


class OrderDelta(StrictModel):
    kind: OrderDeltaKind
    order: Order


class ResourceDelta(StrictModel):
    kind: ResourceDeltaKind
    machine_id: Identifier
    effective_time_seconds: Seconds
    new_status: MachineStatus | None = None
    availability_windows: tuple[TimeWindow, ...] | None = None

    @model_validator(mode="after")
    def validate_delta(self) -> Self:
        if self.kind is ResourceDeltaKind.MARK_DOWN and self.new_status is not MachineStatus.DOWN:
            raise ValueError("mark_down requires new_status=down")
        if (
            self.kind is ResourceDeltaKind.RESTORE
            and self.new_status is not MachineStatus.AVAILABLE
        ):
            raise ValueError("restore requires new_status=available")
        if self.kind is ResourceDeltaKind.REPLACE_AVAILABILITY:
            if not self.availability_windows:
                raise ValueError("replace_availability requires availability_windows")
        elif self.availability_windows is not None:
            raise ValueError("availability_windows are only valid for replace_availability")
        return self


PROPOSAL_CONTENT_FIELDS = frozenset(
    {
        "schema_version",
        "proposal_id",
        "run_id",
        "workflow_id",
        "base_state_version",
        "base_snapshot_hash",
        "proposed_plan_revision",
        "assignments",
        "objective",
        "proposed_order_deltas",
        "proposed_resource_deltas",
    }
)


class ScheduleObjective(StrictModel):
    weighted_total_tardiness_seconds: Seconds
    makespan_seconds: Seconds
    ordering: Literal["lexicographic_weighted_tardiness_then_makespan"] = (
        "lexicographic_weighted_tardiness_then_makespan"
    )


class CandidateProposal(StrictModel):
    schema_version: Literal[1]
    proposal_id: Identifier
    run_id: Identifier
    workflow_id: Identifier
    base_state_version: int = Field(strict=True, ge=0)
    base_snapshot_hash: Sha256
    proposed_plan_revision: int = Field(strict=True, ge=1)
    assignments: tuple[ScheduleAssignment, ...]
    objective: ScheduleObjective
    proposed_order_deltas: tuple[OrderDelta, ...] = ()
    proposed_resource_deltas: tuple[ResourceDelta, ...] = ()
    proposal_hash: Sha256
    created_at_utc: WallClockTimestamp | None = None

    @classmethod
    def create(
        cls,
        *,
        schema_version: Literal[1],
        proposal_id: Identifier,
        run_id: Identifier,
        workflow_id: Identifier,
        base_state_version: int,
        base_snapshot_hash: Sha256,
        proposed_plan_revision: int,
        assignments: tuple[ScheduleAssignment, ...],
        objective: ScheduleObjective,
        proposed_order_deltas: tuple[OrderDelta, ...] = (),
        proposed_resource_deltas: tuple[ResourceDelta, ...] = (),
        created_at_utc: WallClockTimestamp | None = None,
    ) -> CandidateProposal:
        content: dict[str, Any] = {
            "schema_version": schema_version,
            "proposal_id": proposal_id,
            "run_id": run_id,
            "workflow_id": workflow_id,
            "base_state_version": base_state_version,
            "base_snapshot_hash": base_snapshot_hash,
            "proposed_plan_revision": proposed_plan_revision,
            "assignments": assignments,
            "objective": objective,
            "proposed_order_deltas": proposed_order_deltas,
            "proposed_resource_deltas": proposed_resource_deltas,
        }
        return cls.model_validate(
            {
                **content,
                "proposal_hash": canonical_hash(content),
                "created_at_utc": created_at_utc,
            }
        )

    def content_payload(self) -> dict[str, JsonValue]:
        return self.model_dump(mode="json", include=PROPOSAL_CONTENT_FIELDS)

    @model_validator(mode="after")
    def validate_candidate(self) -> Self:
        expected_hash = canonical_hash(self.content_payload())
        if self.proposal_hash != expected_hash:
            raise ValueError("proposal_hash does not match immutable proposal content")
        assignment_attempts = [item.attempt_id for item in self.assignments]
        assignment_operations = [item.operation_id for item in self.assignments]
        if len(assignment_attempts) != len(set(assignment_attempts)):
            raise ValueError("candidate contains duplicate attempt assignments")
        if len(assignment_operations) != len(set(assignment_operations)):
            raise ValueError("candidate contains duplicate operation assignments")
        order_delta_ids = [item.order.order_id for item in self.proposed_order_deltas]
        resource_delta_ids = [item.machine_id for item in self.proposed_resource_deltas]
        if len(order_delta_ids) != len(set(order_delta_ids)):
            raise ValueError("candidate contains duplicate order deltas")
        if len(resource_delta_ids) != len(set(resource_delta_ids)):
            raise ValueError("candidate contains duplicate resource deltas")
        return self


class ApprovalRecord(StrictModel):
    schema_version: Literal[1]
    approval_id: Identifier
    proposal_id: Identifier
    proposal_hash: Sha256
    run_id: Identifier
    workflow_id: Identifier
    base_state_version: int = Field(strict=True, ge=0)
    decision: ApprovalDecision
    actor: str = Field(min_length=1, max_length=100)
    decided_at_utc: WallClockTimestamp
    reason: str | None = Field(default=None, max_length=500)


class TelemetryValue(StrictModel):
    name: Identifier
    value: int = Field(strict=True)
    unit: str = Field(min_length=1, max_length=40)


class Observation(StrictModel):
    schema_version: Literal[1]
    observation_id: Identifier
    run_id: Identifier
    machine_id: Identifier
    observed_at_seconds: Seconds
    signals: tuple[TelemetryValue, ...] = Field(min_length=1)
    summary: str = Field(min_length=1, max_length=300)
    recorded_at_utc: WallClockTimestamp | None = None

    @model_validator(mode="after")
    def validate_signals(self) -> Self:
        names = [item.name for item in self.signals]
        if len(names) != len(set(names)):
            raise ValueError("observation signal names must be unique")
        return self


class DomainEvent(StrictModel):
    schema_version: Literal[1]
    event_id: Identifier
    run_id: Identifier
    sequence: int = Field(strict=True, ge=1)
    simulated_time_seconds: Seconds
    event_type: EventType
    payload: dict[str, JsonValue]
    recorded_at_utc: WallClockTimestamp | None = None

    @property
    def ordering_key(self) -> tuple[int, int, int]:
        return (
            self.simulated_time_seconds,
            EVENT_TYPE_PRIORITY[self.event_type],
            self.sequence,
        )


class FailureDetail(StrictModel):
    code: FailureCode
    field: str | None = None
    message: str = Field(min_length=1, max_length=300)
    references: dict[str, str] = Field(default_factory=dict)


class StatusResult(StrictModel):
    status: ResultStatus
    failures: tuple[FailureDetail, ...] = ()
    references: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_result(self) -> Self:
        if self.status is ResultStatus.SUCCESS and self.failures:
            raise ValueError("success results cannot include failures")
        if self.status is not ResultStatus.SUCCESS and not self.failures:
            raise ValueError("non-success results require at least one typed failure")
        return self


class FeasibilityResult(StrictModel):
    status: FeasibilityStatus
    total_tardiness_seconds: Seconds
    makespan_seconds: Seconds
    hard_horizon_seconds: PositiveSeconds
    failures: tuple[FailureDetail, ...] = ()

    @model_validator(mode="after")
    def validate_feasibility(self) -> Self:
        if self.makespan_seconds > self.hard_horizon_seconds:
            if self.status is not FeasibilityStatus.HARD_HORIZON_INFEASIBLE:
                raise ValueError("makespan beyond hard horizon must be hard-horizon infeasible")
        elif self.status is FeasibilityStatus.HARD_HORIZON_INFEASIBLE:
            if not self.failures:
                raise ValueError("hard-horizon infeasibility requires a typed hard failure")
        elif self.total_tardiness_seconds > 0:
            if self.status is not FeasibilityStatus.FEASIBLE_TARDY:
                raise ValueError("positive tardiness must be labeled feasible_tardy")
        elif self.status is not FeasibilityStatus.FEASIBLE_ON_TIME:
            raise ValueError("zero-tardiness feasible result must be labeled feasible_on_time")
        return self
