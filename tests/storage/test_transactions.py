"""S02-A01 through A04 and generic transaction-boundary acceptance tests."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from shopfloor_pilot.domain.canonical import canonical_hash
from shopfloor_pilot.domain.models import OperationalSnapshot
from shopfloor_pilot.storage import (
    CURRENT_SCHEMA_VERSION,
    AuditRecord,
    DatabaseNotInitializedError,
    IdempotencyConflictError,
    MutationResult,
    SchemaVersionError,
    SQLiteStateRepository,
    TransactionStateError,
    VersionConflictError,
)

FIXED_UTC = "2026-10-20T18:30:00+02:00"


def _table_counts(database_path: Path) -> dict[str, int]:
    tables = (
        "runs",
        "event_groups",
        "operational_events",
        "snapshot_checkpoints",
        "idempotency_records",
        "audit_records",
    )
    with sqlite3.connect(database_path) as connection:
        return {
            table: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in tables
        }


def test_a01_initialization_is_idempotent_and_committed_state_survives_reopen(
    database_path: Path,
    initial_snapshot: OperationalSnapshot,
    apply_time: Callable[..., MutationResult],
) -> None:
    repository = SQLiteStateRepository(database_path)
    with pytest.raises(DatabaseNotInitializedError):
        repository.schema_version()

    assert repository.initialize(applied_at_utc=FIXED_UTC) == CURRENT_SCHEMA_VERSION
    migrations = repository.applied_migrations()
    assert len(migrations) == CURRENT_SCHEMA_VERSION == 1
    assert repository.initialize(applied_at_utc="2026-10-20T18:31:00+02:00") == 1
    assert repository.applied_migrations() == migrations

    repository.create_run(initial_snapshot, created_at_utc=FIXED_UTC)
    result = apply_time(
        repository,
        run_id=initial_snapshot.run_id,
        expected_state_version=0,
        target_time_seconds=10,
        idempotency_key="mutation-a01-persist",
    )
    expected_hash = canonical_hash(result.checkpoint.snapshot)

    reopened = SQLiteStateRepository(database_path)
    assert reopened.initialize(applied_at_utc="2026-10-20T18:32:00+02:00") == 1
    assert reopened.applied_migrations() == migrations
    assert canonical_hash(reopened.get_snapshot(initial_snapshot.run_id)) == expected_hash
    assert reopened.get_checkpoint(initial_snapshot.run_id) == result.checkpoint
    assert reopened.list_events(initial_snapshot.run_id) == result.events
    assert len(reopened.list_checkpoints(initial_snapshot.run_id)) == 2
    assert reopened.integrity_check() == "ok"


def test_a01_unknown_newer_schema_version_fails_explicitly(database_path: Path) -> None:
    with sqlite3.connect(database_path) as connection:
        connection.execute(f"PRAGMA user_version = {CURRENT_SCHEMA_VERSION + 1}")

    repository = SQLiteStateRepository(database_path)
    with pytest.raises(SchemaVersionError) as raised:
        repository.initialize(applied_at_utc=FIXED_UTC)
    assert raised.value.found == CURRENT_SCHEMA_VERSION + 1
    assert raised.value.supported == CURRENT_SCHEMA_VERSION


def test_a02_matching_version_commits_once_and_stale_version_has_no_side_effects(
    repository: SQLiteStateRepository,
    initial_snapshot: OperationalSnapshot,
    apply_time: Callable[..., MutationResult],
) -> None:
    run_id = initial_snapshot.run_id
    before = repository.get_checkpoint(run_id)
    result = apply_time(
        repository,
        run_id=run_id,
        expected_state_version=0,
        target_time_seconds=10,
        idempotency_key="mutation-a02-first",
    )

    assert result.previous_state_version == 0
    assert result.resulting_state_version == 1
    assert result.checkpoint.snapshot.state_version == 1
    assert result.checkpoint.snapshot.plan_revision == 0
    assert result.checkpoint.snapshot.simulated_time_seconds == 10
    assert result.checkpoint.last_sequence == 1
    assert canonical_hash(result.checkpoint.snapshot) != canonical_hash(before.snapshot)

    committed = repository.get_checkpoint(run_id)
    events_before_conflict = repository.list_events(run_id)
    checkpoints_before_conflict = repository.list_checkpoints(run_id)
    with pytest.raises(VersionConflictError) as raised:
        apply_time(
            repository,
            run_id=run_id,
            expected_state_version=0,
            target_time_seconds=20,
            idempotency_key="mutation-a02-stale",
        )

    assert raised.value.run_id == run_id
    assert raised.value.expected == raised.value.expected_version == 0
    assert raised.value.actual == raised.value.actual_version == 1
    assert repository.get_checkpoint(run_id) == committed
    assert repository.list_events(run_id) == events_before_conflict
    assert repository.list_checkpoints(run_id) == checkpoints_before_conflict


def test_a03_exception_after_event_insert_rolls_back_every_operational_write(
    repository: SQLiteStateRepository,
    database_path: Path,
    initial_snapshot: OperationalSnapshot,
    apply_time: Callable[..., MutationResult],
) -> None:
    class InjectedFailure(RuntimeError):
        pass

    stages: list[str] = []

    def fail_after_events(stage: str) -> None:
        stages.append(stage)
        if stage == "after_event_insert":
            raise InjectedFailure("stop between event insert and snapshot insert")

    run_id = initial_snapshot.run_id
    before = repository.get_checkpoint(run_id)
    counts_before = _table_counts(database_path)
    with pytest.raises(InjectedFailure, match="between event insert"):
        apply_time(
            repository,
            run_id=run_id,
            expected_state_version=0,
            target_time_seconds=10,
            idempotency_key="mutation-a03-rollback",
            failure_injector=fail_after_events,
        )

    assert stages == ["after_event_insert"]
    reopened = SQLiteStateRepository(database_path)
    after = reopened.get_checkpoint(run_id)
    assert canonical_hash(after.snapshot) == canonical_hash(before.snapshot)
    assert after == before
    assert reopened.list_events(run_id) == ()
    assert reopened.list_checkpoints(run_id) == (before,)
    assert _table_counts(database_path) == counts_before
    assert reopened.integrity_check() == "ok"

    # The same key can commit after the failure, proving no idempotency outcome leaked.
    retry = apply_time(
        reopened,
        run_id=run_id,
        expected_state_version=0,
        target_time_seconds=10,
        idempotency_key="mutation-a03-rollback",
    )
    assert retry.replayed_idempotent is False
    assert retry.resulting_state_version == 1


def test_a04_exact_retry_precedes_stale_check_and_changed_content_conflicts(
    repository: SQLiteStateRepository,
    initial_snapshot: OperationalSnapshot,
    apply_time: Callable[..., MutationResult],
) -> None:
    run_id = initial_snapshot.run_id
    first = apply_time(
        repository,
        run_id=run_id,
        expected_state_version=0,
        target_time_seconds=10,
        idempotency_key="mutation-a04-exact",
    )
    immediate_retry = apply_time(
        repository,
        run_id=run_id,
        expected_state_version=0,
        target_time_seconds=10,
        idempotency_key="mutation-a04-exact",
    )
    assert immediate_retry.replayed_idempotent is True
    assert immediate_retry.model_dump(exclude={"replayed_idempotent"}) == first.model_dump(
        exclude={"replayed_idempotent"}
    )
    assert len(repository.list_events(run_id)) == 1
    assert len(repository.list_checkpoints(run_id)) == 2

    later = apply_time(
        repository,
        run_id=run_id,
        expected_state_version=1,
        target_time_seconds=20,
        idempotency_key="mutation-a04-later",
    )
    counts_after_later = (
        len(repository.list_events(run_id)),
        len(repository.list_checkpoints(run_id)),
    )
    delayed_retry = apply_time(
        repository,
        run_id=run_id,
        expected_state_version=0,
        target_time_seconds=10,
        idempotency_key="mutation-a04-exact",
    )
    assert delayed_retry.replayed_idempotent is True
    assert delayed_retry.checkpoint == first.checkpoint
    assert repository.get_checkpoint(run_id) == later.checkpoint
    assert counts_after_later == (2, 3)

    with pytest.raises(IdempotencyConflictError) as raised:
        apply_time(
            repository,
            run_id=run_id,
            expected_state_version=0,
            target_time_seconds=11,
            idempotency_key="mutation-a04-exact",
        )
    assert raised.value.run_id == run_id
    assert raised.value.idempotency_key == "mutation-a04-exact"
    assert repository.get_checkpoint(run_id) == later.checkpoint
    assert (
        len(repository.list_events(run_id)),
        len(repository.list_checkpoints(run_id)),
    ) == counts_after_later


def test_generic_participant_exception_rolls_back_events_checkpoint_audit_and_idempotency(
    repository: SQLiteStateRepository,
    database_path: Path,
    initial_snapshot: OperationalSnapshot,
    apply_time: Callable[..., MutationResult],
) -> None:
    class ParticipantFailure(RuntimeError):
        pass

    run_id = initial_snapshot.run_id
    audit = AuditRecord(
        audit_id="audit-participant-rollback",
        run_id=run_id,
        category="transaction",
        action="must_rollback",
        actor="storage-test",
        details={"expected": "rollback"},
        recorded_at_utc=FIXED_UTC,
    )
    captured: list[Any] = []

    def participant(transaction: Any) -> None:
        captured.append(transaction)
        transaction.append_audit(audit)
        raise ParticipantFailure("participant rejected the atomic apply")

    before = repository.get_checkpoint(run_id)
    counts_before = _table_counts(database_path)
    with pytest.raises(ParticipantFailure, match="rejected"):
        apply_time(
            repository,
            run_id=run_id,
            expected_state_version=0,
            target_time_seconds=10,
            idempotency_key="mutation-participant-rollback",
            participant=participant,
        )

    assert repository.get_checkpoint(run_id) == before
    assert repository.list_events(run_id) == ()
    assert repository.export_lifecycle(run_id).audits == ()
    assert _table_counts(database_path) == counts_before
    with pytest.raises(TransactionStateError, match="no longer active"):
        captured[0].append_audit(audit)

    retry = apply_time(
        repository,
        run_id=run_id,
        expected_state_version=0,
        target_time_seconds=10,
        idempotency_key="mutation-participant-rollback",
    )
    assert retry.replayed_idempotent is False
