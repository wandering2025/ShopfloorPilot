"""Strict event-draft, persistence-envelope, checkpoint, and payload contracts."""

from __future__ import annotations

from typing import Literal, Self

from pydantic import Field, JsonValue, model_validator

from shopfloor_pilot.domain.base import (
    Identifier,
    Seconds,
    Sha256,
    StrictModel,
    WallClockTimestamp,
)
from shopfloor_pilot.domain.canonical import canonical_hash
from shopfloor_pilot.domain.models import (
    DomainEvent,
    EventType,
    Observation,
    Operation,
    OperationalSnapshot,
    OperationAttempt,
    Order,
    TimeWindow,
)


class EventDraft(StrictModel):
    """An operational fact before repository-owned identity and sequence allocation."""

    schema_version: Literal[1] = 1
    simulated_time_seconds: Seconds
    event_type: EventType
    payload: dict[str, JsonValue]
    recorded_at_utc: WallClockTimestamp | None = None


class EventEnvelope(StrictModel):
    """One persisted event plus its atomic mutation/version boundary."""

    schema_version: Literal[1] = 1
    mutation_id: Identifier
    base_state_version: int = Field(strict=True, ge=0)
    resulting_state_version: int = Field(strict=True, ge=1)
    event_index: int = Field(strict=True, ge=0)
    event_count: int = Field(strict=True, gt=0)
    event: DomainEvent

    @model_validator(mode="after")
    def validate_group_position(self) -> Self:
        if self.resulting_state_version != self.base_state_version + 1:
            raise ValueError("resulting_state_version must equal base_state_version + 1")
        if self.event_index >= self.event_count:
            raise ValueError("event_index must be less than event_count")
        return self


class SnapshotCheckpoint(StrictModel):
    """Replayable state at an event-group boundary.

    The last event time/priority make suffix ordering independently verifiable. Event
    and mutation histories retain enough identity information to reject duplicates
    that refer back into an already reduced prefix.
    """

    schema_version: Literal[1] = 1
    snapshot: OperationalSnapshot
    last_sequence: int = Field(strict=True, ge=0)
    snapshot_hash: Sha256
    last_event_simulated_time_seconds: Seconds | None = None
    last_event_priority: int | None = Field(default=None, strict=True, ge=1)
    seen_event_ids: tuple[Identifier, ...] = ()
    seen_mutation_ids: tuple[Identifier, ...] = ()

    @classmethod
    def create(cls, snapshot: OperationalSnapshot) -> SnapshotCheckpoint:
        """Create the version-zero replay anchor for a new run."""

        if snapshot.state_version != 0:
            raise ValueError("an initial checkpoint requires snapshot.state_version == 0")
        return cls(
            snapshot=snapshot,
            last_sequence=0,
            snapshot_hash=canonical_hash(snapshot),
        )

    @model_validator(mode="after")
    def validate_checkpoint(self) -> Self:
        if self.snapshot_hash != canonical_hash(self.snapshot):
            raise ValueError("snapshot_hash does not match canonical snapshot content")
        if len(set(self.seen_event_ids)) != len(self.seen_event_ids):
            raise ValueError("seen_event_ids must be unique")
        if len(set(self.seen_mutation_ids)) != len(self.seen_mutation_ids):
            raise ValueError("seen_mutation_ids must be unique")
        if len(self.seen_event_ids) != self.last_sequence:
            raise ValueError("seen_event_ids must contain one ID for every accepted sequence")
        if len(self.seen_mutation_ids) != self.snapshot.state_version:
            raise ValueError("seen_mutation_ids must contain one ID for every state version")
        ordering_metadata = (
            self.last_event_simulated_time_seconds,
            self.last_event_priority,
        )
        if self.last_sequence == 0:
            if ordering_metadata != (None, None):
                raise ValueError("an empty checkpoint cannot have last-event ordering metadata")
        elif None in ordering_metadata:
            raise ValueError("a non-empty checkpoint requires complete last-event metadata")
        return self

    @property
    def last_ordering_key(self) -> tuple[int, int, int] | None:
        if self.last_sequence == 0:
            return None
        assert self.last_event_simulated_time_seconds is not None
        assert self.last_event_priority is not None
        return (
            self.last_event_simulated_time_seconds,
            self.last_event_priority,
            self.last_sequence,
        )


class TimeAdvancedPayload(StrictModel):
    target_time_seconds: Seconds


class MachineDownPayload(StrictModel):
    machine_id: Identifier
    status: Literal["down"]
    availability_windows: tuple[TimeWindow, ...] | None = Field(default=None, min_length=1)


class MachineRestoredPayload(StrictModel):
    machine_id: Identifier
    status: Literal["available"]
    availability_windows: tuple[TimeWindow, ...] | None = Field(default=None, min_length=1)


class OrderRegisteredPayload(StrictModel):
    order: Order


class PlanAppliedPayload(StrictModel):
    plan_revision: int = Field(strict=True, ge=1)
    operations: tuple[Operation, ...]
    attempts: tuple[OperationAttempt, ...]


class OperationStartedPayload(StrictModel):
    attempt_id: Identifier


class OperationCompletedPayload(StrictModel):
    attempt_id: Identifier


class OperationInterruptedPayload(StrictModel):
    attempt_id: Identifier


class ObservationRecordedPayload(StrictModel):
    observation: Observation


EVENT_PAYLOAD_MODELS: dict[EventType, type[StrictModel]] = {
    EventType.TIME_ADVANCED: TimeAdvancedPayload,
    EventType.ORDER_REGISTERED: OrderRegisteredPayload,
    EventType.PLAN_APPLIED: PlanAppliedPayload,
    EventType.OPERATION_STARTED: OperationStartedPayload,
    EventType.OPERATION_COMPLETED: OperationCompletedPayload,
    EventType.OPERATION_INTERRUPTED: OperationInterruptedPayload,
    EventType.MACHINE_DOWN: MachineDownPayload,
    EventType.MACHINE_RESTORED: MachineRestoredPayload,
    EventType.OBSERVATION_RECORDED: ObservationRecordedPayload,
}

EventPayload = (
    TimeAdvancedPayload
    | MachineDownPayload
    | MachineRestoredPayload
    | OrderRegisteredPayload
    | PlanAppliedPayload
    | OperationStartedPayload
    | OperationCompletedPayload
    | OperationInterruptedPayload
    | ObservationRecordedPayload
)
