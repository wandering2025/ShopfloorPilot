"""Persistent per-workflow provider budget, deadline, and attempt ledger.

The ledger is deliberately separate from the authoritative operational SQLite
database.  It records only provider bookkeeping, keyed by the persistent
``workflow_id`` and linked to the manufacturing/evaluation ``run_id``.
"""

from __future__ import annotations

import json
import math
import sqlite3
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Final
from uuid import uuid4

from .providers.errors import (
    BudgetExceededError,
    LedgerError,
    ProviderErrorCode,
    WorkflowStateError,
)
from .providers.models import MAX_OUTPUT_TOKENS
from .providers.redaction import SecretRedactor

DEFAULT_MAX_PROVIDER_ATTEMPTS: Final = 8
DEFAULT_MAX_TOOL_EXECUTIONS: Final = 16
DEFAULT_MAX_RETRIES_PER_REQUEST: Final = 2
DEFAULT_ATTEMPT_TIMEOUT_SECONDS: Final = 30.0
DEFAULT_MAX_ACTIVE_SECONDS: Final = 120.0
DEFAULT_MAX_TOKENS: Final = 32_000
MICROS: Final = 1_000_000


def _now_utc() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


def _seconds_to_micros(seconds: float) -> int:
    if not isinstance(seconds, (int, float)) or isinstance(seconds, bool):
        raise TypeError("seconds must be numeric")
    if not math.isfinite(seconds) or seconds < 0:
        raise ValueError("seconds must be a finite non-negative value")
    return int(math.ceil(float(seconds) * MICROS))


def _micros_to_seconds(micros: int) -> float:
    return micros / MICROS


@dataclass(frozen=True, slots=True)
class ProviderLimits:
    """Configurable hard ceilings for one workflow; persisted on first use."""

    max_provider_attempts: int = DEFAULT_MAX_PROVIDER_ATTEMPTS
    max_tool_executions: int = DEFAULT_MAX_TOOL_EXECUTIONS
    max_retries_per_request: int = DEFAULT_MAX_RETRIES_PER_REQUEST
    attempt_timeout_seconds: float = DEFAULT_ATTEMPT_TIMEOUT_SECONDS
    max_active_seconds: float = DEFAULT_MAX_ACTIVE_SECONDS
    max_tokens: int = DEFAULT_MAX_TOKENS

    def __post_init__(self) -> None:
        for name, value, upper in (
            ("max_provider_attempts", self.max_provider_attempts, 100),
            ("max_tool_executions", self.max_tool_executions, 1_000),
            ("max_retries_per_request", self.max_retries_per_request, 10),
            ("max_tokens", self.max_tokens, MAX_OUTPUT_TOKENS * 100),
        ):
            if not isinstance(value, int) or isinstance(value, bool):
                raise TypeError(f"{name} must be an integer")
            if not 0 <= value <= upper:
                raise ValueError(f"{name} must be between 0 and {upper}")
        if self.max_provider_attempts < 1:
            raise ValueError("max_provider_attempts must be at least 1")
        if self.max_tokens < 1:
            raise ValueError("max_tokens must be at least 1")
        for name, value in (
            ("attempt_timeout_seconds", self.attempt_timeout_seconds),
            ("max_active_seconds", self.max_active_seconds),
        ):
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                raise TypeError(f"{name} must be numeric")
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be a finite positive number")

    def public_dict(self) -> dict[str, Any]:
        return asdict(self)


class WorkflowStatus(StrEnum):
    RUNNING = "running"
    PAUSED_HUMAN = "paused_human"
    CANCELLED = "cancelled"
    EXHAUSTED = "exhausted"


class AttemptState(StrEnum):
    STARTED = "started"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    ABANDONED = "abandoned"
    LATE_AFTER_CANCEL = "late_after_cancel"


class AttemptOutcome(StrEnum):
    SUCCESS = "success"
    FAILURE = "failure"


class UsageCharge(StrEnum):
    ACTUAL = "actual"
    RESERVED = "reserved"
    ZERO = "zero"


