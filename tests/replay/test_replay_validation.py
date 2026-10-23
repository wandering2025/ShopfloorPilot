"""S02-A08 strict stream rejection and fail-closed replay acceptance."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest

from shopfloor_pilot.domain.canonical import canonical_hash
from shopfloor_pilot.domain.models import EventType
from shopfloor_pilot.events import (
    EventConflictError,
    EventDraft,
    GroupVersionConflictError,
    MalformedEnvelopeError,
    MalformedPayloadError,
    OrderingConflictError,
    ReductionConflictError,
    SequenceConflictError,
    TimeAdvancedPayload,
    UnsupportedSchemaError,
    materialize_event_group,
    reduce_event_group,
    replay_events,
)


def _raw(hand_built_replay: Any) -> list[dict[str, object]]:
    return [item.model_dump(mode="json") for item in hand_built_replay.envelopes]


@pytest.mark.parametrize("target", ["envelope", "event"])
def test_a08_unsupported_envelope_or_event_schema_is_explicit(
    hand_built_replay: Any, target: str
) -> None:
    raw = _raw(hand_built_replay)
    if target == "envelope":
        raw[0]["schema_version"] = 2
    else:
        event = raw[0]["event"]
        assert isinstance(event, dict)
        event["schema_version"] = 2

    before = canonical_hash(hand_built_replay.initial.snapshot)
    with pytest.raises(UnsupportedSchemaError, match="supported=1"):
        replay_events(hand_built_replay.initial, raw)
    assert canonical_hash(hand_built_replay.initial.snapshot) == before


def test_a08_malformed_payload_and_missing_sequence_fail_precisely(
    hand_built_replay: Any,
) -> None:
    malformed_payload = _raw(hand_built_replay)
    time_event = malformed_payload[4]["event"]
    assert isinstance(time_event, dict)
    payload = time_event["payload"]
    assert isinstance(payload, dict)
    del payload["target_time_seconds"]
    with pytest.raises(MalformedPayloadError, match="time_advanced"):
        replay_events(hand_built_replay.initial, malformed_payload)

    missing_sequence = _raw(hand_built_replay)
    event = missing_sequence[0]["event"]
    assert isinstance(event, dict)
    del event["sequence"]
    with pytest.raises(SequenceConflictError, match="missing required sequence"):
        replay_events(hand_built_replay.initial, missing_sequence)


def test_a08_sequence_gap_and_duplicate_event_id_are_rejected(
    hand_built_replay: Any,
) -> None:
    gap = _raw(hand_built_replay)
    event = gap[0]["event"]
    assert isinstance(event, dict)
    event["sequence"] = 2
    with pytest.raises(SequenceConflictError, match="expected 1"):
        replay_events(hand_built_replay.initial, gap)

    duplicate = _raw(hand_built_replay)
    first_event = duplicate[0]["event"]
    second_event = duplicate[1]["event"]
    assert isinstance(first_event, dict) and isinstance(second_event, dict)
    second_event["event_id"] = first_event["event_id"]
    with pytest.raises(EventConflictError, match="duplicated"):
        replay_events(hand_built_replay.initial, duplicate)

    duplicate_mutation = _raw(hand_built_replay)
    duplicate_mutation[1]["mutation_id"] = duplicate_mutation[0]["mutation_id"]
    with pytest.raises(GroupVersionConflictError, match="already reduced"):
        replay_events(hand_built_replay.initial, duplicate_mutation)


def test_a08_priority_inversion_is_rejected_before_reduction(
    hand_built_replay: Any,
) -> None:
    raw = _raw(hand_built_replay)
    # Group 3 spans offsets [2, 5). Reverse its events while retaining a structurally
    # valid group and contiguous sequences so only the priority ordering is wrong.
    reversed_events = [deepcopy(raw[index]["event"]) for index in (4, 3, 2)]
    for index, event in enumerate(reversed_events, start=3):
        assert isinstance(event, dict)
        event["sequence"] = index
    for group_index, stream_index in enumerate(range(2, 5)):
        raw[stream_index]["event"] = reversed_events[group_index]
        raw[stream_index]["event_index"] = group_index

    with pytest.raises(OrderingConflictError, match="ordering key"):
        replay_events(hand_built_replay.initial, raw)


def test_a08_group_version_and_mid_group_suffix_are_rejected(
    hand_built_replay: Any,
) -> None:
    wrong_version = _raw(hand_built_replay)
    wrong_version[1]["base_state_version"] = 0
    wrong_version[1]["resulting_state_version"] = 1
    with pytest.raises(GroupVersionConflictError, match="does not match checkpoint"):
        replay_events(hand_built_replay.initial, wrong_version)

    with pytest.raises(GroupVersionConflictError, match="event_index"):
        replay_events(hand_built_replay.initial, hand_built_replay.envelopes[3:])

    truncated = _raw(hand_built_replay)[:4]
    with pytest.raises(GroupVersionConflictError, match="truncated"):
        replay_events(hand_built_replay.initial, truncated)


def test_a08_malformed_envelope_and_semantic_reduction_conflict_are_distinct(
    hand_built_replay: Any,
) -> None:
    malformed = _raw(hand_built_replay)
    del malformed[0]["mutation_id"]
    with pytest.raises(MalformedEnvelopeError, match="mutation_id"):
        replay_events(hand_built_replay.initial, malformed)

    zero_advance = EventDraft(
        simulated_time_seconds=0,
        event_type=EventType.TIME_ADVANCED,
        payload=TimeAdvancedPayload(target_time_seconds=0).model_dump(mode="json"),
    )
    group = materialize_event_group(
        hand_built_replay.initial, "mutation-invalid-zero-advance", (zero_advance,)
    )
    with pytest.raises(ReductionConflictError, match="strictly forward"):
        reduce_event_group(hand_built_replay.initial, group)


def test_a08_materialization_rejects_empty_mixed_time_and_backdated_groups(
    hand_built_replay: Any,
) -> None:
    with pytest.raises(GroupVersionConflictError, match="at least one"):
        materialize_event_group(hand_built_replay.initial, "mutation-empty", ())

    one = EventDraft(
        simulated_time_seconds=1,
        event_type=EventType.TIME_ADVANCED,
        payload=TimeAdvancedPayload(target_time_seconds=1).model_dump(mode="json"),
    )
    two = EventDraft(
        simulated_time_seconds=2,
        event_type=EventType.TIME_ADVANCED,
        payload=TimeAdvancedPayload(target_time_seconds=2).model_dump(mode="json"),
    )
    with pytest.raises(OrderingConflictError, match="same simulated second"):
        materialize_event_group(hand_built_replay.initial, "mutation-mixed-time", (one, two))

    final_checkpoint = replay_events(hand_built_replay.initial, hand_built_replay.envelopes)
    backdated = EventDraft(
        simulated_time_seconds=35,
        event_type=EventType.TIME_ADVANCED,
        payload=TimeAdvancedPayload(target_time_seconds=35).model_dump(mode="json"),
    )
    with pytest.raises(OrderingConflictError, match="precedes snapshot time"):
        materialize_event_group(final_checkpoint, "mutation-backdated", (backdated,))
