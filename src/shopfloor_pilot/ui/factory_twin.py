"""Read-only visual projection of one run for the factory-twin view.

The timeline is derived exclusively from authoritative records: the run's
version-zero checkpoint and its persisted operational event groups, reduced one
group at a time with the same pure reducer used by replay. Every keyframe is the
exact snapshot after one committed group. Nothing here writes to SQLite, calls a
service command, reads evaluator-only scenario data, or reads telemetry.

Between keyframes the only modeled change is the progress of running work, which
is defined by ``actual_start_seconds`` and ``planned_duration_seconds``; the
browser interpolates exactly that. Transport, operator movement and physical
causes are not modeled and are therefore not represented as facts.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from shopfloor_pilot.domain import (
    AttemptStatus,
    DomainCatalog,
    MachineStatus,
    OperationalSnapshot,
    OperationAttempt,
    OperationStatus,
)
from shopfloor_pilot.domain.models import EventType
from shopfloor_pilot.events import EventEnvelope, SnapshotCheckpoint, reduce_event_group

TWIN_SCHEMA = "shopfloor_pilot.factory_twin/1"

# Groups containing these events are "key" frames: playback can hold on them.
KEY_EVENT_TYPES = frozenset(
    {
        EventType.MACHINE_DOWN,
        EventType.OPERATION_INTERRUPTED,
        EventType.MACHINE_RESTORED,
        EventType.ORDER_REGISTERED,
        EventType.PLAN_APPLIED,
    }
)
# Groups made only of these events change no visual state except the clock.
_CLOCK_ONLY_EVENT_TYPES = frozenset({EventType.TIME_ADVANCED, EventType.OBSERVATION_RECORDED})

_MACHINE_DISPLAY_STATUS = {
    MachineStatus.AVAILABLE: "idle",
    MachineStatus.RUNNING: "running",
    MachineStatus.DOWN: "failed",
    MachineStatus.MAINTENANCE: "maintenance",
}


class TwinTimelineError(RuntimeError):
    """The recorded stream could not be projected; nothing is rendered as truth."""


@dataclass(frozen=True, slots=True)
class TwinVerification:
    """Replay of persisted events compared with the stored authoritative state."""

    state_version: int
    simulated_time_seconds: int
    stored_snapshot_hash: str
    replayed_snapshot_hash: str
    projection_matches: bool
    mismatches: tuple[str, ...]

    @property
    def hash_matches(self) -> bool:
        return self.stored_snapshot_hash == self.replayed_snapshot_hash

    @property
    def passed(self) -> bool:
        return self.hash_matches and self.projection_matches

    def as_payload(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "hash_matches": self.hash_matches,
            "projection_matches": self.projection_matches,
            "state_version": self.state_version,
            "simulated_time_seconds": self.simulated_time_seconds,
            "snapshot_hash": self.stored_snapshot_hash,
            "mismatches": list(self.mismatches),
        }


@dataclass(frozen=True, slots=True)
class TwinTimeline:
    """JSON-ready playback payload plus the server-side verification result."""

    payload: dict[str, Any]
    verification: TwinVerification

    @property
    def frames(self) -> list[dict[str, Any]]:
        return self.payload["frames"]


# ------------------------------------------------------------------ projection


def unit_key(order_id: str, unit_index: int) -> str:
    return f"{order_id}#{unit_index}"


def project_snapshot(snapshot: OperationalSnapshot) -> dict[str, Any]:
    """Visual state that is fully determined by one snapshot.

    This is also the contract checked at the end of browser playback, so it only
    contains fields both the replayed keyframe and the stored snapshot determine.
    History-derived annotations (``restored``, ``reassigned_from``) are excluded.
    """

    attempts_by_operation: dict[str, list[OperationAttempt]] = defaultdict(list)
    for attempt in snapshot.attempts:
        attempts_by_operation[attempt.operation_id].append(attempt)
    for items in attempts_by_operation.values():
        items.sort(key=lambda item: item.attempt_number)

    running_by_machine = {
        item.machine_id: item for item in snapshot.attempts if item.status is AttemptStatus.RUNNING
    }
    operations = {item.operation_id: item for item in snapshot.operations}

    machines: dict[str, dict[str, Any]] = {}
    for machine in snapshot.machines:
        running = running_by_machine.get(machine.machine_id)
        operation = None if running is None else operations[running.operation_id]
        machines[machine.machine_id] = {
            "status": _MACHINE_DISPLAY_STATUS[machine.status],
            "running": None
            if running is None or operation is None
            else {
                "attempt_id": running.attempt_id,
                "operation_id": running.operation_id,
                "unit": unit_key(operation.order_id, operation.unit_index),
                "step": operation.sequence_index,
                "attempt_number": running.attempt_number,
                "start_seconds": running.actual_start_seconds,
                "duration_seconds": running.planned_duration_seconds,
            },
            "queue": [],
            "held": [],
        }

    operations_by_unit: dict[str, list[Any]] = defaultdict(list)
    for operation in snapshot.operations:
        operations_by_unit[unit_key(operation.order_id, operation.unit_index)].append(operation)

    units: dict[str, dict[str, Any]] = {}
    queue_entries: dict[str, list[tuple[int, str]]] = defaultdict(list)
    for order in snapshot.orders:
        for unit_index in range(1, order.quantity + 1):
            key = unit_key(order.order_id, unit_index)
            chain = sorted(operations_by_unit.get(key, ()), key=lambda item: item.sequence_index)
            unit = _project_unit(chain, attempts_by_operation)
            units[key] = unit
            if unit["zone"] == "queue":
                queue_entries[unit["machine_id"]].append((unit["scheduled_start"], key))
            elif unit["zone"] == "held":
                machines[unit["machine_id"]]["held"].append(key)
    for machine_id, entries in queue_entries.items():
        machines[machine_id]["queue"] = [key for _, key in sorted(entries)]

    return {
        "state_version": snapshot.state_version,
        "plan_revision": snapshot.plan_revision,
        "simulated_time_seconds": snapshot.simulated_time_seconds,
        "machines": machines,
        "units": units,
    }


def _project_unit(
    chain: Sequence[Any],
    attempts_by_operation: Mapping[str, Sequence[OperationAttempt]],
) -> dict[str, Any]:
    total = len(chain)
    completed = sum(item.status is OperationStatus.COMPLETED for item in chain)
    base = {
        "completed_steps": completed,
        "total_steps": total,
        "operation_id": None,
        "step": None,
        "machine_id": None,
        "attempt_number": None,
        "scheduled_start": None,
    }
    if total == 0:
        return {**base, "zone": "incoming", "state": "unplanned"}
    if completed == total:
        return {**base, "zone": "finished", "state": "completed"}

    current = next(item for item in chain if item.status is not OperationStatus.COMPLETED)
    attempts = attempts_by_operation.get(current.operation_id, ())
    live = [item for item in attempts if item.status is not AttemptStatus.CANCELLED]
    latest = live[-1] if live else None
    located = {
        **base,
        "operation_id": current.operation_id,
        "step": current.sequence_index,
    }
    if latest is None:
        # A planned chain without an assignment for its next step (for example an
        # infeasible recovery). The unit waits where its last step left it.
        return {**located, "zone": "incoming" if completed == 0 else "buffer", "state": "waiting"}
    located["machine_id"] = latest.machine_id
    located["attempt_number"] = latest.attempt_number
    if latest.status is AttemptStatus.RUNNING:
        return {**located, "zone": "machine", "state": "running"}
    if latest.status is AttemptStatus.INTERRUPTED:
        return {**located, "zone": "held", "state": "interrupted"}
    if latest.status is AttemptStatus.SCHEDULED and latest.scheduled_window is not None:
        located["scheduled_start"] = latest.scheduled_window.start_seconds
        state = "queued_restart" if latest.restart_of_attempt_id else "queued"
        return {**located, "zone": "queue", "state": state}
    return {**located, "zone": "buffer", "state": "waiting"}


def compare_projections(
    displayed: Mapping[str, Any], authoritative: Mapping[str, Any]
) -> list[str]:
    """Field-level differences between two ``project_snapshot`` results."""

    mismatches: list[str] = []
    for field in ("state_version", "plan_revision", "simulated_time_seconds"):
        if displayed.get(field) != authoritative.get(field):
            mismatches.append(f"{field}: {displayed.get(field)!r} != {authoritative.get(field)!r}")
    for group in ("machines", "units"):
        left = displayed.get(group, {})
        right = authoritative.get(group, {})
        for key in sorted(set(left) | set(right)):
            if left.get(key) != right.get(key):
                mismatches.append(f"{group}.{key} differs")
    return mismatches


# -------------------------------------------------------------------- timeline


def build_twin_timeline(
    *,
    initial_checkpoint: SnapshotCheckpoint,
    envelopes: Sequence[EventEnvelope],
    stored_checkpoint: SnapshotCheckpoint,
    catalog: DomainCatalog,
    scenario_id: str | None = None,
) -> TwinTimeline:
    """Reduce every persisted group into one keyframe and verify the final state."""

    if initial_checkpoint.snapshot.state_version != 0:
        raise TwinTimelineError("playback must start from the version-zero checkpoint")
    run_id = initial_checkpoint.snapshot.run_id
    if stored_checkpoint.snapshot.run_id != run_id:
        raise TwinTimelineError("stored checkpoint belongs to a different run")

    tracker = _HistoryTracker()
    checkpoint = initial_checkpoint
    frames = [_frame(0, checkpoint.snapshot, (), tracker, {})]
    groups = _split_groups(envelopes)
    for position, group in enumerate(groups):
        previous = checkpoint.snapshot
        try:
            checkpoint = reduce_event_group(checkpoint, group)
        except Exception as exc:  # replay errors are typed; never render a guess
            raise TwinTimelineError(f"event replay failed: {exc}") from exc
        types = {item.event.event_type for item in group}
        reassignments = (
            _reassignments(previous, checkpoint.snapshot) if EventType.PLAN_APPLIED in types else {}
        )
        tracker.observe(group, reassignments, checkpoint.snapshot)
        is_last = position == len(groups) - 1
        if types <= _CLOCK_ONLY_EVENT_TYPES and not is_last:
            continue
        frames.append(_frame(len(frames), checkpoint.snapshot, group, tracker, reassignments))

    replayed = checkpoint.snapshot
    stored = stored_checkpoint.snapshot
    authoritative = project_snapshot(stored)
    mismatches = compare_projections(project_snapshot(replayed), authoritative)
    verification = TwinVerification(
        state_version=stored.state_version,
        simulated_time_seconds=stored.simulated_time_seconds,
        stored_snapshot_hash=stored_checkpoint.snapshot_hash,
        replayed_snapshot_hash=checkpoint.snapshot_hash,
        projection_matches=not mismatches,
        mismatches=tuple(mismatches),
    )
    payload = {
        "schema": TWIN_SCHEMA,
        "run_id": run_id,
        "scenario_id": scenario_id,
        "hard_horizon_seconds": stored.hard_horizon_seconds,
        "machines": [
            {
                "machine_id": item.machine_id,
                "display_name": item.display_name,
                "kind": item.kind.value,
                "capabilities": [capability.value for capability in item.capabilities],
            }
            for item in stored.machines
        ],
        "products": _products(catalog),
        "units": _units(frames, stored),
        "frames": frames,
        "authoritative": authoritative,
        "verification": verification.as_payload(),
    }
    return TwinTimeline(payload=payload, verification=verification)


def build_twin_timeline_from_repository(
    repository: Any,
    run_id: str,
    catalog: DomainCatalog,
    *,
    scenario_id: str | None = None,
    state_version: int | None = None,
) -> TwinTimeline:
    """Read-only convenience over ``SQLiteStateRepository`` reads.

    ``state_version`` pins the projection to one committed checkpoint (default:
    the current one), which makes the result a pure function of its arguments.
    """

    stored = repository.get_checkpoint(run_id, state_version=state_version)
    initial = repository.get_checkpoint(run_id, state_version=0)
    envelopes = repository.list_events(run_id)
    # Bound the stream to the checkpoint read above, in case a writer committed
    # between the reads; the next render picks the newer state up.
    envelopes = tuple(item for item in envelopes if item.event.sequence <= stored.last_sequence)
    return build_twin_timeline(
        initial_checkpoint=initial,
        envelopes=envelopes,
        stored_checkpoint=stored,
        catalog=catalog,
        scenario_id=scenario_id,
    )


def _split_groups(envelopes: Sequence[EventEnvelope]) -> list[tuple[EventEnvelope, ...]]:
    groups: list[tuple[EventEnvelope, ...]] = []
    offset = 0
    items = tuple(envelopes)
    while offset < len(items):
        first = items[offset]
        if first.event_index != 0:
            raise TwinTimelineError(f"event stream is not group-aligned at item {offset}")
        end = offset + first.event_count
        if end > len(items):
            raise TwinTimelineError(f"mutation {first.mutation_id} is truncated")
        groups.append(items[offset:end])
        offset = end
    return groups


class _HistoryTracker:
    """History-derived annotations that one snapshot alone cannot express."""

    def __init__(self) -> None:
        self.restored: set[str] = set()
        # operation_id -> reassignment, kept from the applying plan until that
        # operation completes, so the change stays visible through its execution.
        # A reassignment made while machines were down also ends once all of those
        # machines are restored: the cause of the move no longer applies.
        self.reassigned: dict[str, dict[str, Any]] = {}
        self.causes: dict[str, set[str]] = {}

    def observe(
        self,
        group: Sequence[EventEnvelope],
        reassignments: Mapping[str, dict[str, Any]],
        snapshot: OperationalSnapshot,
    ) -> None:
        if reassignments:
            down = {
                item.machine_id
                for item in snapshot.machines
                if item.status in (MachineStatus.DOWN, MachineStatus.MAINTENANCE)
            }
            for operation_id in reassignments:
                self.causes[operation_id] = set(down)
        self.reassigned.update(reassignments)
        for envelope in group:
            event = envelope.event
            if event.event_type is EventType.OPERATION_COMPLETED:
                attempt_id = event.payload.get("attempt_id")
                operation_id = next(
                    (
                        item.operation_id
                        for item in snapshot.attempts
                        if item.attempt_id == attempt_id
                    ),
                    None,
                )
                self.reassigned.pop(operation_id, None)
                self.causes.pop(operation_id, None)
            machine_id = event.payload.get("machine_id")
            if event.event_type is EventType.OPERATION_STARTED:
                machine_id = _attempt_machine(snapshot, event.payload.get("attempt_id"))
            if not isinstance(machine_id, str):
                continue
            if event.event_type is EventType.MACHINE_RESTORED:
                self.restored.add(machine_id)
                for operation_id, causes in list(self.causes.items()):
                    if machine_id in causes:
                        causes.discard(machine_id)
                        if not causes:
                            self.reassigned.pop(operation_id, None)
                            del self.causes[operation_id]
            elif event.event_type in (EventType.MACHINE_DOWN, EventType.OPERATION_STARTED):
                # "Restored" means back in service and not yet loaded again.
                self.restored.discard(machine_id)


def _frame(
    index: int,
    snapshot: OperationalSnapshot,
    group: Sequence[EventEnvelope],
    tracker: _HistoryTracker,
    reassignments: Mapping[str, dict[str, Any]],
) -> dict[str, Any]:
    projection = project_snapshot(snapshot)
    for machine_id, machine in projection["machines"].items():
        machine["restored"] = machine["status"] == "idle" and machine_id in tracker.restored
    for unit in projection["units"].values():
        change = tracker.reassigned.get(unit["operation_id"])
        visible = change is not None and unit["zone"] in ("queue", "machine", "held")
        unit["reassigned_from"] = change["from"] if visible else None
        unit["reassigned_restart"] = bool(visible and change["restart"])
    events = [
        _event_summary(envelope, snapshot)
        for envelope in group
        if envelope.event.event_type is not EventType.OBSERVATION_RECORDED
        and envelope.event.event_type is not EventType.TIME_ADVANCED
    ]
    types = {envelope.event.event_type for envelope in group}
    return {
        "index": index,
        "state_version": snapshot.state_version,
        "plan_revision": snapshot.plan_revision,
        "time": snapshot.simulated_time_seconds,
        "key": bool(types & KEY_EVENT_TYPES),
        "events": events,
        "reassignments": list(reassignments.values()),
        "machines": projection["machines"],
        "units": projection["units"],
    }


def _attempt_machine(snapshot: OperationalSnapshot, attempt_id: object) -> str | None:
    return next(
        (item.machine_id for item in snapshot.attempts if item.attempt_id == attempt_id), None
    )


def _reassignments(
    before: OperationalSnapshot, after: OperationalSnapshot
) -> dict[str, dict[str, Any]]:
    """Operations whose next machine changed between two plan revisions."""

    def current_machine(snapshot: OperationalSnapshot) -> dict[str, OperationAttempt]:
        latest: dict[str, OperationAttempt] = {}
        for attempt in snapshot.attempts:
            if attempt.status in (AttemptStatus.SCHEDULED, AttemptStatus.INTERRUPTED):
                prior = latest.get(attempt.operation_id)
                if prior is None or attempt.attempt_number > prior.attempt_number:
                    latest[attempt.operation_id] = attempt
        return latest

    old = current_machine(before)
    new = {
        key: value
        for key, value in current_machine(after).items()
        if value.status is AttemptStatus.SCHEDULED
    }
    operations = {item.operation_id: item for item in after.operations}
    changes: dict[str, dict[str, Any]] = {}
    for operation_id, attempt in sorted(new.items()):
        prior = old.get(operation_id)
        if prior is not None and prior.machine_id == attempt.machine_id:
            continue
        if prior is None and before.plan_revision == 0:
            continue  # the initial plan assigns everything; that is not a change
        operation = operations[operation_id]
        changes[operation_id] = {
            "operation_id": operation_id,
            "unit": unit_key(operation.order_id, operation.unit_index),
            "step": operation.sequence_index,
            "from": None if prior is None else prior.machine_id,
            "to": attempt.machine_id,
            "restart": attempt.restart_of_attempt_id is not None,
        }
    return changes


def _event_summary(envelope: EventEnvelope, snapshot: OperationalSnapshot) -> dict[str, Any]:
    event = envelope.event
    payload = event.payload
    summary: dict[str, Any] = {
        "type": event.event_type.value,
        "sequence": event.sequence,
        "time": event.simulated_time_seconds,
    }
    if event.event_type in (EventType.MACHINE_DOWN, EventType.MACHINE_RESTORED):
        machine_id = str(payload.get("machine_id"))
        summary["machine_id"] = machine_id
        summary["label"] = (
            f"{machine_id} failed (machine_down)"
            if event.event_type is EventType.MACHINE_DOWN
            else f"{machine_id} restored by explicit maintenance confirmation"
        )
        return summary
    if event.event_type is EventType.PLAN_APPLIED:
        summary["plan_revision"] = payload.get("plan_revision")
        summary["label"] = f"Approved plan revision {payload.get('plan_revision')} applied"
        if event.simulated_time_seconds > snapshot.simulated_time_seconds:
            # S05/S06 pre-stage a plan at the next open second without moving the clock.
            summary["label"] += f" (effective from {event.simulated_time_seconds:,} s)"
        return summary
    if event.event_type is EventType.ORDER_REGISTERED:
        order = payload.get("order")
        order_id = order.get("order_id") if isinstance(order, dict) else None
        summary["order_id"] = order_id
        summary["label"] = f"Order {order_id} registered with an approved plan"
        return summary

    attempt_id = payload.get("attempt_id")
    attempt = next((item for item in snapshot.attempts if item.attempt_id == attempt_id), None)
    if attempt is None:
        summary["label"] = event.event_type.value
        return summary
    operation = next(
        item for item in snapshot.operations if item.operation_id == attempt.operation_id
    )
    unit = unit_key(operation.order_id, operation.unit_index)
    summary.update(
        {
            "machine_id": attempt.machine_id,
            "attempt_id": attempt.attempt_id,
            "operation_id": operation.operation_id,
            "unit": unit,
            "step": operation.sequence_index,
        }
    )
    verb = {
        EventType.OPERATION_STARTED: "started",
        EventType.OPERATION_COMPLETED: "completed",
        EventType.OPERATION_INTERRUPTED: "interrupted",
    }[event.event_type]
    label = f"{unit} step {operation.sequence_index + 1} {verb} on {attempt.machine_id}"
    if event.event_type is EventType.OPERATION_INTERRUPTED:
        summary["wasted_seconds"] = attempt.consumed_seconds
        label += f" ({attempt.consumed_seconds} s wasted)"
    if event.event_type is EventType.OPERATION_STARTED and attempt.attempt_number > 1:
        label += f" (restart, attempt {attempt.attempt_number})"
    summary["label"] = label
    return summary


def _products(catalog: DomainCatalog) -> list[dict[str, Any]]:
    return [
        {
            "product_id": product.product_id,
            "name": product.canonical_name,
            "route": [
                {
                    "step": item.sequence_index,
                    "name": item.name,
                    "capability": item.required_capability.value,
                    "duration_seconds": item.duration_seconds,
                }
                for item in product.operations
            ],
        }
        for product in catalog.products
    ]


def _units(
    frames: Sequence[Mapping[str, Any]], stored: OperationalSnapshot
) -> list[dict[str, Any]]:
    """Static unit descriptors; ``first_frame`` marks when an order first exists."""

    first_frame: dict[str, int] = {}
    for frame in frames:
        for key in frame["units"]:
            first_frame.setdefault(key, frame["index"])
    orders = {item.order_id: item for item in stored.orders}
    units: list[dict[str, Any]] = []
    for key, index in first_frame.items():
        order_id, unit_index = key.rsplit("#", 1)
        order = orders[order_id]
        units.append(
            {
                "key": key,
                "order_id": order_id,
                "unit_index": int(unit_index),
                "product_id": order.product_id,
                "release_time_seconds": order.release_time_seconds,
                "due_time_seconds": order.due_time_seconds,
                "priority_weight": order.priority_weight,
                "first_frame": index,
            }
        )
    return units


__all__ = [
    "KEY_EVENT_TYPES",
    "TWIN_SCHEMA",
    "TwinTimeline",
    "TwinTimelineError",
    "TwinVerification",
    "build_twin_timeline",
    "build_twin_timeline_from_repository",
    "compare_projections",
    "project_snapshot",
    "unit_key",
]