@dataclass(frozen=True, slots=True)
class WorkflowSnapshot:
    workflow_id: str
    run_id: str
    status: WorkflowStatus
    limits: ProviderLimits
    provider_attempts: int
    tool_executions: int
    active_seconds: float
    used_tokens: int
    reserved_tokens: int
    actual_cost_micros: int
    reserved_cost_micros: int
    workflow_cap_micros: int
    run_cap_micros: int
    currency: str
    run_actual_cost_micros: int
    run_reserved_cost_micros: int
    pause_reason: str | None
    last_error_code: str | None

    @property
    def remaining_tokens(self) -> int:
        return max(self.limits.max_tokens - self.used_tokens - self.reserved_tokens, 0)

    @property
    def remaining_active_seconds(self) -> float:
        return max(self.limits.max_active_seconds - self.active_seconds, 0.0)

    @property
    def remaining_provider_attempts(self) -> int:
        return max(self.limits.max_provider_attempts - self.provider_attempts, 0)

    @property
    def remaining_tool_executions(self) -> int:
        return max(self.limits.max_tool_executions - self.tool_executions, 0)

    @property
    def remaining_workflow_micros(self) -> int:
        return max(
            self.workflow_cap_micros - self.actual_cost_micros - self.reserved_cost_micros,
            0,
        )

    @property
    def remaining_run_micros(self) -> int:
        return max(
            self.run_cap_micros - self.run_actual_cost_micros - self.run_reserved_cost_micros,
            0,
        )

    def public_dict(self) -> dict[str, Any]:
        return {
            "workflow_id": self.workflow_id,
            "run_id": self.run_id,
            "status": self.status.value,
            "limits": self.limits.public_dict(),
            "provider_attempts": self.provider_attempts,
            "tool_executions": self.tool_executions,
            "active_seconds": round(self.active_seconds, 6),
            "used_tokens": self.used_tokens,
            "reserved_tokens": self.reserved_tokens,
            "actual_cost_micros": self.actual_cost_micros,
            "reserved_cost_micros": self.reserved_cost_micros,
            "workflow_cap_micros": self.workflow_cap_micros,
            "run_cap_micros": self.run_cap_micros,
            "currency": self.currency,
            "run_actual_cost_micros": self.run_actual_cost_micros,
            "run_reserved_cost_micros": self.run_reserved_cost_micros,
            "pause_reason": self.pause_reason,
            "last_error_code": self.last_error_code,
        }


@dataclass(frozen=True, slots=True)
class AttemptPermit:
    attempt_id: str
    workflow_id: str
    request_id: str
    attempt_number: int
    reserved_tokens: int
    reserved_cost_micros: int
    timeout_seconds: float


@dataclass(frozen=True, slots=True)
class FinalizeResult:
    attempt_id: str
    state: AttemptState
    late: bool
    used_tokens: int
    cost_micros: int
    over_budget: bool
    snapshot: WorkflowSnapshot


@dataclass(frozen=True, slots=True)
class ToolReceipt:
    execution_id: str
    workflow_id: str
    tool_name: str


_SCHEMA = """
CREATE TABLE IF NOT EXISTS provider_runs (
    run_id TEXT PRIMARY KEY,
    currency TEXT NOT NULL,
    cap_micros INTEGER NOT NULL CHECK (cap_micros > 0),
    created_at_utc TEXT NOT NULL
) STRICT;

CREATE TABLE IF NOT EXISTS provider_workflows (
    workflow_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES provider_runs(run_id) ON UPDATE RESTRICT ON DELETE RESTRICT,
    status TEXT NOT NULL,
    limits_json TEXT NOT NULL,
    workflow_cap_micros INTEGER NOT NULL CHECK (workflow_cap_micros > 0),
    provider_attempts INTEGER NOT NULL DEFAULT 0 CHECK (provider_attempts >= 0),
    tool_executions INTEGER NOT NULL DEFAULT 0 CHECK (tool_executions >= 0),
    active_micros INTEGER NOT NULL DEFAULT 0 CHECK (active_micros >= 0),
    used_tokens INTEGER NOT NULL DEFAULT 0 CHECK (used_tokens >= 0),
    reserved_tokens INTEGER NOT NULL DEFAULT 0 CHECK (reserved_tokens >= 0),
    actual_cost_micros INTEGER NOT NULL DEFAULT 0 CHECK (actual_cost_micros >= 0),
    reserved_cost_micros INTEGER NOT NULL DEFAULT 0 CHECK (reserved_cost_micros >= 0),
    pause_reason TEXT,
    last_error_code TEXT,
    created_at_utc TEXT NOT NULL,
    updated_at_utc TEXT NOT NULL
) STRICT;

CREATE TABLE IF NOT EXISTS provider_attempts (
    attempt_id TEXT PRIMARY KEY,
    workflow_id TEXT NOT NULL
        REFERENCES provider_workflows(workflow_id) ON UPDATE RESTRICT ON DELETE CASCADE,
    request_id TEXT NOT NULL,
    attempt_number INTEGER NOT NULL CHECK (attempt_number >= 1),
    state TEXT NOT NULL,
    reserved_tokens INTEGER NOT NULL CHECK (reserved_tokens >= 0),
    reserved_cost_micros INTEGER NOT NULL CHECK (reserved_cost_micros >= 0),
    timeout_micros INTEGER NOT NULL CHECK (timeout_micros > 0),
    used_tokens INTEGER NOT NULL DEFAULT 0 CHECK (used_tokens >= 0),
    actual_cost_micros INTEGER NOT NULL DEFAULT 0 CHECK (actual_cost_micros >= 0),
    latency_micros INTEGER NOT NULL DEFAULT 0 CHECK (latency_micros >= 0),
    error_code TEXT,
    retryable INTEGER NOT NULL DEFAULT 0,
    started_at_utc TEXT NOT NULL,
    finished_at_utc TEXT,
    UNIQUE (workflow_id, request_id, attempt_number)
) STRICT;

CREATE TABLE IF NOT EXISTS provider_tool_executions (
    execution_id TEXT PRIMARY KEY,
    workflow_id TEXT NOT NULL
        REFERENCES provider_workflows(workflow_id) ON UPDATE RESTRICT ON DELETE CASCADE,
    tool_name TEXT NOT NULL,
    state TEXT NOT NULL,
    active_micros INTEGER NOT NULL DEFAULT 0 CHECK (active_micros >= 0),
    started_at_utc TEXT NOT NULL,
    finished_at_utc TEXT
) STRICT;
"""


