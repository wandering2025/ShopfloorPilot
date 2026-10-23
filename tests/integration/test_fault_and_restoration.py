"""S11-A04 fault interruption semantics and S11-A05 repair claims vs confirmed restoration."""

from __future__ import annotations

from pathlib import Path

import pytest
from recovery_support import PINNED, harness, lifecycle_counts, record_evidence

from shopfloor_pilot.agent.fake import ScriptedCall, ScriptedTurn
from shopfloor_pilot.domain.canonical import canonical_hash
from shopfloor_pilot.domain.models import (
    AttemptStatus,
    CandidateProposal,
    EventType,
    MachineStatus,
    ScheduleAssignment,
)
from shopfloor_pilot.services import ApplicationServiceError, ServiceErrorCode
from shopfloor_pilot.tools import WorkflowPhase
from shopfloor_pilot.workflows import (
    ActionStatus,
    CoordinatorError,
    CoordinatorErrorCode,
    DecisionKind,
    DecisionSource,
    DecisionStatus,
    check_run_invariants,
    invariants_passed,
)


def _attempt_map(snapshot) -> dict[str, object]:
    return {item.attempt_id: item for item in snapshot.attempts}


def test_a04_fault_preserves_history_fixes_running_and_restarts_full_duration(
    tmp_path: Path,
) -> None:
    h = harness(tmp_path, "fault")
    fault_checkpoint = h.services.get_checkpoint(h.run_id)
    fault_time = fault_checkpoint.snapshot.simulated_time_seconds
    assert fault_time == 2700
    # The checkpoint before the fault group: what was completed/running before 2700.
    fault_event = next(
        item
        for item in h.services.list_events(h.run_id)
        if item.event.event_type is EventType.MACHINE_DOWN
    )
    pre_fault = h.services.get_checkpoint(h.run_id, fault_event.base_state_version).snapshot
    completed_before = {
        item.attempt_id: canonical_hash(item)
        for item in pre_fault.attempts
        if item.status is AttemptStatus.COMPLETED
    }
    at_fault = _attempt_map(fault_checkpoint.snapshot)
    interrupted = [item for item in at_fault.values() if item.status is AttemptStatus.INTERRUPTED]
    running = [item for item in at_fault.values() if item.status is AttemptStatus.RUNNING]
    assert len(interrupted) == 1 and interrupted[0].machine_id == "CNC02"
    faulted = interrupted[0]
    assert faulted.actual_end_seconds == fault_time
    assert faulted.consumed_seconds == fault_time - faulted.actual_start_seconds > 0
    assert running and all(item.machine_id != "CNC02" for item in running)
    assert completed_before, "fixture must have completed history before the fault"

    decision = h.open_fault()
    assert decision.decision_time_seconds == fault_time
    step = h.run_fault_supervisor(decision)
    assert step.outcome.terminal is WorkflowPhase.AWAITING_APPROVAL
    assert step.operational_state_unchanged
    awaiting = step.decision
    proposal = next(
        item
        for item in h.services.export_lifecycle(h.run_id).proposals
        if item.proposal_id == awaiting.pending_proposal_id
    )
    by_attempt = {item.attempt_id: item for item in proposal.assignments}
    for attempt in running:
        assert attempt.attempt_id not in by_attempt, "running work is fixed, not reassigned"

    applied = h.approve_apply(awaiting)
    assert applied.status is ActionStatus.APPLIED
    assert applied.effective_time_seconds == fault_time + 1
    after_apply = _attempt_map(h.services.get_snapshot(h.run_id))
    for attempt in running:
        moved = after_apply[attempt.attempt_id]
        assert (moved.machine_id, moved.actual_start_seconds, moved.scheduled_window) == (
            attempt.machine_id,
            attempt.actual_start_seconds,
            attempt.scheduled_window,
        )
    target = proposal.objective.makespan_seconds
    h.coordinator.advance(h.run_id, target, "advance-a04-final")

    final = h.services.get_snapshot(h.run_id)
    final_attempts = _attempt_map(final)
    for attempt_id, digest in completed_before.items():
        assert canonical_hash(final_attempts[attempt_id]) == digest, "completed work changed"
    for attempt in running:
        done = final_attempts[attempt.attempt_id]
        assert done.status is AttemptStatus.COMPLETED
        assert done.machine_id == attempt.machine_id
        assert done.actual_start_seconds == attempt.actual_start_seconds
        assert (
            done.actual_end_seconds
            == attempt.actual_start_seconds + attempt.planned_duration_seconds
        )
    still_interrupted = final_attempts[faulted.attempt_id]
    assert still_interrupted == faulted, "the interrupted attempt keeps its exact record"
    successors = [
        item for item in final.attempts if item.restart_of_attempt_id == faulted.attempt_id
    ]
    assert len(successors) == 1
    successor = successors[0]
    assert successor.attempt_id != faulted.attempt_id
    assert successor.attempt_number == faulted.attempt_number + 1
    assert successor.operation_id == faulted.operation_id
    assert successor.planned_duration_seconds == faulted.planned_duration_seconds == 900
    assert successor.machine_id == "CNC03"
    assert successor.status is AttemptStatus.COMPLETED
    assert successor.actual_start_seconds > fault_time
    assert successor.actual_end_seconds - successor.actual_start_seconds == 900

    metrics = h.services.calculate_run_metrics(h.run_id)
    assert metrics.wasted_processing_seconds == faulted.consumed_seconds == 180
    by_machine = {item.machine_id: item.wasted_processing_seconds for item in metrics.machines}
    assert by_machine["CNC02"] == 180 and sum(by_machine.values()) == 180
    h.services.export_replay_bundle(h.run_id, tmp_path / "events.jsonl")
    replayed = h.services.replay_bundle(tmp_path / "events.jsonl")
    assert replayed.actual_metrics.wasted_processing_seconds == 180
    assert replayed.final_snapshot_hash == h.services.get_checkpoint(h.run_id).snapshot_hash
    results = check_run_invariants(h.services, h.run_id)
    assert invariants_passed(results), [item for item in results if not item.passed]
    record_evidence(
        "a04_fault_interruption",
        {
            "fault_time_seconds": fault_time,
            "completed_before_fault": len(completed_before),
            "completed_unchanged": True,
            "unaffected_running": [
                {
                    "attempt_id": item.attempt_id,
                    "machine_id": item.machine_id,
                    "actual_start_seconds": item.actual_start_seconds,
                    "scheduled_window": item.scheduled_window.model_dump(mode="json"),
                    "final_actual_end_seconds": final_attempts[item.attempt_id].actual_end_seconds,
                }
                for item in running
            ],
            "interrupted": faulted.model_dump(mode="json"),
            "successor": successor.model_dump(mode="json"),
            "wasted_processing_seconds": metrics.wasted_processing_seconds,
            "wasted_by_machine": by_machine,
            "replayed_wasted_processing_seconds": replayed.actual_metrics.wasted_processing_seconds,
            "invariants": {item.code: item.passed for item in results},
        },
    )


