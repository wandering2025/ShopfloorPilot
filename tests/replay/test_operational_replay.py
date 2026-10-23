"""S02-A06/A07 deterministic operational replay acceptance."""

from __future__ import annotations

from typing import Any

from shopfloor_pilot.domain.canonical import canonical_hash
from shopfloor_pilot.domain.models import (
    AttemptStatus,
    EventType,
    MachineStatus,
    OperationStatus,
    OrderStatus,
)
from shopfloor_pilot.events import replay_events


def test_a06_hand_built_stream_reconstructs_complete_operational_state(
    hand_built_replay: Any,
) -> None:
    result = replay_events(hand_built_replay.initial, hand_built_replay.envelopes)

    assert result.snapshot == hand_built_replay.expected_snapshot
    assert result.snapshot_hash == canonical_hash(hand_built_replay.expected_snapshot)
    assert (
        result.snapshot_hash == "561cc955a20f303d881f407ea278b068d0a400a3211d490b2c0f7542ea03f84c"
    )
    assert canonical_hash(hand_built_replay.envelopes) == (
        "2732902f0b84b986c94b5b06c27937751eba41cab1167bd50924233ba765aecd"
    )
    assert result.snapshot.state_version == 6
    assert result.snapshot.plan_revision == 2
    assert result.snapshot.simulated_time_seconds == 36
    assert result.last_sequence == 16
    assert len(result.seen_mutation_ids) == result.snapshot.state_version
    assert len(result.seen_event_ids) == result.last_sequence

    operations = {item.operation_id: item for item in result.snapshot.operations}
    attempts = {item.attempt_id: item for item in result.snapshot.attempts}
    orders = {item.order_id: item for item in result.snapshot.orders}
    machines = {item.machine_id: item for item in result.snapshot.machines}

    assert operations["op-replay-first"].status is OperationStatus.COMPLETED
    assert operations["op-replay-second"].status is OperationStatus.COMPLETED
    assert attempts["attempt-replay-first-1"].status is AttemptStatus.COMPLETED
    assert attempts["attempt-replay-first-1"].actual_start_seconds == 0
    assert attempts["attempt-replay-first-1"].actual_end_seconds == 10
    interrupted = attempts["attempt-replay-second-1"]
    assert interrupted.status is AttemptStatus.INTERRUPTED
    assert interrupted.actual_start_seconds == 10
    assert interrupted.actual_end_seconds == 15
    assert interrupted.consumed_seconds == 5
    restarted = attempts["attempt-replay-second-2"]
    assert restarted.status is AttemptStatus.COMPLETED
    assert restarted.restart_of_attempt_id == interrupted.attempt_id
    assert restarted.planned_duration_seconds == restarted.consumed_seconds == 20
    assert (restarted.actual_start_seconds, restarted.actual_end_seconds) == (16, 36)
    assert orders["order-replay-main"].status is OrderStatus.COMPLETED
    assert orders["order-replay-urgent"].status is OrderStatus.PENDING
    assert machines["CNC02"].status is MachineStatus.AVAILABLE
    assert machines["CNC02"].availability_windows[0].end_seconds == 100
    assert machines["CNC03"].status is MachineStatus.AVAILABLE

    expected_types = (
        EventType.PLAN_APPLIED,
        EventType.OPERATION_STARTED,
        EventType.OPERATION_COMPLETED,
        EventType.OPERATION_STARTED,
        EventType.TIME_ADVANCED,
        EventType.MACHINE_DOWN,
        EventType.OPERATION_INTERRUPTED,
        EventType.ORDER_REGISTERED,
        EventType.OBSERVATION_RECORDED,
        EventType.TIME_ADVANCED,
        EventType.MACHINE_RESTORED,
        EventType.PLAN_APPLIED,
        EventType.OPERATION_STARTED,
        EventType.TIME_ADVANCED,
        EventType.OPERATION_COMPLETED,
        EventType.TIME_ADVANCED,
    )
    assert tuple(item.event.event_type for item in hand_built_replay.envelopes) == expected_types
    assert [item.event.sequence for item in hand_built_replay.envelopes] == list(range(1, 17))


def test_a07_full_replay_is_exactly_repeatable_and_suffix_matches(
    hand_built_replay: Any,
) -> None:
    first = replay_events(hand_built_replay.initial, hand_built_replay.envelopes)
    second = replay_events(hand_built_replay.initial, hand_built_replay.envelopes)
    assert first == second
    assert canonical_hash(first.snapshot) == canonical_hash(second.snapshot)

    split = hand_built_replay.group_end_offsets[3]
    prefix_checkpoint = replay_events(
        hand_built_replay.initial, hand_built_replay.envelopes[:split]
    )
    suffix_result = replay_events(prefix_checkpoint, hand_built_replay.envelopes[split:])
    assert suffix_result == first
    assert suffix_result.snapshot == hand_built_replay.expected_snapshot


def test_a07_materialized_ids_are_deterministic_and_wall_clock_free(
    hand_built_replay: Any,
) -> None:
    first = tuple(item.event.event_id for item in hand_built_replay.envelopes)
    replayed = replay_events(hand_built_replay.initial, hand_built_replay.envelopes)
    assert tuple(replayed.seen_event_ids) == first
    assert len(first) == len(set(first))
    assert all(event_id.startswith("event-") for event_id in first)
