"""Read/report service boundaries and durable workflow terminal semantics."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from shopfloor_pilot.domain.models import ApprovalDecision, OrderStatus
from shopfloor_pilot.scheduling import SolverConfig
from shopfloor_pilot.services import (
    ApplicationServiceError,
    ApplicationServices,
    ServiceErrorCode,
)
from shopfloor_pilot.storage import WorkflowStatus

FIXED_UTC = "2026-10-20T20:45:00+02:00"


def _stored_candidate(services: ApplicationServices, suffix: str) -> Any:
    workflow_id = f"workflow-{suffix}"
    services.ensure_workflow(
        "run-s06-services",
        workflow_id,
        payload={"phase": suffix},
        updated_at_utc=FIXED_UTC,
    )
    result = services.generate_proposal(
        "run-s06-services",
        workflow_id,
        f"proposal-{suffix}",
        config=SolverConfig(max_time_seconds=1.0),
        created_at_utc=FIXED_UTC,
    )
    assert result.candidate is not None
    return result.candidate


def test_read_metrics_reports_and_portable_replay_share_application_services(
    service_factory: Callable[[str], tuple[ApplicationServices, Any, Path]],
    tmp_path: Path,
) -> None:
    services, case, _ = service_factory()
    candidate = _stored_candidate(services, "read-side")
    approval = services.record_deterministic_policy_decision(
        candidate.run_id,
        candidate.proposal_id,
        candidate.proposal_hash,
        approval_id="approval-read-side",
        policy_name="deterministic-test-v1",
        decision=ApprovalDecision.APPROVED,
        decided_at_utc=FIXED_UTC,
    )
    latency = services.calculate_decision_latency(
        start_simulated_seconds=0,
        end_simulated_seconds=0,
        started_at_utc="2026-10-20T18:45:00+00:00",
        completed_at_utc="2026-10-20T18:45:02.500+00:00",
    )
    report = services.for_model().build_proposal_comparison(
        candidate.run_id,
        candidate.proposal_id,
        expected_proposal_hash=candidate.proposal_hash,
        decision_latency=latency,
    )
    assert report.decision_latency == latency
    assert report.realized_improvement is None
    paths = services.export_proposal_reports(
        candidate.run_id,
        candidate.proposal_id,
        tmp_path / "proposal",
        expected_proposal_hash=candidate.proposal_hash,
        decision_latency=latency,
    )
    serialized = json.loads(paths.comparison.read_text(encoding="utf-8"))
    assert serialized["decision_latency"]["simulated_elapsed_seconds"] == 0
    assert serialized["decision_latency"]["wall_clock_elapsed_ms"] == 2500

    services.apply_approved_plan(
        candidate.run_id,
        candidate.proposal_id,
        candidate.proposal_hash,
        approval.approval_id,
        "apply-read-side",
    )
    services.advance_simulation(
        candidate.run_id,
        candidate.objective.makespan_seconds,
        "advance-read-side",
    )
    snapshot = services.get_snapshot(candidate.run_id)
    assert all(order.status is OrderStatus.COMPLETED for order in snapshot.orders)
    metrics = services.calculate_run_metrics(candidate.run_id)
    assert metrics.completed_order_count == 1
    assert metrics.actual_makespan_seconds == candidate.objective.makespan_seconds

    replay_path = tmp_path / "events.jsonl"
    header = services.export_replay_bundle(candidate.run_id, replay_path)
    bundle = ApplicationServices.load_replay_bundle(replay_path)
    replayed = ApplicationServices.replay_bundle(bundle)
    assert replayed.final_snapshot_hash == services.get_checkpoint(candidate.run_id).snapshot_hash
    assert replayed.final_snapshot_hash == header.final_snapshot_hash
    assert replayed.actual_metrics == metrics
    assert services.list_events(candidate.run_id) == bundle.events
    assert services.export_lifecycle(candidate.run_id).approvals == (approval,)
    assert services.get_checkpoint(candidate.run_id, 0).snapshot == case.snapshot


def test_mark_workflow_blocked_is_terminal_idempotent_and_non_operational(
    service_factory: Callable[[str], tuple[ApplicationServices, Any, Path]],
) -> None:
    services, case, _ = service_factory()
    services.ensure_workflow(
        case.snapshot.run_id,
        "workflow-infeasible",
        payload={"phase": "recovery"},
        updated_at_utc=FIXED_UTC,
    )
    checkpoint_before = services.get_checkpoint(case.snapshot.run_id)
    events_before = services.list_events(case.snapshot.run_id)

    blocked = services.mark_workflow_blocked(
        case.snapshot.run_id,
        "workflow-infeasible",
        reason_code="no_feasible_recovery",
        details={"solver_outcome": "infeasible"},
        updated_at_utc=FIXED_UTC,
    )
    repeated = services.mark_workflow_blocked(
        case.snapshot.run_id,
        "workflow-infeasible",
        reason_code="no_feasible_recovery",
        details={"solver_outcome": "infeasible"},
        updated_at_utc="2026-10-20T20:46:00+02:00",
    )

    assert repeated == blocked
    assert blocked.status is WorkflowStatus.FAILED
    assert blocked.payload["outcome"] == "blocked"
    assert blocked.payload["reason_code"] == "no_feasible_recovery"
    assert blocked.payload["details"] == {"solver_outcome": "infeasible"}
    assert services.get_checkpoint(case.snapshot.run_id) == checkpoint_before
    assert services.list_events(case.snapshot.run_id) == events_before

    with pytest.raises(ApplicationServiceError) as conflict:
        services.mark_workflow_blocked(
            case.snapshot.run_id,
            "workflow-infeasible",
            reason_code="different_reason",
        )
    assert conflict.value.code is ServiceErrorCode.WORKFLOW_CONFLICT
    assert services.get_checkpoint(case.snapshot.run_id) == checkpoint_before
    assert services.list_events(case.snapshot.run_id) == events_before
