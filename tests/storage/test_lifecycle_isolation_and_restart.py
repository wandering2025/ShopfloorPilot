"""S02-A09 through A11 lifecycle, run-isolation, immutability, and restart tests."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from shopfloor_pilot.domain.canonical import canonical_hash
from shopfloor_pilot.domain.fixtures import load_scenario, project_public_view
from shopfloor_pilot.domain.models import (
    ApprovalDecision,
    ApprovalRecord,
    EventType,
    MachineStatus,
    OperationalSnapshot,
)
from shopfloor_pilot.events import (
    EventDraft,
    MachineDownPayload,
    TimeAdvancedPayload,
)
from shopfloor_pilot.storage import (
    AuditRecord,
    CrossRunReferenceError,
    MutationResult,
    SQLiteStateRepository,
)

FIXED_UTC = "2026-10-20T18:30:00+02:00"


def _apply_fault_and_time(
    repository: SQLiteStateRepository,
    *,
    run_id: str,
    expected_state_version: int,
    target_time_seconds: int,
    idempotency_key: str,
) -> MutationResult:
    down = MachineDownPayload(machine_id="CNC01", status="down")
    advance = TimeAdvancedPayload(target_time_seconds=target_time_seconds)
    return repository.apply_mutation(
        run_id=run_id,
        expected_state_version=expected_state_version,
        idempotency_key=idempotency_key,
        command_name="record_fault_and_advance",
        command_payload={
            "machine_id": "CNC01",
            "target_time_seconds": target_time_seconds,
        },
        events=(
            EventDraft(
                simulated_time_seconds=target_time_seconds,
                event_type=EventType.TIME_ADVANCED,
                payload=advance.model_dump(mode="json"),
                recorded_at_utc=FIXED_UTC,
            ),
            EventDraft(
                simulated_time_seconds=target_time_seconds,
                event_type=EventType.MACHINE_DOWN,
                payload=down.model_dump(mode="json"),
                recorded_at_utc=FIXED_UTC,
            ),
        ),
        created_at_utc=FIXED_UTC,
    )


def test_a09_lifecycle_records_and_reads_preserve_operational_version(
    repository: SQLiteStateRepository,
    initial_snapshot: OperationalSnapshot,
    persist_lifecycle: Callable[..., Any],
) -> None:
    run_id = initial_snapshot.run_id
    before = repository.get_checkpoint(run_id)
    records = persist_lifecycle(repository, run_id, suffix="a09")

    assert repository.get_snapshot(run_id) == before.snapshot
    assert repository.get_checkpoint(run_id) == before
    assert repository.list_events(run_id) == ()
    assert repository.list_checkpoints(run_id) == (before,)
    lifecycle = repository.export_lifecycle(run_id)
    assert lifecycle.proposals == (records.proposal,)
    assert lifecycle.approvals == (records.approval,)
    assert lifecycle.workflows == (records.workflow,)
    assert lifecycle.audits == (records.audit,)
    assert lifecycle.approval_consumptions == ()
    assert repository.get_checkpoint(run_id).snapshot.state_version == 0

    result = _apply_fault_and_time(
        repository,
        run_id=run_id,
        expected_state_version=0,
        target_time_seconds=10,
        idempotency_key="mutation-a09-fault",
    )
    assert result.resulting_state_version == 1
    assert result.checkpoint.snapshot.plan_revision == 0
    assert result.checkpoint.snapshot.simulated_time_seconds == 10
    cnc01 = next(
        machine for machine in result.checkpoint.snapshot.machines if machine.machine_id == "CNC01"
    )
    assert cnc01.status is MachineStatus.DOWN

    persisted = repository.get_proposal(run_id, records.proposal.proposal_id)
    assert persisted.base_state_version == 0
    assert persisted.base_snapshot_hash == before.snapshot_hash
    assert repository.get_approval(run_id, records.approval.approval_id) == records.approval
    assert repository.export_lifecycle(run_id).approval_consumptions == ()


def test_a09_fresh_repository_restores_operational_and_lifecycle_state(
    repository: SQLiteStateRepository,
    database_path: Path,
    initial_snapshot: OperationalSnapshot,
    persist_lifecycle: Callable[..., Any],
    apply_time: Callable[..., MutationResult],
) -> None:
    run_id = initial_snapshot.run_id
    records = persist_lifecycle(repository, run_id, suffix="restart")
    committed = apply_time(
        repository,
        run_id=run_id,
        expected_state_version=0,
        target_time_seconds=12,
        idempotency_key="mutation-restart",
    )

    reopened = SQLiteStateRepository(database_path)
    assert reopened.get_checkpoint(run_id) == committed.checkpoint
    assert reopened.get_snapshot(run_id) == committed.checkpoint.snapshot
    assert reopened.list_events(run_id) == committed.events
    assert reopened.replay_run(run_id, from_state_version=0) == committed.checkpoint

    current_restore = reopened.restore_run(run_id)
    assert current_restore.checkpoint == committed.checkpoint
    assert current_restore.events == ()
    assert current_restore.lifecycle.proposals == (records.proposal,)
    assert current_restore.lifecycle.approvals == (records.approval,)
    assert current_restore.lifecycle.workflows == (records.workflow,)
    assert current_restore.lifecycle.audits == (records.audit,)
    assert current_restore.lifecycle.approval_consumptions == ()

    suffix_restore = reopened.restore_run(run_id, from_state_version=0)
    assert suffix_restore.checkpoint.snapshot.state_version == 0
    assert suffix_restore.events == committed.events
    assert suffix_restore.lifecycle == current_restore.lifecycle

    durable_retry = apply_time(
        reopened,
        run_id=run_id,
        expected_state_version=0,
        target_time_seconds=12,
        idempotency_key="mutation-restart",
    )
    assert durable_retry.replayed_idempotent is True
    assert durable_retry.checkpoint == committed.checkpoint
    assert reopened.integrity_check() == "ok"


def test_a10_reset_creates_isolated_run_and_cross_run_approval_cannot_apply(
    repository: SQLiteStateRepository,
    initial_snapshot: OperationalSnapshot,
    snapshot_factory: Callable[[str], OperationalSnapshot],
    persist_lifecycle: Callable[..., Any],
    apply_time: Callable[..., MutationResult],
) -> None:
    run_a = initial_snapshot.run_id
    run_b = "run-storage-reset"
    reset = repository.reset_run(
        run_a,
        snapshot_factory(run_b),
        created_at_utc=FIXED_UTC,
    )
    assert reset.run_id == run_b
    assert reset.reset_from_run_id == run_a
    assert repository.get_snapshot(run_a).state_version == 0
    assert repository.get_snapshot(run_b).state_version == 0

    records = persist_lifecycle(repository, run_a, suffix="run-a")
    with pytest.raises(CrossRunReferenceError) as proposal_error:
        repository.get_proposal(run_b, records.proposal.proposal_id)
    assert proposal_error.value.expected_run_id == run_b
    assert proposal_error.value.actual_run_id == run_a
    with pytest.raises(CrossRunReferenceError):
        repository.get_approval(run_b, records.approval.approval_id)

    cross_run_approval = ApprovalRecord(
        schema_version=1,
        approval_id="approval-cross-run",
        proposal_id=records.proposal.proposal_id,
        proposal_hash=records.proposal.proposal_hash,
        run_id=run_b,
        workflow_id=records.workflow.workflow_id,
        base_state_version=0,
        decision=ApprovalDecision.APPROVED,
        actor="storage-test-operator",
        decided_at_utc=FIXED_UTC,
        reason="Must be rejected because the proposal belongs to another run.",
    )
    with pytest.raises(CrossRunReferenceError):
        repository.save_approval(cross_run_approval)

    def consume_old_approval(transaction: Any) -> None:
        transaction.consume_approval(records.approval.approval_id, consumed_at_utc=FIXED_UTC)

    with pytest.raises(CrossRunReferenceError):
        apply_time(
            repository,
            run_id=run_b,
            expected_state_version=0,
            target_time_seconds=5,
            idempotency_key="mutation-cross-run-rejected",
            participant=consume_old_approval,
        )
    assert repository.get_snapshot(run_b).state_version == 0
    assert repository.list_events(run_b) == ()
    assert repository.export_lifecycle(run_b).approval_consumptions == ()

    result_a = apply_time(
        repository,
        run_id=run_a,
        expected_state_version=0,
        target_time_seconds=10,
        idempotency_key="mutation-run-a-only",
    )
    assert result_a.resulting_state_version == 1
    assert repository.get_snapshot(run_b).state_version == 0
    assert repository.get_snapshot(run_b).simulated_time_seconds == 0
    assert repository.list_events(run_b) == ()
    assert repository.export_lifecycle(run_b).proposals == ()


def test_a11_snapshot_reads_are_detached_immutable_and_public_projection_is_allowlisted(
    repository: SQLiteStateRepository,
    initial_snapshot: OperationalSnapshot,
) -> None:
    run_id = initial_snapshot.run_id
    private_sentinel = "credential-sentinel-must-not-be-public"
    repository.append_audit(
        AuditRecord(
            audit_id="audit-private-sentinel",
            run_id=run_id,
            category="private_test_data",
            action="store_non_operational_detail",
            actor="storage-test",
            details={"private_note": private_sentinel},
            recorded_at_utc=FIXED_UTC,
        )
    )

    first = repository.get_snapshot(run_id)
    second = repository.get_snapshot(run_id)
    assert first == second
    assert first is not second
    assert first.machines[0] is not second.machines[0]
    with pytest.raises(ValidationError, match="frozen"):
        first.simulated_time_seconds = 999  # type: ignore[misc]
    with pytest.raises(ValidationError, match="frozen"):
        first.machines[0].status = MachineStatus.DOWN  # type: ignore[misc]

    dumped = first.model_dump(mode="json")
    dumped["machines"][0]["status"] = "down"
    dumped["orders"].clear()
    copied = first.model_copy(update={"simulated_time_seconds": 999})
    assert copied.simulated_time_seconds == 999
    fresh = repository.get_snapshot(run_id)
    assert fresh == second
    assert canonical_hash(fresh) == canonical_hash(initial_snapshot)
    assert len(fresh.orders) == len(initial_snapshot.orders)
    assert fresh.machines[0].status is initial_snapshot.machines[0].status
    assert private_sentinel not in fresh.model_dump_json()

    bundle = load_scenario("machine_breakdown")
    public = project_public_view(bundle, simulated_time_seconds=2700)
    public_data = json.loads(public.model_dump_json())
    assert set(public_data) == {
        "schema_version",
        "scenario_id",
        "run_id",
        "simulated_time_seconds",
        "observations",
    }
    for observation in public_data["observations"]:
        assert set(observation) == {
            "schema_version",
            "observation_id",
            "run_id",
            "machine_id",
            "observed_at_seconds",
            "signals",
            "summary",
            "recorded_at_utc",
        }
    serialized = public.model_dump_json().casefold()
    for forbidden in (
        "fault_truth",
        "private_truth_ref",
        "interventions",
        "cause_label",
        "deepseek_api_key",
        private_sentinel,
    ):
        assert forbidden not in serialized
