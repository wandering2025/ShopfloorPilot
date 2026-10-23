"""SQLite authority for run state, operational events, and lifecycle records.

Every method opens a short-lived connection. Operational changes use
``BEGIN IMMEDIATE`` and one compare-and-swap transaction; callers must finish any
network/provider work before entering :meth:`SQLiteStateRepository.apply_mutation`.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, JsonValue, TypeAdapter, ValidationError

from shopfloor_pilot.domain.base import Identifier
from shopfloor_pilot.domain.canonical import canonical_hash
from shopfloor_pilot.domain.models import (
    EVENT_TYPE_PRIORITY,
    ApprovalDecision,
    ApprovalRecord,
    CandidateProposal,
    OperationalSnapshot,
)
from shopfloor_pilot.events import (
    EventDraft,
    EventEnvelope,
    SnapshotCheckpoint,
    materialize_event_group,
    reduce_event_group,
    replay_events,
)

from .errors import (
    CrossRunReferenceError,
    DatabaseBusyError,
    DatabaseNotInitializedError,
    IdempotencyConflictError,
    RecordConflictError,
    RunConflictError,
    RunNotFoundError,
    TransactionStateError,
    VersionConflictError,
)
from .models import (
    ApprovalConsumptionRecord,
    AuditRecord,
    LifecycleExport,
    MutationResult,
    RestoredRunState,
    RunRecord,
    WorkflowRecord,
    WorkflowStatus,
)
from .schema import CURRENT_SCHEMA_VERSION, initialize_schema, read_schema_version

FailureInjector = Callable[[str], None]
TransactionParticipant = Callable[["OperationalTransaction"], None]

_IDENTIFIER_ADAPTER = TypeAdapter(Identifier)


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _model_json(value: BaseModel) -> str:
    return value.model_dump_json()


def _storage_hash(value: Any) -> str:
    """Hash every persisted field, including non-operational wall-clock metadata."""

    return canonical_hash(value, exclude_wall_clock=False)


def _parse_stored_model[ModelT: BaseModel](
    model_type: type[ModelT],
    raw_json: str,
    *,
    record_type: str,
    record_id: str,
) -> ModelT:
    """Deserialize persisted JSON without leaking codec/validation implementation errors."""

    try:
        return model_type.model_validate_json(raw_json)
    except (ValidationError, TypeError, ValueError) as exc:
        raise RecordConflictError(
            record_type,
            record_id,
            f"stored JSON is invalid for {model_type.__name__}",
        ) from exc


def _require_projection(
    record_type: str,
    record_id: str,
    field_name: str,
    stored: Any,
    expected: Any,
) -> None:
    if stored != expected:
        raise RecordConflictError(
            record_type,
            record_id,
            f"stored {field_name} projection does not match JSON content",
        )


def _is_busy_error(error: sqlite3.OperationalError) -> bool:
    message = str(error).casefold()
    return "locked" in message or "busy" in message


class OperationalTransaction:
    """Restricted participant in one repository-owned operational transaction.

    S06 can use this boundary to consume an approval and append an audit record in
    the same commit as events, the snapshot checkpoint, version CAS, and idempotency
    result. The SQLite connection is intentionally not exposed and the object becomes
    unusable as soon as the callback returns.
    """

    def __init__(
        self,
        connection: sqlite3.Connection,
        base_checkpoint: SnapshotCheckpoint,
        result_checkpoint: SnapshotCheckpoint,
        mutation_id: str,
    ) -> None:
        self._connection = connection
        self._base_checkpoint = base_checkpoint
        self._result_checkpoint = result_checkpoint
        self._mutation_id = mutation_id
        self._active = True

    @property
    def run_id(self) -> str:
        return self._base_checkpoint.snapshot.run_id

    @property
    def base_checkpoint(self) -> SnapshotCheckpoint:
        return self._base_checkpoint

    @property
    def result_checkpoint(self) -> SnapshotCheckpoint:
        return self._result_checkpoint

    @property
    def mutation_id(self) -> str:
        return self._mutation_id

    def _require_active(self) -> None:
        if not self._active:
            raise TransactionStateError("transaction participant is no longer active")

    def _close(self) -> None:
        self._active = False

    def consume_approval(
        self,
        approval_id: str,
        *,
        expected_proposal_id: str | None = None,
        expected_proposal_hash: str | None = None,
        expected_result_snapshot_hash: str | None = None,
        consumed_at_utc: str | None = None,
    ) -> ApprovalConsumptionRecord:
        """Validate and record one approval consumption inside this transaction.

        The optional expected bindings let an application service prove that the
        proposal selected by its command, rather than merely some proposal with the
        same next plan revision, authorized the exact resulting snapshot.  Existing
        storage-only callers may omit them; production approved-plan application uses
        :meth:`consume_approved_proposal`, which requires all three values.
        """

        self._require_active()
        approval = _load_approval(self._connection, self.run_id, approval_id)
        proposal = _load_proposal(self._connection, self.run_id, approval.proposal_id)
        if approval.decision is not ApprovalDecision.APPROVED:
            raise RecordConflictError("approval", approval_id, "decision is not approved")
        if (
            approval.proposal_hash != proposal.proposal_hash
            or approval.workflow_id != proposal.workflow_id
            or approval.base_state_version != proposal.base_state_version
        ):
            raise RecordConflictError(
                "approval", approval_id, "binding does not match the stored proposal"
            )
        if expected_proposal_id is not None and proposal.proposal_id != expected_proposal_id:
            raise RecordConflictError(
                "approval",
                approval_id,
                "approved proposal_id does not match the application command",
            )
        if expected_proposal_hash is not None and proposal.proposal_hash != expected_proposal_hash:
            raise RecordConflictError(
                "approval",
                approval_id,
                "approved proposal_hash does not match the application command",
            )
        if proposal.base_state_version != self._base_checkpoint.snapshot.state_version:
            raise VersionConflictError(
                self.run_id,
                proposal.base_state_version,
                self._base_checkpoint.snapshot.state_version,
            )
        if proposal.base_snapshot_hash != self._base_checkpoint.snapshot_hash:
            raise RecordConflictError(
                "proposal", proposal.proposal_id, "base snapshot hash is stale"
            )
        if self._result_checkpoint.snapshot.plan_revision != proposal.proposed_plan_revision:
            raise RecordConflictError(
                "proposal",
                proposal.proposal_id,
                "resulting plan revision does not match the approved proposal",
            )
        if (
            expected_result_snapshot_hash is not None
            and self._result_checkpoint.snapshot_hash != expected_result_snapshot_hash
        ):
            raise RecordConflictError(
                "proposal",
                proposal.proposal_id,
                "resulting snapshot does not match the approved application projection",
            )
        record = ApprovalConsumptionRecord(
            run_id=self.run_id,
            approval_id=approval.approval_id,
            proposal_id=proposal.proposal_id,
            mutation_id=self._mutation_id,
            resulting_state_version=self._result_checkpoint.snapshot.state_version,
            consumed_at_utc=consumed_at_utc or _utc_now(),
        )
        try:
            self._connection.execute(
                """
                INSERT INTO approval_consumptions(
                    run_id, approval_id, proposal_id, mutation_id,
                    resulting_state_version, consumed_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    record.run_id,
                    record.approval_id,
                    record.proposal_id,
                    record.mutation_id,
                    record.resulting_state_version,
                    record.consumed_at_utc,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise RecordConflictError(
                "approval", approval_id, "approval was already consumed or binding conflicts"
            ) from exc
        return record

    def consume_approved_proposal(
        self,
        approval_id: str,
        *,
        expected_proposal_id: str,
        expected_proposal_hash: str,
        expected_result_snapshot_hash: str,
        consumed_at_utc: str | None = None,
    ) -> ApprovalConsumptionRecord:
        """Consume an approval bound to one exact proposal and result snapshot."""

        return self.consume_approval(
            approval_id,
            expected_proposal_id=expected_proposal_id,
            expected_proposal_hash=expected_proposal_hash,
            expected_result_snapshot_hash=expected_result_snapshot_hash,
            consumed_at_utc=consumed_at_utc,
        )

    def append_audit(
        self,
        record: AuditRecord,
        *,
        workflow_id: str | None = None,
    ) -> int:
        """Append non-operational audit evidence in the atomic mutation commit."""

        self._require_active()
        if record.run_id != self.run_id:
            raise CrossRunReferenceError("audit", record.audit_id, self.run_id, record.run_id)
        return _insert_audit(
            self._connection,
            record,
            workflow_id=workflow_id,
            observed_state_version=self._result_checkpoint.snapshot.state_version,
        )

    def transition_workflow(
        self,
        workflow_id: str,
        *,
        expected_status: WorkflowStatus,
        resulting_status: WorkflowStatus,
        payload_updates: Mapping[str, JsonValue] | None = None,
        updated_at_utc: str | None = None,
    ) -> WorkflowRecord:
        """Compare-and-set one workflow inside the operational transaction."""

        self._require_active()
        return _transition_workflow(
            self._connection,
            self.run_id,
            workflow_id,
            expected_status=expected_status,
            resulting_status=resulting_status,
            payload_updates=payload_updates,
            updated_at_utc=updated_at_utc,
            allow_already_resulting=False,
        )


class SQLiteStateRepository:
    """Authoritative, run-scoped SQLite repository with deterministic replay."""

    def __init__(self, database_path: str | Path, *, busy_timeout_seconds: float = 1.0) -> None:
        if busy_timeout_seconds < 0:
            raise ValueError("busy_timeout_seconds must be non-negative")
        self._database_path = Path(database_path)
        self._busy_timeout_seconds = busy_timeout_seconds
        self._busy_timeout_ms = int(busy_timeout_seconds * 1000)

    @property
    def database_path(self) -> Path:
        return self._database_path

    def _connect(self, *, require_initialized: bool = True) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self._database_path,
            timeout=self._busy_timeout_seconds,
            isolation_level=None,
        )
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute(f"PRAGMA busy_timeout = {self._busy_timeout_ms:d}")
            connection.execute("PRAGMA synchronous = FULL")
            if require_initialized:
                version = read_schema_version(connection)
                if version == 0:
                    raise DatabaseNotInitializedError(self._database_path)
            return connection
        except sqlite3.OperationalError as exc:
            connection.close()
            self._raise_if_busy(exc)
            raise
        except BaseException:
            connection.close()
            raise

    def _raise_if_busy(self, error: sqlite3.OperationalError) -> None:
        if _is_busy_error(error):
            raise DatabaseBusyError(self._busy_timeout_seconds) from error

    def _begin_immediate(self, connection: sqlite3.Connection) -> None:
        try:
            connection.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as exc:
            self._raise_if_busy(exc)
            raise

    def initialize(self, *, applied_at_utc: str | None = None) -> int:
        """Idempotently create/validate the explicit schema and durability settings."""

        connection = self._connect(require_initialized=False)
        try:
            version = initialize_schema(connection, applied_at_utc or _utc_now())
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = FULL")
            return version
        except sqlite3.OperationalError as exc:
            self._raise_if_busy(exc)
            raise
        finally:
            connection.close()

    def schema_version(self) -> int:
        connection = self._connect()
        try:
            return read_schema_version(connection)
        finally:
            connection.close()

    def applied_migrations(self) -> tuple[tuple[int, str, str], ...]:
        connection = self._connect()
        try:
            rows = connection.execute(
                "SELECT version, name, checksum FROM schema_migrations ORDER BY version"
            ).fetchall()
            return tuple((row["version"], row["name"], row["checksum"]) for row in rows)
        finally:
            connection.close()

    def integrity_check(self) -> str:
        connection = self._connect()
        try:
            row = connection.execute("PRAGMA integrity_check").fetchone()
            return str(row[0]) if row is not None else "missing result"
        finally:
            connection.close()

    def create_run(
        self,
        initial_snapshot: OperationalSnapshot,
        *,
        reset_from_run_id: str | None = None,
        created_at_utc: str | None = None,
    ) -> RunRecord:
        """Create an isolated version-zero run; reset is modeled as a new run."""

        checkpoint = SnapshotCheckpoint.create(initial_snapshot)
        record = RunRecord(
            run_id=initial_snapshot.run_id,
            reset_from_run_id=reset_from_run_id,
            created_at_utc=created_at_utc or _utc_now(),
            initial_checkpoint=checkpoint,
        )
        connection = self._connect()
        try:
            self._begin_immediate(connection)
            if reset_from_run_id is not None:
                _require_run(connection, reset_from_run_id)
            try:
                connection.execute(
                    """
                    INSERT INTO runs(
                        run_id, schema_version, current_state_version,
                        initial_snapshot_json, initial_snapshot_hash,
                        reset_from_run_id, created_at_utc
                    ) VALUES (?, 1, 0, ?, ?, ?, ?)
                    """,
                    (
                        record.run_id,
                        _model_json(initial_snapshot),
                        checkpoint.snapshot_hash,
                        record.reset_from_run_id,
                        record.created_at_utc,
                    ),
                )
                _insert_checkpoint(connection, checkpoint, record.created_at_utc)
            except sqlite3.IntegrityError as exc:
                raise RunConflictError(record.run_id, str(exc)) from exc
            connection.commit()
            return record
        except sqlite3.OperationalError as exc:
            if connection.in_transaction:
                connection.rollback()
            self._raise_if_busy(exc)
            raise
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    def reset_run(
        self,
        source_run_id: str,
        new_initial_snapshot: OperationalSnapshot,
        *,
        created_at_utc: str | None = None,
    ) -> RunRecord:
        if source_run_id == new_initial_snapshot.run_id:
            raise RunConflictError(source_run_id, "reset requires a new run identity")
        return self.create_run(
            new_initial_snapshot,
            reset_from_run_id=source_run_id,
            created_at_utc=created_at_utc,
        )

    def get_run(self, run_id: str) -> RunRecord:
        connection = self._connect()
        try:
            row = _require_run(connection, run_id)
            snapshot = _parse_stored_model(
                OperationalSnapshot,
                row["initial_snapshot_json"],
                record_type="run",
                record_id=run_id,
            )
            _require_projection("run", run_id, "run_id", row["run_id"], snapshot.run_id)
            _require_projection(
                "run",
                run_id,
                "schema_version",
                row["schema_version"],
                snapshot.schema_version,
            )
            try:
                checkpoint = SnapshotCheckpoint.create(snapshot)
            except (ValidationError, TypeError, ValueError) as exc:
                raise RecordConflictError(
                    "run",
                    run_id,
                    "stored initial snapshot is not a version-zero checkpoint",
                ) from exc
            if checkpoint.snapshot_hash != row["initial_snapshot_hash"]:
                raise RecordConflictError("run", run_id, "initial snapshot hash mismatch")
            try:
                return RunRecord(
                    run_id=run_id,
                    reset_from_run_id=row["reset_from_run_id"],
                    created_at_utc=row["created_at_utc"],
                    initial_checkpoint=checkpoint,
                )
            except (ValidationError, TypeError, ValueError) as exc:
                raise RecordConflictError(
                    "run",
                    run_id,
                    "stored run projections are invalid",
                ) from exc
        finally:
            connection.close()

    def list_reset_successors(self, run_id: str) -> tuple[str, ...]:
        """Return run IDs whose ``reset_from_run_id`` names ``run_id``."""

        connection = self._connect()
        try:
            _require_run(connection, run_id)
            return tuple(
                str(row["run_id"])
                for row in connection.execute(
                    "SELECT run_id FROM runs WHERE reset_from_run_id = ? ORDER BY run_id",
                    (run_id,),
                )
            )
        finally:
            connection.close()

    def get_checkpoint(
        self,
        run_id: str,
        *,
        state_version: int | None = None,
    ) -> SnapshotCheckpoint:
        connection = self._connect()
        try:
            return _load_checkpoint(connection, run_id, state_version=state_version)
        finally:
            connection.close()

    def get_snapshot(self, run_id: str) -> OperationalSnapshot:
        """Return a newly deserialized immutable authoritative snapshot."""

        return self.get_checkpoint(run_id).snapshot

    def list_checkpoints(self, run_id: str) -> tuple[SnapshotCheckpoint, ...]:
        connection = self._connect()
        try:
            _require_run(connection, run_id)
            versions = connection.execute(
                "SELECT state_version FROM snapshot_checkpoints WHERE run_id = ? "
                "ORDER BY state_version",
                (run_id,),
            ).fetchall()
            return tuple(
                _load_checkpoint(connection, run_id, state_version=row["state_version"])
                for row in versions
            )
        finally:
            connection.close()

    def list_events(
        self,
        run_id: str,
        *,
        after_sequence: int = 0,
    ) -> tuple[EventEnvelope, ...]:
        if after_sequence < 0:
            raise ValueError("after_sequence must be non-negative")
        connection = self._connect()
        try:
            _require_run(connection, run_id)
            return _load_events_after(connection, run_id, after_sequence)
        finally:
            connection.close()

    def apply_mutation(
        self,
        *,
        run_id: str,
        expected_state_version: int,
        idempotency_key: str,
        command_name: str,
        command_payload: Mapping[str, JsonValue],
        events: Sequence[EventDraft | Mapping[str, Any]],
        participant: TransactionParticipant | None = None,
        _failure_injector: FailureInjector | None = None,
        created_at_utc: str | None = None,
    ) -> MutationResult:
        """Commit one compare-and-swap event group and checkpoint atomically.

        Idempotency is checked before the expected-version comparison, so an exact
        retry returns its original stored outcome even though its version is now old.
        A participant may perform only the typed, local operations exposed by
        :class:`OperationalTransaction`; it must never make network calls.
        """

        if expected_state_version < 0:
            raise ValueError("expected_state_version must be non-negative")
        if not command_name.strip():
            raise ValueError("command_name must be non-empty")
        idempotency_key = _IDENTIFIER_ADAPTER.validate_python(
            idempotency_key,
            strict=True,
        )
        request_hash = canonical_hash(
            {
                "schema_version": 1,
                "run_id": run_id,
                "expected_state_version": expected_state_version,
                "command_name": command_name,
                "command_payload": dict(command_payload),
                "events": [
                    item.model_dump(mode="json") if isinstance(item, EventDraft) else dict(item)
                    for item in events
                ],
            }
        )
        timestamp = created_at_utc or _utc_now()
        connection = self._connect()
        transaction: OperationalTransaction | None = None
        try:
            self._begin_immediate(connection)

            existing = connection.execute(
                """
                SELECT run_id, idempotency_key, command_hash,
                       expected_state_version, resulting_state_version,
                       mutation_id, result_json, result_hash
                FROM idempotency_records
                WHERE run_id = ? AND idempotency_key = ?
                """,
                (run_id, idempotency_key),
            ).fetchone()
            if existing is not None:
                if existing["command_hash"] != request_hash:
                    raise IdempotencyConflictError(run_id, idempotency_key)
                result = _decode_idempotency_result(
                    connection,
                    existing,
                    requested_run_id=run_id,
                    requested_key=idempotency_key,
                    request_hash=request_hash,
                )
                connection.commit()
                return result.as_idempotent_replay()

            base = _load_checkpoint(connection, run_id)
            actual_version = base.snapshot.state_version
            if actual_version != expected_state_version:
                raise VersionConflictError(run_id, expected_state_version, actual_version)

            envelopes = materialize_event_group(base, idempotency_key, events)
            result_checkpoint = reduce_event_group(base, envelopes)
            result = MutationResult(
                run_id=run_id,
                idempotency_key=idempotency_key,
                mutation_id=idempotency_key,
                command_hash=request_hash,
                previous_state_version=expected_state_version,
                resulting_state_version=expected_state_version + 1,
                events=envelopes,
                checkpoint=result_checkpoint,
            )

            first_sequence = envelopes[0].event.sequence
            connection.execute(
                """
                INSERT INTO event_groups(
                    run_id, mutation_id, base_state_version, resulting_state_version,
                    first_sequence, event_count, command_hash, created_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    idempotency_key,
                    expected_state_version,
                    expected_state_version + 1,
                    first_sequence,
                    len(envelopes),
                    request_hash,
                    timestamp,
                ),
            )
            for envelope in envelopes:
                event = envelope.event
                connection.execute(
                    """
                    INSERT INTO operational_events(
                        run_id, sequence, event_id, mutation_id, event_index,
                        schema_version, simulated_time_seconds, event_priority,
                        event_type, envelope_json, envelope_hash, recorded_at_utc
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        event.sequence,
                        event.event_id,
                        idempotency_key,
                        envelope.event_index,
                        event.schema_version,
                        event.simulated_time_seconds,
                        EVENT_TYPE_PRIORITY[event.event_type],
                        event.event_type.value,
                        _model_json(envelope),
                        _storage_hash(envelope),
                        event.recorded_at_utc,
                    ),
                )

            if _failure_injector is not None:
                _failure_injector("after_event_insert")

            transaction = OperationalTransaction(
                connection,
                base,
                result_checkpoint,
                idempotency_key,
            )
            if participant is not None:
                participant(transaction)
            transaction._close()

            _insert_checkpoint(connection, result_checkpoint, timestamp)
            updated = connection.execute(
                """
                UPDATE runs
                SET current_state_version = ?
                WHERE run_id = ? AND current_state_version = ?
                """,
                (expected_state_version + 1, run_id, expected_state_version),
            )
            if updated.rowcount != 1:
                current = _require_run(connection, run_id)["current_state_version"]
                raise VersionConflictError(run_id, expected_state_version, current)

            if _failure_injector is not None:
                _failure_injector("after_snapshot_insert")

            connection.execute(
                """
                INSERT INTO idempotency_records(
                    run_id, idempotency_key, command_hash, expected_state_version,
                    resulting_state_version, mutation_id, result_json, result_hash,
                    created_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    idempotency_key,
                    request_hash,
                    expected_state_version,
                    expected_state_version + 1,
                    idempotency_key,
                    _model_json(result),
                    _storage_hash(result),
                    timestamp,
                ),
            )
            if _failure_injector is not None:
                _failure_injector("before_commit")
            connection.commit()
            return result
        except sqlite3.OperationalError as exc:
            if connection.in_transaction:
                connection.rollback()
            self._raise_if_busy(exc)
            raise
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            if transaction is not None:
                transaction._close()
            connection.close()

    def replay_run(
        self,
        run_id: str,
        *,
        from_state_version: int = 0,
    ) -> SnapshotCheckpoint:
        checkpoint = self.get_checkpoint(run_id, state_version=from_state_version)
        events = self.list_events(run_id, after_sequence=checkpoint.last_sequence)
        return replay_events(checkpoint, events)

    def restore_run(
        self,
        run_id: str,
        *,
        from_state_version: int | None = None,
    ) -> RestoredRunState:
        """Restore operational and lifecycle data without making history authoritative."""

        checkpoint = self.get_checkpoint(run_id, state_version=from_state_version)
        events = (
            self.list_events(run_id, after_sequence=checkpoint.last_sequence)
            if from_state_version is not None
            else ()
        )
        return RestoredRunState(
            checkpoint=checkpoint,
            events=events,
            lifecycle=self.export_lifecycle(run_id),
        )

    def save_workflow(self, record: WorkflowRecord) -> WorkflowRecord:
        connection = self._connect()
        try:
            self._begin_immediate(connection)
            _require_run(connection, record.run_id)
            payload = _model_json(record)
            connection.execute(
                """
                INSERT INTO workflows(
                    run_id, workflow_id, schema_version, status, payload_json,
                    record_hash, updated_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id, workflow_id) DO UPDATE SET
                    schema_version = excluded.schema_version,
                    status = excluded.status,
                    payload_json = excluded.payload_json,
                    record_hash = excluded.record_hash,
                    updated_at_utc = excluded.updated_at_utc
                """,
                (
                    record.run_id,
                    record.workflow_id,
                    record.schema_version,
                    record.status.value,
                    payload,
                    _storage_hash(record),
                    record.updated_at_utc,
                ),
            )
            connection.commit()
            return record
        except sqlite3.OperationalError as exc:
            if connection.in_transaction:
                connection.rollback()
            self._raise_if_busy(exc)
            raise
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    def ensure_running_workflow(
        self,
        run_id: str,
        workflow_id: str,
        *,
        payload: Mapping[str, JsonValue] | None = None,
        updated_at_utc: str | None = None,
    ) -> WorkflowRecord:
        """Atomically create RUNNING or return an existing RUNNING workflow.

        Existing workflow content is never updated. Non-running state is a
        conflict, so an ensure call cannot revive or reset lifecycle authority.
        """

        connection = self._connect()
        try:
            self._begin_immediate(connection)
            _require_run(connection, run_id)
            row = connection.execute(
                "SELECT * FROM workflows WHERE run_id = ? AND workflow_id = ?",
                (run_id, workflow_id),
            ).fetchone()
            if row is not None:
                existing = _decode_workflow_row(row)
                if existing.status is not WorkflowStatus.RUNNING:
                    raise RecordConflictError(
                        "workflow",
                        workflow_id,
                        f"expected status 'running', found {existing.status.value!r}",
                    )
                connection.commit()
                return existing

            _foreign_run(
                connection,
                table="workflows",
                id_column="workflow_id",
                record_id=workflow_id,
                expected_run_id=run_id,
            )
            record = WorkflowRecord(
                run_id=run_id,
                workflow_id=workflow_id,
                status=WorkflowStatus.RUNNING,
                payload=dict(payload or {}),
                updated_at_utc=updated_at_utc,
            )
            connection.execute(
                """
                INSERT INTO workflows(
                    run_id, workflow_id, schema_version, status, payload_json,
                    record_hash, updated_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.run_id,
                    record.workflow_id,
                    record.schema_version,
                    record.status.value,
                    _model_json(record),
                    _storage_hash(record),
                    record.updated_at_utc,
                ),
            )
            connection.commit()
            return record
        except sqlite3.OperationalError as exc:
            if connection.in_transaction:
                connection.rollback()
            self._raise_if_busy(exc)
            raise
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    def transition_workflow(
        self,
        run_id: str,
        workflow_id: str,
        *,
        expected_status: WorkflowStatus,
        resulting_status: WorkflowStatus,
        payload_updates: Mapping[str, JsonValue] | None = None,
        updated_at_utc: str | None = None,
    ) -> WorkflowRecord:
        """Atomically compare-and-set non-operational workflow lifecycle state.

        Repeating an already completed transition with identical payload updates is
        idempotent. A conflicting status or changed terminal payload is rejected.
        """

        connection = self._connect()
        try:
            self._begin_immediate(connection)
            record = _transition_workflow(
                connection,
                run_id,
                workflow_id,
                expected_status=expected_status,
                resulting_status=resulting_status,
                payload_updates=payload_updates,
                updated_at_utc=updated_at_utc,
                allow_already_resulting=True,
            )
            connection.commit()
            return record
        except sqlite3.OperationalError as exc:
            if connection.in_transaction:
                connection.rollback()
            self._raise_if_busy(exc)
            raise
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    def get_workflow(self, run_id: str, workflow_id: str) -> WorkflowRecord:
        connection = self._connect()
        try:
            return _load_workflow(connection, run_id, workflow_id)
        finally:
            connection.close()

    def save_proposal(self, proposal: CandidateProposal) -> CandidateProposal:
        connection = self._connect()
        try:
            self._begin_immediate(connection)
            stored = _save_proposal(connection, proposal)
            connection.commit()
            return stored
        except sqlite3.OperationalError as exc:
            if connection.in_transaction:
                connection.rollback()
            self._raise_if_busy(exc)
            raise
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    def save_proposal_and_transition_workflow(
        self,
        proposal: CandidateProposal,
        *,
        expected_workflow_status: WorkflowStatus,
        resulting_workflow_status: WorkflowStatus,
        workflow_payload_updates: Mapping[str, JsonValue] | None = None,
        updated_at_utc: str | None = None,
    ) -> CandidateProposal:
        """Persist a proposal and its checked lifecycle transition atomically.

        Exact proposal retries and an already-resulting identical transition are
        idempotent. A changed proposal binding or an unexpected workflow status
        rolls back both lifecycle writes.
        """

        connection = self._connect()
        try:
            self._begin_immediate(connection)
            stored = _save_proposal(connection, proposal)
            _transition_workflow(
                connection,
                proposal.run_id,
                proposal.workflow_id,
                expected_status=expected_workflow_status,
                resulting_status=resulting_workflow_status,
                payload_updates=workflow_payload_updates,
                updated_at_utc=updated_at_utc,
                allow_already_resulting=True,
            )
            connection.commit()
            return stored
        except sqlite3.OperationalError as exc:
            if connection.in_transaction:
                connection.rollback()
            self._raise_if_busy(exc)
            raise
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    def get_proposal(self, run_id: str, proposal_id: str) -> CandidateProposal:
        connection = self._connect()
        try:
            return _load_proposal(connection, run_id, proposal_id)
        finally:
            connection.close()

    def save_approval(self, approval: ApprovalRecord) -> ApprovalRecord:
        connection = self._connect()
        try:
            self._begin_immediate(connection)
            proposal = _load_proposal(connection, approval.run_id, approval.proposal_id)
            if (
                approval.proposal_hash != proposal.proposal_hash
                or approval.workflow_id != proposal.workflow_id
                or approval.base_state_version != proposal.base_state_version
            ):
                raise RecordConflictError(
                    "approval", approval.approval_id, "binding does not match proposal"
                )
            existing = connection.execute(
                "SELECT * FROM approvals WHERE run_id = ? AND approval_id = ?",
                (approval.run_id, approval.approval_id),
            ).fetchone()
            if existing is not None:
                stored = _decode_approval_row(existing)
                if canonical_hash(stored) != canonical_hash(approval):
                    raise RecordConflictError("approval", approval.approval_id)
                connection.commit()
                return stored
            try:
                connection.execute(
                    """
                    INSERT INTO approvals(
                        run_id, approval_id, schema_version, proposal_id, proposal_hash,
                        base_state_version, decision, approval_json,
                        record_hash, decided_at_utc
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        approval.run_id,
                        approval.approval_id,
                        approval.schema_version,
                        approval.proposal_id,
                        approval.proposal_hash,
                        approval.base_state_version,
                        approval.decision.value,
                        _model_json(approval),
                        _storage_hash(approval),
                        approval.decided_at_utc,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise RecordConflictError("approval", approval.approval_id, str(exc)) from exc
            connection.commit()
            return approval
        except sqlite3.OperationalError as exc:
            if connection.in_transaction:
                connection.rollback()
            self._raise_if_busy(exc)
            raise
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    def get_approval(self, run_id: str, approval_id: str) -> ApprovalRecord:
        connection = self._connect()
        try:
            return _load_approval(connection, run_id, approval_id)
        finally:
            connection.close()

    def append_audit(
        self,
        record: AuditRecord,
        *,
        workflow_id: str | None = None,
    ) -> int:
        connection = self._connect()
        try:
            self._begin_immediate(connection)
            checkpoint = _load_checkpoint(connection, record.run_id)
            sequence = _insert_audit(
                connection,
                record,
                workflow_id=workflow_id,
                observed_state_version=checkpoint.snapshot.state_version,
            )
            connection.commit()
            return sequence
        except sqlite3.OperationalError as exc:
            if connection.in_transaction:
                connection.rollback()
            self._raise_if_busy(exc)
            raise
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    def export_lifecycle(self, run_id: str) -> LifecycleExport:
        connection = self._connect()
        try:
            _require_run(connection, run_id)
            proposals = tuple(
                _decode_proposal_row(row)
                for row in connection.execute(
                    "SELECT * FROM proposals WHERE run_id = ? ORDER BY proposal_id",
                    (run_id,),
                )
            )
            approvals = tuple(
                _decode_approval_row(row)
                for row in connection.execute(
                    "SELECT * FROM approvals WHERE run_id = ? ORDER BY approval_id",
                    (run_id,),
                )
            )
            workflows = tuple(
                _decode_workflow_row(row)
                for row in connection.execute(
                    "SELECT * FROM workflows WHERE run_id = ? ORDER BY workflow_id",
                    (run_id,),
                )
            )
            audits = tuple(
                _decode_audit_row(row)
                for row in connection.execute(
                    "SELECT * FROM audit_records WHERE run_id = ? ORDER BY audit_sequence",
                    (run_id,),
                )
            )
            consumptions = tuple(
                _decode_approval_consumption_row(row)
                for row in connection.execute(
                    """
                    SELECT
                        c.run_id,
                        c.approval_id,
                        c.proposal_id,
                        c.mutation_id,
                        c.resulting_state_version,
                        c.consumed_at_utc,
                        a.run_id AS bound_approval_run_id,
                        a.approval_id AS bound_approval_id,
                        a.proposal_id AS bound_proposal_id,
                        g.run_id AS bound_group_run_id,
                        g.mutation_id AS bound_mutation_id,
                        g.resulting_state_version AS bound_resulting_state_version
                    FROM approval_consumptions AS c
                    LEFT JOIN approvals AS a
                      ON a.run_id = c.run_id
                     AND a.approval_id = c.approval_id
                     AND a.proposal_id = c.proposal_id
                    LEFT JOIN event_groups AS g
                      ON g.run_id = c.run_id
                     AND g.mutation_id = c.mutation_id
                     AND g.resulting_state_version = c.resulting_state_version
                    WHERE c.run_id = ?
                    ORDER BY c.resulting_state_version
                    """,
                    (run_id,),
                )
            )
            try:
                return LifecycleExport(
                    run_id=run_id,
                    proposals=proposals,
                    approvals=approvals,
                    workflows=workflows,
                    audits=audits,
                    approval_consumptions=consumptions,
                )
            except (ValidationError, TypeError, ValueError) as exc:
                raise RecordConflictError(
                    "lifecycle",
                    run_id,
                    "stored lifecycle references are inconsistent",
                ) from exc
        finally:
            connection.close()


_EVENT_SELECT = """
    SELECT
        e.run_id,
        e.sequence,
        e.event_id,
        e.mutation_id,
        e.event_index,
        e.schema_version,
        e.simulated_time_seconds,
        e.event_priority,
        e.event_type,
        e.envelope_json,
        e.envelope_hash,
        e.recorded_at_utc,
        g.run_id AS group_run_id,
        g.mutation_id AS group_mutation_id,
        g.base_state_version AS group_base_state_version,
        g.resulting_state_version AS group_resulting_state_version,
        g.first_sequence AS group_first_sequence,
        g.event_count AS group_event_count,
        g.command_hash AS group_command_hash
    FROM operational_events AS e
    LEFT JOIN event_groups AS g
      ON g.run_id = e.run_id AND g.mutation_id = e.mutation_id
"""


def _decode_event_row(row: sqlite3.Row) -> EventEnvelope:
    record_id = str(row["event_id"])
    envelope = _parse_stored_model(
        EventEnvelope,
        row["envelope_json"],
        record_type="event",
        record_id=record_id,
    )
    event = envelope.event
    projections = {
        "run_id": (row["run_id"], event.run_id),
        "sequence": (row["sequence"], event.sequence),
        "event_id": (row["event_id"], event.event_id),
        "mutation_id": (row["mutation_id"], envelope.mutation_id),
        "event_index": (row["event_index"], envelope.event_index),
        "schema_version": (row["schema_version"], event.schema_version),
        "simulated_time_seconds": (
            row["simulated_time_seconds"],
            event.simulated_time_seconds,
        ),
        "event_priority": (
            row["event_priority"],
            EVENT_TYPE_PRIORITY[event.event_type],
        ),
        "event_type": (row["event_type"], event.event_type.value),
        "recorded_at_utc": (row["recorded_at_utc"], event.recorded_at_utc),
        "group.run_id": (row["group_run_id"], event.run_id),
        "group.mutation_id": (row["group_mutation_id"], envelope.mutation_id),
        "group.base_state_version": (
            row["group_base_state_version"],
            envelope.base_state_version,
        ),
        "group.resulting_state_version": (
            row["group_resulting_state_version"],
            envelope.resulting_state_version,
        ),
        "group.first_sequence": (
            row["group_first_sequence"],
            event.sequence - envelope.event_index,
        ),
        "group.event_count": (row["group_event_count"], envelope.event_count),
    }
    for field_name, (stored, expected) in projections.items():
        _require_projection("event", record_id, field_name, stored, expected)
    if envelope.schema_version != row["schema_version"]:
        raise RecordConflictError(
            "event",
            record_id,
            "envelope schema_version does not match relational projection",
        )
    if _storage_hash(envelope) != row["envelope_hash"]:
        raise RecordConflictError("event", record_id, "stored envelope hash mismatch")
    return envelope


def _load_events_after(
    connection: sqlite3.Connection,
    run_id: str,
    after_sequence: int,
) -> tuple[EventEnvelope, ...]:
    rows = connection.execute(
        _EVENT_SELECT
        + """
        WHERE e.run_id = ? AND e.sequence > ?
        ORDER BY e.sequence
        """,
        (run_id, after_sequence),
    ).fetchall()
    return tuple(_decode_event_row(row) for row in rows)


def _load_events_for_group(
    connection: sqlite3.Connection,
    run_id: str,
    mutation_id: str,
) -> tuple[EventEnvelope, ...]:
    rows = connection.execute(
        _EVENT_SELECT
        + """
        WHERE e.run_id = ? AND e.mutation_id = ?
        ORDER BY e.sequence
        """,
        (run_id, mutation_id),
    ).fetchall()
    return tuple(_decode_event_row(row) for row in rows)


def _load_event_at_sequence(
    connection: sqlite3.Connection,
    run_id: str,
    sequence: int,
) -> EventEnvelope:
    row = connection.execute(
        _EVENT_SELECT
        + """
        WHERE e.run_id = ? AND e.sequence = ?
        """,
        (run_id, sequence),
    ).fetchone()
    if row is None:
        raise RecordConflictError("event", f"{run_id}@{sequence}", "not found")
    return _decode_event_row(row)


def _decode_idempotency_result(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
    *,
    requested_run_id: str,
    requested_key: str,
    request_hash: str,
) -> MutationResult:
    record_id = f"{requested_run_id}/{requested_key}"
    result = _parse_stored_model(
        MutationResult,
        row["result_json"],
        record_type="idempotency",
        record_id=record_id,
    )
    if result.replayed_idempotent:
        raise RecordConflictError(
            "idempotency",
            record_id,
            "stored result must be the original non-replayed outcome",
        )
    if _storage_hash(result) != row["result_hash"]:
        raise RecordConflictError(
            "idempotency",
            record_id,
            "stored result hash mismatch",
        )
    projections = {
        "run_id": (row["run_id"], requested_run_id),
        "idempotency_key": (row["idempotency_key"], requested_key),
        "command_hash": (row["command_hash"], request_hash),
        "result.run_id": (result.run_id, requested_run_id),
        "result.idempotency_key": (result.idempotency_key, requested_key),
        "result.mutation_id": (result.mutation_id, row["mutation_id"]),
        "result.command_hash": (result.command_hash, request_hash),
        "result.previous_state_version": (
            result.previous_state_version,
            row["expected_state_version"],
        ),
        "result.resulting_state_version": (
            result.resulting_state_version,
            row["resulting_state_version"],
        ),
    }
    for field_name, (stored, expected) in projections.items():
        _require_projection("idempotency", record_id, field_name, stored, expected)

    group = connection.execute(
        """
        SELECT run_id, mutation_id, base_state_version, resulting_state_version,
               first_sequence, event_count, command_hash
        FROM event_groups
        WHERE run_id = ? AND mutation_id = ?
        """,
        (requested_run_id, row["mutation_id"]),
    ).fetchone()
    if group is None:
        raise RecordConflictError(
            "idempotency",
            record_id,
            "referenced event group is missing",
        )
    group_projections = {
        "group.run_id": (group["run_id"], requested_run_id),
        "group.mutation_id": (group["mutation_id"], result.mutation_id),
        "group.base_state_version": (
            group["base_state_version"],
            result.previous_state_version,
        ),
        "group.resulting_state_version": (
            group["resulting_state_version"],
            result.resulting_state_version,
        ),
        "group.first_sequence": (
            group["first_sequence"],
            result.events[0].event.sequence,
        ),
        "group.event_count": (group["event_count"], len(result.events)),
        "group.command_hash": (group["command_hash"], request_hash),
    }
    for field_name, (stored, expected) in group_projections.items():
        _require_projection("idempotency", record_id, field_name, stored, expected)

    authoritative_events = _load_events_for_group(
        connection,
        requested_run_id,
        result.mutation_id,
    )
    if authoritative_events != result.events:
        raise RecordConflictError(
            "idempotency",
            record_id,
            "stored result events do not match the authoritative event group",
        )
    authoritative_checkpoint = _load_checkpoint(
        connection,
        requested_run_id,
        state_version=result.resulting_state_version,
    )
    if authoritative_checkpoint != result.checkpoint:
        raise RecordConflictError(
            "idempotency",
            record_id,
            "stored result checkpoint does not match the authoritative checkpoint",
        )
    return result


def _require_run(connection: sqlite3.Connection, run_id: str) -> sqlite3.Row:
    row = connection.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
    if row is None:
        raise RunNotFoundError(run_id)
    return row


def _load_checkpoint(
    connection: sqlite3.Connection,
    run_id: str,
    *,
    state_version: int | None = None,
) -> SnapshotCheckpoint:
    run = _require_run(connection, run_id)
    version = run["current_state_version"] if state_version is None else state_version
    row = connection.execute(
        """
        SELECT * FROM snapshot_checkpoints
        WHERE run_id = ? AND state_version = ?
        """,
        (run_id, version),
    ).fetchone()
    if row is None:
        raise RecordConflictError("checkpoint", f"{run_id}@{version}", "not found")
    record_id = f"{run_id}@{version}"
    snapshot = _parse_stored_model(
        OperationalSnapshot,
        row["snapshot_json"],
        record_type="checkpoint",
        record_id=record_id,
    )
    if snapshot.run_id != run_id or snapshot.state_version != version:
        raise RecordConflictError("checkpoint", record_id, "identity mismatch")
    if snapshot.plan_revision != row["plan_revision"]:
        raise RecordConflictError("checkpoint", record_id, "revision mismatch")
    _require_projection(
        "checkpoint",
        record_id,
        "schema_version",
        row["schema_version"],
        snapshot.schema_version,
    )
    relational_event_ids = tuple(
        item["event_id"]
        for item in connection.execute(
            """
            SELECT event_id FROM operational_events
            WHERE run_id = ? AND sequence <= ? ORDER BY sequence
            """,
            (run_id, row["last_sequence"]),
        )
    )
    relational_mutation_ids = tuple(
        item["mutation_id"]
        for item in connection.execute(
            """
            SELECT mutation_id FROM event_groups
            WHERE run_id = ? AND resulting_state_version <= ?
            ORDER BY resulting_state_version
            """,
            (run_id, version),
        )
    )
    try:
        stored_event_ids = tuple(json.loads(row["seen_event_ids_json"]))
        stored_mutation_ids = tuple(json.loads(row["seen_mutation_ids_json"]))
    except (json.JSONDecodeError, TypeError) as exc:
        raise RecordConflictError(
            "checkpoint",
            record_id,
            "stored identity history JSON is invalid",
        ) from exc
    if stored_event_ids != relational_event_ids:
        raise RecordConflictError("checkpoint", record_id, "event identity history mismatch")
    if stored_mutation_ids != relational_mutation_ids:
        raise RecordConflictError("checkpoint", record_id, "mutation identity history mismatch")
    try:
        checkpoint = SnapshotCheckpoint(
            snapshot=snapshot,
            last_sequence=row["last_sequence"],
            snapshot_hash=row["snapshot_hash"],
            last_event_simulated_time_seconds=row["last_event_simulated_time_seconds"],
            last_event_priority=row["last_event_priority"],
            seen_event_ids=stored_event_ids,
            seen_mutation_ids=stored_mutation_ids,
        )
    except (ValidationError, TypeError, ValueError) as exc:
        raise RecordConflictError(
            "checkpoint",
            record_id,
            "stored checkpoint metadata is invalid",
        ) from exc
    if checkpoint.last_sequence > 0:
        last_envelope = _load_event_at_sequence(
            connection,
            run_id,
            checkpoint.last_sequence,
        )
        last_event = last_envelope.event
        expected_priority = EVENT_TYPE_PRIORITY[last_event.event_type]
        if (
            checkpoint.last_event_simulated_time_seconds != last_event.simulated_time_seconds
            or checkpoint.last_event_priority != expected_priority
        ):
            raise RecordConflictError(
                "checkpoint",
                record_id,
                "last-event ordering metadata does not match the authoritative event",
            )
        if (
            last_envelope.resulting_state_version != version
            or last_envelope.event_index != last_envelope.event_count - 1
        ):
            raise RecordConflictError(
                "checkpoint",
                record_id,
                "last_sequence is not an event-group boundary for this state version",
            )
    return checkpoint


def _insert_checkpoint(
    connection: sqlite3.Connection,
    checkpoint: SnapshotCheckpoint,
    created_at_utc: str,
) -> None:
    snapshot = checkpoint.snapshot
    connection.execute(
        """
        INSERT INTO snapshot_checkpoints(
            run_id, schema_version, state_version, plan_revision, last_sequence,
            last_event_simulated_time_seconds, last_event_priority,
            seen_event_ids_json, seen_mutation_ids_json,
            snapshot_json, snapshot_hash, created_at_utc
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            snapshot.run_id,
            checkpoint.schema_version,
            snapshot.state_version,
            snapshot.plan_revision,
            checkpoint.last_sequence,
            checkpoint.last_event_simulated_time_seconds,
            checkpoint.last_event_priority,
            json.dumps(checkpoint.seen_event_ids, separators=(",", ":")),
            json.dumps(checkpoint.seen_mutation_ids, separators=(",", ":")),
            _model_json(snapshot),
            checkpoint.snapshot_hash,
            created_at_utc,
        ),
    )


