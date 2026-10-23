"""Pure canonical serialization for hashes, replay, and semantic comparisons."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from enum import Enum
from typing import Any

from pydantic import BaseModel

WALL_CLOCK_FIELDS = frozenset(
    {
        "created_at_utc",
        "decided_at_utc",
        "recorded_at_utc",
        "wall_clock_elapsed_ms",
    }
)

SEMANTIC_COUNTER_FIELDS = frozenset(
    {
        "state_version",
        "base_state_version",
        "plan_revision",
        "proposed_plan_revision",
        "sequence",
    }
)

GENERATED_ID_FIELDS = frozenset(
    {
        "run_id",
        "workflow_id",
        "proposal_id",
        "approval_id",
        "event_id",
        "observation_id",
        "attempt_id",
        "restart_of_attempt_id",
    }
)


def _json_ready(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        return {str(key): _json_ready(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_json_ready(item) for item in value]
    return value


def _drop_fields(value: Any, excluded: frozenset[str]) -> Any:
    if isinstance(value, Mapping):
        return {
            key: _drop_fields(item, excluded) for key, item in value.items() if key not in excluded
        }
    if isinstance(value, list):
        return [_drop_fields(item, excluded) for item in value]
    return value


def canonical_data(value: Any, *, exclude_wall_clock: bool = True) -> Any:
    """Return JSON-compatible operational data with deterministic field treatment."""

    data = _json_ready(value)
    if exclude_wall_clock:
        data = _drop_fields(data, WALL_CLOCK_FIELDS)
    return data


def canonical_json_bytes(value: Any, *, exclude_wall_clock: bool = True) -> bytes:
    """Serialize with sorted keys and no insignificant whitespace."""

    data = canonical_data(value, exclude_wall_clock=exclude_wall_clock)
    return json.dumps(
        data,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def canonical_hash(value: Any, *, exclude_wall_clock: bool = True) -> str:
    """Return the lowercase SHA-256 of canonical JSON bytes."""

    return hashlib.sha256(
        canonical_json_bytes(value, exclude_wall_clock=exclude_wall_clock)
    ).hexdigest()


def semantic_data(value: Any) -> Any:
    """Normalize generated control identity for cross-run business comparisons.

    Stable business identifiers (orders, operations, machines, products), simulated
    times, attempt content, resources, and outcomes are deliberately retained.
    """

    data = _drop_fields(canonical_data(value, exclude_wall_clock=True), SEMANTIC_COUNTER_FIELDS)
    replacements: dict[tuple[str, str], str] = {}
    counters: dict[str, int] = {}

    def visit(item: Any, field_name: str | None = None) -> Any:
        if isinstance(item, Mapping):
            return {key: visit(child, key) for key, child in item.items()}
        if isinstance(item, list):
            return [visit(child, field_name) for child in item]
        if field_name in GENERATED_ID_FIELDS and isinstance(item, str):
            key = (field_name, item)
            if key not in replacements:
                counters[field_name] = counters.get(field_name, 0) + 1
                replacements[key] = f"<{field_name}:{counters[field_name]}>"
            return replacements[key]
        return item

    return visit(data)


def semantic_json_bytes(value: Any) -> bytes:
    """Serialize the documented cross-run semantic comparison view."""

    return json.dumps(
        semantic_data(value),
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def semantic_hash(value: Any) -> str:
    return hashlib.sha256(semantic_json_bytes(value)).hexdigest()
