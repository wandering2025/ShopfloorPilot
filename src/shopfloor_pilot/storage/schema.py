"""Explicit SQLite schema initialization for authoritative ShopfloorPilot state.

The schema is deliberately small and run-scoped.  Operational mutations, their
events, checkpoints, and idempotency outcomes have relational links that prevent
cross-run references.  Lifecycle records are stored separately because reading or
writing them does not itself advance operational state.
"""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from typing import Final

from .errors import SchemaVersionError

CURRENT_SCHEMA_VERSION: Final = 1


@dataclass(frozen=True, slots=True)
class _Migration:
    """One immutable, ordered schema migration."""

    version: int
    name: str
    statements: tuple[str, ...]

    @property
    def checksum(self) -> str:
        payload = "\n-- statement boundary --\n".join(self.statements).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()


_V1_STATEMENTS = (
    """
    CREATE TABLE schema_migrations (
        version INTEGER PRIMARY KEY CHECK (version > 0),
        name TEXT NOT NULL UNIQUE CHECK (length(trim(name)) > 0),
        checksum TEXT NOT NULL CHECK (
            length(checksum) = 64
            AND checksum NOT GLOB '*[^0-9a-f]*'
        ),
        applied_at_utc TEXT NOT NULL CHECK (length(trim(applied_at_utc)) > 0)
    ) STRICT
    """,
    """
    CREATE TABLE runs (
        run_id TEXT PRIMARY KEY CHECK (length(run_id) BETWEEN 1 AND 64),
        schema_version INTEGER NOT NULL CHECK (schema_version = 1),
        current_state_version INTEGER NOT NULL DEFAULT 0 CHECK (current_state_version >= 0),
        initial_snapshot_json TEXT NOT NULL CHECK (
            json_valid(initial_snapshot_json)
            AND json_type(initial_snapshot_json) = 'object'
        ),
        initial_snapshot_hash TEXT NOT NULL CHECK (
            length(initial_snapshot_hash) = 64
            AND initial_snapshot_hash NOT GLOB '*[^0-9a-f]*'
        ),
        reset_from_run_id TEXT CHECK (
            reset_from_run_id IS NULL
            OR (
                length(reset_from_run_id) BETWEEN 1 AND 64
                AND reset_from_run_id <> run_id
            )
        ),
        created_at_utc TEXT NOT NULL CHECK (length(trim(created_at_utc)) > 0),
        FOREIGN KEY (reset_from_run_id) REFERENCES runs(run_id)
            ON UPDATE RESTRICT ON DELETE RESTRICT
    ) STRICT
    """,
    """
    CREATE TABLE snapshot_checkpoints (
        run_id TEXT NOT NULL,
        schema_version INTEGER NOT NULL CHECK (schema_version = 1),
        state_version INTEGER NOT NULL CHECK (state_version >= 0),
        plan_revision INTEGER NOT NULL CHECK (plan_revision >= 0),
        last_sequence INTEGER NOT NULL CHECK (last_sequence >= 0),
        last_event_simulated_time_seconds INTEGER CHECK (
            last_event_simulated_time_seconds >= 0
        ),
        last_event_priority INTEGER CHECK (last_event_priority > 0),
        seen_event_ids_json TEXT NOT NULL DEFAULT '[]' CHECK (
            json_valid(seen_event_ids_json)
            AND json_type(seen_event_ids_json) = 'array'
            AND json_array_length(seen_event_ids_json) = last_sequence
        ),
        seen_mutation_ids_json TEXT NOT NULL DEFAULT '[]' CHECK (
            json_valid(seen_mutation_ids_json)
            AND json_type(seen_mutation_ids_json) = 'array'
            AND json_array_length(seen_mutation_ids_json) = state_version
        ),
        snapshot_json TEXT NOT NULL CHECK (
            json_valid(snapshot_json)
            AND json_type(snapshot_json) = 'object'
        ),
        snapshot_hash TEXT NOT NULL CHECK (
            length(snapshot_hash) = 64
            AND snapshot_hash NOT GLOB '*[^0-9a-f]*'
        ),
        created_at_utc TEXT NOT NULL CHECK (length(trim(created_at_utc)) > 0),
        PRIMARY KEY (run_id, state_version),
        UNIQUE (run_id, last_sequence),
        CHECK (
            (state_version = 0 AND last_sequence = 0)
            OR (state_version > 0 AND last_sequence > 0)
        ),
        CHECK (
            (last_sequence = 0
                AND last_event_simulated_time_seconds IS NULL
                AND last_event_priority IS NULL)
            OR (last_sequence > 0
                AND last_event_simulated_time_seconds IS NOT NULL
                AND last_event_priority IS NOT NULL)
        ),
        FOREIGN KEY (run_id) REFERENCES runs(run_id)
            ON UPDATE RESTRICT ON DELETE CASCADE
    ) STRICT
    """,
    """
    CREATE TABLE event_groups (
        run_id TEXT NOT NULL,
        mutation_id TEXT NOT NULL CHECK (length(mutation_id) BETWEEN 1 AND 64),
        base_state_version INTEGER NOT NULL CHECK (base_state_version >= 0),
        resulting_state_version INTEGER NOT NULL CHECK (
            resulting_state_version = base_state_version + 1
        ),
        first_sequence INTEGER NOT NULL CHECK (first_sequence >= 1),
        event_count INTEGER NOT NULL CHECK (event_count >= 1),
        command_hash TEXT NOT NULL CHECK (
            length(command_hash) = 64
            AND command_hash NOT GLOB '*[^0-9a-f]*'
        ),
        created_at_utc TEXT NOT NULL CHECK (length(trim(created_at_utc)) > 0),
        PRIMARY KEY (run_id, mutation_id),
        UNIQUE (run_id, base_state_version),
        UNIQUE (run_id, resulting_state_version),
        UNIQUE (run_id, first_sequence),
        UNIQUE (run_id, mutation_id, resulting_state_version),
        UNIQUE (run_id, mutation_id, base_state_version, resulting_state_version),
        FOREIGN KEY (run_id) REFERENCES runs(run_id)
            ON UPDATE RESTRICT ON DELETE CASCADE,
        FOREIGN KEY (run_id, resulting_state_version)
            REFERENCES snapshot_checkpoints(run_id, state_version)
            ON UPDATE RESTRICT ON DELETE RESTRICT
            DEFERRABLE INITIALLY DEFERRED
    ) STRICT
    """,
    """
    CREATE TABLE operational_events (
        run_id TEXT NOT NULL,
        sequence INTEGER NOT NULL CHECK (sequence >= 1),
        event_id TEXT NOT NULL UNIQUE CHECK (length(event_id) BETWEEN 1 AND 64),
        mutation_id TEXT NOT NULL CHECK (length(mutation_id) BETWEEN 1 AND 64),
        event_index INTEGER NOT NULL CHECK (event_index >= 0),
        schema_version INTEGER NOT NULL CHECK (schema_version = 1),
        simulated_time_seconds INTEGER NOT NULL CHECK (simulated_time_seconds >= 0),
        event_priority INTEGER NOT NULL,
        event_type TEXT NOT NULL,
        envelope_json TEXT NOT NULL CHECK (
            json_valid(envelope_json)
            AND json_type(envelope_json) = 'object'
        ),
        envelope_hash TEXT NOT NULL CHECK (
            length(envelope_hash) = 64
            AND envelope_hash NOT GLOB '*[^0-9a-f]*'
        ),
        recorded_at_utc TEXT CHECK (
            recorded_at_utc IS NULL OR length(trim(recorded_at_utc)) > 0
        ),
        PRIMARY KEY (run_id, sequence),
        UNIQUE (run_id, mutation_id, event_index),
        CHECK (
            (event_type = 'operation_completed' AND event_priority = 10)
            OR (event_type = 'machine_down' AND event_priority = 20)
            OR (event_type = 'operation_interrupted' AND event_priority = 30)
            OR (event_type = 'machine_restored' AND event_priority = 40)
            OR (event_type = 'order_registered' AND event_priority = 50)
            OR (event_type = 'plan_applied' AND event_priority = 60)
            OR (event_type = 'operation_started' AND event_priority = 70)
            OR (event_type = 'observation_recorded' AND event_priority = 80)
            OR (event_type = 'time_advanced' AND event_priority = 90)
        ),
        FOREIGN KEY (run_id, mutation_id)
            REFERENCES event_groups(run_id, mutation_id)
            ON UPDATE RESTRICT ON DELETE CASCADE
    ) STRICT
    """,
    """
    CREATE TABLE idempotency_records (
        run_id TEXT NOT NULL,
        idempotency_key TEXT NOT NULL CHECK (
            length(idempotency_key) BETWEEN 1 AND 64
            AND substr(idempotency_key, 1, 1) GLOB '[A-Za-z]'
            AND idempotency_key NOT GLOB '*[^A-Za-z0-9_.:-]*'
        ),
        command_hash TEXT NOT NULL CHECK (
            length(command_hash) = 64
            AND command_hash NOT GLOB '*[^0-9a-f]*'
        ),
        expected_state_version INTEGER NOT NULL CHECK (expected_state_version >= 0),
        resulting_state_version INTEGER NOT NULL CHECK (
            resulting_state_version = expected_state_version + 1
        ),
        mutation_id TEXT NOT NULL CHECK (length(mutation_id) BETWEEN 1 AND 64),
        result_json TEXT NOT NULL CHECK (
            json_valid(result_json)
            AND json_type(result_json) = 'object'
        ),
        result_hash TEXT NOT NULL CHECK (
            length(result_hash) = 64
            AND result_hash NOT GLOB '*[^0-9a-f]*'
        ),
        created_at_utc TEXT NOT NULL CHECK (length(trim(created_at_utc)) > 0),
        PRIMARY KEY (run_id, idempotency_key),
        UNIQUE (run_id, mutation_id),
        FOREIGN KEY (
            run_id,
            mutation_id,
            expected_state_version,
            resulting_state_version
        ) REFERENCES event_groups(
            run_id,
            mutation_id,
            base_state_version,
            resulting_state_version
        )
            ON UPDATE RESTRICT ON DELETE CASCADE
    ) STRICT
    """,
    """
    CREATE TABLE workflows (
        run_id TEXT NOT NULL,
        workflow_id TEXT NOT NULL CHECK (length(workflow_id) BETWEEN 1 AND 64),
        schema_version INTEGER NOT NULL CHECK (schema_version = 1),
        status TEXT NOT NULL CHECK (
            status IN ('created', 'running', 'waiting_approval', 'completed', 'failed', 'cancelled')
        ),
        payload_json TEXT NOT NULL CHECK (
            json_valid(payload_json)
            AND json_type(payload_json) = 'object'
        ),
        record_hash TEXT NOT NULL CHECK (
            length(record_hash) = 64
            AND record_hash NOT GLOB '*[^0-9a-f]*'
        ),
        updated_at_utc TEXT CHECK (
            updated_at_utc IS NULL OR length(trim(updated_at_utc)) > 0
        ),
        PRIMARY KEY (run_id, workflow_id),
        FOREIGN KEY (run_id) REFERENCES runs(run_id)
            ON UPDATE RESTRICT ON DELETE CASCADE
    ) STRICT
    """,
    """
    CREATE TABLE proposals (
        run_id TEXT NOT NULL,
        proposal_id TEXT NOT NULL CHECK (length(proposal_id) BETWEEN 1 AND 64),
        schema_version INTEGER NOT NULL CHECK (schema_version = 1),
        workflow_id TEXT NOT NULL CHECK (length(workflow_id) BETWEEN 1 AND 64),
        base_state_version INTEGER NOT NULL CHECK (base_state_version >= 0),
        base_snapshot_hash TEXT NOT NULL CHECK (
            length(base_snapshot_hash) = 64
            AND base_snapshot_hash NOT GLOB '*[^0-9a-f]*'
        ),
        proposal_hash TEXT NOT NULL CHECK (
            length(proposal_hash) = 64
            AND proposal_hash NOT GLOB '*[^0-9a-f]*'
        ),
        proposal_json TEXT NOT NULL CHECK (
            json_valid(proposal_json)
            AND json_type(proposal_json) = 'object'
        ),
        record_hash TEXT NOT NULL CHECK (
            length(record_hash) = 64
            AND record_hash NOT GLOB '*[^0-9a-f]*'
        ),
        created_at_utc TEXT CHECK (
            created_at_utc IS NULL OR length(trim(created_at_utc)) > 0
        ),
        PRIMARY KEY (run_id, proposal_id),
        UNIQUE (run_id, proposal_hash),
        UNIQUE (run_id, proposal_id, proposal_hash),
        FOREIGN KEY (run_id, workflow_id)
            REFERENCES workflows(run_id, workflow_id)
            ON UPDATE RESTRICT ON DELETE RESTRICT
    ) STRICT
    """,
    """
    CREATE TABLE approvals (
        run_id TEXT NOT NULL,
        approval_id TEXT NOT NULL CHECK (length(approval_id) BETWEEN 1 AND 64),
        schema_version INTEGER NOT NULL CHECK (schema_version = 1),
        proposal_id TEXT NOT NULL CHECK (length(proposal_id) BETWEEN 1 AND 64),
        proposal_hash TEXT NOT NULL CHECK (
            length(proposal_hash) = 64
            AND proposal_hash NOT GLOB '*[^0-9a-f]*'
        ),
        base_state_version INTEGER NOT NULL CHECK (base_state_version >= 0),
        decision TEXT NOT NULL CHECK (decision IN ('approved', 'rejected')),
        approval_json TEXT NOT NULL CHECK (
            json_valid(approval_json)
            AND json_type(approval_json) = 'object'
        ),
        record_hash TEXT NOT NULL CHECK (
            length(record_hash) = 64
            AND record_hash NOT GLOB '*[^0-9a-f]*'
        ),
        decided_at_utc TEXT NOT NULL CHECK (length(trim(decided_at_utc)) > 0),
        PRIMARY KEY (run_id, approval_id),
        UNIQUE (run_id, proposal_id),
        UNIQUE (run_id, approval_id, proposal_id),
        FOREIGN KEY (run_id, proposal_id, proposal_hash)
            REFERENCES proposals(run_id, proposal_id, proposal_hash)
            ON UPDATE RESTRICT ON DELETE RESTRICT
    ) STRICT
    """,
    """
    CREATE TABLE approval_consumptions (
        run_id TEXT NOT NULL,
        approval_id TEXT NOT NULL CHECK (length(approval_id) BETWEEN 1 AND 64),
        proposal_id TEXT NOT NULL CHECK (length(proposal_id) BETWEEN 1 AND 64),
        mutation_id TEXT NOT NULL CHECK (length(mutation_id) BETWEEN 1 AND 64),
        resulting_state_version INTEGER NOT NULL CHECK (resulting_state_version >= 1),
        consumed_at_utc TEXT NOT NULL CHECK (length(trim(consumed_at_utc)) > 0),
        PRIMARY KEY (run_id, approval_id),
        UNIQUE (run_id, mutation_id),
        FOREIGN KEY (run_id, approval_id, proposal_id)
            REFERENCES approvals(run_id, approval_id, proposal_id)
            ON UPDATE RESTRICT ON DELETE RESTRICT,
        FOREIGN KEY (run_id, mutation_id, resulting_state_version)
            REFERENCES event_groups(run_id, mutation_id, resulting_state_version)
            ON UPDATE RESTRICT ON DELETE RESTRICT
    ) STRICT
    """,
    """
    CREATE TABLE audit_records (
        run_id TEXT NOT NULL,
        audit_sequence INTEGER NOT NULL CHECK (audit_sequence >= 1),
        audit_id TEXT NOT NULL CHECK (length(audit_id) BETWEEN 1 AND 64),
        schema_version INTEGER NOT NULL CHECK (schema_version = 1),
        workflow_id TEXT CHECK (
            workflow_id IS NULL OR length(workflow_id) BETWEEN 1 AND 64
        ),
        observed_state_version INTEGER NOT NULL CHECK (observed_state_version >= 0),
        category TEXT NOT NULL CHECK (length(category) BETWEEN 1 AND 80),
        action TEXT NOT NULL CHECK (length(action) BETWEEN 1 AND 120),
        actor TEXT NOT NULL CHECK (length(actor) BETWEEN 1 AND 100),
        record_json TEXT NOT NULL CHECK (
            json_valid(record_json)
            AND json_type(record_json) = 'object'
        ),
        record_hash TEXT NOT NULL CHECK (
            length(record_hash) = 64
            AND record_hash NOT GLOB '*[^0-9a-f]*'
        ),
        recorded_at_utc TEXT NOT NULL CHECK (length(trim(recorded_at_utc)) > 0),
        PRIMARY KEY (run_id, audit_sequence),
        UNIQUE (run_id, audit_id),
        FOREIGN KEY (run_id) REFERENCES runs(run_id)
            ON UPDATE RESTRICT ON DELETE CASCADE,
        FOREIGN KEY (run_id, workflow_id)
            REFERENCES workflows(run_id, workflow_id)
            ON UPDATE RESTRICT ON DELETE RESTRICT,
        FOREIGN KEY (run_id, observed_state_version)
            REFERENCES snapshot_checkpoints(run_id, state_version)
            ON UPDATE RESTRICT ON DELETE RESTRICT
            DEFERRABLE INITIALLY DEFERRED
    ) STRICT
    """,
)

