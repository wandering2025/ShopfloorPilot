"""S11-A10 process restart around application and portable replay plus lifecycle export."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from recovery_support import harness, lifecycle_counts, record_evidence

from shopfloor_pilot.domain.fixtures import load_scenario
from shopfloor_pilot.domain.models import AttemptStatus
from shopfloor_pilot.services import ApplicationServiceError, ApplicationServices, ServiceErrorCode
from shopfloor_pilot.storage import SQLiteStateRepository
from shopfloor_pilot.workflows import (
    ActionStatus,
    DecisionStatus,
    check_run_invariants,
    invariants_passed,
    lifecycle_view,
)

CHILD = Path(__file__).with_name("crash_child.py")


def _awaiting_approved(h):
    decision = h.run_fault_supervisor(h.open_fault()).decision
    approval = h.coordinator.approve(
        h.run_id,
        decision.workflow_id,
        proposal_id=decision.pending_proposal_id,
        proposal_hash=decision.pending_proposal_hash,
        actor="human:alice",
    )
    assert approval.status is ActionStatus.APPROVED
    return decision, approval


def _child(h, decision, approval, mode: str) -> int:
    completed = subprocess.run(
        [
            sys.executable,
            str(CHILD),
            str(h.root / "state.sqlite3"),
            h.bundle.scenario.scenario_id,
            h.run_id,
            decision.workflow_id,
            decision.pending_proposal_id,
            decision.pending_proposal_hash,
            approval.approval_id,
            mode,
        ],
        capture_output=True,
        timeout=120,
        check=False,
    )
    return completed.returncode


def test_a10_restart_before_application_restores_pending_and_applies_once(tmp_path: Path) -> None:
    h = harness(tmp_path, "fault")
    decision, approval = _awaiting_approved(h)
    decisions_before = [item.public_dict() for item in h.coordinator.decisions(h.run_id)]
    counts = lifecycle_counts(h.services, h.run_id)
    del h.coordinator, h.services  # the process goes away before application

    restarted = h.reopen()
    assert [
        item.public_dict() for item in restarted.coordinator.decisions(h.run_id)
    ] == decisions_before
    report = restarted.coordinator.restore(h.run_id)
    assert [item["workflow_id"] for item in report["awaiting_approval"]] == [decision.workflow_id]
    assert report["approvals_from_history_are_actionable"] is False
    assert lifecycle_counts(restarted.services, h.run_id) == counts
    applied = restarted.coordinator.apply(
        h.run_id,
        decision.workflow_id,
        proposal_id=decision.pending_proposal_id,
        proposal_hash=decision.pending_proposal_hash,
        approval_id=approval.approval_id,
    )
    assert applied.status is ActionStatus.APPLIED
    again = restarted.reopen().coordinator.apply(
        h.run_id,
        decision.workflow_id,
        proposal_id=decision.pending_proposal_id,
        proposal_hash=decision.pending_proposal_hash,
        approval_id=approval.approval_id,
    )
    assert again.status is ActionStatus.ALREADY_APPLIED
    after = lifecycle_counts(restarted.services, h.run_id)
    assert after["consumptions"] == counts["consumptions"] + 1
    assert after["plan_applied_events"] == counts["plan_applied_events"] + 1


def test_a10_hard_crash_inside_the_write_transaction_rolls_back(tmp_path: Path) -> None:
    h = harness(tmp_path, "fault")
    decision, approval = _awaiting_approved(h)
    before = h.identity()
    counts = lifecycle_counts(h.services, h.run_id)
    assert _child(h, decision, approval, "crash_before_commit") == 17
    restarted = h.reopen()
    assert restarted.identity() == before
    assert lifecycle_counts(restarted.services, h.run_id) == counts
    assert restarted.services.repository.integrity_check() == "ok"
    pending = restarted.coordinator.decision(h.run_id, decision.workflow_id)
    assert pending.status is DecisionStatus.AWAITING_APPROVAL
    assert pending.consumed_approval_id is None
    applied = restarted.coordinator.apply(
        h.run_id,
        decision.workflow_id,
        proposal_id=decision.pending_proposal_id,
        proposal_hash=decision.pending_proposal_hash,
        approval_id=approval.approval_id,
    )
    assert applied.status is ActionStatus.APPLIED
    assert (
        lifecycle_counts(restarted.services, h.run_id)["consumptions"] == counts["consumptions"] + 1
    )
    record_evidence(
        "a10_crash_inside_transaction",
        {
            "child_exit_code": 17,
            "identity_unchanged_after_crash": True,
            "integrity_check": "ok",
            "decision_status_after_crash": pending.status.value,
            "retry_status": applied.status.value,
        },
    )


def test_a10_hard_exit_after_commit_never_duplicates(tmp_path: Path) -> None:
    h = harness(tmp_path, "fault")
    decision, approval = _awaiting_approved(h)
    version = h.services.get_snapshot(h.run_id).state_version
    counts = lifecycle_counts(h.services, h.run_id)
    assert _child(h, decision, approval, "exit_after_commit") == 23
    restarted = h.reopen()
    assert restarted.services.get_snapshot(h.run_id).state_version == version + 1
    assert restarted.coordinator.decision(h.run_id, decision.workflow_id).status is (
        DecisionStatus.APPLIED
    )
    for key in (None, "apply-retry-with-new-key"):
        retry = restarted.coordinator.apply(
            h.run_id,
            decision.workflow_id,
            proposal_id=decision.pending_proposal_id,
            proposal_hash=decision.pending_proposal_hash,
            approval_id=approval.approval_id,
            idempotency_key=key,
        )
        assert retry.status is ActionStatus.ALREADY_APPLIED
        assert retry.resulting_state_version == version + 1
    after = lifecycle_counts(restarted.services, h.run_id)
    assert after["plan_applied_events"] == counts["plan_applied_events"] + 1
    assert after["events"] == counts["events"] + 1
    assert restarted.services.get_snapshot(h.run_id).state_version == version + 1
    record_evidence(
        "a10_exit_after_commit",
        {
            "child_exit_code": 23,
            "state_version_before": version,
            "state_version_after": version + 1,
            "retries": [ActionStatus.ALREADY_APPLIED.value] * 2,
            "plan_applied_events_added": 1,
            "events_added": 1,
        },
    )


def test_a10_replay_and_lifecycle_export_are_separate_and_read_only(tmp_path: Path) -> None:
    h = harness(tmp_path, "fault")
    stale = h.run_fault_supervisor(h.open_fault("workflow-s11-a10-stale")).decision
    h.coordinator.advance(h.run_id, 2701, "advance-a10-supersede", cancel_open_decisions=True)
    decision = h.run_fault_supervisor(h.open_fault("workflow-s11-a10-applied")).decision
    applied = h.approve_apply(decision)
    assert applied.status is ActionStatus.APPLIED
    lifecycle = h.services.export_lifecycle(h.run_id)
    proposal = next(
        item for item in lifecycle.proposals if item.proposal_id == decision.pending_proposal_id
    )
    h.coordinator.advance(h.run_id, proposal.objective.makespan_seconds, "advance-a10-final")
    assert invariants_passed(check_run_invariants(h.services, h.run_id))

    bundle_path = tmp_path / "export" / "events.jsonl"
    bundle_path.parent.mkdir()
    header = h.services.export_replay_bundle(h.run_id, bundle_path)
    (tmp_path / "export" / "lifecycle.json").write_text(
        json.dumps(h.services.export_lifecycle(h.run_id).model_dump(mode="json"))
    )
    current = h.services.get_checkpoint(h.run_id)
    metrics = h.services.calculate_run_metrics(h.run_id)
    del h.coordinator, h.services

    # Standalone replay: no repository, no scenario, no solver.
    replayed = ApplicationServices.replay_bundle(bundle_path)
    assert replayed.final_snapshot_hash == current.snapshot_hash == header.final_snapshot_hash
    assert replayed.checkpoint.snapshot.state_version == current.snapshot.state_version
    assert replayed.checkpoint.snapshot.plan_revision == current.snapshot.plan_revision
    interrupted = [
        item
        for item in replayed.checkpoint.snapshot.attempts
        if item.status is AttemptStatus.INTERRUPTED
    ]
    assert len(interrupted) == 1 and interrupted[0].consumed_seconds == 180
    assert any(
        item.restart_of_attempt_id == interrupted[0].attempt_id
        for item in replayed.checkpoint.snapshot.attempts
    )
    assert replayed.actual_metrics == metrics
    assert replayed.actual_metrics.wasted_processing_seconds == 180

    view = lifecycle_view(json.loads((tmp_path / "export" / "lifecycle.json").read_text()))
    assert view["actionable"] is False
    statuses = {item["workflow_id"]: item["status"] for item in view["proposals"]}
    assert statuses[decision.workflow_id] == "applied"
    assert statuses[stale.workflow_id] == "superseded"
    historical = next(
        approval
        for item in view["proposals"]
        if item["workflow_id"] == decision.workflow_id
        for approval in item["approvals"]
    )
    assert historical["actor"] == "human:alice" and historical["consumed_by_mutation"]

    # A replayed approval cannot authorize a live action anywhere.
    fresh_repo = SQLiteStateRepository(tmp_path / "fresh.sqlite3")
    fresh_repo.initialize()
    fresh = ApplicationServices(fresh_repo, load_scenario("machine_breakdown").catalog, seed=0)
    fresh.create_run(
        ApplicationServices.load_replay_bundle(bundle_path).header.initial_checkpoint.snapshot
    )
    with pytest.raises(ApplicationServiceError) as excinfo:
        fresh.apply_approved_plan(
            current.snapshot.run_id,
            decision.pending_proposal_id,
            decision.pending_proposal_hash,
            historical["approval_id"],
            "apply-from-replayed-approval",
        )
    assert excinfo.value.code in (
        ServiceErrorCode.PROPOSAL_NOT_FOUND,
        ServiceErrorCode.APPROVAL_NOT_FOUND,
    )
    assert fresh.get_snapshot(current.snapshot.run_id).state_version == 0
    record_evidence(
        "a10_replay_and_lifecycle",
        {
            "replay_final_snapshot_hash": replayed.final_snapshot_hash,
            "live_final_snapshot_hash": current.snapshot_hash,
            "state_version": replayed.checkpoint.snapshot.state_version,
            "plan_revision": replayed.checkpoint.snapshot.plan_revision,
            "interrupted_attempts": len(interrupted),
            "replayed_wasted_processing_seconds": replayed.actual_metrics.wasted_processing_seconds,
            "lifecycle_view_actionable": view["actionable"],
            "proposal_statuses": statuses,
            "replayed_approval_live_use": excinfo.value.code.value,
        },
    )