def _claim_turns(h, claims: list[dict[str, object]]) -> list[ScriptedTurn]:
    turns = h.fault_turns()
    good = turns[-1]
    bad_args = dict(good.calls[0].arguments)
    bad_args["claims"] = claims
    return [
        *turns[:-1],
        ScriptedTurn(calls=(ScriptedCall("persist_proposal", bad_args),)),
        ScriptedTurn(calls=(ScriptedCall("restore_machine", {**PINNED, "machine_id": "CNC02"}),)),
        good,
    ]


def test_a05_model_repair_claim_never_restores_capacity(tmp_path: Path) -> None:
    h = harness(tmp_path, "fault")
    decision = h.open_fault()
    repair_claim = [
        {
            "kind": "observation",
            "text": "CNC02 has been repaired and is now restored to service.",
            "evidence_ids": ["$evidence_first:observation"],
        },
        {
            "kind": "plan",
            "text": "Use CNC02 again because its repair is already confirmed.",
            "evidence_ids": ["$evidence_first:validation"],
        },
    ]
    step = h.run_fault_supervisor(decision, _claim_turns(h, repair_claim))
    assert step.outcome.terminal is WorkflowPhase.AWAITING_APPROVAL
    assert step.operational_state_unchanged
    snapshot = h.services.get_snapshot(h.run_id)
    machines = {item.machine_id: item.status for item in snapshot.machines}
    assert machines["CNC02"] is MachineStatus.DOWN, "a claim alone never restores capacity"
    events = [item.event.event_type for item in h.services.list_events(h.run_id)]
    assert EventType.MACHINE_RESTORED not in events
    proposal = next(
        item
        for item in h.services.export_lifecycle(h.run_id).proposals
        if item.proposal_id == step.decision.pending_proposal_id
    )
    assert all(item.machine_id != "CNC02" for item in proposal.assignments)
    audit = next(
        item
        for item in h.services.export_lifecycle(h.run_id).audits
        if item.action == "supervisor_outcome_recorded"
    )
    assert audit.details["repair_recommendation"]["confirmed_restoration"] is False
    assert all("restored" not in str(item["text"]).lower() for item in audit.details["claims"])

    # A plan that uses CNC02 while it is down fails independent validation.
    forged_assignments = tuple(
        ScheduleAssignment.model_validate({**item.model_dump(mode="python"), "machine_id": "CNC02"})
        if item.machine_id == "CNC03"
        else item
        for item in proposal.assignments
    )
    forged = CandidateProposal.create(
        **{
            **proposal.model_dump(mode="python", exclude={"proposal_hash", "created_at_utc"}),
            "proposal_id": "proposal-forged-cnc02",
            "assignments": forged_assignments,
        }
    )
    with pytest.raises(ApplicationServiceError) as excinfo:
        h.services.save_validated_proposal(forged)
    assert excinfo.value.code is ServiceErrorCode.INVALID_CANDIDATE

    applied = h.approve_apply(step.decision)
    assert applied.status is ActionStatus.APPLIED
    applied_revision = h.services.get_snapshot(h.run_id).plan_revision

    for actor in ("model:deepseek", "policy:deterministic", "supervisor", ""):
        before = h.identity()
        with pytest.raises(CoordinatorError) as refused:
            h.coordinator.confirm_restoration(
                h.run_id,
                "CNC02",
                confirmation_id="by-" + (actor or "nobody").replace(":", "-"),
                actor=actor,
            )
        assert refused.value.code is CoordinatorErrorCode.INVALID_ACTOR
        assert h.identity() == before

    restored = h.coordinator.confirm_restoration(
        h.run_id, "CNC02", confirmation_id="repair-ticket-17", actor="operator:alice"
    )
    after = h.services.get_snapshot(h.run_id)
    assert {item.machine_id: item.status for item in after.machines}[
        "CNC02"
    ] is MachineStatus.AVAILABLE
    others_before = {
        item.machine_id: item.status for item in snapshot.machines if item.machine_id != "CNC02"
    }
    assert {k: v for k, v in machines.items() if k != "CNC02"} == others_before
    assert after.plan_revision == applied_revision, "restoration applies no schedule"
    restore_events = [
        item for item in restored.events if item.event.event_type is EventType.MACHINE_RESTORED
    ]
    assert len(restore_events) == 1 and restore_events[0].event.payload["machine_id"] == "CNC02"
    assert any(
        item.action == "restoration_confirmed-repair-ticket-17" and item.actor == "operator:alice"
        for item in h.services.export_lifecycle(h.run_id).audits
    )
    with pytest.raises(CoordinatorError) as twice:
        h.coordinator.confirm_restoration(
            h.run_id, "CNC02", confirmation_id="repair-ticket-18", actor="operator:alice"
        )
    assert twice.value.code is CoordinatorErrorCode.INVALID_RESTORATION

    # Any later schedule using the restored capacity still needs validation + approval.
    counts = lifecycle_counts(h.services, h.run_id)
    replan = h.coordinator.open_decision(
        h.run_id,
        "workflow-s11-a05-replan",
        kind=DecisionKind.REPLAN,
        source=DecisionSource.DETERMINISTIC_BASELINE,
    )

    def generate(current):
        return h.services.generate_proposal(
            h.run_id,
            current.workflow_id,
            "proposal-a05-replan",
            not_before_seconds=current.decision_boundary_seconds,
            created_at_utc=h.wall(),
        )

    replan, result = h.coordinator.propose_deterministic(replan, generate)
    assert result.candidate is not None and replan.status is DecisionStatus.AWAITING_APPROVAL
    assert h.services.get_snapshot(h.run_id).plan_revision == applied_revision
    assert (
        lifecycle_counts(h.services, h.run_id)["plan_applied_events"]
        == counts["plan_applied_events"]
    )
    with pytest.raises(ApplicationServiceError) as unapproved:
        h.services.apply_approved_plan(
            h.run_id,
            replan.pending_proposal_id,
            replan.pending_proposal_hash,
            f"approval-{step.decision.pending_proposal_id}",
            "apply-reuse-old-approval",
        )
    assert unapproved.value.code is ServiceErrorCode.APPROVAL_BINDING_MISMATCH
    reapplied = h.approve_apply(replan, actor="human:bob")
    assert reapplied.status is ActionStatus.APPLIED
    record_evidence(
        "a05_restoration_authority",
        {
            "cnc02_after_model_repair_claim": machines["CNC02"].value,
            "machine_restored_events_before_confirmation": 0,
            "rejected_claim_audit": audit.details["repair_recommendation"],
            "forged_cnc02_plan": ServiceErrorCode.INVALID_CANDIDATE.value,
            "refused_restoration_actors": [
                "model:deepseek",
                "policy:deterministic",
                "supervisor",
                "",
            ],
            "confirmed_by": "operator:alice",
            "restoration_event_count": len(restore_events),
            "plan_revision_unchanged_by_restoration": True,
            "replan_status_before_approval": replan.status.value,
            "old_approval_reuse": unapproved.value.code.value,
            "replan_applied_after_fresh_approval": reapplied.status.value,
        },
    )
    assert invariants_passed(check_run_invariants(h.services, h.run_id))