def _foreign_run(
    connection: sqlite3.Connection,
    *,
    table: str,
    id_column: str,
    record_id: str,
    expected_run_id: str,
) -> None:
    allowed = {
        ("workflows", "workflow_id"),
        ("proposals", "proposal_id"),
        ("approvals", "approval_id"),
    }
    if (table, id_column) not in allowed:
        raise AssertionError("unapproved cross-run lookup")
    row = connection.execute(
        f"SELECT run_id FROM {table} WHERE {id_column} = ? LIMIT 1",  # noqa: S608
        (record_id,),
    ).fetchone()
    if row is not None and row["run_id"] != expected_run_id:
        raise CrossRunReferenceError(
            table.removesuffix("s"), record_id, expected_run_id, row["run_id"]
        )


def _decode_workflow_row(row: sqlite3.Row) -> WorkflowRecord:
    record_id = str(row["workflow_id"])
    record = _parse_stored_model(
        WorkflowRecord,
        row["payload_json"],
        record_type="workflow",
        record_id=record_id,
    )
    projections = {
        "run_id": (row["run_id"], record.run_id),
        "workflow_id": (row["workflow_id"], record.workflow_id),
        "schema_version": (row["schema_version"], record.schema_version),
        "status": (row["status"], record.status.value),
        "updated_at_utc": (row["updated_at_utc"], record.updated_at_utc),
    }
    for field_name, (stored, expected) in projections.items():
        _require_projection("workflow", record_id, field_name, stored, expected)
    if _storage_hash(record) != row["record_hash"]:
        raise RecordConflictError("workflow", record_id, "stored record hash mismatch")
    return record


