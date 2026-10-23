"""Portable JSONL export and pure event replay without solver or scenario sampling."""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from shopfloor_pilot.domain.canonical import canonical_hash
from shopfloor_pilot.events import EventEnvelope, SnapshotCheckpoint, replay_events
from shopfloor_pilot.metrics import calculate_run_metrics

from .errors import ReplayBundleError
from .models import (
    ReplayBundle,
    ReplayBundleHeader,
    ReplayEventRecord,
    ReplayResult,
)


def operational_stream_hash(
    initial_checkpoint: SnapshotCheckpoint,
    events: Sequence[EventEnvelope],
) -> str:
    """Bind the version-zero anchor and ordered canonical operational content."""

    return canonical_hash(
        {
            "schema_version": 1,
            "initial_checkpoint": initial_checkpoint,
            "events": events,
        }
    )


def build_replay_bundle(
    initial_checkpoint: SnapshotCheckpoint,
    events: Sequence[EventEnvelope],
) -> ReplayBundle:
    event_tuple = tuple(events)
    try:
        checkpoint = replay_events(initial_checkpoint, event_tuple)
        metrics = calculate_run_metrics(initial_checkpoint, event_tuple)
    except Exception as exc:
        raise ReplayBundleError(f"cannot build bundle from invalid event history: {exc}") from exc
    header = ReplayBundleHeader(
        run_id=initial_checkpoint.snapshot.run_id,
        initial_checkpoint=initial_checkpoint,
        event_count=len(event_tuple),
        operational_stream_hash=operational_stream_hash(initial_checkpoint, event_tuple),
        final_snapshot_hash=checkpoint.snapshot_hash,
        actual_metrics=metrics,
        actual_metrics_hash=canonical_hash(metrics),
    )
    return ReplayBundle(header=header, events=event_tuple)


def export_replay_bundle(
    path: str | Path,
    initial_checkpoint: SnapshotCheckpoint,
    events: Sequence[EventEnvelope],
) -> ReplayBundleHeader:
    """Write a self-contained JSONL stream with one header then full envelopes."""

    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    bundle = build_replay_bundle(initial_checkpoint, tuple(events))
    records = [bundle.header.model_dump_json()]
    records.extend(
        ReplayEventRecord(envelope=envelope).model_dump_json() for envelope in bundle.events
    )
    destination.write_text("\n".join(records) + "\n", encoding="utf-8")
    return bundle.header


def _parse_record(raw: str, line_number: int) -> dict[str, Any]:
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ReplayBundleError(f"invalid JSON on line {line_number}: {exc.msg}") from exc
    if not isinstance(parsed, dict):
        raise ReplayBundleError(f"line {line_number} must contain a JSON object")
    return parsed


def load_replay_bundle(path: str | Path) -> ReplayBundle:
    """Strictly load and authenticate structure/hash metadata from a JSONL bundle."""

    source = Path(path)
    try:
        lines = source.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ReplayBundleError(f"cannot read replay bundle {source}: {exc}") from exc
    if not lines:
        raise ReplayBundleError("replay bundle is empty")
    if any(not line.strip() for line in lines):
        raise ReplayBundleError("replay bundle contains an empty record")
    try:
        _parse_record(lines[0], 1)
        header = ReplayBundleHeader.model_validate_json(lines[0])
        events: list[EventEnvelope] = []
        for line_number, raw in enumerate(lines[1:], start=2):
            _parse_record(raw, line_number)
            record = ReplayEventRecord.model_validate_json(raw)
            events.append(record.envelope)
        bundle = ReplayBundle(header=header, events=tuple(events))
    except (ValidationError, TypeError, ValueError) as exc:
        raise ReplayBundleError(f"replay bundle schema validation failed: {exc}") from exc
    expected_stream_hash = operational_stream_hash(header.initial_checkpoint, bundle.events)
    if expected_stream_hash != header.operational_stream_hash:
        raise ReplayBundleError("replay bundle operational stream hash mismatch")
    if canonical_hash(header.actual_metrics) != header.actual_metrics_hash:
        raise ReplayBundleError("replay bundle embedded metrics hash mismatch")
    return bundle


def replay_bundle(bundle_or_path: ReplayBundle | str | Path) -> ReplayResult:
    """Replay only embedded events and recompute exact operational KPIs."""

    bundle = (
        bundle_or_path
        if isinstance(bundle_or_path, ReplayBundle)
        else load_replay_bundle(bundle_or_path)
    )
    header = bundle.header
    stream_hash = operational_stream_hash(header.initial_checkpoint, bundle.events)
    if stream_hash != header.operational_stream_hash:
        raise ReplayBundleError("replay bundle operational stream hash mismatch")
    try:
        checkpoint = replay_events(header.initial_checkpoint, bundle.events)
        metrics = calculate_run_metrics(header.initial_checkpoint, bundle.events)
    except Exception as exc:
        raise ReplayBundleError(f"event replay failed: {exc}") from exc
    if checkpoint.snapshot_hash != header.final_snapshot_hash:
        raise ReplayBundleError("replayed final snapshot hash does not match the export")
    metrics_hash = canonical_hash(metrics)
    if metrics_hash != header.actual_metrics_hash or metrics != header.actual_metrics:
        raise ReplayBundleError("replayed actual metrics do not match the export")
    return ReplayResult(
        run_id=header.run_id,
        event_count=len(bundle.events),
        operational_stream_hash=stream_hash,
        final_snapshot_hash=checkpoint.snapshot_hash,
        actual_metrics_hash=metrics_hash,
        checkpoint=checkpoint,
        actual_metrics=metrics,
    )
