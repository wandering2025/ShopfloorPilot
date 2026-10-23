"""S11-A06 wall-clock waiting never moves simulation; S11-A07 obsolete work stays dead."""

from __future__ import annotations

from datetime import timedelta
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

from shopfloor_pilot.budgets import BudgetLedger
from shopfloor_pilot.tools import WorkflowPhase
from shopfloor_pilot.workflows import (
    ActionStatus,
    CoordinatorError,
    CoordinatorErrorCode,
    DecisionStatus,
)

PERSIST_TURN = -1  # the provider response that would persist the proposal


def _time_view(h) -> dict[str, object]:
    metrics = h.services.calculate_run_metrics(h.run_id)
    snapshot = h.services.get_snapshot(h.run_id)
    return {
        "simulated_time_seconds": snapshot.simulated_time_seconds,
        "attempts": [item.model_dump(mode="json") for item in snapshot.attempts],
        "as_of": metrics.as_of_seconds,
        "tardiness_lower_bounds": {
            item.order_id: item.weighted_tardiness_lower_bound_seconds for item in metrics.orders
        },
        "predicted_completion": {
            item.order_id: item.predicted_completion_seconds for item in metrics.orders
        },
        "wasted": metrics.wasted_processing_seconds,
    }


def test_a06_provider_and_human_waiting_do_not_drift_simulation(tmp_path: Path) -> None:
    h = harness(tmp_path, "fault")
    before = _time_view(h)
    identity_before = h.identity()
    decision = h.open_fault()
    gate = Gate()
    turns = with_gate(h.fault_turns(), 2, gate)  # held mid-evidence gathering
    thread, box = run_in_thread(lambda: h.run_fault_supervisor(decision, turns))
    assert gate.entered.wait(30)
    # The provider call is outstanding; wall time jumps by hours.
    h.wall.jump(timedelta(hours=6))
    for _ in range(3):
        assert _time_view(h) == before
        assert h.identity() == identity_before
    gate.release.set()
    thread.join(30)
    step = box["result"]
    assert step.outcome.terminal is WorkflowPhase.AWAITING_APPROVAL
    awaiting = step.decision
    assert awaiting.decision_time_seconds == before["simulated_time_seconds"]

    # A human takes three days to decide.
    h.wall.jump(timedelta(days=3))
    assert _time_view(h) == before
    ledger = BudgetLedger(tmp_path / "provider_ledger.sqlite3")
    try:
        budget = ledger.snapshot(decision.workflow_id).public_dict()
    finally:
        ledger.close()
    assert budget["status"] == "paused_human"
    applied = h.approve_apply(awaiting)
    assert applied.status is ActionStatus.APPLIED
    assert applied.effective_time_seconds == awaiting.decision_boundary_seconds == 2701

    proposal = next(
        item
        for item in h.services.export_lifecycle(h.run_id).proposals
        if item.proposal_id == awaiting.pending_proposal_id
    )
    assert min(item.window.start_seconds for item in proposal.assignments) >= 2701
    approval = next(
        item
        for item in h.services.export_lifecycle(h.run_id).approvals
        if item.proposal_id == proposal.proposal_id
    )
    latency = h.services.calculate_decision_latency(
        start_simulated_seconds=awaiting.decision_time_seconds,
        end_simulated_seconds=awaiting.decision_time_seconds,
        started_at_utc=proposal.created_at_utc,
        completed_at_utc=approval.decided_at_utc,
    )
    assert latency.simulated_elapsed_seconds == 0
    assert latency.wall_clock_elapsed_ms >= 3 * 24 * 3600 * 1000
    record_evidence(
        "a06_wall_clock_waiting",
        {
            "decision_time_seconds": awaiting.decision_time_seconds,
            "decision_boundary_seconds": awaiting.decision_boundary_seconds,
            "provider_hold_wall_jump_hours": 6,
            "human_hold_wall_jump_days": 3,
            "time_view_unchanged": True,
            "ledger_status_while_waiting": budget["status"],
            "applied_effective_time_seconds": applied.effective_time_seconds,
            "earliest_new_assignment_seconds": min(
                item.window.start_seconds for item in proposal.assignments
            ),
            "decision_latency": latency.model_dump(mode="json"),
        },
    )
    after = h.services.get_snapshot(h.run_id)
    assert after.simulated_time_seconds == before["simulated_time_seconds"]
    h.coordinator.advance(h.run_id, 2701, "advance-a06-resume")
    started = [
        item
        for item in h.services.get_snapshot(h.run_id).attempts
        if item.actual_start_seconds == 2701
    ]
    assert started or all(item.window.start_seconds > 2701 for item in proposal.assignments)