def _load_workflow(connection: sqlite3.Connection, run_id: str, workflow_id: str) -> WorkflowRecord:
    _require_run(connection, run_id)
    row = connection.execute(
        "SELECT * FROM workflows WHERE run_id = ? AND workflow_id = ?",
        (run_id, workflow_id),
    ).fetchone()
    if row is None:
        _foreign_run(
            connection,
            table="workflows",
            id_column="workflow_id",
            record_id=workflow_id,
            expected_run_id=run_id,
        )
        raise RecordConflictError("workflow", workflow_id, "not found in run")
    return _decode_workflow_row(row)


def _transition_workflow(
    connection: sqlite3.Connection,
    run_id: str,
    workflow_id: str,
    *,
    expected_status: WorkflowStatus,
    resulting_status: WorkflowStatus,
    payload_updates: Mapping[str, JsonValue] | None,
    updated_at_utc: str | None,
    allow_already_resulting: bool,
) -> WorkflowRecord:
    current = _load_workflow(connection, run_id, workflow_id)
    updates = dict(payload_updates or {})
    if allow_already_resulting and current.status is resulting_status:
        if all(current.payload.get(key) == value for key, value in updates.items()):
            return current
        raise RecordConflictError(
            "workflow",
            workflow_id,
            "terminal workflow payload conflicts with the requested transition",
        )
    if current.status is not expected_status:
        raise RecordConflictError(
            "workflow",
            workflow_id,
            f"expected status {expected_status.value!r}, found {current.status.value!r}",
        )
    payload = dict(current.payload)
    payload.update(updates)
    transitioned = WorkflowRecord(
        run_id=run_id,
        workflow_id=workflow_id,
        status=resulting_status,
        payload=payload,
        updated_at_utc=updated_at_utc or _utc_now(),
    )
    updated = connection.execute(
        """
        UPDATE workflows
        SET schema_version = ?, status = ?, payload_json = ?, record_hash = ?,
            updated_at_utc = ?
        WHERE run_id = ? AND workflow_id = ? AND status = ?
        """,
        (
            transitioned.schema_version,
            transitioned.status.value,
            _model_json(transitioned),
            _storage_hash(transitioned),
            transitioned.updated_at_utc,
            run_id,
            workflow_id,
            expected_status.value,
        ),
    )
    if updated.rowcount != 1:
        raise RecordConflictError(
            "workflow",
            workflow_id,
            "workflow changed during status transition",
        )
    return transitioned