def test_s11_probe_regression_completion_due_at_the_decision_boundary(tmp_path: Path) -> None:
    """Live probe machine_breakdown seed 7: a completion at T+1 made the decision unapplyable."""

    h = harness(tmp_path, "fault")
    qc = next(
        item
        for item in h.services.get_snapshot(h.run_id).attempts
        if item.status is AttemptStatus.RUNNING and item.machine_id == "QC01"
    )
    due = qc.actual_start_seconds + qc.planned_duration_seconds
    h.coordinator.advance(h.run_id, due - 1, "advance-to-just-before-completion")
    before = h.identity()
    with pytest.raises(CoordinatorError) as blocked:
        h.open_fault()
    assert blocked.value.code is CoordinatorErrorCode.DECISION_BOUNDARY_BLOCKED
    assert blocked.value.details["advance_to_seconds"] == due
    assert h.identity() == before, "refusing to open changes nothing"
    assert h.coordinator.settle_decision_boundary(h.run_id, "advance-a04-settle") == [due]
    decision = h.open_fault()
    assert decision.decision_time_seconds == due
    step = h.run_fault_supervisor(decision)
    applied = h.approve_apply(step.decision)
    assert applied.status is ActionStatus.APPLIED
    assert applied.effective_time_seconds == due + 1
    assert invariants_passed(check_run_invariants(h.services, h.run_id))
