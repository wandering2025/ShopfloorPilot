"""S11-A08 provider/solver/transaction failures and S11-A09 horizon vs soft due dates."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from recovery_support import harness, lifecycle_counts, record_evidence

from shopfloor_pilot.agent.fake import ScriptedCall, ScriptedTurn
from shopfloor_pilot.budgets import ProviderLimits
from shopfloor_pilot.domain.models import (
    CandidateProposal,
    Order,
    OrderDelta,
    OrderDeltaKind,
    OrderStatus,
    ScheduleAssignment,
    TimeWindow,
)
from shopfloor_pilot.providers.errors import ProviderError, ProviderErrorCode
from shopfloor_pilot.scheduling import SolveOutcome
from shopfloor_pilot.services import ApplicationServiceError, ServiceErrorCode
from shopfloor_pilot.tools import WorkflowPhase
from shopfloor_pilot.validation import ViolationCode
from shopfloor_pilot.workflows import (
    ActionStatus,
    CoordinatorError,
    CoordinatorErrorCode,
    DecisionKind,
    DecisionSource,
    DecisionStatus,
    approval_request,
    check_run_invariants,
    invariants_passed,
)


def _outage() -> list[ScriptedTurn]:
    error = ProviderError(ProviderErrorCode.CONNECTION, "synthetic outage")
    return [ScriptedTurn(failure=error)] * 3  # first attempt plus two bounded retries


def _malformed() -> list[ScriptedTurn]:
    return [
        ScriptedTurn(calls=(ScriptedCall("get_observable_snapshot", raw_arguments="{not json"),))
    ] * 3


def _solver_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    import shopfloor_pilot.tools.supervisor_tools as tools

    real = tools.solve_schedule

    def unavailable(*args, **kwargs):
        result = real(*args, **kwargs)
        return result.model_copy(
            update={"outcome": SolveOutcome.UNKNOWN, "candidate": None, "validation": None}
        )

    monkeypatch.setattr(tools, "solve_schedule", unavailable)


CASES = {
    "outage": ("fault", "provider_failure", WorkflowPhase.FAILED),
    "malformed": ("fault", "repeated_invalid_turns", WorkflowPhase.CYCLE_DETECTED),
    "budget": ("fault", "budget_exhausted", WorkflowPhase.BUDGET_EXHAUSTED),
    "solver_unavailable": ("fault", "solver_unknown", WorkflowPhase.FAILED),
    "infeasible": ("no_feasible", "no_feasible_schedule", WorkflowPhase.INFEASIBLE),
    "urgent_outage": ("urgent", "provider_failure", WorkflowPhase.FAILED),
}


@pytest.mark.parametrize("case", sorted(CASES))
def test_a08_failures_preserve_the_paused_snapshot(
    case: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    point, reason, terminal = CASES[case]
    h = harness(tmp_path, point)
    kwargs = {}
    if case == "urgent_outage":
        decision = h.open_order()
        task, _turns = h.order_task_and_turns()
        turns = _outage()
    else:
        decision = h.open_fault()
        task = h.fault_task()
        turns = {
            "outage": _outage,
            "malformed": _malformed,
        }.get(case, h.fault_turns)()
    if case == "budget":
        kwargs["limits"] = ProviderLimits(max_tool_executions=2)
    if case == "solver_unavailable":
        _solver_unavailable(monkeypatch)
    paused_identity = h.identity()
    counts = lifecycle_counts(h.services, h.run_id)
    step = h.coordinator.run_supervisor(decision, h.factory(decision, task, turns, **kwargs))
    assert step.outcome.terminal is terminal
    assert step.outcome.reason_code == reason
    # Applied schedule, orders, machines, time, version, and revision are untouched.
    assert h.identity() == paused_identity
    assert step.operational_state_unchanged
    after = lifecycle_counts(h.services, h.run_id)
    assert after["events"] == counts["events"]
    assert after["consumptions"] == counts["consumptions"]
    assert after["proposals"] == counts["proposals"]
    paused = h.coordinator.decision(h.run_id, decision.workflow_id)
    assert paused.status is DecisionStatus.PAUSED
    assert paused.reason_code == reason
    assert paused.details["operational_state_preserved"] is True
    status = h.coordinator.run_status(h.run_id)
    assert status["state"] == "paused_blocked" and status["reason_code"] == reason
    if case == "urgent_outage":
        orders = {item.order_id for item in h.services.get_snapshot(h.run_id).orders}
        assert not any(item.startswith("order-req-") for item in orders)
    record_evidence(
        f"a08_{case}",
        {
            "case": case,
            "terminal": step.outcome.terminal.value,
            "reason_code": step.outcome.reason_code,
            "decision_status": paused.status.value,
            "run_status": status["state"],
            "operational_identity_unchanged": True,
            "events_before_after": [counts["events"], after["events"]],
            "proposals_before_after": [counts["proposals"], after["proposals"]],
        },
    )


def test_a08_fallback_is_explicit_labeled_and_still_needs_approval(tmp_path: Path) -> None:
    h = harness(tmp_path, "fault")
    decision = h.open_fault()
    h.coordinator.run_supervisor(decision, h.factory(decision, h.fault_task(), _outage()))
    paused_identity = h.identity()
    for actor in ("model:deepseek", "policy:deterministic"):
        with pytest.raises(CoordinatorError) as excinfo:
            h.coordinator.run_deterministic_fallback(
                h.run_id, decision.workflow_id, workflow_id="workflow-s11-fb", actor=actor
            )
        assert excinfo.value.code is CoordinatorErrorCode.INVALID_ACTOR
    fallback, result = h.coordinator.run_deterministic_fallback(
        h.run_id, decision.workflow_id, workflow_id="workflow-s11-fallback", actor="human:alice"
    )
    assert result.candidate is not None
    assert fallback.source is DecisionSource.DETERMINISTIC_FALLBACK
    assert fallback.fallback_for_workflow_id == decision.workflow_id
    assert fallback.status is DecisionStatus.AWAITING_APPROVAL
    assert h.identity() == paused_identity, "a fallback proposal is not an application"
    audits = h.services.export_lifecycle(h.run_id).audits
    selected = next(item for item in audits if item.action == "deterministic_fallback_selected")
    assert selected.actor == "human:alice"
    assert selected.details["counts_as_llm_success"] is False
    applied = h.approve_apply(fallback)
    assert applied.status is ActionStatus.APPLIED
    # The supervisor decision itself stays paused/failed; nothing relabels it a success.
    assert h.coordinator.decision(h.run_id, decision.workflow_id).status is DecisionStatus.PAUSED
    assert invariants_passed(check_run_invariants(h.services, h.run_id))


def test_a08_supervisor_decision_is_never_silently_filled(tmp_path: Path) -> None:
    h = harness(tmp_path, "fault")
    decision = h.open_fault()
    with pytest.raises(CoordinatorError) as excinfo:
        h.coordinator.propose_deterministic(decision, lambda _d: None)
    assert excinfo.value.code is CoordinatorErrorCode.FALLBACK_NOT_ALLOWED


@pytest.mark.parametrize(
    "failpoint", ["after_event_insert", "after_snapshot_insert", "before_commit"]
)
def test_a08_transaction_failure_leaves_no_partial_order_or_schedule(
    failpoint: str, tmp_path: Path
) -> None:
    h = harness(tmp_path, "urgent")
    decision = h.open_order()
    task, turns = h.order_task_and_turns()
    step = h.coordinator.run_supervisor(decision, h.factory(decision, task, turns))
    awaiting = step.decision
    assert awaiting.status is DecisionStatus.AWAITING_APPROVAL
    approval = h.coordinator.approve(
        h.run_id,
        awaiting.workflow_id,
        proposal_id=awaiting.pending_proposal_id,
        proposal_hash=awaiting.pending_proposal_hash,
        actor="human:alice",
    )
    before = h.identity()
    counts = lifecycle_counts(h.services, h.run_id)

    def explode(point: str) -> None:
        if point == failpoint:
            raise RuntimeError(f"injected failure at {point}")

    failed = h.coordinator.apply(
        h.run_id,
        awaiting.workflow_id,
        proposal_id=awaiting.pending_proposal_id,
        proposal_hash=awaiting.pending_proposal_hash,
        approval_id=approval.approval_id,
        _failure_injector=explode,
    )
    assert failed.status is ActionStatus.FAILED and failed.reason_code == "transaction_failed"
    assert h.identity() == before
    assert lifecycle_counts(h.services, h.run_id) == counts
    orders = {item.order_id for item in h.services.get_snapshot(h.run_id).orders}
    assert not any(item.startswith("order-req-") for item in orders), "no partial order"
    assert h.coordinator.decision(h.run_id, awaiting.workflow_id).status is (
        DecisionStatus.AWAITING_APPROVAL
    )
    retried = h.coordinator.apply(
        h.run_id,
        awaiting.workflow_id,
        proposal_id=awaiting.pending_proposal_id,
        proposal_hash=awaiting.pending_proposal_hash,
        approval_id=approval.approval_id,
    )
    assert retried.status is ActionStatus.APPLIED
    group = [
        item
        for item in h.services.list_events(h.run_id)
        if item.mutation_id == retried.details["mutation_id"]
    ]
    assert [item.event.event_type.value for item in group] == ["order_registered", "plan_applied"]
    assert {item.event.simulated_time_seconds for item in group} == {3600}
    assert lifecycle_counts(h.services, h.run_id)["consumptions"] == counts["consumptions"] + 1


def test_a09_hard_horizon_violation_is_rejected(tmp_path: Path) -> None:
    h = harness(tmp_path, "fault")
    decision = h.run_fault_supervisor(h.open_fault()).decision
    lifecycle = h.services.export_lifecycle(h.run_id)
    proposal = next(
        item for item in lifecycle.proposals if item.proposal_id == decision.pending_proposal_id
    )
    horizon = h.services.get_snapshot(h.run_id).hard_horizon_seconds
    last = max(proposal.assignments, key=lambda item: item.window.end_seconds)
    duration = last.window.end_seconds - last.window.start_seconds
    shifted = ScheduleAssignment(
        operation_id=last.operation_id,
        attempt_id=last.attempt_id,
        machine_id=last.machine_id,
        window=TimeWindow(start_seconds=horizon - duration + 60, end_seconds=horizon + 60),
    )
    forged = CandidateProposal.create(
        **{
            **proposal.model_dump(mode="python", exclude={"proposal_hash", "created_at_utc"}),
            "proposal_id": "proposal-beyond-horizon",
            "assignments": tuple(
                shifted if item.attempt_id == last.attempt_id else item
                for item in proposal.assignments
            ),
        }
    )
    before = h.identity()
    with pytest.raises(ApplicationServiceError) as excinfo:
        h.services.save_validated_proposal(forged)
    assert excinfo.value.code is ServiceErrorCode.INVALID_CANDIDATE
    codes = {item.code for item in excinfo.value.validation.violations}
    assert ViolationCode.HARD_HORIZON_VIOLATION in codes
    record_evidence(
        "a09_hard_horizon",
        {
            "hard_horizon_seconds": horizon,
            "forged_window_end_seconds": horizon + 60,
            "service_code": excinfo.value.code.value,
            "violation_codes": sorted(item.value for item in codes),
        },
    )
    assert h.identity() == before
    assert "proposal-beyond-horizon" not in {
        item.proposal_id for item in h.services.export_lifecycle(h.run_id).proposals
    }


def test_a09_soft_due_date_miss_stays_feasible_and_is_reported(tmp_path: Path) -> None:
    h = harness(tmp_path, "urgent")
    snapshot = h.services.get_snapshot(h.run_id)
    template = next(item for item in snapshot.orders)
    tight = Order(
        order_id="order-typed-tight-due",
        product_id=template.product_id,
        quantity=1,
        release_time_seconds=3600,
        due_time_seconds=3660,  # far shorter than the route: a soft miss
        priority_weight=10,
        status=OrderStatus.PENDING,
    )
    decision = h.coordinator.open_decision(
        h.run_id,
        "workflow-s11-a09-typed-order",
        kind=DecisionKind.URGENT_ORDER,
        source=DecisionSource.DETERMINISTIC_BASELINE,
        with_order_delta=True,
        details={"typed_operator_input": True},
    )

    def generate(current):
        return h.services.generate_proposal(
            h.run_id,
            current.workflow_id,
            "proposal-a09-typed-order",
            proposed_order_deltas=(OrderDelta(kind=OrderDeltaKind.ADD, order=tight),),
            not_before_seconds=current.decision_boundary_seconds,
            created_at_utc=h.wall(),
        )

    decision, result = h.coordinator.propose_deterministic(decision, generate)
    assert result.outcome in (SolveOutcome.OPTIMAL, SolveOutcome.FEASIBLE)
    assert result.validation.is_valid, "a due-date miss alone is not infeasible"
    request = approval_request(h.services, decision)
    warning = next(
        item for item in request["due_date_warnings"] if item["order_id"] == tight.order_id
    )
    assert warning["hard_constraint"] is False
    assert warning["predicted_completion_seconds"] > tight.due_time_seconds
    assert warning["predicted_weighted_tardiness_seconds"] == 10 * (
        warning["predicted_completion_seconds"] - tight.due_time_seconds
    )
    applied = h.approve_apply(decision)
    assert applied.status is ActionStatus.APPLIED
    target = result.candidate.objective.makespan_seconds
    h.coordinator.advance(h.run_id, target, "advance-a09-final")
    metrics = h.services.calculate_run_metrics(h.run_id)
    order = next(item for item in metrics.orders if item.order_id == tight.order_id)
    assert order.actual_weighted_tardiness_seconds is not None
    assert order.actual_weighted_tardiness_seconds > 0
    assert metrics.actual_weighted_tardiness_seconds >= order.actual_weighted_tardiness_seconds
    record_evidence(
        "a09_soft_due_date",
        {
            "order_id": tight.order_id,
            "due_time_seconds": tight.due_time_seconds,
            "solver_outcome": result.outcome.value,
            "validation_is_valid": result.validation.is_valid,
            "warning": warning,
            "actual_weighted_tardiness_seconds": order.actual_weighted_tardiness_seconds,
        },
    )
    assert invariants_passed(check_run_invariants(h.services, h.run_id))


def test_a11_regression_digest_stops_evidence_loop_after_diagnosis(tmp_path: Path) -> None:
    """Live attempt 1 re-gathered evidence after diagnosis and ran out of tokens."""

    h = harness(tmp_path, "fault")
    decision = h.open_fault()
    step = h.run_fault_supervisor(decision)
    requests = h.sessions[-1].workflow._provider.requests  # noqa: SLF001 - scripted fake
    next_steps = []
    for request in requests:
        digest = json.loads(request.messages[1].content)
        next_steps.append((digest["workflow"]["phase"], digest["next_step"]))
    after_diagnosis = [
        text for phase, text in next_steps if phase == "gather_evidence" and "registered" in text
    ]
    assert after_diagnosis and "solve_schedule" in after_diagnosis[0]
    assert "do not repeat evidence tools" in after_diagnosis[0]
    assert step.outcome.terminal is WorkflowPhase.AWAITING_APPROVAL