def _decode_proposal_row(row: sqlite3.Row) -> CandidateProposal:
    record_id = str(row["proposal_id"])
    record = _parse_stored_model(
        CandidateProposal,
        row["proposal_json"],
        record_type="proposal",
        record_id=record_id,
    )
    projections = {
        "run_id": (row["run_id"], record.run_id),
        "proposal_id": (row["proposal_id"], record.proposal_id),
        "schema_version": (row["schema_version"], record.schema_version),
        "workflow_id": (row["workflow_id"], record.workflow_id),
        "base_state_version": (row["base_state_version"], record.base_state_version),
        "base_snapshot_hash": (row["base_snapshot_hash"], record.base_snapshot_hash),
        "proposal_hash": (row["proposal_hash"], record.proposal_hash),
        "created_at_utc": (row["created_at_utc"], record.created_at_utc),
    }
    for field_name, (stored, expected) in projections.items():
        _require_projection("proposal", record_id, field_name, stored, expected)
    if _storage_hash(record) != row["record_hash"]:
        raise RecordConflictError("proposal", record_id, "stored record hash mismatch")
    return record


def _save_proposal(
    connection: sqlite3.Connection,
    proposal: CandidateProposal,
) -> CandidateProposal:
    """Insert immutable proposal content using the caller's open transaction."""

    _require_run(connection, proposal.run_id)
    _load_workflow(connection, proposal.run_id, proposal.workflow_id)
    checkpoint = _load_checkpoint(
        connection,
        proposal.run_id,
        state_version=proposal.base_state_version,
    )
    if checkpoint.snapshot_hash != proposal.base_snapshot_hash:
        raise RecordConflictError(
            "proposal",
            proposal.proposal_id,
            "base snapshot hash does not exist in run",
        )
    existing = connection.execute(
        "SELECT * FROM proposals WHERE run_id = ? AND proposal_id = ?",
        (proposal.run_id, proposal.proposal_id),
    ).fetchone()
    if existing is not None:
        stored = _decode_proposal_row(existing)
        if canonical_hash(stored) != canonical_hash(proposal):
            raise RecordConflictError("proposal", proposal.proposal_id)
        return stored
    try:
        connection.execute(
            """
            INSERT INTO proposals(
                run_id, proposal_id, schema_version, workflow_id,
                base_state_version, base_snapshot_hash, proposal_hash,
                proposal_json, record_hash, created_at_utc
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                proposal.run_id,
                proposal.proposal_id,
                proposal.schema_version,
                proposal.workflow_id,
                proposal.base_state_version,
                proposal.base_snapshot_hash,
                proposal.proposal_hash,
                _model_json(proposal),
                _storage_hash(proposal),
                proposal.created_at_utc,
            ),
        )
    except sqlite3.IntegrityError as exc:
        raise RecordConflictError("proposal", proposal.proposal_id, str(exc)) from exc
    return proposal


def _load_proposal(
    connection: sqlite3.Connection, run_id: str, proposal_id: str
) -> CandidateProposal:
    _require_run(connection, run_id)
    row = connection.execute(
        "SELECT * FROM proposals WHERE run_id = ? AND proposal_id = ?",
        (run_id, proposal_id),
    ).fetchone()
    if row is None:
        _foreign_run(
            connection,
            table="proposals",
            id_column="proposal_id",
            record_id=proposal_id,
            expected_run_id=run_id,
        )
        raise RecordConflictError("proposal", proposal_id, "not found in run")
    return _decode_proposal_row(row)


def _decode_approval_row(row: sqlite3.Row) -> ApprovalRecord:
    record_id = str(row["approval_id"])
    record = _parse_stored_model(
        ApprovalRecord,
        row["approval_json"],
        record_type="approval",
        record_id=record_id,
    )
    projections = {
        "run_id": (row["run_id"], record.run_id),
        "approval_id": (row["approval_id"], record.approval_id),
        "schema_version": (row["schema_version"], record.schema_version),
        "proposal_id": (row["proposal_id"], record.proposal_id),
        "proposal_hash": (row["proposal_hash"], record.proposal_hash),
        "base_state_version": (row["base_state_version"], record.base_state_version),
        "decision": (row["decision"], record.decision.value),
        "decided_at_utc": (row["decided_at_utc"], record.decided_at_utc),
    }
    for field_name, (stored, expected) in projections.items():
        _require_projection("approval", record_id, field_name, stored, expected)
    if _storage_hash(record) != row["record_hash"]:
        raise RecordConflictError("approval", record_id, "stored record hash mismatch")
    return record


def _load_approval(connection: sqlite3.Connection, run_id: str, approval_id: str) -> ApprovalRecord:
    _require_run(connection, run_id)
    row = connection.execute(
        "SELECT * FROM approvals WHERE run_id = ? AND approval_id = ?",
        (run_id, approval_id),
    ).fetchone()
    if row is None:
        _foreign_run(
            connection,
            table="approvals",
            id_column="approval_id",
            record_id=approval_id,
            expected_run_id=run_id,
        )
        raise RecordConflictError("approval", approval_id, "not found in run")
    return _decode_approval_row(row)


def _decode_approval_consumption_row(row: sqlite3.Row) -> ApprovalConsumptionRecord:
    record_id = f"{row['run_id']}/{row['approval_id']}"
    try:
        record = ApprovalConsumptionRecord(
            run_id=row["run_id"],
            approval_id=row["approval_id"],
            proposal_id=row["proposal_id"],
            mutation_id=row["mutation_id"],
            resulting_state_version=row["resulting_state_version"],
            consumed_at_utc=row["consumed_at_utc"],
        )
    except (ValidationError, TypeError, ValueError) as exc:
        raise RecordConflictError(
            "approval_consumption",
            record_id,
            "stored relational record is invalid",
        ) from exc
    projections = {
        "approval.run_id": (row["bound_approval_run_id"], record.run_id),
        "approval.approval_id": (row["bound_approval_id"], record.approval_id),
        "approval.proposal_id": (row["bound_proposal_id"], record.proposal_id),
        "event_group.run_id": (row["bound_group_run_id"], record.run_id),
        "event_group.mutation_id": (row["bound_mutation_id"], record.mutation_id),
        "event_group.resulting_state_version": (
            row["bound_resulting_state_version"],
            record.resulting_state_version,
        ),
    }
    for field_name, (stored, expected) in projections.items():
        _require_projection(
            "approval_consumption",
            record_id,
            field_name,
            stored,
            expected,
        )
    return record


def _decode_audit_row(row: sqlite3.Row) -> AuditRecord:
    record_id = str(row["audit_id"])
    record = _parse_stored_model(
        AuditRecord,
        row["record_json"],
        record_type="audit",
        record_id=record_id,
    )
    projections = {
        "run_id": (row["run_id"], record.run_id),
        "audit_id": (row["audit_id"], record.audit_id),
        "schema_version": (row["schema_version"], record.schema_version),
        "category": (row["category"], record.category),
        "action": (row["action"], record.action),
        "actor": (row["actor"], record.actor),
        "recorded_at_utc": (row["recorded_at_utc"], record.recorded_at_utc),
    }
    for field_name, (stored, expected) in projections.items():
        _require_projection("audit", record_id, field_name, stored, expected)
    if _storage_hash(record) != row["record_hash"]:
        raise RecordConflictError("audit", record_id, "stored record hash mismatch")
    return record


def _insert_audit(
    connection: sqlite3.Connection,
    record: AuditRecord,
    *,
    workflow_id: str | None,
    observed_state_version: int,
) -> int:
    _require_run(connection, record.run_id)
    if workflow_id is not None:
        _load_workflow(connection, record.run_id, workflow_id)
    existing = connection.execute(
        "SELECT * FROM audit_records WHERE run_id = ? AND audit_id = ?",
        (record.run_id, record.audit_id),
    ).fetchone()
    if existing is not None:
        stored = _decode_audit_row(existing)
        if stored != record:
            raise RecordConflictError("audit", record.audit_id, "stored content differs")
        if existing["workflow_id"] != workflow_id:
            raise RecordConflictError(
                "audit",
                record.audit_id,
                "stored workflow binding differs",
            )
        if existing["observed_state_version"] != observed_state_version:
            raise RecordConflictError(
                "audit",
                record.audit_id,
                "stored observed state version differs",
            )
        return int(existing["audit_sequence"])
    row = connection.execute(
        "SELECT COALESCE(MAX(audit_sequence), 0) + 1 AS next FROM audit_records WHERE run_id = ?",
        (record.run_id,),
    ).fetchone()
    sequence = int(row["next"])
    connection.execute(
        """
        INSERT INTO audit_records(
            run_id, audit_sequence, audit_id, schema_version, workflow_id,
            observed_state_version, category, action, actor,
            record_json, record_hash, recorded_at_utc
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            record.run_id,
            sequence,
            record.audit_id,
            record.schema_version,
            workflow_id,
            observed_state_version,
            record.category,
            record.action,
            record.actor,
            _model_json(record),
            _storage_hash(record),
            record.recorded_at_utc,
        ),
    )
    return sequence


__all__ = [
    "CURRENT_SCHEMA_VERSION",
    "OperationalTransaction",
    "SQLiteStateRepository",
]
