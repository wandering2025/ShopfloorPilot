"""Machine-readable state invariants for recovery runs.

These checks read only persisted authority (checkpoints, events, lifecycle) and
re-run the independent S03 validator. A run's success is judged by these results,
not by a status string.
"""

from __future__ import annotations

from typing import Any

from shopfloor_pilot.domain.canonical import canonical_hash
from shopfloor_pilot.domain.models import AttemptStatus, EventType
from shopfloor_pilot.events import replay_events
from shopfloor_pilot.services import ApplicationServices
from shopfloor_pilot.validation import validate_candidate

from .models import DECISION_MODE, DecisionStatus, InvariantResult

ALLOWED_APPROVAL_ACTOR_PREFIXES = ("human:", "operator:", "policy:")


def _result(code: str, failures: list[Any], **details: Any) -> InvariantResult:
    return InvariantResult(
        code=code,
        passed=not failures,
        details={**details, "failures": failures[:20], "failure_count": len(failures)},
    )


def check_run_invariants(services: ApplicationServices, run_id: str) -> tuple[InvariantResult, ...]:
    current = services.get_checkpoint(run_id)
    version = current.snapshot.state_version
    checkpoints = [
        services.get_checkpoint(run_id, state_version=item) for item in range(version + 1)
    ]
    events = services.list_events(run_id)
    lifecycle = services.export_lifecycle(run_id)
    proposals = {item.proposal_id: item for item in lifecycle.proposals}
    approvals = {item.approval_id: item for item in lifecycle.approvals}
    results: list[InvariantResult] = []

    # 1. Every applied plan passes the independent validator against its base.
    failures: list[Any] = []
    for consumption in lifecycle.approval_consumptions:
        proposal = proposals[consumption.proposal_id]
        base = checkpoints[proposal.base_state_version]
        report = validate_candidate(
            base.snapshot,
            services.catalog,
            proposal,
            projected_order_deltas=proposal.proposed_order_deltas,
            projected_resource_deltas=proposal.proposed_resource_deltas,
        )
        if not report.is_valid:
            failures.append(
                {
                    "proposal_id": proposal.proposal_id,
                    "violations": [item.code.value for item in report.violations],
                }
            )
    results.append(
        _result(
            "applied_plans_pass_independent_validator",
            failures,
            applied_proposal_count=len(lifecycle.approval_consumptions),
        )
    )

    # 2. Exactly one plan_applied event per consumed approval, one consumption each.
    plan_mutations = [
        item.mutation_id for item in events if item.event.event_type is EventType.PLAN_APPLIED
    ]
    consumed_mutations = [item.mutation_id for item in lifecycle.approval_consumptions]
    failures = []
    if sorted(plan_mutations) != sorted(consumed_mutations):
        failures.append(
            {"plan_mutations": plan_mutations, "consumed_mutations": consumed_mutations}
        )
    consumed_proposals = [item.proposal_id for item in lifecycle.approval_consumptions]
    if len(consumed_proposals) != len(set(consumed_proposals)):
        failures.append({"duplicate_consumed_proposals": consumed_proposals})
    results.append(
        _result(
            "one_plan_application_per_consumed_approval",
            failures,
            plan_applied_events=len(plan_mutations),
            consumed_approvals=len(consumed_mutations),
        )
    )

    # 3. Only humans, operators, or an explicitly named batch policy approve.
    failures = [
        {"approval_id": item.approval_id, "actor": item.actor}
        for item in lifecycle.approvals
        if not item.actor.startswith(ALLOWED_APPROVAL_ACTOR_PREFIXES)
    ]
    results.append(
        _result(
            "approvals_by_human_or_named_policy_only",
            failures,
            consumed_actors=sorted(
                {approvals[item.approval_id].actor for item in lifecycle.approval_consumptions}
            ),
        )
    )

    # 4. Completed work never changes once completed.
    failures = []
    first_completed: dict[str, tuple[int, str]] = {}
    for checkpoint in checkpoints:
        attempts = {item.attempt_id: item for item in checkpoint.snapshot.attempts}
        for attempt_id, (since, digest) in first_completed.items():
            later = attempts.get(attempt_id)
            if later is None or canonical_hash(later) != digest:
                failures.append(
                    {
                        "attempt_id": attempt_id,
                        "completed_at_version": since,
                        "changed_at_version": checkpoint.snapshot.state_version,
                    }
                )
        for attempt in checkpoint.snapshot.attempts:
            if (
                attempt.status is AttemptStatus.COMPLETED
                and attempt.attempt_id not in first_completed
            ):
                first_completed[attempt.attempt_id] = (
                    checkpoint.snapshot.state_version,
                    canonical_hash(attempt),
                )
    results.append(
        _result("completed_work_never_changes", failures, completed_attempts=len(first_completed))
    )

    # 5. A plan application never moves unaffected running work.
    failures = []
    checked = 0
    plan_versions = sorted(
        {
            item.resulting_state_version
            for item in events
            if item.event.event_type is EventType.PLAN_APPLIED
        }
    )
    for resulting in plan_versions:
        before = {item.attempt_id: item for item in checkpoints[resulting - 1].snapshot.attempts}
        after = {item.attempt_id: item for item in checkpoints[resulting].snapshot.attempts}
        for attempt_id, attempt in before.items():
            if attempt.status is not AttemptStatus.RUNNING:
                continue
            checked += 1
            moved = after.get(attempt_id)
            if moved is None or (
                moved.status,
                moved.machine_id,
                moved.actual_start_seconds,
                moved.scheduled_window,
            ) != (
                attempt.status,
                attempt.machine_id,
                attempt.actual_start_seconds,
                attempt.scheduled_window,
            ):
                failures.append({"attempt_id": attempt_id, "plan_version": resulting})
    results.append(
        _result("unaffected_running_work_fixed", failures, running_attempts_checked=checked)
    )

    # 6. Interrupted attempts keep their waste and restart for the full duration.
    final = current.snapshot
    by_id = {item.attempt_id: item for item in final.attempts}
    successors = {
        item.restart_of_attempt_id: item for item in final.attempts if item.restart_of_attempt_id
    }
    failures = []
    interrupted = [item for item in final.attempts if item.status is AttemptStatus.INTERRUPTED]
    waste = 0
    for attempt in interrupted:
        assert attempt.actual_start_seconds is not None and attempt.actual_end_seconds is not None
        elapsed = attempt.actual_end_seconds - attempt.actual_start_seconds
        waste += elapsed
        successor = successors.get(attempt.attempt_id)
        if attempt.consumed_seconds != elapsed:
            failures.append({"attempt_id": attempt.attempt_id, "reason": "consumed_mismatch"})
        if successor is not None and (
            successor.attempt_id == attempt.attempt_id
            or successor.operation_id != attempt.operation_id
            or successor.planned_duration_seconds != attempt.planned_duration_seconds
            or successor.attempt_number != attempt.attempt_number + 1
        ):
            failures.append(
                {"attempt_id": attempt.attempt_id, "reason": "successor_not_full_restart"}
            )
    orphan_links = [
        item.attempt_id
        for item in final.attempts
        if item.restart_of_attempt_id is not None and item.restart_of_attempt_id not in by_id
    ]
    failures.extend(
        {"attempt_id": item, "reason": "dangling_restart_link"} for item in orphan_links
    )
    results.append(
        _result(
            "interrupted_attempts_restart_full_duration",
            failures,
            interrupted_attempts=[item.attempt_id for item in interrupted],
            restarted=[
                successors[item.attempt_id].attempt_id
                for item in interrupted
                if item.attempt_id in successors
            ],
        )
    )

    # 7. KPI accounting succeeds and counts wasted work exactly once.
    failures = []
    metrics_waste: int | None = None
    try:
        metrics = services.calculate_run_metrics(run_id)
        metrics_waste = metrics.wasted_processing_seconds
        if metrics_waste != waste:
            failures.append({"metrics_waste": metrics_waste, "attempt_waste": waste})
    except Exception as exc:  # metrics reject overlap, work while down, etc.
        failures.append({"metrics_error": type(exc).__name__, "message": str(exc)[:200]})
    results.append(
        _result(
            "wasted_work_counted_once",
            failures,
            wasted_processing_seconds=metrics_waste,
            interrupted_elapsed_seconds=waste,
        )
    )

    # 8. Event replay from version zero reproduces the current checkpoint.
    failures = []
    replayed = replay_events(checkpoints[0], events)
    if replayed.snapshot_hash != current.snapshot_hash:
        failures.append({"replayed": replayed.snapshot_hash, "current": current.snapshot_hash})
    results.append(
        _result(
            "replay_reproduces_current_checkpoint",
            failures,
            snapshot_hash=current.snapshot_hash,
            event_count=len(events),
        )
    )

    # 9. Decisions: applied ⇔ consumed; proposals bound to the recorded boundary.
    failures = []
    consumed_by_proposal = {item.proposal_id: item for item in lifecycle.approval_consumptions}
    event_time_by_mutation = {
        item.mutation_id: item.event.simulated_time_seconds for item in events
    }
    decision_count = 0
    for workflow in lifecycle.workflows:
        payload = workflow.payload
        if payload.get("mode") != DECISION_MODE:
            continue
        decision_count += 1
        pending = payload.get("pending_proposal_id")
        applied = workflow.status.value == "completed"
        consumption = consumed_by_proposal.get(pending) if isinstance(pending, str) else None
        if applied != (consumption is not None):
            failures.append(
                {"workflow_id": workflow.workflow_id, "reason": "applied_status_mismatch"}
            )
        if isinstance(pending, str):
            proposal = proposals[pending]
            if proposal.base_state_version != payload["base_state_version"]:
                failures.append(
                    {"workflow_id": workflow.workflow_id, "reason": "proposal_off_boundary"}
                )
            if consumption is not None and (
                event_time_by_mutation[consumption.mutation_id]
                != payload["decision_boundary_seconds"]
            ):
                failures.append(
                    {"workflow_id": workflow.workflow_id, "reason": "applied_off_decision_boundary"}
                )
        if not applied and DecisionStatus.APPLIED.value == payload.get("outcome"):
            failures.append({"workflow_id": workflow.workflow_id, "reason": "claims_applied"})
    results.append(
        _result("decisions_bound_to_recorded_boundary", failures, decisions=decision_count)
    )
    return tuple(results)


def invariants_passed(results: tuple[InvariantResult, ...]) -> bool:
    return all(item.passed for item in results)


__all__ = ["check_run_invariants", "invariants_passed"]