class BudgetLedger:
    """Single-writer SQLite ledger that persists all provider allowances."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._redactor = SecretRedactor()
        try:
            self._connection = sqlite3.connect(
                self.path,
                isolation_level=None,
                timeout=5.0,
            )
        except sqlite3.Error:
            raise LedgerError("could not open the provider budget ledger") from None
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA foreign_keys=ON")
        self._connection.execute("PRAGMA busy_timeout=5000")
        self._connection.executescript(_SCHEMA)

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> BudgetLedger:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        try:
            self._connection.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError:
            raise LedgerError("provider budget ledger is busy") from None
        try:
            yield self._connection
        except BaseException:
            self._connection.rollback()
            raise
        else:
            self._connection.commit()

    def open_run(self, *, run_id: str, currency: str, cap_micros: int) -> None:
        self._validate_identifier(run_id, "run_id")
        if not isinstance(currency, str) or len(currency) != 3 or not currency.isalpha():
            raise LedgerError("run currency must be a three-letter code")
        if not isinstance(cap_micros, int) or isinstance(cap_micros, bool) or cap_micros <= 0:
            raise LedgerError("run monetary cap must be a positive integer in micro-units")
        currency = currency.upper()
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM provider_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if row is not None:
                if row["currency"] != currency or row["cap_micros"] != cap_micros:
                    raise LedgerError(
                        "persisted run budget differs from the requested run budget",
                        details={"run_id": run_id},
                    )
                return
            connection.execute(
                "INSERT INTO provider_runs(run_id, currency, cap_micros, created_at_utc) "
                "VALUES (?, ?, ?, ?)",
                (run_id, currency, cap_micros, _now_utc()),
            )

    def open_workflow(
        self,
        *,
        workflow_id: str,
        run_id: str,
        limits: ProviderLimits,
        workflow_cap_micros: int,
    ) -> WorkflowSnapshot:
        self._validate_identifier(workflow_id, "workflow_id")
        self._validate_identifier(run_id, "run_id")
        if not isinstance(limits, ProviderLimits):
            raise LedgerError("limits must be a ProviderLimits value")
        if (
            not isinstance(workflow_cap_micros, int)
            or isinstance(workflow_cap_micros, bool)
            or workflow_cap_micros <= 0
        ):
            raise LedgerError("workflow monetary cap must be a positive integer in micro-units")
        limits_json = self._limits_json(limits)
        with self._transaction() as connection:
            run = connection.execute(
                "SELECT * FROM provider_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if run is None:
                raise LedgerError(f"unknown run_id {run_id!r}")
            row = connection.execute(
                "SELECT * FROM provider_workflows WHERE workflow_id = ?", (workflow_id,)
            ).fetchone()
            if row is not None:
                if row["run_id"] != run_id:
                    raise LedgerError("workflow is already linked to a different run_id")
                if row["limits_json"] != limits_json:
                    raise LedgerError(
                        "persisted workflow limits differ; "
                        "a restart does not grant a new allowance",
                        details={"workflow_id": workflow_id},
                    )
                if row["workflow_cap_micros"] != workflow_cap_micros:
                    raise LedgerError(
                        "persisted workflow monetary cap differs; "
                        "a restart does not grant a new allowance",
                        details={"workflow_id": workflow_id},
                    )
                self._recover_orphans(connection, workflow_id, limits)
            else:
                timestamp = _now_utc()
                connection.execute(
                    "INSERT INTO provider_workflows("
                    "workflow_id, run_id, status, limits_json, workflow_cap_micros, "
                    "created_at_utc, updated_at_utc"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        workflow_id,
                        run_id,
                        WorkflowStatus.RUNNING.value,
                        limits_json,
                        workflow_cap_micros,
                        timestamp,
                        timestamp,
                    ),
                )
        return self.snapshot(workflow_id)

    def begin_attempt(
        self,
        *,
        workflow_id: str,
        request_id: str,
        reserved_tokens: int,
        reserved_cost_micros: int,
    ) -> AttemptPermit:
        self._validate_identifier(workflow_id, "workflow_id")
        self._validate_identifier(request_id, "request_id")
        for name, value in (
            ("reserved_tokens", reserved_tokens),
            ("reserved_cost_micros", reserved_cost_micros),
        ):
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise LedgerError(f"{name} must be a non-negative integer")
        if reserved_tokens < 1:
            raise LedgerError("an attempt must reserve at least one token")

        permit: AttemptPermit | None = None
        denial: BudgetExceededError | WorkflowStateError | None = None
        with self._transaction() as connection:
            row = self._workflow_row(connection, workflow_id)
            run = connection.execute(
                "SELECT * FROM provider_runs WHERE run_id = ?", (row["run_id"],)
            ).fetchone()
            assert run is not None
            limits = self._decode_limits(row["limits_json"])
            status = WorkflowStatus(row["status"])
            if status is WorkflowStatus.PAUSED_HUMAN:
                denial = WorkflowStateError(
                    ProviderErrorCode.WORKFLOW_STATE,
                    "workflow is paused for human approval; no provider work may start",
                )
            elif status is WorkflowStatus.CANCELLED:
                denial = WorkflowStateError(
                    ProviderErrorCode.CANCELLED,
                    "workflow is cancelled; late responses cannot revive it",
                )
            elif status is WorkflowStatus.EXHAUSTED:
                denial = BudgetExceededError(
                    "workflow_exhausted",
                    "workflow already exhausted a provider allowance",
                )
            else:
                request_attempts = connection.execute(
                    "SELECT COUNT(*) AS count FROM provider_attempts "
                    "WHERE workflow_id = ? AND request_id = ?",
                    (workflow_id, request_id),
                ).fetchone()["count"]
                if request_attempts >= limits.max_retries_per_request + 1:
                    denial = BudgetExceededError(
                        "retries_per_request",
                        "request exhausted its retry allowance",
                        details={"retries": request_attempts - 1},
                    )
                elif row["provider_attempts"] + 1 > limits.max_provider_attempts:
                    denial = self._exhaust(
                        connection,
                        row,
                        "provider_attempts",
                        "workflow provider attempt limit reached",
                    )
                elif (
                    row["used_tokens"] + row["reserved_tokens"] + reserved_tokens
                    > limits.max_tokens
                ):
                    denial = self._exhaust(
                        connection,
                        row,
                        "tokens",
                        "workflow token limit would be exceeded",
                    )
                else:
                    remaining_active_micros = (
                        _seconds_to_micros(limits.max_active_seconds) - row["active_micros"]
                    )
                    if remaining_active_micros <= 0:
                        denial = self._exhaust(
                            connection,
                            row,
                            "active_time",
                            "workflow active-time limit reached",
                        )
                    elif (
                        row["actual_cost_micros"]
                        + row["reserved_cost_micros"]
                        + reserved_cost_micros
                        > row["workflow_cap_micros"]
                    ):
                        denial = self._exhaust(
                            connection,
                            row,
                            "workflow_cost",
                            "workflow monetary cap would be exceeded",
                        )
                    else:
                        totals = self._run_totals(connection, row["run_id"])
                        if (
                            totals["actual"] + totals["reserved"] + reserved_cost_micros
                            > run["cap_micros"]
                        ):
                            denial = self._exhaust(
                                connection,
                                row,
                                "run_cost",
                                "parent run monetary cap would be exceeded",
                            )
                        else:
                            timeout_micros = min(
                                _seconds_to_micros(limits.attempt_timeout_seconds),
                                remaining_active_micros,
                            )
                            attempt_number = request_attempts + 1
                            attempt_id = f"attempt-{uuid4().hex}"
                            timestamp = _now_utc()
                            connection.execute(
                                "INSERT INTO provider_attempts("
                                "attempt_id, workflow_id, request_id, attempt_number, state, "
                                "reserved_tokens, reserved_cost_micros, timeout_micros, "
                                "started_at_utc"
                                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                                (
                                    attempt_id,
                                    workflow_id,
                                    request_id,
                                    attempt_number,
                                    AttemptState.STARTED.value,
                                    reserved_tokens,
                                    reserved_cost_micros,
                                    timeout_micros,
                                    timestamp,
                                ),
                            )
                            connection.execute(
                                "UPDATE provider_workflows SET "
                                "provider_attempts = provider_attempts + 1, "
                                "reserved_tokens = reserved_tokens + ?, "
                                "reserved_cost_micros = reserved_cost_micros + ?, "
                                "updated_at_utc = ? WHERE workflow_id = ?",
                                (
                                    reserved_tokens,
                                    reserved_cost_micros,
                                    timestamp,
                                    workflow_id,
                                ),
                            )
                            permit = AttemptPermit(
                                attempt_id=attempt_id,
                                workflow_id=workflow_id,
                                request_id=request_id,
                                attempt_number=attempt_number,
                                reserved_tokens=reserved_tokens,
                                reserved_cost_micros=reserved_cost_micros,
                                timeout_seconds=_micros_to_seconds(timeout_micros),
                            )
        if denial is not None:
            raise denial
        assert permit is not None
        return permit

    def finish_attempt(
        self,
        permit: AttemptPermit,
        *,
        outcome: AttemptOutcome,
        latency_seconds: float,
        charge: UsageCharge,
        error_code: ProviderErrorCode | None = None,
        retryable: bool = False,
        usage_tokens: int | None = None,
        cost_micros: int | None = None,
    ) -> FinalizeResult:
        if not isinstance(permit, AttemptPermit):
            raise LedgerError("permit is not an AttemptPermit")
        latency_micros = _seconds_to_micros(latency_seconds)
        with self._transaction() as connection:
            attempt = connection.execute(
                "SELECT * FROM provider_attempts WHERE attempt_id = ?", (permit.attempt_id,)
            ).fetchone()
            if attempt is None or attempt["workflow_id"] != permit.workflow_id:
                raise LedgerError("attempt permit does not match the ledger")
            if attempt["state"] != AttemptState.STARTED.value:
                raise LedgerError("attempt was already finalized")
            row = self._workflow_row(connection, permit.workflow_id)
            limits = self._decode_limits(row["limits_json"])
            run = connection.execute(
                "SELECT * FROM provider_runs WHERE run_id = ?", (row["run_id"],)
            ).fetchone()
            assert run is not None
            late = row["status"] == WorkflowStatus.CANCELLED.value
            effective_charge = UsageCharge.RESERVED if late else charge
            delta_tokens = self._charged_tokens(
                effective_charge, attempt, usage_tokens=usage_tokens
            )
            delta_cost = self._charged_cost(effective_charge, attempt, cost_micros=cost_micros)
            state = (
                AttemptState.LATE_AFTER_CANCEL
                if late
                else (
                    AttemptState.SUCCEEDED
                    if outcome is AttemptOutcome.SUCCESS
                    else AttemptState.FAILED
                )
            )
            effective_error = ProviderErrorCode.CANCELLED if late else error_code
            timestamp = _now_utc()
            connection.execute(
                "UPDATE provider_attempts SET state = ?, used_tokens = ?, "
                "actual_cost_micros = ?, latency_micros = ?, error_code = ?, retryable = ?, "
                "finished_at_utc = ? WHERE attempt_id = ?",
                (
                    state.value,
                    delta_tokens,
                    delta_cost,
                    latency_micros,
                    None if effective_error is None else effective_error.value,
                    int(bool(retryable)),
                    timestamp,
                    permit.attempt_id,
                ),
            )
            reserved_tokens = max(row["reserved_tokens"] - attempt["reserved_tokens"], 0)
            reserved_cost = max(row["reserved_cost_micros"] - attempt["reserved_cost_micros"], 0)
            used_tokens = row["used_tokens"] + delta_tokens
            actual_cost = row["actual_cost_micros"] + delta_cost
            active_micros = row["active_micros"] + latency_micros
            connection.execute(
                "UPDATE provider_workflows SET reserved_tokens = ?, reserved_cost_micros = ?, "
                "used_tokens = ?, actual_cost_micros = ?, active_micros = ?, "
                "last_error_code = ?, updated_at_utc = ? WHERE workflow_id = ?",
                (
                    reserved_tokens,
                    reserved_cost,
                    used_tokens,
                    actual_cost,
                    active_micros,
                    None if effective_error is None else effective_error.value,
                    timestamp,
                    permit.workflow_id,
                ),
            )
            totals = self._run_totals(connection, row["run_id"])
            over_budget = self._is_over_budget(
                row,
                limits,
                run,
                totals,
                used_tokens=used_tokens,
                reserved_tokens=reserved_tokens,
                actual_cost=actual_cost,
                reserved_cost=reserved_cost,
                active_micros=active_micros,
            )
            if over_budget and not late:
                connection.execute(
                    "UPDATE provider_workflows SET status = ?, last_error_code = ?, "
                    "updated_at_utc = ? WHERE workflow_id = ?",
                    (
                        WorkflowStatus.EXHAUSTED.value,
                        ProviderErrorCode.BUDGET.value,
                        timestamp,
                        permit.workflow_id,
                    ),
                )
        snapshot = self.snapshot(permit.workflow_id)
        return FinalizeResult(
            attempt_id=permit.attempt_id,
            state=state,
            late=late,
            used_tokens=delta_tokens,
            cost_micros=delta_cost,
            over_budget=over_budget,
            snapshot=snapshot,
        )

    def record_backoff(self, workflow_id: str, seconds: float) -> WorkflowSnapshot:
        micros = _seconds_to_micros(seconds)
        with self._transaction() as connection:
            row = self._workflow_row(connection, workflow_id)
            status = WorkflowStatus(row["status"])
            if status is not WorkflowStatus.RUNNING:
                raise WorkflowStateError(
                    ProviderErrorCode.WORKFLOW_STATE,
                    f"cannot record retry backoff while workflow is {status.value}",
                )
            active_micros = row["active_micros"] + micros
            timestamp = _now_utc()
            connection.execute(
                "UPDATE provider_workflows SET active_micros = ?, updated_at_utc = ? "
                "WHERE workflow_id = ?",
                (active_micros, timestamp, workflow_id),
            )
            limits = self._decode_limits(row["limits_json"])
            if active_micros >= _seconds_to_micros(limits.max_active_seconds):
                connection.execute(
                    "UPDATE provider_workflows SET status = ?, last_error_code = ?, "
                    "updated_at_utc = ? WHERE workflow_id = ?",
                    (
                        WorkflowStatus.EXHAUSTED.value,
                        ProviderErrorCode.ACTIVE_DEADLINE.value,
                        timestamp,
                        workflow_id,
                    ),
                )
        return self.snapshot(workflow_id)

    def record_tool_execution(self, workflow_id: str, tool_name: str) -> ToolReceipt:
        self._validate_identifier(tool_name, "tool_name")
        receipt: ToolReceipt | None = None
        denial: BudgetExceededError | WorkflowStateError | None = None
        with self._transaction() as connection:
            row = self._workflow_row(connection, workflow_id)
            status = WorkflowStatus(row["status"])
            if status is not WorkflowStatus.RUNNING:
                denial = WorkflowStateError(
                    ProviderErrorCode.WORKFLOW_STATE,
                    f"cannot execute a tool while workflow is {status.value}",
                )
            else:
                limits = self._decode_limits(row["limits_json"])
                if row["active_micros"] >= _seconds_to_micros(limits.max_active_seconds):
                    denial = self._exhaust(
                        connection,
                        row,
                        "active_time",
                        "workflow active-time limit reached before tool execution",
                    )
                elif row["tool_executions"] + 1 > limits.max_tool_executions:
                    denial = self._exhaust(
                        connection,
                        row,
                        "tool_executions",
                        "workflow tool-execution limit reached",
                    )
                else:
                    execution_id = f"tool-{uuid4().hex}"
                    timestamp = _now_utc()
                    connection.execute(
                        "INSERT INTO provider_tool_executions("
                        "execution_id, workflow_id, tool_name, state, started_at_utc"
                        ") VALUES (?, ?, ?, ?, ?)",
                        (execution_id, workflow_id, tool_name, "started", timestamp),
                    )
                    connection.execute(
                        "UPDATE provider_workflows SET tool_executions = tool_executions + 1, "
                        "updated_at_utc = ? WHERE workflow_id = ?",
                        (timestamp, workflow_id),
                    )
                    receipt = ToolReceipt(
                        execution_id=execution_id,
                        workflow_id=workflow_id,
                        tool_name=tool_name,
                    )
        if denial is not None:
            raise denial
        assert receipt is not None
        return receipt

    def finish_tool_execution(
        self,
        receipt: ToolReceipt,
        *,
        active_seconds: float = 0.0,
        succeeded: bool = True,
    ) -> WorkflowSnapshot:
        if not isinstance(receipt, ToolReceipt):
            raise LedgerError("receipt is not a ToolReceipt")
        micros = _seconds_to_micros(active_seconds)
        with self._transaction() as connection:
            row = self._workflow_row(connection, receipt.workflow_id)
            execution = connection.execute(
                "SELECT * FROM provider_tool_executions WHERE execution_id = ?",
                (receipt.execution_id,),
            ).fetchone()
            if (
                execution is None
                or execution["workflow_id"] != receipt.workflow_id
                or execution["tool_name"] != receipt.tool_name
            ):
                raise LedgerError("tool execution receipt does not match the ledger")
            if execution["state"] != "started":
                raise LedgerError("tool execution was already finalized")
            timestamp = _now_utc()
            connection.execute(
                "UPDATE provider_tool_executions SET state = ?, active_micros = ?, "
                "finished_at_utc = ? WHERE execution_id = ?",
                (
                    "succeeded" if succeeded else "failed",
                    micros,
                    timestamp,
                    receipt.execution_id,
                ),
            )
            active_micros = row["active_micros"] + micros
            connection.execute(
                "UPDATE provider_workflows SET active_micros = ?, updated_at_utc = ? "
                "WHERE workflow_id = ?",
                (active_micros, timestamp, receipt.workflow_id),
            )
            limits = self._decode_limits(row["limits_json"])
            if active_micros >= _seconds_to_micros(limits.max_active_seconds):
                connection.execute(
                    "UPDATE provider_workflows SET status = ?, last_error_code = ?, "
                    "updated_at_utc = ? WHERE workflow_id = ?",
                    (
                        WorkflowStatus.EXHAUSTED.value,
                        ProviderErrorCode.ACTIVE_DEADLINE.value,
                        timestamp,
                        receipt.workflow_id,
                    ),
                )
        return self.snapshot(receipt.workflow_id)

    def pause_for_human(self, workflow_id: str, reason: str = "human_approval") -> WorkflowSnapshot:
        with self._transaction() as connection:
            row = self._workflow_row(connection, workflow_id)
            status = WorkflowStatus(row["status"])
            if status is WorkflowStatus.PAUSED_HUMAN:
                return self.snapshot(workflow_id)
            if status is not WorkflowStatus.RUNNING:
                raise WorkflowStateError(
                    ProviderErrorCode.WORKFLOW_STATE,
                    f"cannot pause workflow in state {status.value}",
                )
            connection.execute(
                "UPDATE provider_workflows SET status = ?, pause_reason = ?, "
                "updated_at_utc = ? WHERE workflow_id = ?",
                (WorkflowStatus.PAUSED_HUMAN.value, reason, _now_utc(), workflow_id),
            )
        return self.snapshot(workflow_id)

    def resume(self, workflow_id: str) -> WorkflowSnapshot:
        with self._transaction() as connection:
            row = self._workflow_row(connection, workflow_id)
            status = WorkflowStatus(row["status"])
            if status is WorkflowStatus.RUNNING:
                return self.snapshot(workflow_id)
            if status is not WorkflowStatus.PAUSED_HUMAN:
                raise WorkflowStateError(
                    ProviderErrorCode.WORKFLOW_STATE,
                    f"cannot resume workflow in state {status.value}",
                )
            connection.execute(
                "UPDATE provider_workflows SET status = ?, pause_reason = NULL, "
                "updated_at_utc = ? WHERE workflow_id = ?",
                (WorkflowStatus.RUNNING.value, _now_utc(), workflow_id),
            )
        return self.snapshot(workflow_id)

    def cancel(self, workflow_id: str) -> WorkflowSnapshot:
        with self._transaction() as connection:
            row = self._workflow_row(connection, workflow_id)
            if row["status"] == WorkflowStatus.CANCELLED.value:
                return self.snapshot(workflow_id)
            connection.execute(
                "UPDATE provider_workflows SET status = ?, last_error_code = ?, "
                "updated_at_utc = ? WHERE workflow_id = ?",
                (
                    WorkflowStatus.CANCELLED.value,
                    ProviderErrorCode.CANCELLED.value,
                    _now_utc(),
                    workflow_id,
                ),
            )
        return self.snapshot(workflow_id)

    def snapshot(self, workflow_id: str) -> WorkflowSnapshot:
        row = self._connection.execute(
            "SELECT * FROM provider_workflows WHERE workflow_id = ?", (workflow_id,)
        ).fetchone()
        if row is None:
            raise LedgerError(f"unknown workflow_id {workflow_id!r}")
        run = self._connection.execute(
            "SELECT * FROM provider_runs WHERE run_id = ?", (row["run_id"],)
        ).fetchone()
        assert run is not None
        totals = self._run_totals(self._connection, row["run_id"])
        return self._snapshot_from_rows(row, run, totals)

    def _workflow_row(self, connection: sqlite3.Connection, workflow_id: str) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM provider_workflows WHERE workflow_id = ?", (workflow_id,)
        ).fetchone()
        if row is None:
            raise LedgerError(f"unknown workflow_id {workflow_id!r}")
        return row

    def _run_totals(self, connection: sqlite3.Connection, run_id: str) -> dict[str, int]:
        row = connection.execute(
            "SELECT COALESCE(SUM(actual_cost_micros), 0) AS actual, "
            "COALESCE(SUM(reserved_cost_micros), 0) AS reserved "
            "FROM provider_workflows WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        return {"actual": int(row["actual"]), "reserved": int(row["reserved"])}

    def _snapshot_from_rows(
        self,
        row: sqlite3.Row,
        run: sqlite3.Row,
        totals: Mapping[str, int],
    ) -> WorkflowSnapshot:
        return WorkflowSnapshot(
            workflow_id=row["workflow_id"],
            run_id=row["run_id"],
            status=WorkflowStatus(row["status"]),
            limits=self._decode_limits(row["limits_json"]),
            provider_attempts=row["provider_attempts"],
            tool_executions=row["tool_executions"],
            active_seconds=_micros_to_seconds(row["active_micros"]),
            used_tokens=row["used_tokens"],
            reserved_tokens=row["reserved_tokens"],
            actual_cost_micros=row["actual_cost_micros"],
            reserved_cost_micros=row["reserved_cost_micros"],
            workflow_cap_micros=row["workflow_cap_micros"],
            run_cap_micros=run["cap_micros"],
            currency=run["currency"],
            run_actual_cost_micros=totals["actual"],
            run_reserved_cost_micros=totals["reserved"],
            pause_reason=row["pause_reason"],
            last_error_code=row["last_error_code"],
        )

    def _recover_orphans(
        self,
        connection: sqlite3.Connection,
        workflow_id: str,
        limits: ProviderLimits,
    ) -> None:
        attempt_orphans = connection.execute(
            "SELECT * FROM provider_attempts WHERE workflow_id = ? AND state = ?",
            (workflow_id, AttemptState.STARTED.value),
        ).fetchall()
        tool_orphans = connection.execute(
            "SELECT * FROM provider_tool_executions WHERE workflow_id = ? AND state = ? "
            "ORDER BY started_at_utc, execution_id",
            (workflow_id, "started"),
        ).fetchall()
        if not attempt_orphans and not tool_orphans:
            return
        timestamp = _now_utc()
        for attempt in attempt_orphans:
            connection.execute(
                "UPDATE provider_attempts SET state = ?, used_tokens = ?, "
                "actual_cost_micros = ?, latency_micros = ?, error_code = ?, "
                "finished_at_utc = ? WHERE attempt_id = ?",
                (
                    AttemptState.ABANDONED.value,
                    attempt["reserved_tokens"],
                    attempt["reserved_cost_micros"],
                    attempt["timeout_micros"],
                    "process_restart",
                    timestamp,
                    attempt["attempt_id"],
                ),
            )
            connection.execute(
                "UPDATE provider_workflows SET reserved_tokens = MAX(reserved_tokens - ?, 0), "
                "reserved_cost_micros = MAX(reserved_cost_micros - ?, 0), "
                "used_tokens = used_tokens + ?, actual_cost_micros = actual_cost_micros + ?, "
                "active_micros = active_micros + ?, last_error_code = ?, updated_at_utc = ? "
                "WHERE workflow_id = ?",
                (
                    attempt["reserved_tokens"],
                    attempt["reserved_cost_micros"],
                    attempt["reserved_tokens"],
                    attempt["reserved_cost_micros"],
                    attempt["timeout_micros"],
                    "process_restart",
                    timestamp,
                    workflow_id,
                ),
            )
        if tool_orphans:
            row = self._workflow_row(connection, workflow_id)
            active_limit = _seconds_to_micros(limits.max_active_seconds)
            remaining_active = max(active_limit - row["active_micros"], 0)
            for index, execution in enumerate(tool_orphans):
                charged_active = remaining_active if index == 0 else 0
                connection.execute(
                    "UPDATE provider_tool_executions SET state = ?, active_micros = ?, "
                    "finished_at_utc = ? WHERE execution_id = ?",
                    ("abandoned", charged_active, timestamp, execution["execution_id"]),
                )
            connection.execute(
                "UPDATE provider_workflows SET active_micros = active_micros + ?, "
                "last_error_code = ?, updated_at_utc = ? WHERE workflow_id = ?",
                (
                    remaining_active,
                    ProviderErrorCode.ACTIVE_DEADLINE.value,
                    timestamp,
                    workflow_id,
                ),
            )
        row = self._workflow_row(connection, workflow_id)
        run = connection.execute(
            "SELECT * FROM provider_runs WHERE run_id = ?", (row["run_id"],)
        ).fetchone()
        assert run is not None
        totals = self._run_totals(connection, row["run_id"])
        if tool_orphans and row["status"] == WorkflowStatus.RUNNING.value:
            connection.execute(
                "UPDATE provider_workflows SET status = ?, last_error_code = ?, "
                "updated_at_utc = ? WHERE workflow_id = ?",
                (
                    WorkflowStatus.EXHAUSTED.value,
                    ProviderErrorCode.ACTIVE_DEADLINE.value,
                    timestamp,
                    workflow_id,
                ),
            )
        elif (
            self._is_over_budget(
                row,
                limits,
                run,
                totals,
                used_tokens=row["used_tokens"],
                reserved_tokens=row["reserved_tokens"],
                actual_cost=row["actual_cost_micros"],
                reserved_cost=row["reserved_cost_micros"],
                active_micros=row["active_micros"],
            )
            and row["status"] == WorkflowStatus.RUNNING.value
        ):
            connection.execute(
                "UPDATE provider_workflows SET status = ?, last_error_code = ?, "
                "updated_at_utc = ? WHERE workflow_id = ?",
                (
                    WorkflowStatus.EXHAUSTED.value,
                    ProviderErrorCode.BUDGET.value,
                    timestamp,
                    workflow_id,
                ),
            )

    @staticmethod
    def _charged_tokens(
        charge: UsageCharge,
        attempt: sqlite3.Row,
        *,
        usage_tokens: int | None,
    ) -> int:
        if charge is UsageCharge.ACTUAL:
            if usage_tokens is None or usage_tokens < 0:
                raise LedgerError("actual usage charge requires non-negative usage_tokens")
            return int(usage_tokens)
        if charge is UsageCharge.RESERVED:
            return int(attempt["reserved_tokens"])
        return 0

    @staticmethod
    def _charged_cost(
        charge: UsageCharge,
        attempt: sqlite3.Row,
        *,
        cost_micros: int | None,
    ) -> int:
        if charge is UsageCharge.ACTUAL:
            if cost_micros is None or cost_micros < 0:
                raise LedgerError("actual usage charge requires non-negative cost_micros")
            return int(cost_micros)
        if charge is UsageCharge.RESERVED:
            return int(attempt["reserved_cost_micros"])
        return 0

    @staticmethod
    def _is_over_budget(
        row: sqlite3.Row,
        limits: ProviderLimits,
        run: sqlite3.Row,
        totals: Mapping[str, int],
        *,
        used_tokens: int,
        reserved_tokens: int,
        actual_cost: int,
        reserved_cost: int,
        active_micros: int,
    ) -> bool:
        return bool(
            used_tokens + reserved_tokens > limits.max_tokens
            or active_micros >= _seconds_to_micros(limits.max_active_seconds)
            or actual_cost + reserved_cost > row["workflow_cap_micros"]
            or totals["actual"] + totals["reserved"] > run["cap_micros"]
        )

    @staticmethod
    def _exhaust(
        connection: sqlite3.Connection,
        row: sqlite3.Row,
        limit: str,
        message: str,
    ) -> BudgetExceededError:
        connection.execute(
            "UPDATE provider_workflows SET status = ?, last_error_code = ?, "
            "updated_at_utc = ? WHERE workflow_id = ?",
            (
                WorkflowStatus.EXHAUSTED.value,
                ProviderErrorCode.BUDGET.value,
                _now_utc(),
                row["workflow_id"],
            ),
        )
        return BudgetExceededError(limit, message)

    @staticmethod
    def _limits_json(limits: ProviderLimits) -> str:
        return json.dumps(asdict(limits), sort_keys=True, separators=(",", ":"))

    @staticmethod
    def _decode_limits(raw: str) -> ProviderLimits:
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            raise LedgerError("persisted workflow limits are not valid JSON") from None
        if not isinstance(payload, dict):
            raise LedgerError("persisted workflow limits are not an object")
        try:
            return ProviderLimits(**payload)
        except (TypeError, ValueError):
            raise LedgerError("persisted workflow limits are invalid") from None

    @staticmethod
    def _validate_identifier(value: object, name: str) -> None:
        if not isinstance(value, str) or not 1 <= len(value) <= 64:
            raise LedgerError(f"{name} must be a string of 1..64 characters")
        allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.:-")
        if any(character not in allowed for character in value):
            raise LedgerError(f"{name} contains unsupported characters")