def _blocked_fault_supervisor(h, workflow_id: str):
    decision = h.open_fault(workflow_id)
    gate = Gate()
    turns = with_gate(h.fault_turns(), PERSIST_TURN, gate)
    return decision, gate, turns


def test_a07_timeout_then_delayed_success_is_discarded(tmp_path: Path) -> None:
    h = harness(tmp_path, "fault")
    identity_before = h.identity()
    decision, gate, turns = _blocked_fault_supervisor(h, "workflow-s11-a07-timeout")
    factory = h.factory(decision, h.fault_task(), turns)
    # The deadline fires exactly when the provider call is held: no timing race.
    thread, box = run_in_thread(
        lambda: h.coordinator.run_supervisor(decision, factory, deadline=gate.entered)
    )
    assert gate.entered.wait(30)
    thread.join(30)
    step = box["result"]
    assert step.timed_out and step.outcome is None
    closed = h.coordinator.decision(h.run_id, decision.workflow_id)
    assert closed.status is DecisionStatus.CANCELLED and closed.outcome == "timed_out"
    gate.release.set()  # the delayed provider response now arrives with persist_proposal
    late = h.coordinator.wait_for_late_result(h.run_id, decision.workflow_id, 30)
    assert late is not None and late["late"] is True
    assert late["supervisor_terminal"] == WorkflowPhase.CANCELLED.value
    assert late["proposal_id"] is None
    assert not [
        item
        for item in h.services.export_lifecycle(h.run_id).proposals
        if item.workflow_id == decision.workflow_id
    ]
    assert h.identity() == identity_before
    audits = h.services.export_lifecycle(h.run_id).audits
    discarded = [item for item in audits if item.action == "late_supervisor_result_discarded"]
    assert len(discarded) == 1 and discarded[0].run_id == h.run_id
    # The decision cannot be reopened; a fresh decision is required.
    with pytest.raises(CoordinatorError) as excinfo:
        h.coordinator.open_decision(
            h.run_id, decision.workflow_id, kind=decision.kind, source=decision.source
        )
    assert excinfo.value.code is CoordinatorErrorCode.DECISION_CLOSED


def test_a07_durable_cancel_seen_by_another_process_blocks_late_persist(tmp_path: Path) -> None:
    """The in-memory flag is never set; only SQLite knows the workflow was cancelled."""

    h = harness(tmp_path, "fault")
    identity_before = h.identity()
    decision, gate, turns = _blocked_fault_supervisor(h, "workflow-s11-a07-durable")
    thread, box = run_in_thread(lambda: h.run_fault_supervisor(decision, turns))
    assert gate.entered.wait(30)
    other = h.reopen()  # a second backend process
    h.services.cancel_workflow(
        h.run_id, decision.workflow_id, reason_code="operator_cancelled_elsewhere"
    )
    del other
    gate.release.set()
    thread.join(30)
    step = box["result"]
    assert step.discarded
    assert step.outcome.terminal is WorkflowPhase.FAILED
    assert step.outcome.reason_code == "persistence_workflow_conflict"
    assert step.decision.status is DecisionStatus.CANCELLED
    assert not [
        item
        for item in h.services.export_lifecycle(h.run_id).proposals
        if item.workflow_id == decision.workflow_id
    ]
    assert h.identity() == identity_before
    assert any(
        item.action == "supervisor_result_discarded"
        for item in h.services.export_lifecycle(h.run_id).audits
    )


def test_a07_operator_cancel_during_provider_call(tmp_path: Path) -> None:
    h = harness(tmp_path, "fault")
    identity_before = h.identity()
    decision, gate, turns = _blocked_fault_supervisor(h, "workflow-s11-a07-cancel")
    thread, box = run_in_thread(lambda: h.run_fault_supervisor(decision, turns))
    assert gate.entered.wait(30)
    cancelled = h.coordinator.cancel(h.run_id, decision.workflow_id, actor="operator:alice")
    assert cancelled.status is ActionStatus.CANCELLED
    gate.release.set()
    thread.join(30)
    step = box["result"]
    assert step.discarded and step.outcome.terminal is WorkflowPhase.CANCELLED
    assert h.identity() == identity_before
    ledger = BudgetLedger(tmp_path / "provider_ledger.sqlite3")
    try:
        assert ledger.snapshot(decision.workflow_id).public_dict()["status"] == "cancelled"
    finally:
        ledger.close()