_MIGRATIONS: Final = (
    _Migration(version=1, name="authoritative_state_and_events", statements=_V1_STATEMENTS),
)
_KNOWN_SCHEMA_VERSIONS: Final = frozenset({0, *(item.version for item in _MIGRATIONS)})


def _validate_migration_definitions() -> None:
    versions = tuple(item.version for item in _MIGRATIONS)
    expected = tuple(range(1, CURRENT_SCHEMA_VERSION + 1))
    if versions != expected:
        raise RuntimeError(f"migration versions {versions!r} do not match {expected!r}")


_validate_migration_definitions()


def _require_supported_version(version: int) -> int:
    if version < 0:
        raise SchemaVersionError(version, CURRENT_SCHEMA_VERSION)
    if version > CURRENT_SCHEMA_VERSION:
        raise SchemaVersionError(version, CURRENT_SCHEMA_VERSION)
    if version not in _KNOWN_SCHEMA_VERSIONS:
        raise SchemaVersionError(version, CURRENT_SCHEMA_VERSION)
    return version


def read_schema_version(connection: sqlite3.Connection) -> int:
    """Return a supported ``PRAGMA user_version`` value for ``connection``.

    Version zero is the recognized empty-database state.  Negative, newer, and
    otherwise unknown versions are all rejected rather than guessed or downgraded.
    """

    row = connection.execute("PRAGMA user_version").fetchone()
    if row is None or len(row) != 1 or not isinstance(row[0], int):
        raise SchemaVersionError(-1, CURRENT_SCHEMA_VERSION)
    return _require_supported_version(row[0])


