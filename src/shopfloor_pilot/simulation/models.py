"""Public command results and allowlisted simulator projections."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import Field

from shopfloor_pilot.domain.base import Identifier, Seconds, StrictModel
from shopfloor_pilot.domain.models import (
    DomainEvent,
    EventType,
    Machine,
    Observation,
    Operation,
    OperationalSnapshot,
    OperationAttempt,
    Order,
)
from shopfloor_pilot.events import EventEnvelope
from shopfloor_pilot.storage import MutationResult
from shopfloor_pilot.validation import ValidationReport


class CommandStatus(StrEnum):
    APPLIED = "applied"
    IDEMPOTENT = "idempotent"
    NO_OP = "no_op"


class BlockedReason(StrEnum):
    MACHINE_DOWN = "machine_down"
    MACHINE_MAINTENANCE = "machine_maintenance"
    MACHINE_BUSY = "machine_busy"
    OUTSIDE_AVAILABILITY_WINDOW = "outside_availability_window"
    PREDECESSOR_INCOMPLETE = "predecessor_incomplete"
    MISSED_SCHEDULED_START = "missed_scheduled_start"


class BlockedAttempt(StrictModel):
    attempt_id: Identifier
    operation_id: Identifier
    machine_id: Identifier
    scheduled_start_seconds: Seconds
    reason: BlockedReason


class PublicRuntimeEvent(StrictModel):
    """Allowlisted event metadata; arbitrary payload dictionaries are never exposed."""

    event_id: Identifier
    sequence: int = Field(strict=True, ge=1)
    simulated_time_seconds: Seconds
    event_type: EventType
    machine_id: Identifier | None = None
    attempt_id: Identifier | None = None
    operation_id: Identifier | None = None
    order_id: Identifier | None = None
    observation_id: Identifier | None = None
    plan_revision: int | None = Field(default=None, strict=True, ge=1)


class RuntimeProjection(StrictModel):
    """Agent-safe state reconstructed only from persisted snapshot/events."""

    schema_version: Literal[1] = 1
    run_id: Identifier
    state_version: int = Field(strict=True, ge=0)
    plan_revision: int = Field(strict=True, ge=0)
    simulated_time_seconds: Seconds
    hard_horizon_seconds: int = Field(strict=True, gt=0)
    machines: tuple[Machine, ...]
    orders: tuple[Order, ...]
    operations: tuple[Operation, ...]
    attempts: tuple[OperationAttempt, ...]
    observations: tuple[Observation, ...] = ()
    blocked_attempts: tuple[BlockedAttempt, ...] = ()
    events: tuple[PublicRuntimeEvent, ...] = ()


class ActivationResult(StrictModel):
    status: CommandStatus
    run_id: Identifier
    effective_time_seconds: Seconds
    previous_state_version: int = Field(strict=True, ge=0)
    resulting_state_version: int = Field(strict=True, ge=0)
    validation: ValidationReport
    snapshot: OperationalSnapshot
    mutation: MutationResult | None = None
    projection: RuntimeProjection

    @property
    def idempotent(self) -> bool:
        return self.status is CommandStatus.IDEMPOTENT

    @property
    def applied(self) -> bool:
        return self.status is CommandStatus.APPLIED

    @property
    def events(self) -> tuple[EventEnvelope, ...]:
        return () if self.mutation is None else self.mutation.events

    @property
    def domain_events(self) -> tuple[DomainEvent, ...]:
        return tuple(item.event for item in self.events)


class AdvanceResult(StrictModel):
    status: CommandStatus
    run_id: Identifier
    requested_target_seconds: Seconds
    previous_time_seconds: Seconds
    resulting_time_seconds: Seconds
    previous_state_version: int = Field(strict=True, ge=0)
    resulting_state_version: int = Field(strict=True, ge=0)
    snapshot: OperationalSnapshot
    mutations: tuple[MutationResult, ...] = ()
    projection: RuntimeProjection

    @property
    def idempotent(self) -> bool:
        return self.status is CommandStatus.IDEMPOTENT

    @property
    def events(self) -> tuple[EventEnvelope, ...]:
        return tuple(event for mutation in self.mutations for event in mutation.events)

    @property
    def domain_events(self) -> tuple[DomainEvent, ...]:
        return tuple(item.event for item in self.events)
