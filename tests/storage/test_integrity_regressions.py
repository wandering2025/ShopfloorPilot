"""Focused fail-closed regressions for reviewed SQLite integrity boundaries."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from shopfloor_pilot.domain.models import (
    ApprovalRecord,
    CandidateProposal,
    EventType,
    OperationalSnapshot,
    ScheduleObjective,
)
from shopfloor_pilot.events import EventDraft, PlanAppliedPayload
from shopfloor_pilot.storage import (
    AuditRecord,
    MutationResult,
    RecordConflictError,
    SQLiteStateRepository,
    WorkflowRecord,
    WorkflowStatus,
)

FIXED_UTC = "2026-10-20T18:30:00+02:00"


def _replace_json_column(
    database_path: Path,
    *,
    table: str,
    column: str,
    value: str,
    run_id: str,
    id_column: str,
    record_id: str,
) -> None:
    allowed = {
        ("idempotency_records", "result_json", "idempotency_key"),
        ("workflows", "payload_json", "workflow_id"),
        ("proposals", "proposal_json", "proposal_id"),
        ("approvals", "approval_json", "approval_id"),
        ("audit_records", "record_json", "audit_id"),
    }
    assert (table, column, id_column) in allowed
    with sqlite3.connect(database_path) as connection:
        updated = connection.execute(
            f"UPDATE {table} SET {column} = ? "  # noqa: S608 - allowlisted test identifiers
            f"WHERE run_id = ? AND {id_column} = ?",  # noqa: S608
            (value, run_id, record_id),
        )
        assert updated.rowcount == 1


def test_tampered_idempotency_result_fails_closed_before_returning_cached_outcome(
    repository: SQLiteStateRepository,
    database_path: Path,
    initial_snapshot: OperationalSnapshot,
    apply_time: Callable[..., MutationResult],
) -> None:
    run_id = initial_snapshot.run_id
    committed = apply_time(
        repository,
        run_id=run_id,
        expected_state_version=0,
        target_time_seconds=10,
        idempotency_key="mutation-integrity-idempotency",
    )
    raw = json.loads(committed.model_dump_json())
    raw["command_hash"] = "f" * 64
    _replace_json_column(
        database_path,
        table="idempotency_records",
        column="result_json",
        value=json.dumps(raw, separators=(",", ":")),
        run_id=run_id,
        id_column="idempotency_key",
        record_id=committed.idempotency_key,
    )

    with pytest.raises(RecordConflictError):
        apply_time(
            SQLiteStateRepository(database_path),
            run_id=run_id,
            expected_state_version=0,
            target_time_seconds=10,
            idempotency_key="mutation-integrity-idempotency",
        )
    assert repository.get_checkpoint(run_id) == committed.checkpoint
    assert repository.list_events(run_id) == committed.events


@pytest.mark.parametrize("record_kind", ["workflow", "proposal", "approval", "audit"])
def test_lifecycle_export_rejects_valid_json_that_disagrees_with_stored_binding(
    record_kind: str,
    repository: SQLiteStateRepository,
    database_path: Path,
    initial_snapshot: OperationalSnapshot,
    persist_lifecycle: Callable[..., Any],
) -> None:
    run_id = initial_snapshot.run_id
    records = persist_lifecycle(repository, run_id, suffix=f"tamper-{record_kind}")

    if record_kind == "workflow":
        tampered = WorkflowRecord.model_validate(
            {
                **records.workflow.model_dump(mode="python"),
                "status": WorkflowStatus.FAILED,
                "payload": {"tampered": True},
            }
        )
        table, column, id_column, record_id = (
            "workflows",
            "payload_json",
            "workflow_id",
            records.workflow.workflow_id,
        )
    elif record_kind == "proposal":
        original = records.proposal
        tampered = CandidateProposal.create(
            schema_version=1,
            proposal_id=original.proposal_id,
            run_id=original.run_id,
            workflow_id=original.workflow_id,
            base_state_version=original.base_state_version,
            base_snapshot_hash=original.base_snapshot_hash,
            proposed_plan_revision=original.proposed_plan_revision,
            assignments=original.assignments,
            objective=ScheduleObjective(
                weighted_total_tardiness_seconds=1,
                makespan_seconds=original.objective.makespan_seconds,
            ),
            proposed_order_deltas=original.proposed_order_deltas,
            proposed_resource_deltas=original.proposed_resource_deltas,
            created_at_utc=original.created_at_utc,
        )
        table, column, id_column, record_id = (
            "proposals",
            "proposal_json",
            "proposal_id",
            original.proposal_id,
        )
    elif record_kind == "approval":
        tampered = ApprovalRecord.model_validate(
            {
                **records.approval.model_dump(mode="python"),
                "reason": "Tampered but structurally valid approval display text.",
            }
        )
        table, column, id_column, record_id = (
            "approvals",
            "approval_json",
            "approval_id",
            records.approval.approval_id,
        )
    else:
        tampered = AuditRecord.model_validate(
            {
                **records.audit.model_dump(mode="python"),
                "details": {"tampered": True},
            }
        )
        table, column, id_column, record_id = (
            "audit_records",
            "record_json",
            "audit_id",
            records.audit.audit_id,
        )

    _replace_json_column(
        database_path,
        table=table,
        column=column,
        value=tampered.model_dump_json(),
        run_id=run_id,
        id_column=id_column,
        record_id=record_id,
    )
    with pytest.raises(RecordConflictError):
        SQLiteStateRepository(database_path).export_lifecycle(run_id)


def test_audit_duplicate_is_bound_to_workflow_and_observed_state_version(
    repository: SQLiteStateRepository,
    initial_snapshot: OperationalSnapshot,
    apply_time: Callable[..., MutationResult],
) -> None:
    run_id = initial_snapshot.run_id
    workflow = WorkflowRecord(
        run_id=run_id,
        workflow_id="workflow-audit-binding",
        status=WorkflowStatus.RUNNING,
        updated_at_utc=FIXED_UTC,
    )
    repository.save_workflow(workflow)
    workflow_bound = AuditRecord(
        audit_id="audit-binding-workflow",
        run_id=run_id,
        category="integrity",
        action="duplicate_binding",
        actor="storage-test",
        details={"case": "workflow"},
        recorded_at_utc=FIXED_UTC,
    )
    assert repository.append_audit(workflow_bound) == 1
    assert repository.append_audit(workflow_bound) == 1
    with pytest.raises(RecordConflictError):
        repository.append_audit(workflow_bound, workflow_id=workflow.workflow_id)

    version_bound = AuditRecord(
        audit_id="audit-binding-version",
        run_id=run_id,
        category="integrity",
        action="duplicate_binding",
        actor="storage-test",
        details={"case": "state_version"},
        recorded_at_utc=FIXED_UTC,
    )
    assert repository.append_audit(version_bound) == 2
    apply_time(
        repository,
        run_id=run_id,
        expected_state_version=0,
        target_time_seconds=10,
        idempotency_key="mutation-audit-version-binding",
    )
    with pytest.raises(RecordConflictError):
        repository.append_audit(version_bound)


def test_audit_ids_are_run_scoped_and_do_not_couple_independent_runs(
    repository: SQLiteStateRepository,
    initial_snapshot: OperationalSnapshot,
    snapshot_factory: Callable[[str], OperationalSnapshot],
) -> None:
    run_a = initial_snapshot.run_id
    run_b = "run-audit-independent"
    repository.create_run(snapshot_factory(run_b), created_at_utc=FIXED_UTC)

    def audit(run_id: str) -> AuditRecord:
        return AuditRecord(
            audit_id="audit-shared-local-id",
            run_id=run_id,
            category="integrity",
            action="same_local_identity",
            actor="storage-test",
            details={"run": run_id},
            recorded_at_utc=FIXED_UTC,
        )

    audit_a = audit(run_a)
    audit_b = audit(run_b)
    assert repository.append_audit(audit_a) == 1
    assert repository.append_audit(audit_b) == 1
    assert repository.export_lifecycle(run_a).audits == (audit_a,)
    assert repository.export_lifecycle(run_b).audits == (audit_b,)


@pytest.mark.parametrize("tamper", ["time", "type_priority", "event_index"])
def test_event_relational_column_tampering_fails_closed(
    tamper: str,
    repository: SQLiteStateRepository,
    database_path: Path,
    initial_snapshot: OperationalSnapshot,
    apply_time: Callable[..., MutationResult],
) -> None:
    run_id = initial_snapshot.run_id
    apply_time(
        repository,
        run_id=run_id,
        expected_state_version=0,
        target_time_seconds=10,
        idempotency_key=f"mutation-event-column-{tamper}",
    )
    with sqlite3.connect(database_path) as connection:
        if tamper == "time":
            connection.execute(
                "UPDATE operational_events SET simulated_time_seconds = 11 "
                "WHERE run_id = ? AND sequence = 1",
                (run_id,),
            )
        elif tamper == "type_priority":
            connection.execute(
                "UPDATE operational_events SET event_type = 'machine_down', event_priority = 20 "
                "WHERE run_id = ? AND sequence = 1",
                (run_id,),
            )
        else:
            connection.execute(
                "UPDATE operational_events SET event_index = 1 WHERE run_id = ? AND sequence = 1",
                (run_id,),
            )

    assert repository.integrity_check() == "ok"
    with pytest.raises(RecordConflictError):
        SQLiteStateRepository(database_path).list_events(run_id)


def test_atomic_approved_plan_consumption_is_exactly_idempotent_without_duplicates(
    repository: SQLiteStateRepository,
    initial_snapshot: OperationalSnapshot,
    persist_lifecycle: Callable[..., Any],
) -> None:
    run_id = initial_snapshot.run_id
    records = persist_lifecycle(repository, run_id, suffix="approved-plan")
    plan = PlanAppliedPayload(plan_revision=1, operations=(), attempts=())
    event = EventDraft(
        simulated_time_seconds=0,
        event_type=EventType.PLAN_APPLIED,
        payload=plan.model_dump(mode="json"),
        recorded_at_utc=FIXED_UTC,
    )
    applied_audit = AuditRecord(
        audit_id="audit-approved-plan-applied",
        run_id=run_id,
        category="approval",
        action="consume_and_apply",
        actor="storage-test-operator",
        details={"approval_id": records.approval.approval_id},
        simulated_time_seconds=0,
        recorded_at_utc=FIXED_UTC,
    )
    consumed: list[Any] = []

    def consume_and_audit(transaction: Any) -> None:
        consumed.append(
            transaction.consume_approval(
                records.approval.approval_id,
                consumed_at_utc=FIXED_UTC,
            )
        )
        transaction.append_audit(applied_audit, workflow_id=records.workflow.workflow_id)

    result = repository.apply_mutation(
        run_id=run_id,
        expected_state_version=0,
        idempotency_key="mutation-approved-plan",
        command_name="apply_approved_plan",
        command_payload={
            "approval_id": records.approval.approval_id,
            "proposal_id": records.proposal.proposal_id,
            "proposal_hash": records.proposal.proposal_hash,
        },
        events=(event,),
        participant=consume_and_audit,
        created_at_utc=FIXED_UTC,
    )
    assert result.resulting_state_version == 1
    assert result.checkpoint.snapshot.plan_revision == 1
    assert len(consumed) == 1
    lifecycle = repository.export_lifecycle(run_id)
    assert lifecycle.approval_consumptions == tuple(consumed)
    assert lifecycle.audits == (records.audit, applied_audit)

    participant_called = False

    def must_not_run_on_exact_retry(_transaction: Any) -> None:
        nonlocal participant_called
        participant_called = True
        raise AssertionError("participant must not run for an exact idempotent retry")

    retry = repository.apply_mutation(
        run_id=run_id,
        expected_state_version=0,
        idempotency_key="mutation-approved-plan",
        command_name="apply_approved_plan",
        command_payload={
            "approval_id": records.approval.approval_id,
            "proposal_id": records.proposal.proposal_id,
            "proposal_hash": records.proposal.proposal_hash,
        },
        events=(event,),
        participant=must_not_run_on_exact_retry,
        created_at_utc=FIXED_UTC,
    )
    assert participant_called is False
    assert retry.replayed_idempotent is True
    assert retry.model_dump(exclude={"replayed_idempotent"}) == result.model_dump(
        exclude={"replayed_idempotent"}
    )
    after_retry = repository.export_lifecycle(run_id)
    assert after_retry.approval_consumptions == tuple(consumed)
    assert after_retry.audits == lifecycle.audits
    assert len(repository.list_events(run_id)) == 1
    assert len(repository.list_checkpoints(run_id)) == 2