def test_a07_reset_then_delayed_callback_touches_neither_run(tmp_path: Path) -> None:
    h = harness(tmp_path, "fault")
    old_identity = h.identity()
    old_counts = lifecycle_counts(h.services, h.run_id)
    decision, gate, turns = _blocked_fault_supervisor(h, "workflow-s11-a07-reset")
    thread, box = run_in_thread(lambda: h.run_fault_supervisor(decision, turns))
    assert gate.entered.wait(30)
    reset = h.coordinator.reset_run(h.run_id, "run-breakdown-s11-reset")
    new_run = reset["new_run_id"]
    assert [item["status"] for item in reset["cancelled"]] == ["cancelled"]
    new_identity = h.identity(new_run)
    assert new_identity["state_version"] == 0
    gate.release.set()
    thread.join(30)
    step = box["result"]
    assert step.discarded
    assert step.decision.outcome == "run_reset"
    assert h.identity() == old_identity
    assert h.identity(new_run) == new_identity
    new_lifecycle = h.services.export_lifecycle(new_run)
    assert not new_lifecycle.proposals and not new_lifecycle.workflows
    assert not new_lifecycle.audits and not new_lifecycle.approvals
    old_after = lifecycle_counts(h.services, h.run_id)
    assert old_after["proposals"] == old_counts["proposals"]
    assert old_after["consumptions"] == old_counts["consumptions"]
    discarded = [
        item
        for item in h.services.export_lifecycle(h.run_id).audits
        if item.action == "supervisor_result_discarded"
    ]
    assert discarded and all(item.run_id == h.run_id for item in discarded)
    for action in (
        lambda: h.coordinator.advance(h.run_id, 2800, "advance-obsolete-run"),
        lambda: h.coordinator.open_decision(
            h.run_id, "workflow-s11-after-reset", kind=decision.kind, source=decision.source
        ),
    ):
        with pytest.raises(CoordinatorError) as excinfo:
            action()
        assert excinfo.value.code is CoordinatorErrorCode.RUN_SUPERSEDED
    assert h.identity() == old_identity
    record_evidence(
        "a07_reset_late_callback",
        {
            "old_run_id": h.run_id,
            "new_run_id": new_run,
            "cancelled_on_reset": reset["cancelled"],
            "late_terminal": step.outcome.terminal.value,
            "late_reason": step.outcome.reason_code,
            "decision_outcome": step.decision.outcome,
            "old_run_operational_identity_unchanged": True,
            "new_run_state_version": h.identity(new_run)["state_version"],
            "new_run_lifecycle_empty": True,
            "discard_audits_in_old_run": [item.action for item in discarded],
        },
    )


def test_a07_wall_timeout_while_worker_is_held(tmp_path: Path) -> None:
    """The plain wall timeout fires while the worker is provably held before any work."""

    h = harness(tmp_path, "fault")
    identity_before = h.identity()
    decision = h.open_fault("workflow-s11-a07-wall-timeout")
    inner = h.factory(decision, h.fault_task(), h.fault_turns())
    gate = Gate()

    def held_factory():
        gate()
        return inner()

    step = h.coordinator.run_supervisor(decision, held_factory, timeout_seconds=0.05)
    assert step.timed_out and gate.entered.wait(30)
    closed = h.coordinator.decision(h.run_id, decision.workflow_id)
    assert closed.outcome == "timed_out"
    assert closed.details["trigger"] == "timeout_seconds"
    gate.release.set()
    late = h.coordinator.wait_for_late_result(h.run_id, decision.workflow_id, 60)
    assert late is not None and late["supervisor_terminal"] == WorkflowPhase.FAILED.value
    assert late["supervisor_reason_code"] == "workflow_conflict"  # durable cancel won
    assert h.identity() == identity_before
    assert not [
        item
        for item in h.services.export_lifecycle(h.run_id).proposals
        if item.workflow_id == decision.workflow_id
    ]
