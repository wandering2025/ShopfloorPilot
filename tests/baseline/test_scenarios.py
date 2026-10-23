"""S06-A01/A11/A12 deterministic baseline and capability acceptance."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from shopfloor_pilot.baseline import BaselineStatus
from shopfloor_pilot.domain.fixtures import load_scenario
from shopfloor_pilot.domain.models import AttemptStatus, Capability, EventType, OrderStatus
from shopfloor_pilot.planning import expand_process_route
from shopfloor_pilot.services import ApplicationServices
from shopfloor_pilot.storage import SQLiteStateRepository, WorkflowStatus
from shopfloor_pilot.validation import validate_candidate

EXPECTED = {
    "normal": (BaselineStatus.COMPLETED, 8, 1),
    "urgent_order": (BaselineStatus.COMPLETED, 9, 2),
    "machine_breakdown": (BaselineStatus.COMPLETED, 8, 2),
    "no_feasible_recovery": (BaselineStatus.BLOCKED, 8, 1),
}


@pytest.mark.parametrize("scenario_id", tuple(EXPECTED))
def test_four_no_key_scenarios_have_valid_applied_plans_and_structured_artifacts(
    baseline_runs: dict[str, tuple[object, Path]],
    scenario_id: str,
) -> None:
    result, output = baseline_runs[scenario_id]
    expected_status, expected_orders, expected_revisions = EXPECTED[scenario_id]
    assert result.status is expected_status
    assert result.exit_code == 0
    assert output.is_dir()
    assert (output / "state.sqlite3").is_file()
    assert (output / "events.jsonl").is_file()
    assert (output / "metrics.json").is_file()
    assert (output / "final_snapshot.json").is_file()
    for report_name in ("proposal_before.json", "proposal_after.json", "proposal_comparison.json"):
        assert (output / "proposals" / "initial" / report_name).is_file()
    if scenario_id == "urgent_order":
        assert (output / "proposals" / "urgent" / "proposal_comparison.json").is_file()
    if scenario_id == "machine_breakdown":
        assert (output / "proposals" / "recovery" / "proposal_comparison.json").is_file()

    summary = json.loads((output / "summary.json").read_text(encoding="utf-8"))
    assert summary["status"] == expected_status.value
    assert summary["exit_code"] == 0
    assert summary["output_directory"] == str(output.resolve())

    bundle = load_scenario(scenario_id)
    repository = SQLiteStateRepository(output / "state.sqlite3")
    snapshot = repository.get_snapshot(result.run_id)
    lifecycle = repository.export_lifecycle(result.run_id)
    plan_events = tuple(
        item
        for item in repository.list_events(result.run_id)
        if item.event.event_type is EventType.PLAN_APPLIED
    )

    assert len(snapshot.orders) == expected_orders
    assert snapshot.plan_revision == expected_revisions
    assert len(plan_events) == expected_revisions
    assert len(lifecycle.approval_consumptions) == expected_revisions
    assert len(lifecycle.approvals) == expected_revisions
    assert all(item.actor == "policy:deterministic" for item in lifecycle.approvals)

    proposals = {item.proposal_id: item for item in lifecycle.proposals}
    for consumption in lifecycle.approval_consumptions:
        candidate = proposals[consumption.proposal_id]
        base = repository.get_checkpoint(
            result.run_id,
            state_version=candidate.base_state_version,
        )
        validation = validate_candidate(
            base.snapshot,
            bundle.catalog,
            candidate,
            projected_order_deltas=candidate.proposed_order_deltas,
            projected_resource_deltas=candidate.proposed_resource_deltas,
        )
        assert validation.is_valid
        assert consumption.mutation_id in {item.mutation_id for item in plan_events}

    if expected_status is BaselineStatus.COMPLETED:
        assert all(item.status is OrderStatus.COMPLETED for item in snapshot.orders)
    else:
        assert result.reason_code == "no_feasible_recovery"
        assert snapshot.simulated_time_seconds == 3000
        assert len(lifecycle.proposals) == 1
        recovery_workflow = next(
            item for item in lifecycle.workflows if item.workflow_id == "workflow-recovery"
        )
        assert recovery_workflow.status is WorkflowStatus.FAILED
        assert recovery_workflow.payload["outcome"] == "blocked"
        assert recovery_workflow.payload["reason_code"] == "no_feasible_recovery"


@pytest.mark.parametrize(
    ("scenario_id", "phase", "simulated_seconds"),
    (
        ("normal", "initial", 0),
        ("urgent_order", "initial", 0),
        ("urgent_order", "urgent", 3599),
        ("machine_breakdown", "initial", 0),
        ("machine_breakdown", "recovery", 2700),
    ),
)
def test_proposal_reports_separate_real_decision_latency_from_simulated_time(
    baseline_runs: dict[str, tuple[object, Path]],
    scenario_id: str,
    phase: str,
    simulated_seconds: int,
) -> None:
    _, output = baseline_runs[scenario_id]
    report = json.loads(
        (output / "proposals" / phase / "proposal_comparison.json").read_text(encoding="utf-8")
    )
    latency = report["decision_latency"]
    assert latency is not None
    assert latency["start_simulated_seconds"] == simulated_seconds
    assert latency["end_simulated_seconds"] == simulated_seconds
    assert latency["simulated_elapsed_seconds"] == 0
    assert latency["wall_clock_elapsed_ms"] == 125
    assert report["after"]["objective_basis"] == "predicted"


def test_urgent_order_and_replan_are_one_approved_atomic_group(
    baseline_runs: dict[str, tuple[object, Path]],
) -> None:
    result, output = baseline_runs["urgent_order"]
    repository = SQLiteStateRepository(output / "state.sqlite3")
    group = tuple(
        item for item in repository.list_events(result.run_id) if item.mutation_id == "apply-urgent"
    )
    assert tuple(item.event.event_type for item in group) == (
        EventType.ORDER_REGISTERED,
        EventType.PLAN_APPLIED,
    )
    assert {item.event.simulated_time_seconds for item in group} == {3600}
    assert len({item.base_state_version for item in group}) == 1
    assert len({item.resulting_state_version for item in group}) == 1

    base_version = group[0].base_state_version
    result_version = group[0].resulting_state_version
    before = repository.get_checkpoint(result.run_id, state_version=base_version).snapshot
    after = repository.get_checkpoint(result.run_id, state_version=result_version).snapshot
    assert "order-urgent-009" not in {item.order_id for item in before.orders}
    assert "order-urgent-009" in {item.order_id for item in after.orders}
    assert after.plan_revision == before.plan_revision + 1

    lifecycle = repository.export_lifecycle(result.run_id)
    consumption = next(
        item for item in lifecycle.approval_consumptions if item.mutation_id == "apply-urgent"
    )
    approval = next(
        item for item in lifecycle.approvals if item.approval_id == consumption.approval_id
    )
    proposal = next(
        item for item in lifecycle.proposals if item.proposal_id == consumption.proposal_id
    )
    assert approval.proposal_hash == proposal.proposal_hash
    assert tuple(item.order.order_id for item in proposal.proposed_order_deltas) == (
        "order-urgent-009",
    )
    policy = json.loads((output / "planning_policy.json").read_text(encoding="utf-8"))
    assert policy["policy_id"] == "positive_time_origin_floor"
    assert policy["uses_evaluator_future_truth"] is False
    assert policy["not_before_seconds"] == 1
    assert "reserved_atomic_boundary_seconds" not in policy


def test_recoverable_breakdown_records_waste_and_full_restart_on_alternate(
    baseline_runs: dict[str, tuple[object, Path]],
) -> None:
    result, output = baseline_runs["machine_breakdown"]
    repository = SQLiteStateRepository(output / "state.sqlite3")
    snapshot = repository.get_snapshot(result.run_id)
    interrupted = next(
        item for item in snapshot.attempts if item.status is AttemptStatus.INTERRUPTED
    )
    restarted = next(
        item for item in snapshot.attempts if item.restart_of_attempt_id == interrupted.attempt_id
    )
    assert interrupted.machine_id == "CNC02"
    assert interrupted.planned_duration_seconds == 900
    assert interrupted.consumed_seconds == 180
    assert (interrupted.actual_start_seconds, interrupted.actual_end_seconds) == (2520, 2700)
    assert restarted.machine_id == "CNC03"
    assert restarted.planned_duration_seconds == restarted.consumed_seconds == 900
    assert restarted.status is AttemptStatus.COMPLETED

    bundle = load_scenario("machine_breakdown")
    operations = {
        item.operation_id: item
        for order in bundle.scenario.orders
        for item in expand_process_route(order, bundle.catalog).operations
    }
    initial = next(
        item
        for item in repository.export_lifecycle(result.run_id).proposals
        if item.proposal_id == "proposal-initial"
    )
    slotting = tuple(
        item
        for item in initial.assignments
        if operations[item.operation_id].required_capability is Capability.SLOTTING
    )
    assert slotting
    assert all(item.window.end_seconds <= 2700 for item in slotting)
    policy = json.loads((output / "planning_policy.json").read_text(encoding="utf-8"))
    assert policy["policy_id"] == "scarce_capability_first"
    assert policy["original_objective_optimal"] is False
    assert policy["uses_evaluator_future_truth"] is False
    assert policy["bottleneck_capability"] == "slotting"
    assert policy["priority_view_due_seconds"] == 2520
    assert "disruption_time_seconds" not in policy
    assert "faulted_machine_ids" not in policy


def test_fault_bootstrap_is_identical_before_distinct_evaluator_interventions(
    baseline_runs: dict[str, tuple[object, Path]],
) -> None:
    bottleneck_assignments = []
    policies = []
    for scenario_id in ("machine_breakdown", "no_feasible_recovery"):
        result, output = baseline_runs[scenario_id]
        repository = SQLiteStateRepository(output / "state.sqlite3")
        proposal = next(
            item
            for item in repository.export_lifecycle(result.run_id).proposals
            if item.proposal_id == "proposal-initial"
        )
        bundle = load_scenario(scenario_id)
        operations = {
            item.operation_id: item
            for order in bundle.scenario.orders
            for item in expand_process_route(order, bundle.catalog).operations
        }
        bottleneck_assignments.append(
            tuple(
                (assignment.operation_id, assignment.machine_id, assignment.window)
                for assignment in proposal.assignments
                if operations[assignment.operation_id].required_capability is Capability.SLOTTING
            )
        )
        policies.append(json.loads((output / "planning_policy.json").read_text(encoding="utf-8")))

    assert bottleneck_assignments[0] == bottleneck_assignments[1]
    assert policies[0] == policies[1]


def test_capability_exposure_keeps_model_tools_out_of_approval_and_application(
    tmp_path: Path,
) -> None:
    bundle = load_scenario("normal")
    services = ApplicationServices(
        SQLiteStateRepository(tmp_path / "capabilities.sqlite3"),
        bundle.catalog,
    )
    capabilities = services.capabilities
    assert "record_human_decision" in capabilities.operator_actions
    assert "apply_approved_plan" in capabilities.operator_actions
    assert "record_deterministic_policy_decision" in capabilities.batch_policy_actions
    assert not (
        set(capabilities.model_readable_actions)
        & (set(capabilities.operator_actions) | set(capabilities.batch_policy_actions))
    )
    facade = services.for_model()
    assert not hasattr(facade, "record_human_decision")
    assert not hasattr(facade, "record_deterministic_policy_decision")
    assert not hasattr(facade, "apply_approved_plan")
    assert not hasattr(facade, "advance_simulation")
