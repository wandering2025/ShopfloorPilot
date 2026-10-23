"""S11-A01: the four canonical scenarios reach their specified terminal states."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from recovery_support import event_type_counts, record_evidence

from shopfloor_pilot.domain.fixtures import load_scenario
from shopfloor_pilot.services import ApplicationServices
from shopfloor_pilot.storage import SQLiteStateRepository
from shopfloor_pilot.workflows import (
    CoordinatorError,
    CoordinatorErrorCode,
    DecisionStatus,
    check_run_invariants,
    invariants_passed,
)
from shopfloor_pilot.workflows.scenario_runner import (
    EXPECTED_TERMINALS,
    RECOVERY_SCENARIOS,
    decide_recovery_scenario,
    run_recovery_scenario,
)


def _services(output: Path, scenario_id: str) -> ApplicationServices:
    bundle = load_scenario(scenario_id)
    return ApplicationServices(
        SQLiteStateRepository(output / "state.sqlite3"), bundle.catalog, seed=0
    )


@pytest.mark.parametrize("scenario_id", RECOVERY_SCENARIOS)
def test_a01_scenario_reaches_its_terminal_with_invariants(
    scenario_id: str, tmp_path: Path
) -> None:
    output = tmp_path / scenario_id
    result = run_recovery_scenario(
        provider_kind="scripted-fake",
        scenario_id=scenario_id,
        seed=0,
        output=output,
        approval_policy="deterministic",
    )
    summary = result.summary
    expected_status, expected_reason = EXPECTED_TERMINALS[scenario_id]
    assert (result.status, summary["reason_code"]) == (expected_status, expected_reason)
    assert result.exit_code == 0
    assert summary["invariants_passed"], summary["failed_invariants"]
    assert summary["evidence_label"].startswith("OFFLINE CONTRACT EVIDENCE ONLY")
    services = _services(output, scenario_id)
    run_id = summary["run_id"]
    # Re-derive from SQLite rather than trusting the summary.
    invariants = check_run_invariants(services, run_id)
    assert invariants_passed(invariants)
    decisions = {item["workflow_id"]: item for item in summary["decisions"]}
    sources = {item["source"] for item in decisions.values()}
    assert "supervisor" in sources
    for item in decisions.values():
        if item["status"] == "applied":
            assert item["consumed_approval_id"]
    lifecycle = services.export_lifecycle(run_id)
    actors = {item.actor for item in lifecycle.approvals}
    assert actors == {"policy:deterministic"}
    final = services.get_snapshot(run_id)
    if expected_status == "completed":
        assert all(item.status.value == "completed" for item in final.orders)
    else:
        blocked = [item for item in decisions.values() if item["status"] == "paused"]
        assert [item["reason_code"] for item in blocked] == ["no_feasible_schedule"]
        recovery = blocked[0]
        # The infeasible pause preserved the snapshot it paused on.
        paused_at = services.get_checkpoint(run_id, state_version=final.state_version)
        assert final.simulated_time_seconds == recovery["decision_time_seconds"]
        assert paused_at.snapshot.plan_revision == 1
        assert len(lifecycle.approval_consumptions) == 1
    validation = {item.code: item.passed for item in invariants}
    record_evidence(
        f"a01_{scenario_id}",
        {
            "scenario_id": scenario_id,
            "provider": "scripted-fake",
            "evidence_label": summary["evidence_label"],
            "approval_policy": summary["approval_policy"],
            "terminal": {"status": result.status, "reason_code": summary["reason_code"]},
            "expected": {"status": expected_status, "reason_code": expected_reason},
            "decisions": summary["decisions"],
            "final": {
                key: summary["final"][key]
                for key in (
                    "state_version",
                    "plan_revision",
                    "simulated_time_seconds",
                    "snapshot_hash",
                )
            },
            "event_counts": event_type_counts(services, run_id),
            "approval_actors": sorted(actors),
            "invariants": validation,
            "replay_matches": summary["final"]["snapshot_hash"]
            == summary["final"]["replay_final_snapshot_hash"],
            "wasted_processing_seconds": services.calculate_run_metrics(
                run_id
            ).wasted_processing_seconds,
        },
    )


def test_a01_interactive_run_stops_for_human_approval_including_initial(tmp_path: Path) -> None:
    output = tmp_path / "interactive"
    first = run_recovery_scenario(
        provider_kind="scripted-fake", scenario_id="urgent_order", seed=0, output=output
    )
    assert first.status == "awaiting_approval" and first.exit_code == 0
    request = json.loads((output / "approval_request.json").read_text())
    assert request["decision_kind"] == "initial_planning"
    assert request["source"] == "deterministic_baseline"
    services = _services(output, "urgent_order")
    assert services.get_snapshot(first.summary["run_id"]).plan_revision == 0, "nothing applied"
    assert not services.export_lifecycle(first.summary["run_id"]).approvals
    with pytest.raises(CoordinatorError) as refused:  # a policy cannot pose as a human
        decide_recovery_scenario(
            output=output,
            workflow_id=request["workflow_id"],
            proposal_hash=request["proposal_hash"],
            actor="policy:deterministic",
            approve=True,
        )
    assert refused.value.code is CoordinatorErrorCode.INVALID_ACTOR
    second = decide_recovery_scenario(
        output=output,
        workflow_id=request["workflow_id"],
        proposal_hash=request["proposal_hash"],
        actor="human:alice",
        approve=True,
    )
    assert second.status == "awaiting_approval"
    urgent = json.loads((output / "approval_request.json").read_text())
    assert urgent["decision_kind"] == "urgent_order" and urgent["source"] == "supervisor"
    assert urgent["order_deltas"] and urgent["decision_time_seconds"] == 3599
    assert urgent["decision_boundary_seconds"] == 3600
    assert not any(
        item.order_id.startswith("order-req-")
        for item in services.get_snapshot(first.summary["run_id"]).orders
    ), "the urgent order is a draft until approved"
    third = decide_recovery_scenario(
        output=output,
        workflow_id=urgent["workflow_id"],
        proposal_hash=urgent["proposal_hash"],
        actor="human:alice",
        approve=True,
    )
    assert third.status == "completed" and third.exit_code == 0
    assert third.summary["invariants_passed"]
    assert {
        item.actor for item in services.export_lifecycle(first.summary["run_id"]).approvals
    } == {"human:alice"}
    assert not (output / "approval_request.json").exists()


def test_a01_human_rejection_stops_safely(tmp_path: Path) -> None:
    output = tmp_path / "reject"
    first = run_recovery_scenario(
        provider_kind="scripted-fake", scenario_id="machine_breakdown", seed=0, output=output
    )
    request = json.loads((output / "approval_request.json").read_text())
    rejected = decide_recovery_scenario(
        output=output,
        workflow_id=request["workflow_id"],
        proposal_hash=request["proposal_hash"],
        actor="human:alice",
        approve=False,
        reason="not today",
    )
    assert rejected.status == "blocked" and rejected.summary["reason_code"] == "proposal_rejected"
    assert rejected.exit_code == 0
    services = _services(output, "machine_breakdown")
    run_id = first.summary["run_id"]
    assert services.get_snapshot(run_id).plan_revision == 0
    decision = rejected.summary["decisions"][0]
    assert decision["status"] == DecisionStatus.CANCELLED.value
    assert decision["outcome"] == "rejected"


def test_a01_brief_cli_command_runs_the_closed_loop(tmp_path: Path) -> None:
    output = tmp_path / "recovery"
    env = {key: value for key, value in os.environ.items() if not key.startswith("DEEPSEEK")}
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "shopfloor_pilot.cli",
            "agent",
            "--provider",
            "scripted-fake",
            "--scenario",
            "machine_breakdown",
            "--seed",
            "0",
            "--approval-policy",
            "deterministic",
            "--output",
            str(output),
        ],
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
        env=env,
    )
    assert completed.returncode == 0, completed.stderr
    summary = json.loads(completed.stdout)
    assert summary["command"] == "recovery"
    assert summary["status"] == "completed" and summary["invariants_passed"]
    for name in ("events.jsonl", "lifecycle.json", "lifecycle_view.json", "invariants.json"):
        assert (output / name).is_file()


@pytest.mark.parametrize("seed", range(1, 10))
@pytest.mark.parametrize(
    "scenario_id", ["urgent_order", "machine_breakdown", "no_feasible_recovery"]
)
def test_s11_probe_regression_offline_seed_sweep(
    scenario_id: str, seed: int, tmp_path: Path
) -> None:
    """Seeds change the initial plan; the live probe found a seed-only boundary bug (seed 7)."""

    result = run_recovery_scenario(
        provider_kind="scripted-fake",
        scenario_id=scenario_id,
        seed=seed,
        output=tmp_path / f"{scenario_id}-{seed}",
        approval_policy="deterministic",
    )
    assert (result.status, result.summary["reason_code"]) == EXPECTED_TERMINALS[scenario_id]
    assert result.summary["invariants_passed"], result.summary["failed_invariants"]
