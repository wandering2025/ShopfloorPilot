"""S11-A02 stale paths at deterministic barriers and S11-A03 idempotent concurrency."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest
from recovery_support import (
    Gate,
    harness,
    lifecycle_counts,
    record_evidence,
    run_in_thread,
    with_gate,
)

from shopfloor_pilot.domain.models import EventType
from shopfloor_pilot.services import ApplicationServiceError, ServiceErrorCode
from shopfloor_pilot.tools import WorkflowPhase
from shopfloor_pilot.workflows import (
    ActionStatus,
    CoordinatorError,
    CoordinatorErrorCode,
    DecisionStatus,
    check_run_invariants,
    invariants_passed,
)


def _plan_state(h) -> dict[str, object]:
    """The applied schedule: revision plus every plan_applied fact."""

    snapshot = h.services.get_snapshot(h.run_id)
    plans = [
        item.event.payload
        for item in h.services.list_events(h.run_id)
        if item.event.event_type is EventType.PLAN_APPLIED
    ]
    return {"plan_revision": snapshot.plan_revision, "plans": plans, "orders": snapshot.orders}


def _out_of_band_advance(h, label: str) -> None:
    """A concurrent writer that bypasses the coordinator guard (e.g. another process)."""

    now = h.services.get_snapshot(h.run_id).simulated_time_seconds
    h.services.advance_simulation(h.run_id, now + 1, f"advance-oob-{label}")


def test_a02_snapshot_to_proposal_barrier_goes_stale(tmp_path: Path) -> None:
    h = harness(tmp_path, "fault")
    plan_before = _plan_state(h)
    decision = h.open_fault()
    gate = Gate(action=lambda: _out_of_band_advance(h, "snapshot"))
    gate.release.set()  # the barrier performs its change, then lets the turn return
    # Turn 0 pins the snapshot; the change lands before turn 1's tool calls execute.
    turns = with_gate(h.fault_turns(), 1, gate)
    step = h.run_fault_supervisor(decision, turns)
    assert gate.entered.is_set()
    assert step.outcome.terminal is WorkflowPhase.STALE_STATE
    assert step.decision.status is DecisionStatus.CANCELLED
    assert step.decision.outcome == "stale"
    assert step.decision.pending_proposal_id is None
    assert not [
        item
        for item in h.services.export_lifecycle(h.run_id).proposals
        if item.workflow_id == decision.workflow_id
    ]
    assert _plan_state(h) == plan_before, "a stale path never alters the applied schedule"


def test_a02_validation_to_approval_and_approval_to_application(tmp_path: Path) -> None:
    h = harness(tmp_path, "fault")
    plan_before = _plan_state(h)

    # Barrier: validation -> approval. The proposal is persisted, then state moves.
    first = h.run_fault_supervisor(h.open_fault("workflow-s11-a02-first")).decision
    assert first.status is DecisionStatus.AWAITING_APPROVAL
    _out_of_band_advance(h, "before-approval")
    counts = lifecycle_counts(h.services, h.run_id)
    refused = h.coordinator.approve(
        h.run_id,
        first.workflow_id,
        proposal_id=first.pending_proposal_id,
        proposal_hash=first.pending_proposal_hash,
        actor="human:alice",
    )
    assert refused.status is ActionStatus.STALE
    assert lifecycle_counts(h.services, h.run_id)["approvals"] == counts["approvals"]
    assert h.coordinator.decision(h.run_id, first.workflow_id).outcome == "stale"
    assert _plan_state(h) == plan_before

    # Barrier: approval -> application. Approval is recorded, then state moves.
    second = h.run_fault_supervisor(h.open_fault("workflow-s11-a02-second")).decision
    approval = h.coordinator.approve(
        h.run_id,
        second.workflow_id,
        proposal_id=second.pending_proposal_id,
        proposal_hash=second.pending_proposal_hash,
        actor="human:alice",
    )
    assert approval.status is ActionStatus.APPROVED
    _out_of_band_advance(h, "before-application")
    identity_after_change = h.identity()
    stale = h.coordinator.apply(
        h.run_id,
        second.workflow_id,
        proposal_id=second.pending_proposal_id,
        proposal_hash=second.pending_proposal_hash,
        approval_id=approval.approval_id,
    )
    assert stale.status is ActionStatus.STALE
    assert h.identity() == identity_after_change, "a rejected apply changes nothing"
    assert lifecycle_counts(h.services, h.run_id)["consumptions"] == counts["consumptions"]
    assert _plan_state(h) == plan_before

    # A fresh proposal requires a fresh approval; the old approval cannot be reused.
    third = h.run_fault_supervisor(h.open_fault("workflow-s11-a02-third")).decision
    assert third.pending_proposal_hash != second.pending_proposal_hash
    reuse = h.coordinator.apply(
        h.run_id,
        third.workflow_id,
        proposal_id=third.pending_proposal_id,
        proposal_hash=third.pending_proposal_hash,
        approval_id=approval.approval_id,
    )
    assert reuse.status is ActionStatus.CONFLICT
    assert reuse.reason_code == ServiceErrorCode.APPROVAL_BINDING_MISMATCH.value
    assert _plan_state(h) == plan_before
    # Old approvals are equally useless directly against S06.
    with pytest.raises(ApplicationServiceError) as excinfo:
        h.services.apply_approved_plan(
            h.run_id,
            third.pending_proposal_id,
            third.pending_proposal_hash,
            approval.approval_id,
            "apply-direct-old-approval",
        )
    assert excinfo.value.code is ServiceErrorCode.APPROVAL_BINDING_MISMATCH
    fresh = h.approve_apply(third)
    assert fresh.status is ActionStatus.APPLIED
    record_evidence(
        "a02_stale_barriers",
        {
            "validation_to_approval": refused.public_dict(),
            "approval_to_application": stale.public_dict(),
            "old_approval_on_fresh_proposal": reuse.public_dict(),
            "direct_s06_old_approval": excinfo.value.code.value,
            "fresh_approval_applied": fresh.public_dict(),
            "applied_schedule_unchanged_on_every_stale_path": True,
        },
    )
    assert invariants_passed(check_run_invariants(h.services, h.run_id))


def test_a02_rejected_proposal_cannot_be_revived(tmp_path: Path) -> None:
    h = harness(tmp_path, "fault")
    decision = h.run_fault_supervisor(h.open_fault()).decision
    rejected = h.coordinator.reject(
        h.run_id,
        decision.workflow_id,
        proposal_id=decision.pending_proposal_id,
        proposal_hash=decision.pending_proposal_hash,
        actor="human:alice",
        reason="prefer another plan",
    )
    assert rejected.status is ActionStatus.REJECTED
    closed = h.coordinator.decision(h.run_id, decision.workflow_id)
    assert closed.status is DecisionStatus.CANCELLED and closed.outcome == "rejected"
    again = h.coordinator.approve(
        h.run_id,
        decision.workflow_id,
        proposal_id=decision.pending_proposal_id,
        proposal_hash=decision.pending_proposal_hash,
        actor="human:bob",
    )
    assert again.status is ActionStatus.CONFLICT
    applied = h.coordinator.apply(
        h.run_id,
        decision.workflow_id,
        proposal_id=decision.pending_proposal_id,
        proposal_hash=decision.pending_proposal_hash,
        approval_id=rejected.approval_id,
    )
    assert applied.status is ActionStatus.CONFLICT
    assert lifecycle_counts(h.services, h.run_id)["consumptions"] == 1  # initial plan only


def test_a02_coordinator_refuses_time_advance_while_paused(tmp_path: Path) -> None:
    h = harness(tmp_path, "fault")
    decision = h.run_fault_supervisor(h.open_fault()).decision
    before = h.identity()
    with pytest.raises(CoordinatorError) as excinfo:
        h.coordinator.advance(h.run_id, 2800, "advance-while-paused")
    assert excinfo.value.code is CoordinatorErrorCode.DECISION_PENDING
    assert h.identity() == before
    with pytest.raises(CoordinatorError):
        h.coordinator.open_decision(
            h.run_id,
            "workflow-s11-second-open",
            kind=decision.kind,
            source=decision.source,
        )
    h.coordinator.advance(h.run_id, 2800, "advance-supersede", cancel_open_decisions=True)
    superseded = h.coordinator.decision(h.run_id, decision.workflow_id)
    assert superseded.status is DecisionStatus.CANCELLED and superseded.outcome == "superseded"


def _two_clients(h):
    return h, h.reopen()


def test_a03_simultaneous_approvals_and_applications_commit_once(tmp_path: Path) -> None:
    h = harness(tmp_path, "fault")
    decision = h.run_fault_supervisor(h.open_fault()).decision
    a, b = _two_clients(h)
    before = lifecycle_counts(h.services, h.run_id)
    version_before = h.services.get_snapshot(h.run_id).state_version

    barrier = threading.Barrier(2)

    def approve(client, actor):
        barrier.wait()
        return client.coordinator.approve(
            client.run_id,
            decision.workflow_id,
            proposal_id=decision.pending_proposal_id,
            proposal_hash=decision.pending_proposal_hash,
            actor=actor,
        )

    t1, box1 = run_in_thread(lambda: approve(a, "human:alice"))
    t2, box2 = run_in_thread(lambda: approve(b, "human:bob"))
    t1.join(30)
    t2.join(30)
    approvals = sorted([box1["result"].status, box2["result"].status], key=str)
    assert approvals == sorted([ActionStatus.APPROVED, ActionStatus.ALREADY_APPROVED], key=str)
    approval_id = box1["result"].approval_id
    assert approval_id == box2["result"].approval_id
    assert lifecycle_counts(h.services, h.run_id)["approvals"] == before["approvals"] + 1

    def apply(client, key):
        barrier.wait()
        return client.coordinator.apply(
            client.run_id,
            decision.workflow_id,
            proposal_id=decision.pending_proposal_id,
            proposal_hash=decision.pending_proposal_hash,
            approval_id=approval_id,
            idempotency_key=key,
        )

    # Same derived key from both clients, then distinct keys from both clients.
    t1, box1 = run_in_thread(lambda: apply(a, None))
    t2, box2 = run_in_thread(lambda: apply(b, None))
    t1.join(30)
    t2.join(30)
    results = [box1["result"], box2["result"]]
    assert sorted(item.status for item in results) == sorted(
        [ActionStatus.APPLIED, ActionStatus.ALREADY_APPLIED]
    )
    assert {item.resulting_state_version for item in results} == {version_before + 1}

    t1, box1 = run_in_thread(lambda: apply(a, "apply-client-a"))
    t2, box2 = run_in_thread(lambda: apply(b, "apply-client-b"))
    t1.join(30)
    t2.join(30)
    distinct = sorted([box1["result"].status.value, box2["result"].status.value])
    assert distinct == [ActionStatus.ALREADY_APPLIED.value] * 2

    # Client disconnect: the response was lost; retrying the same request is safe.
    retry = a.coordinator.apply(
        h.run_id,
        decision.workflow_id,
        proposal_id=decision.pending_proposal_id,
        proposal_hash=decision.pending_proposal_hash,
        approval_id=approval_id,
    )
    assert retry.status is ActionStatus.ALREADY_APPLIED
    assert retry.resulting_state_version == version_before + 1
    after = lifecycle_counts(h.services, h.run_id)
    assert after["consumptions"] == before["consumptions"] + 1
    assert after["plan_applied_events"] == before["plan_applied_events"] + 1
    assert after["events"] == before["events"] + 1
    snapshot = h.services.get_snapshot(h.run_id)
    assert snapshot.state_version == version_before + 1
    assert snapshot.plan_revision == decision.base_plan_revision + 1
    assert h.coordinator.decision(h.run_id, decision.workflow_id).status is DecisionStatus.APPLIED
    record_evidence(
        "a03_idempotent_concurrency",
        {
            "simultaneous_approvals": sorted(str(item) for item in approvals),
            "simultaneous_same_key_applies": sorted(item.status.value for item in results),
            "simultaneous_distinct_key_applies": distinct,
            "disconnect_retry": retry.status.value,
            "counts_before": before,
            "counts_after": after,
            "state_version_before": version_before,
            "state_version_after": snapshot.state_version,
            "plan_revision_after": snapshot.plan_revision,
        },
    )
    assert invariants_passed(check_run_invariants(h.services, h.run_id))


def test_a03_apply_racing_cancel_has_exactly_one_winner(tmp_path: Path) -> None:
    h = harness(tmp_path, "fault")
    decision = h.run_fault_supervisor(h.open_fault()).decision
    approval = h.coordinator.approve(
        h.run_id,
        decision.workflow_id,
        proposal_id=decision.pending_proposal_id,
        proposal_hash=decision.pending_proposal_hash,
        actor="human:alice",
    )
    a, b = _two_clients(h)
    version = h.services.get_snapshot(h.run_id).state_version
    barrier = threading.Barrier(2)

    def apply():
        barrier.wait()
        return a.coordinator.apply(
            h.run_id,
            decision.workflow_id,
            proposal_id=decision.pending_proposal_id,
            proposal_hash=decision.pending_proposal_hash,
            approval_id=approval.approval_id,
        )

    def cancel():
        barrier.wait()
        return b.coordinator.cancel(h.run_id, decision.workflow_id, actor="operator:carol")

    t1, box1 = run_in_thread(apply)
    t2, box2 = run_in_thread(cancel)
    t1.join(30)
    t2.join(30)
    applied, cancelled = box1["result"], box2["result"]
    final = h.coordinator.decision(h.run_id, decision.workflow_id)
    if applied.status is ActionStatus.APPLIED:
        assert cancelled.status is ActionStatus.CONFLICT
        assert final.status is DecisionStatus.APPLIED
        assert h.services.get_snapshot(h.run_id).state_version == version + 1
    else:
        assert cancelled.status is ActionStatus.CANCELLED
        assert applied.status is ActionStatus.CONFLICT
        assert final.status is DecisionStatus.CANCELLED
        assert h.services.get_snapshot(h.run_id).state_version == version
    assert invariants_passed(check_run_invariants(h.services, h.run_id))


def test_a02_concurrent_decision_opens_leave_at_most_one_open(tmp_path: Path) -> None:
    h = harness(tmp_path, "fault")
    a, b = _two_clients(h)
    barrier = threading.Barrier(2)

    def open_with(client, workflow_id):
        barrier.wait()
        try:
            return client.open_fault(workflow_id)
        except CoordinatorError as exc:
            return exc

    t1, box1 = run_in_thread(lambda: open_with(a, "workflow-s11-race-a"))
    t2, box2 = run_in_thread(lambda: open_with(b, "workflow-s11-race-b"))
    t1.join(30)
    t2.join(30)
    open_now = h.coordinator.open_decisions(h.run_id)
    assert len(open_now) <= 1
    for box in (box1, box2):
        if isinstance(box["result"], CoordinatorError):
            assert box["result"].code is CoordinatorErrorCode.DECISION_PENDING
