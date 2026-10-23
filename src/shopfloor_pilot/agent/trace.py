"""Append-only supervisor trace store and its stable UI projection.

Traces live in their own SQLite file, never in the authoritative operational
database. Payloads are sanitized before storage: hidden reasoning keys are
dropped and known or recognizable credentials are redacted.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

from shopfloor_pilot.providers.redaction import REDACTED

TRACE_SCHEMA_VERSION: Final = 1
HIDDEN_KEYS: Final = frozenset({"reasoning", "reasoning_content"})
# Wall-clock, latency, and ledger-generated identifiers excluded from semantic
# comparison of two scripted runs; everything else must match exactly.
NONDETERMINISTIC_KEYS: Final = frozenset(
    {"recorded_at_utc", "latency_seconds", "active_seconds", "created_at_utc", "budget"}
)
_SECRET_PATTERNS = (
    re.compile(r"Bearer\s+[A-Za-z0-9._~+/=-]+", re.IGNORECASE),
    re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b"),
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_trace (
    workflow_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    sequence INTEGER NOT NULL CHECK (sequence >= 1),
    kind TEXT NOT NULL,
    phase TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    recorded_at_utc TEXT NOT NULL,
    PRIMARY KEY (workflow_id, sequence)
) STRICT;
"""


def sanitize(value: Any, secrets: Sequence[str] = ()) -> Any:
    """Drop hidden-reasoning keys and redact credentials, recursively."""

    if isinstance(value, str):
        text = value
        for pattern in _SECRET_PATTERNS:
            text = pattern.sub(REDACTED, text)
        for secret in sorted((item for item in secrets if len(item) >= 4), key=len, reverse=True):
            text = text.replace(secret, REDACTED)
        return text
    if isinstance(value, Mapping):
        return {
            str(key): sanitize(item, secrets)
            for key, item in value.items()
            if str(key) not in HIDDEN_KEYS
        }
    if isinstance(value, (list, tuple)):
        return [sanitize(item, secrets) for item in value]
    return value


@dataclass(frozen=True, slots=True)
class TraceRecord:
    workflow_id: str
    run_id: str
    sequence: int
    kind: str
    phase: str
    payload: dict[str, Any]
    recorded_at_utc: str

    def public_dict(self) -> dict[str, Any]:
        return {
            "sequence": self.sequence,
            "kind": self.kind,
            "phase": self.phase,
            "payload": self.payload,
            "recorded_at_utc": self.recorded_at_utc,
        }


class TraceStore:
    """Single-writer append-only trace; there is no update or delete method."""

    def __init__(self, path: str | Path, *, secrets: Iterable[str] = ()) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._secrets = tuple(item for item in secrets if item)
        self._connection = sqlite3.connect(self.path, isolation_level=None, timeout=5.0)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.executescript(_SCHEMA)

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> TraceStore:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def append(
        self,
        *,
        workflow_id: str,
        run_id: str,
        kind: str,
        phase: str,
        payload: Mapping[str, Any],
    ) -> int:
        clean = sanitize(dict(payload), self._secrets)
        encoded = json.dumps(clean, ensure_ascii=False, sort_keys=True)
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            row = self._connection.execute(
                "SELECT COALESCE(MAX(sequence), 0) AS last FROM agent_trace WHERE workflow_id = ?",
                (workflow_id,),
            ).fetchone()
            sequence = int(row["last"]) + 1
            self._connection.execute(
                "INSERT INTO agent_trace(workflow_id, run_id, sequence, kind, phase, "
                "payload_json, recorded_at_utc) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    workflow_id,
                    run_id,
                    sequence,
                    kind,
                    phase,
                    encoded,
                    datetime.now(UTC).isoformat(timespec="microseconds"),
                ),
            )
        except BaseException:
            self._connection.rollback()
            raise
        self._connection.commit()
        return sequence

    def records(self, workflow_id: str) -> tuple[TraceRecord, ...]:
        rows = self._connection.execute(
            "SELECT * FROM agent_trace WHERE workflow_id = ? ORDER BY sequence",
            (workflow_id,),
        ).fetchall()
        return tuple(
            TraceRecord(
                workflow_id=row["workflow_id"],
                run_id=row["run_id"],
                sequence=row["sequence"],
                kind=row["kind"],
                phase=row["phase"],
                payload=json.loads(row["payload_json"]),
                recorded_at_utc=row["recorded_at_utc"],
            )
            for row in rows
        )


def _step(record: TraceRecord) -> dict[str, Any]:
    payload = record.payload
    step: dict[str, Any] = {
        "sequence": record.sequence,
        "kind": record.kind,
        "phase": record.phase,
    }
    if record.kind == "transition":
        step.update({"from": payload["from"], "to": payload["to"], "reason": payload["reason"]})
    elif record.kind == "provider_request":
        step.update(
            {
                "request_id": payload["request_id"],
                "tools": payload["tools"],
                "message_count": len(payload["messages"]),
            }
        )
    elif record.kind == "provider_response":
        step.update(
            {
                "request_id": payload["request_id"],
                "finish_reason": payload["response"]["finish_reason"],
                "tool_calls": [
                    {"call_id": item["call_id"], "name": item["name"]}
                    for item in payload["validated_tool_calls"]
                ],
                "usage": payload["response"]["usage"],
                "latency_seconds": payload["response"].get("latency_seconds"),
            }
        )
    elif record.kind == "provider_rejection":
        step.update(
            {
                "request_id": payload["request_id"],
                "code": payload["error"]["code"],
                "reason": payload["error"].get("details", {}).get("reason"),
                "tool_name": payload["error"].get("details", {}).get("tool_name"),
            }
        )
    elif record.kind == "tool_call":
        step.update(
            {
                "call_id": payload["call_id"],
                "tool": payload["tool"],
                "arguments": payload["arguments"],
            }
        )
    elif record.kind == "tool_result":
        result = payload["result"]
        step.update(
            {
                "call_id": payload["call_id"],
                "tool": payload["tool"],
                "status": result.get("status"),
                "reason": result.get("reason"),
                "evidence_ids": payload.get("evidence_ids", []),
            }
        )
    elif record.kind in {"workflow_started", "terminal", "notice"}:
        step.update(payload)
    return step


def project_trace(records: Sequence[TraceRecord]) -> dict[str, Any]:
    """Stable, implementation-independent projection for the S12 dashboard."""

    if not records:
        return {"schema_version": TRACE_SCHEMA_VERSION, "view": "supervisor_trace", "steps": []}
    header = records[0].payload if records[0].kind == "workflow_started" else {}
    terminal = next((item.payload for item in reversed(records) if item.kind == "terminal"), None)
    return {
        "schema_version": TRACE_SCHEMA_VERSION,
        "view": "supervisor_trace",
        "workflow_id": records[0].workflow_id,
        "run_id": records[0].run_id,
        "workflow_kind": header.get("workflow_kind"),
        "provider": header.get("provider"),
        "evidence_label": header.get("evidence_label"),
        "terminal": terminal,
        "steps": [_step(item) for item in records],
    }


def normalize_for_comparison(value: Any) -> Any:
    """Remove documented nondeterministic fields for scripted-run comparison."""

    if isinstance(value, Mapping):
        return {
            key: normalize_for_comparison(item)
            for key, item in value.items()
            if key not in NONDETERMINISTIC_KEYS
        }
    if isinstance(value, list):
        return [normalize_for_comparison(item) for item in value]
    return value
