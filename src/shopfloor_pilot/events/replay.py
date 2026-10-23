"""Deterministic operational event materialization, reduction, and replay."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from pydantic import BaseModel, ValidationError

from shopfloor_pilot.domain.canonical import canonical_hash, canonical_json_bytes
from shopfloor_pilot.domain.models import (
    EVENT_TYPE_PRIORITY,
    AttemptStatus,
    DomainEvent,
    EventType,
    Machine,
    MachineStatus,
    Operation,
    OperationalSnapshot,
    OperationAttempt,
    OperationStatus,
    Order,
    OrderStatus,
)

from .errors import (
    EventConflictError,
    GroupVersionConflictError,
    MalformedEnvelopeError,
    MalformedPayloadError,
    OrderingConflictError,
    ReductionConflictError,
    ReplayError,
    SequenceConflictError,
    UnsupportedSchemaError,
)
from .models import (
    EVENT_PAYLOAD_MODELS,
    EventDraft,
    EventEnvelope,
    EventPayload,
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
)


def _json_model[ModelT: BaseModel](model_type: type[ModelT], raw: Any) -> ModelT:
    """Validate JSON-shaped data while retaining strict JSON semantics."""

    return model_type.model_validate_json(
        json.dumps(raw, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    )


def _validated_update[ModelT: BaseModel](model: ModelT, **updates: Any) -> ModelT:
    """Rebuild and validate a frozen model; never use unchecked ``model_copy`` updates."""

    data = model.model_dump(mode="python")
    data.update(updates)
    return type(model).model_validate(data)


def _schema_value(raw: Mapping[str, Any], label: str) -> Any:
    if "schema_version" not in raw:
        raise MalformedEnvelopeError(f"{label} is missing schema_version")
    return raw["schema_version"]


def _coerce_draft(raw: EventDraft | Mapping[str, Any], index: int) -> EventDraft:
    if isinstance(raw, EventDraft):
        raw = raw.model_dump(mode="json")
    if not isinstance(raw, Mapping):
        raise MalformedEnvelopeError(f"draft {index} must be an EventDraft or mapping")
    schema_version = _schema_value(raw, f"draft {index}")
    if schema_version != 1:
        raise UnsupportedSchemaError(
            f"draft {index} uses schema_version={schema_version!r}; supported=1"
        )
    try:
        return _json_model(EventDraft, raw)
    except (ValidationError, TypeError, ValueError) as exc:
        raise MalformedEnvelopeError(f"draft {index} is invalid: {exc}") from exc


def _coerce_envelope(raw: EventEnvelope | Mapping[str, Any], index: int) -> EventEnvelope:
    if isinstance(raw, EventEnvelope):
        raw = raw.model_dump(mode="json")
    if not isinstance(raw, Mapping):
        raise MalformedEnvelopeError(f"envelope {index} must be an EventEnvelope or mapping")
    schema_version = _schema_value(raw, f"envelope {index}")
    if schema_version != 1:
        raise UnsupportedSchemaError(
            f"envelope {index} uses schema_version={schema_version!r}; supported=1"
        )
    event = raw.get("event")
    if not isinstance(event, Mapping):
        raise MalformedEnvelopeError(f"envelope {index} is missing a structured event")
    event_schema = _schema_value(event, f"envelope {index} event")
    if event_schema != 1:
        raise UnsupportedSchemaError(
            f"envelope {index} event uses schema_version={event_schema!r}; supported=1"
        )
    if "sequence" not in event:
        raise SequenceConflictError(f"envelope {index} event is missing required sequence")
    try:
        return _json_model(EventEnvelope, raw)
    except (ValidationError, TypeError, ValueError) as exc:
        raise MalformedEnvelopeError(f"envelope {index} is invalid: {exc}") from exc


def _parse_payload(event: DomainEvent) -> EventPayload:
    model_type = EVENT_PAYLOAD_MODELS[event.event_type]
    try:
        return _json_model(model_type, event.payload)  # type: ignore[return-value]
    except (ValidationError, TypeError, ValueError) as exc:
        raise MalformedPayloadError(
            f"event {event.event_id} ({event.event_type.value}) payload is invalid: {exc}"
        ) from exc


def _event_id(run_id: str, mutation_id: str, event_index: int) -> str:
    identity = canonical_json_bytes(
        {
            "namespace": "shopfloor-pilot-event-v1",
            "run_id": run_id,
            "mutation_id": mutation_id,
            "event_index": event_index,
        },
        exclude_wall_clock=False,
    )
    return f"event-{hashlib.sha256(identity).hexdigest()[:40]}"


def materialize_event_group(
    checkpoint: SnapshotCheckpoint,
    mutation_id: str,
    drafts: Iterable[EventDraft | Mapping[str, Any]],
) -> tuple[EventEnvelope, ...]:
    """Validate and deterministically allocate one atomic operational event group."""

    parsed = tuple(_coerce_draft(item, index) for index, item in enumerate(drafts))
    if not parsed:
        raise GroupVersionConflictError("an operational mutation requires at least one event")
    event_times = {item.simulated_time_seconds for item in parsed}
    if len(event_times) != 1:
        raise OrderingConflictError("all events in one mutation must use the same simulated second")
    event_time = parsed[0].simulated_time_seconds
    snapshot = checkpoint.snapshot
    if event_time < snapshot.simulated_time_seconds:
        raise OrderingConflictError(
            f"event time {event_time} precedes snapshot time {snapshot.simulated_time_seconds}"
        )
    if event_time > snapshot.hard_horizon_seconds:
        raise OrderingConflictError(
            f"event time {event_time} exceeds hard horizon {snapshot.hard_horizon_seconds}"
        )

    ordered = tuple(
        item
        for _, item in sorted(
            enumerate(parsed),
            key=lambda pair: (EVENT_TYPE_PRIORITY[pair[1].event_type], pair[0]),
        )
    )
    for draft in ordered:
        temporary_event = DomainEvent(
            schema_version=1,
            event_id="event-payload-validation",
            run_id=snapshot.run_id,
            sequence=1,
            simulated_time_seconds=draft.simulated_time_seconds,
            event_type=draft.event_type,
            payload=dict(draft.payload),
            recorded_at_utc=draft.recorded_at_utc,
        )
        _parse_payload(temporary_event)

    first_key = (
        event_time,
        EVENT_TYPE_PRIORITY[ordered[0].event_type],
        checkpoint.last_sequence + 1,
    )
    if checkpoint.last_ordering_key is not None and first_key <= checkpoint.last_ordering_key:
        raise OrderingConflictError(
            f"new group begins at {first_key}, not after {checkpoint.last_ordering_key}"
        )

    count = len(ordered)
    envelopes: list[EventEnvelope] = []
    for event_index, draft in enumerate(ordered):
        sequence = checkpoint.last_sequence + event_index + 1
        event = DomainEvent(
            schema_version=1,
            event_id=_event_id(snapshot.run_id, mutation_id, event_index),
            run_id=snapshot.run_id,
            sequence=sequence,
            simulated_time_seconds=draft.simulated_time_seconds,
            event_type=draft.event_type,
            payload=dict(draft.payload),
            recorded_at_utc=draft.recorded_at_utc,
        )
        try:
            envelope = EventEnvelope(
                schema_version=1,
                mutation_id=mutation_id,
                base_state_version=snapshot.state_version,
                resulting_state_version=snapshot.state_version + 1,
                event_index=event_index,
                event_count=count,
                event=event,
            )
        except ValidationError as exc:
            raise MalformedEnvelopeError(f"cannot materialize event group: {exc}") from exc
        envelopes.append(envelope)
    return tuple(envelopes)


def _replace_machine(
    snapshot: OperationalSnapshot,
    machine_id: str,
    *,
    status: MachineStatus,
    availability_windows: tuple[Any, ...] | None,
) -> OperationalSnapshot:
    machines = list(snapshot.machines)
    for index, machine in enumerate(machines):
        if machine.machine_id != machine_id:
            continue
        updates: dict[str, Any] = {"status": status}
        if availability_windows is not None:
            updates["availability_windows"] = availability_windows
        machines[index] = _validated_update(machine, **updates)
        return _validated_update(snapshot, machines=tuple(machines))
    raise ReductionConflictError(f"event references unknown machine {machine_id}")


def _replace_operation(snapshot: OperationalSnapshot, operation: Operation) -> OperationalSnapshot:
    operations = list(snapshot.operations)
    for index, existing in enumerate(operations):
        if existing.operation_id == operation.operation_id:
            operations[index] = operation
            return _validated_update(snapshot, operations=tuple(operations))
    raise ReductionConflictError(f"event references unknown operation {operation.operation_id}")


def _replace_attempt(
    snapshot: OperationalSnapshot, attempt: OperationAttempt
) -> OperationalSnapshot:
    attempts = list(snapshot.attempts)
    for index, existing in enumerate(attempts):
        if existing.attempt_id == attempt.attempt_id:
            attempts[index] = attempt
            return _validated_update(snapshot, attempts=tuple(attempts))
    raise ReductionConflictError(f"event references unknown attempt {attempt.attempt_id}")


def _replace_order(snapshot: OperationalSnapshot, order: Order) -> OperationalSnapshot:
    orders = list(snapshot.orders)
    for index, existing in enumerate(orders):
        if existing.order_id == order.order_id:
            orders[index] = order
            return _validated_update(snapshot, orders=tuple(orders))
    raise ReductionConflictError(f"event references unknown order {order.order_id}")


def _machine(snapshot: OperationalSnapshot, machine_id: str) -> Machine:
    try:
        return next(item for item in snapshot.machines if item.machine_id == machine_id)
    except StopIteration as exc:
        raise ReductionConflictError(f"event references unknown machine {machine_id}") from exc


def _attempt(snapshot: OperationalSnapshot, attempt_id: str) -> OperationAttempt:
    try:
        return next(item for item in snapshot.attempts if item.attempt_id == attempt_id)
    except StopIteration as exc:
        raise ReductionConflictError(f"event references unknown attempt {attempt_id}") from exc


def _operation(snapshot: OperationalSnapshot, operation_id: str) -> Operation:
    try:
        return next(item for item in snapshot.operations if item.operation_id == operation_id)
    except StopIteration as exc:
        raise ReductionConflictError(f"event references unknown operation {operation_id}") from exc


def _order(snapshot: OperationalSnapshot, order_id: str) -> Order:
    try:
        return next(item for item in snapshot.orders if item.order_id == order_id)
    except StopIteration as exc:
        raise ReductionConflictError(f"event references unknown order {order_id}") from exc


def _reduce_machine_down(
    snapshot: OperationalSnapshot, payload: MachineDownPayload
) -> OperationalSnapshot:
    machine = _machine(snapshot, payload.machine_id)
    if machine.status is MachineStatus.DOWN:
        raise ReductionConflictError(f"machine {machine.machine_id} is already down")
    return _replace_machine(
        snapshot,
        machine.machine_id,
        status=MachineStatus.DOWN,
        availability_windows=payload.availability_windows,
    )


def _reduce_machine_restored(
    snapshot: OperationalSnapshot, payload: MachineRestoredPayload
) -> OperationalSnapshot:
    machine = _machine(snapshot, payload.machine_id)
    if machine.status not in (MachineStatus.DOWN, MachineStatus.MAINTENANCE):
        raise ReductionConflictError(
            f"machine {machine.machine_id} cannot be restored from {machine.status.value}"
        )
    return _replace_machine(
        snapshot,
        machine.machine_id,
        status=MachineStatus.AVAILABLE,
        availability_windows=payload.availability_windows,
    )


def _reduce_order_registered(
    snapshot: OperationalSnapshot, payload: OrderRegisteredPayload
) -> OperationalSnapshot:
    if any(item.order_id == payload.order.order_id for item in snapshot.orders):
        raise ReductionConflictError(f"order {payload.order.order_id} is already registered")
    return _validated_update(snapshot, orders=(*snapshot.orders, payload.order))


def _reduce_plan_applied(
    snapshot: OperationalSnapshot, payload: PlanAppliedPayload
) -> OperationalSnapshot:
    if payload.plan_revision != snapshot.plan_revision + 1:
        raise ReductionConflictError(
            "plan_applied plan_revision must increment the current revision exactly once"
        )
    new_operations = {item.operation_id: item for item in payload.operations}
    new_attempts = {item.attempt_id: item for item in payload.attempts}
    for old in snapshot.operations:
        if old.status in (OperationStatus.COMPLETED, OperationStatus.RUNNING):
            if new_operations.get(old.operation_id) != old:
                raise ReductionConflictError(
                    f"plan_applied changed fixed operation {old.operation_id}"
                )
    fixed_attempt_statuses = {
        AttemptStatus.RUNNING,
        AttemptStatus.COMPLETED,
        AttemptStatus.INTERRUPTED,
        AttemptStatus.CANCELLED,
    }
    for old in snapshot.attempts:
        if old.status in fixed_attempt_statuses and new_attempts.get(old.attempt_id) != old:
            raise ReductionConflictError(f"plan_applied changed fixed attempt {old.attempt_id}")
    return _validated_update(
        snapshot,
        plan_revision=payload.plan_revision,
        operations=payload.operations,
        attempts=payload.attempts,
    )


def _reduce_operation_started(
    snapshot: OperationalSnapshot,
    event: DomainEvent,
    payload: OperationStartedPayload,
) -> OperationalSnapshot:
    attempt = _attempt(snapshot, payload.attempt_id)
    operation = _operation(snapshot, attempt.operation_id)
    machine = _machine(snapshot, attempt.machine_id)
    if attempt.status is not AttemptStatus.SCHEDULED:
        raise ReductionConflictError(f"attempt {attempt.attempt_id} is not scheduled")
    if operation.status not in (
        OperationStatus.PENDING,
        OperationStatus.READY,
        OperationStatus.INTERRUPTED,
    ):
        raise ReductionConflictError(
            f"operation {operation.operation_id} cannot start from {operation.status.value}"
        )
    if machine.status is not MachineStatus.AVAILABLE:
        raise ReductionConflictError(
            f"machine {machine.machine_id} cannot start work from {machine.status.value}"
        )
    if attempt.scheduled_window is not None:
        if attempt.scheduled_window.start_seconds != event.simulated_time_seconds:
            raise ReductionConflictError(
                f"attempt {attempt.attempt_id} starts outside its scheduled boundary"
            )
    started = _validated_update(
        attempt,
        status=AttemptStatus.RUNNING,
        actual_start_seconds=event.simulated_time_seconds,
        actual_end_seconds=None,
        consumed_seconds=0,
    )
    snapshot = _replace_attempt(snapshot, started)
    snapshot = _replace_operation(
        snapshot,
        _validated_update(operation, status=OperationStatus.RUNNING),
    )
    snapshot = _replace_machine(
        snapshot,
        machine.machine_id,
        status=MachineStatus.RUNNING,
        availability_windows=None,
    )
    order = _order(snapshot, operation.order_id)
    return _replace_order(snapshot, _validated_update(order, status=OrderStatus.RUNNING))


def _reduce_operation_completed(
    snapshot: OperationalSnapshot,
    event: DomainEvent,
    payload: OperationCompletedPayload,
) -> OperationalSnapshot:
    attempt = _attempt(snapshot, payload.attempt_id)
    operation = _operation(snapshot, attempt.operation_id)
    machine = _machine(snapshot, attempt.machine_id)
    if attempt.status is not AttemptStatus.RUNNING or attempt.actual_start_seconds is None:
        raise ReductionConflictError(f"attempt {attempt.attempt_id} is not running")
    elapsed = event.simulated_time_seconds - attempt.actual_start_seconds
    if elapsed != attempt.planned_duration_seconds:
        raise ReductionConflictError(
            f"attempt {attempt.attempt_id} completion elapsed {elapsed}, "
            f"expected {attempt.planned_duration_seconds}"
        )
    if machine.status is not MachineStatus.RUNNING:
        raise ReductionConflictError(f"machine {machine.machine_id} is not running at completion")
    completed = _validated_update(
        attempt,
        status=AttemptStatus.COMPLETED,
        actual_end_seconds=event.simulated_time_seconds,
        consumed_seconds=attempt.planned_duration_seconds,
    )
    snapshot = _replace_attempt(snapshot, completed)
    snapshot = _replace_operation(
        snapshot,
        _validated_update(operation, status=OperationStatus.COMPLETED),
    )
    snapshot = _replace_machine(
        snapshot,
        machine.machine_id,
        status=MachineStatus.AVAILABLE,
        availability_windows=None,
    )
    order = _order(snapshot, operation.order_id)
    order_operations = [item for item in snapshot.operations if item.order_id == order.order_id]
    order_status = (
        OrderStatus.COMPLETED
        if order_operations
        and all(item.status is OperationStatus.COMPLETED for item in order_operations)
        else OrderStatus.RUNNING
    )
    return _replace_order(snapshot, _validated_update(order, status=order_status))


def _reduce_operation_interrupted(
    snapshot: OperationalSnapshot,
    event: DomainEvent,
    payload: OperationInterruptedPayload,
) -> OperationalSnapshot:
    attempt = _attempt(snapshot, payload.attempt_id)
    operation = _operation(snapshot, attempt.operation_id)
    machine = _machine(snapshot, attempt.machine_id)
    if attempt.status is not AttemptStatus.RUNNING or attempt.actual_start_seconds is None:
        raise ReductionConflictError(f"attempt {attempt.attempt_id} is not running")
    elapsed = event.simulated_time_seconds - attempt.actual_start_seconds
    if not 0 < elapsed < attempt.planned_duration_seconds:
        raise ReductionConflictError(
            f"attempt {attempt.attempt_id} interruption elapsed {elapsed} is not partial"
        )
    if machine.status is not MachineStatus.DOWN:
        raise ReductionConflictError(
            f"machine {machine.machine_id} must be down before interruption"
        )
    interrupted = _validated_update(
        attempt,
        status=AttemptStatus.INTERRUPTED,
        actual_end_seconds=event.simulated_time_seconds,
        consumed_seconds=elapsed,
    )
    snapshot = _replace_attempt(snapshot, interrupted)
    snapshot = _replace_operation(
        snapshot,
        _validated_update(operation, status=OperationStatus.INTERRUPTED),
    )
    order = _order(snapshot, operation.order_id)
    return _replace_order(snapshot, _validated_update(order, status=OrderStatus.BLOCKED))


def _reduce_observation(
    snapshot: OperationalSnapshot,
    event: DomainEvent,
    payload: ObservationRecordedPayload,
) -> OperationalSnapshot:
    observation = payload.observation
    if observation.run_id != snapshot.run_id:
        raise EventConflictError(
            f"observation {observation.observation_id} belongs to run {observation.run_id}"
        )
    if observation.observed_at_seconds != event.simulated_time_seconds:
        raise ReductionConflictError(
            f"observation {observation.observation_id} time does not match its event"
        )
    _machine(snapshot, observation.machine_id)
    return snapshot


def _reduce_time_advanced(
    snapshot: OperationalSnapshot,
    event: DomainEvent,
    payload: TimeAdvancedPayload,
) -> OperationalSnapshot:
    if payload.target_time_seconds != event.simulated_time_seconds:
        raise ReductionConflictError("time_advanced target must equal event simulated time")
    if payload.target_time_seconds <= snapshot.simulated_time_seconds:
        raise ReductionConflictError("time_advanced target must be strictly forward")
    if payload.target_time_seconds > snapshot.hard_horizon_seconds:
        raise ReductionConflictError("time_advanced target exceeds the hard horizon")
    return _validated_update(snapshot, simulated_time_seconds=payload.target_time_seconds)


def _reduce_event(
    snapshot: OperationalSnapshot, event: DomainEvent, payload: EventPayload
) -> OperationalSnapshot:
    try:
        if event.event_type is EventType.MACHINE_DOWN:
            assert isinstance(payload, MachineDownPayload)
            return _reduce_machine_down(snapshot, payload)
        if event.event_type is EventType.MACHINE_RESTORED:
            assert isinstance(payload, MachineRestoredPayload)
            return _reduce_machine_restored(snapshot, payload)
        if event.event_type is EventType.ORDER_REGISTERED:
            assert isinstance(payload, OrderRegisteredPayload)
            return _reduce_order_registered(snapshot, payload)
        if event.event_type is EventType.PLAN_APPLIED:
            assert isinstance(payload, PlanAppliedPayload)
            return _reduce_plan_applied(snapshot, payload)
        if event.event_type is EventType.OPERATION_STARTED:
            assert isinstance(payload, OperationStartedPayload)
            return _reduce_operation_started(snapshot, event, payload)
        if event.event_type is EventType.OPERATION_COMPLETED:
            assert isinstance(payload, OperationCompletedPayload)
            return _reduce_operation_completed(snapshot, event, payload)
        if event.event_type is EventType.OPERATION_INTERRUPTED:
            assert isinstance(payload, OperationInterruptedPayload)
            return _reduce_operation_interrupted(snapshot, event, payload)
        if event.event_type is EventType.OBSERVATION_RECORDED:
            assert isinstance(payload, ObservationRecordedPayload)
            return _reduce_observation(snapshot, event, payload)
        if event.event_type is EventType.TIME_ADVANCED:
            assert isinstance(payload, TimeAdvancedPayload)
            return _reduce_time_advanced(snapshot, event, payload)
    except ReplayError:
        raise
    except (ValidationError, ValueError) as exc:
        raise ReductionConflictError(
            f"event {event.event_id} cannot produce a valid operational snapshot: {exc}"
        ) from exc
    raise MalformedPayloadError(f"event type {event.event_type.value} has no reducer")


def _prevalidate_group(
    checkpoint: SnapshotCheckpoint,
    envelopes: Sequence[EventEnvelope],
) -> tuple[tuple[EventEnvelope, EventPayload], ...]:
    if not envelopes:
        raise GroupVersionConflictError("an event group cannot be empty")
    first = envelopes[0]
    if first.event_index != 0:
        raise GroupVersionConflictError(
            "replay suffix must start at event_index 0 (an event-group boundary)"
        )
    if first.event_count != len(envelopes):
        raise GroupVersionConflictError(
            f"group declares {first.event_count} events but received {len(envelopes)}"
        )
    if first.base_state_version != checkpoint.snapshot.state_version:
        raise GroupVersionConflictError(
            f"group base version {first.base_state_version} does not match checkpoint "
            f"version {checkpoint.snapshot.state_version}"
        )
    if first.resulting_state_version != first.base_state_version + 1:
        raise GroupVersionConflictError("group resulting version must equal base version + 1")
    if first.mutation_id in checkpoint.seen_mutation_ids:
        raise GroupVersionConflictError(f"mutation ID {first.mutation_id} was already reduced")

    expected_sequence = checkpoint.last_sequence + 1
    previous_key = checkpoint.last_ordering_key
    event_time = first.event.simulated_time_seconds
    seen_ids = set(checkpoint.seen_event_ids)
    parsed: list[tuple[EventEnvelope, EventPayload]] = []
    plan_events = 0
    for index, envelope in enumerate(envelopes):
        if envelope.mutation_id != first.mutation_id:
            raise GroupVersionConflictError("one event group contains multiple mutation IDs")
        if (
            envelope.base_state_version != first.base_state_version
            or envelope.resulting_state_version != first.resulting_state_version
            or envelope.event_count != first.event_count
        ):
            raise GroupVersionConflictError("event envelopes disagree on their group boundary")
        if envelope.event_index != index:
            raise GroupVersionConflictError(
                f"group event_index {envelope.event_index} is not expected index {index}"
            )
        event = envelope.event
        if event.run_id != checkpoint.snapshot.run_id:
            raise EventConflictError(
                f"event {event.event_id} run {event.run_id} does not match "
                f"checkpoint run {checkpoint.snapshot.run_id}"
            )
        if event.sequence != expected_sequence:
            raise SequenceConflictError(
                f"event {event.event_id} has sequence {event.sequence}; "
                f"expected {expected_sequence}"
            )
        expected_sequence += 1
        if event.event_id in seen_ids:
            raise EventConflictError(f"event ID {event.event_id} is duplicated")
        seen_ids.add(event.event_id)
        if event.simulated_time_seconds != event_time:
            raise OrderingConflictError("all events in one group must share a simulated second")
        if event.simulated_time_seconds < checkpoint.snapshot.simulated_time_seconds:
            raise OrderingConflictError(
                f"event {event.event_id} predates the checkpoint snapshot time"
            )
        if event.simulated_time_seconds > checkpoint.snapshot.hard_horizon_seconds:
            raise OrderingConflictError(f"event {event.event_id} exceeds the hard horizon")
        ordering_key = event.ordering_key
        if previous_key is not None and ordering_key <= previous_key:
            raise OrderingConflictError(
                f"event {event.event_id} ordering key {ordering_key} is not after {previous_key}"
            )
        previous_key = ordering_key
        payload = _parse_payload(event)
        parsed.append((envelope, payload))
        if event.event_type is EventType.PLAN_APPLIED:
            plan_events += 1
    if plan_events > 1:
        raise GroupVersionConflictError("one mutation cannot apply more than one plan revision")
    return tuple(parsed)


def reduce_event_group(
    checkpoint: SnapshotCheckpoint,
    envelopes: Iterable[EventEnvelope | Mapping[str, Any]],
) -> SnapshotCheckpoint:
    """Apply exactly one complete event group and return a new immutable checkpoint."""

    raw_items = tuple(envelopes)
    parsed_envelopes = tuple(_coerce_envelope(item, index) for index, item in enumerate(raw_items))
    parsed = _prevalidate_group(checkpoint, parsed_envelopes)
    snapshot = checkpoint.snapshot
    for envelope, payload in parsed:
        snapshot = _reduce_event(snapshot, envelope.event, payload)
    group = parsed_envelopes[0]
    try:
        snapshot = _validated_update(snapshot, state_version=group.resulting_state_version)
        last_event = parsed_envelopes[-1].event
        return SnapshotCheckpoint(
            schema_version=1,
            snapshot=snapshot,
            last_sequence=last_event.sequence,
            snapshot_hash=canonical_hash(snapshot),
            last_event_simulated_time_seconds=last_event.simulated_time_seconds,
            last_event_priority=EVENT_TYPE_PRIORITY[last_event.event_type],
            seen_event_ids=(
                *checkpoint.seen_event_ids,
                *(item.event.event_id for item in parsed_envelopes),
            ),
            seen_mutation_ids=(*checkpoint.seen_mutation_ids, group.mutation_id),
        )
    except ValidationError as exc:
        raise ReductionConflictError(f"event group produced an invalid checkpoint: {exc}") from exc


def replay_events(
    checkpoint: SnapshotCheckpoint,
    envelopes: Iterable[EventEnvelope | Mapping[str, Any]],
) -> SnapshotCheckpoint:
    """Strictly replay zero or more complete groups from a trusted checkpoint."""

    raw_items = tuple(envelopes)
    if not raw_items:
        return checkpoint
    parsed = tuple(_coerce_envelope(item, index) for index, item in enumerate(raw_items))

    current = checkpoint
    offset = 0
    while offset < len(parsed):
        first = parsed[offset]
        if first.event_index != 0:
            raise GroupVersionConflictError(
                f"replay item {offset} begins at event_index {first.event_index}, not a boundary"
            )
        end = offset + first.event_count
        if end > len(parsed):
            raise GroupVersionConflictError(
                f"mutation {first.mutation_id} is truncated: expected {first.event_count} events"
            )
        current = reduce_event_group(current, parsed[offset:end])
        offset = end
    return current