def _validate_applied_migrations(connection: sqlite3.Connection, version: int) -> None:
    expected = tuple(
        (item.version, item.name, item.checksum) for item in _MIGRATIONS if item.version <= version
    )
    try:
        actual = tuple(
            tuple(row)
            for row in connection.execute(
                "SELECT version, name, checksum FROM schema_migrations ORDER BY version"
            ).fetchall()
        )
    except sqlite3.DatabaseError as exc:
        raise SchemaVersionError(version, CURRENT_SCHEMA_VERSION) from exc
    if actual != expected:
        raise SchemaVersionError(version, CURRENT_SCHEMA_VERSION)


def initialize_schema(connection: sqlite3.Connection, applied_at_utc: str) -> int:
    """Initialize or validate the database schema and return its current version.

    Migrations run as individual statements inside one ``BEGIN IMMEDIATE``
    transaction.  The migration ledger row and ``PRAGMA user_version`` change are
    therefore committed or rolled back together.  The caller supplies the timestamp;
    this function never reads a wall clock.

    The function expects ownership of the transaction boundary.  Foreign-key
    enforcement is intentionally configured by the repository when opening a
    connection, not by schema initialization.
    """

    if not isinstance(applied_at_utc, str) or not applied_at_utc.strip():
        raise ValueError("applied_at_utc must be a non-empty string")
    if connection.in_transaction:
        raise sqlite3.ProgrammingError(
            "initialize_schema requires a connection without an active transaction"
        )

    current_version = read_schema_version(connection)
    if current_version == CURRENT_SCHEMA_VERSION:
        _validate_applied_migrations(connection, current_version)
        return current_version

    connection.execute("BEGIN IMMEDIATE")
    try:
        # Re-read while holding the write reservation: another initializer may have
        # completed between the optimistic read above and BEGIN IMMEDIATE.
        current_version = read_schema_version(connection)
        if current_version == CURRENT_SCHEMA_VERSION:
            _validate_applied_migrations(connection, current_version)
        else:
            for migration in _MIGRATIONS:
                if migration.version <= current_version:
                    continue
                for statement in migration.statements:
                    connection.execute(statement)
                connection.execute(
                    """
                    INSERT INTO schema_migrations(version, name, checksum, applied_at_utc)
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        migration.version,
                        migration.name,
                        migration.checksum,
                        applied_at_utc,
                    ),
                )
                connection.execute(f"PRAGMA user_version = {migration.version:d}")
                current_version = migration.version
            _validate_applied_migrations(connection, current_version)
        connection.commit()
    except BaseException:
        if connection.in_transaction:
            connection.rollback()
        raise

    return current_version
