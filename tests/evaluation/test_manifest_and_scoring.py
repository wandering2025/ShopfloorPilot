from __future__ import annotations

import json
import os
import shutil
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from shopfloor_pilot.evaluation.collector import (
    aggregate_records,
    collect_run,
    semantic_snapshot_projection,
    wilson,
)
from shopfloor_pilot.evaluation.manifest import (
    REPOSITORY_ROOT,
    EvaluationManifest,
    load_evaluation_manifest,
    prompt_contract_sha256,
)
from shopfloor_pilot.evaluation.runner import (
    FROZEN_LIMIT_FIELDS,
    LIVE_SETTING_FIELDS,
    _frozen_workflow_settings,
    _live_preflight,
    _planned,
    _planned_records_match,
    _thresholds,
)
from shopfloor_pilot.workflows.scenario_runner import run_recovery_scenario

MANIFEST = REPOSITORY_ROOT / "scenarios/evaluation.json"


@pytest.fixture(scope="module")
def audited_baseline_output(tmp_path_factory: pytest.TempPathFactory) -> Path:
    output = tmp_path_factory.mktemp("audited-baseline") / "run"
    result = run_recovery_scenario(
        provider_kind="deterministic-baseline",
        scenario_id="normal",
        seed=0,
        output=output,
        approval_policy="deterministic",
    )
    assert result.exit_code == 0
    return output


def _write_manifest_copy(tmp_path: Path, payload: dict[str, object]) -> Path:
    path = tmp_path / "evaluation.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _configure_valid_live_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> EvaluationManifest:
    manifest = load_evaluation_manifest(MANIFEST)
    for variable, (section, field) in LIVE_SETTING_FIELDS.items():
        monkeypatch.setenv(
            variable,
            str(manifest.payload["live_configuration"][section][field]),
        )
    for variable in FROZEN_LIMIT_FIELDS:
        monkeypatch.delenv(variable, raising=False)
    monkeypatch.delenv("DEEPSEEK_REASONING_EFFORT", raising=False)
    key_file = tmp_path / "dummy-deepseek-key.txt"
    key_file.write_text("dummy-key-for-offline-test", encoding="utf-8")
    monkeypatch.setenv("DEEPSEEK_API_KEY_FILE", str(key_file))
    return manifest


def test_manifest_freezes_required_denominators_budgets_and_contracts() -> None:
    manifest = load_evaluation_manifest(MANIFEST)
    assert manifest.payload["prompt_contract_sha256"] == prompt_contract_sha256()
    assert manifest.campaign("offline").planned_cases == 20
    assert manifest.campaign("paired_live").planned_paid_runs == 12
    reliability = manifest.campaign("reliability")
    assert reliability.planned_paid_runs == 100
    assert reliability.workflow_cap_usd * reliability.planned_paid_runs <= (
        reliability.aggregate_cap_usd
    )
    assert manifest.payload["solver"] == {
        "implementation": "OR-Tools CP-SAT",
        "version": "9.15.6755",
        "max_time_seconds": 5.0,
        "num_search_workers": 1,
        "seed_source": "evaluation case seed",
        "relative_gap_limit": 0.0,
        "absolute_gap_limit": 0.0,
        "objective": "lexicographic weighted total tardiness, then makespan",
        "validator": "independent S03 validate_candidate",
    }


def test_manifest_rejects_post_freeze_source_change(tmp_path: Path) -> None:
    payload = json.loads(MANIFEST.read_text())
    payload["sources"]["order_requests"]["sha256"] = "0" * 64
    changed = tmp_path / "changed.json"
    changed.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="order_requests hash changed"):
        load_evaluation_manifest(changed)


@pytest.mark.parametrize("campaign_id", ["offline", "paired_live", "reliability"])
def test_manifest_rejects_campaign_missing_a_required_scenario(
    tmp_path: Path, campaign_id: str
) -> None:
    payload = json.loads(MANIFEST.read_text(encoding="utf-8"))
    payload["campaigns"][campaign_id]["scenarios"].remove("machine_breakdown")
    changed = _write_manifest_copy(tmp_path, payload)

    with pytest.raises(ValueError, match=f"{campaign_id}.*scenarios"):
        load_evaluation_manifest(changed, verify_sources=False)


def test_manifest_rejects_missing_threshold(tmp_path: Path) -> None:
    payload = json.loads(MANIFEST.read_text(encoding="utf-8"))
    del payload["thresholds"]["normal_success_rate"]
    changed = _write_manifest_copy(tmp_path, payload)

    with pytest.raises(ValueError, match="thresholds keys differ"):
        load_evaluation_manifest(changed, verify_sources=False)


@pytest.mark.parametrize(
    "campaign_id,field,value",
    [
        ("offline", "repetitions_per_seed", 2),
        ("paired_live", "repetitions_per_seed", 2),
        ("reliability", "repetitions_per_seed", 6),
        ("reliability", "arms", ["deepseek", "scripted"]),
    ],
)
def test_manifest_rejects_campaign_shape_drift(
    tmp_path: Path, campaign_id: str, field: str, value: object
) -> None:
    payload = json.loads(MANIFEST.read_text(encoding="utf-8"))
    payload["campaigns"][campaign_id][field] = value
    changed = _write_manifest_copy(tmp_path, payload)

    with pytest.raises(ValueError, match=campaign_id):
        load_evaluation_manifest(changed, verify_sources=False)


@pytest.mark.parametrize(
    "field,value",
    [("max_provider_attempts", 0), ("thinking", "enabled")],
)
def test_manifest_rejects_invalid_frozen_live_configuration(
    tmp_path: Path, field: str, value: object
) -> None:
    payload = json.loads(MANIFEST.read_text(encoding="utf-8"))
    payload["live_configuration"]["provider"][field] = value
    changed = _write_manifest_copy(tmp_path, payload)

    with pytest.raises(ValueError, match=f"live_configuration.provider.{field}"):
        load_evaluation_manifest(changed, verify_sources=False)


@pytest.mark.parametrize(
    "variable,bad_value",
    [(item, "999999") for item in FROZEN_LIMIT_FIELDS] + [("DEEPSEEK_REASONING_EFFORT", "high")],
)
def test_live_preflight_rejects_frozen_limit_or_reasoning_drift(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    variable: str,
    bad_value: str,
) -> None:
    manifest = _configure_valid_live_environment(monkeypatch, tmp_path)
    assert _live_preflight(manifest) == []

    monkeypatch.setenv(variable, bad_value)

    assert _live_preflight(manifest) == [variable]


def test_live_runner_injects_and_restores_all_frozen_limits(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    manifest = _configure_valid_live_environment(monkeypatch, tmp_path)
    campaign = manifest.campaign("reliability")
    assert all(variable not in os.environ for variable in FROZEN_LIMIT_FIELDS)

    with _frozen_workflow_settings(manifest, campaign, live=True):
        environment = os.environ
        provider = manifest.payload["live_configuration"]["provider"]
        assert {variable: environment[variable] for variable in FROZEN_LIMIT_FIELDS} == {
            variable: str(provider[field]) for variable, field in FROZEN_LIMIT_FIELDS.items()
        }
        assert environment["SHOPFLOOR_PROVIDER_WORKFLOW_BUDGET"] == str(campaign.workflow_cap_usd)
        assert environment["SHOPFLOOR_PROVIDER_RUN_BUDGET"] == str(campaign.workflow_cap_usd)
        assert environment["DEEPSEEK_REASONING_EFFORT"] == ""

    assert all(variable not in os.environ for variable in FROZEN_LIMIT_FIELDS)


def test_baseline_and_scripted_have_distinct_meaning_but_same_business_result(
    tmp_path: Path,
) -> None:
    baseline_output = tmp_path / "baseline"
    scripted_output = tmp_path / "scripted"
    baseline = run_recovery_scenario(
        provider_kind="deterministic-baseline",
        scenario_id="normal",
        seed=0,
        output=baseline_output,
        approval_policy="deterministic",
    )
    scripted = run_recovery_scenario(
        provider_kind="scripted-fake",
        scenario_id="normal",
        seed=0,
        output=scripted_output,
        approval_policy="deterministic",
    )
    assert baseline.exit_code == scripted.exit_code == 0
    assert "DETERMINISTIC RULE BASELINE" in baseline.summary["evidence_label"]
    assert "OFFLINE CONTRACT EVIDENCE ONLY" in scripted.summary["evidence_label"]

    baseline_record = collect_run(
        arm="baseline",
        campaign_id="test",
        scenario_id="normal",
        seed=0,
        repetition=0,
        output=baseline_output,
        wall_seconds=0.0,
    )
    scripted_record = collect_run(
        arm="scripted",
        campaign_id="test",
        scenario_id="normal",
        seed=0,
        repetition=0,
        output=scripted_output,
        wall_seconds=0.0,
    )
    assert baseline_record["counts_as_llm_capability"] is False
    assert scripted_record["counts_as_llm_capability"] is False
    assert (
        baseline_record["semantic_snapshot_sha256"] == (scripted_record["semantic_snapshot_sha256"])
    )
    assert baseline_record["metrics"] == scripted_record["metrics"]
    assert baseline_record["interpretation"]["correct"] is True
    assert scripted_record["interpretation"]["correct"] is True
    assert baseline_record["cost_micros"] is None
    assert scripted_record["cost_micros"] is None


def test_semantic_projection_normalizes_generated_ids_but_keeps_business_changes() -> None:
    snapshot = {
        "schema_version": 1,
        "simulated_time_seconds": 10,
        "hard_horizon_seconds": 100,
        "orders": [
            {
                "order_id": "generated-a",
                "product_id": "product",
                "quantity": 1,
                "release_time_seconds": 0,
                "due_time_seconds": 50,
                "priority_weight": 2,
                "status": "completed",
            }
        ],
        "machines": [{"machine_id": "M1", "status": "available"}],
        "operations": [
            {
                "operation_id": "generated-op-a",
                "order_id": "generated-a",
                "operation_template_id": "op-template",
                "predecessor_operation_ids": [],
                "unit_index": 1,
                "sequence_index": 0,
                "duration_seconds": 10,
                "required_capability": "milling",
                "status": "completed",
            }
        ],
        "attempts": [
            {
                "attempt_id": "generated-attempt-a",
                "operation_id": "generated-op-a",
                "attempt_number": 1,
                "restart_of_attempt_id": None,
                "machine_id": "M1",
                "planned_duration_seconds": 10,
                "consumed_seconds": 10,
                "scheduled_window": {"start_seconds": 0, "end_seconds": 10},
                "actual_start_seconds": 0,
                "actual_end_seconds": 10,
                "status": "completed",
            }
        ],
    }
    renamed = json.loads(json.dumps(snapshot))
    renamed["orders"][0]["order_id"] = "generated-b"
    renamed["operations"][0].update({"operation_id": "generated-op-b", "order_id": "generated-b"})
    renamed["attempts"][0].update(
        {"attempt_id": "generated-attempt-b", "operation_id": "generated-op-b"}
    )
    assert semantic_snapshot_projection(snapshot) == semantic_snapshot_projection(renamed)
    renamed["attempts"][0]["machine_id"] = "M2"
    assert semantic_snapshot_projection(snapshot) != semantic_snapshot_projection(renamed)


def test_aggregate_keeps_not_run_and_failures_in_planned_denominator() -> None:
    planned = [
        {"arm": "deepseek", "scenario_id": "normal"},
        {"arm": "deepseek", "scenario_id": "normal"},
    ]
    records = [
        {
            "arm": "deepseek",
            "scenario_id": "normal",
            "attempted": True,
            "status": "completed",
            "end_to_end_success": True,
            "supervisor_success": True,
            "invariants_passed": True,
            "hard_constraint_violations": 0,
            "truth_leak": False,
            "claim_audit": {"has_contradiction": False},
            "interpretation": {"applicable": True, "correct": True},
            "claims": {"citations": 1, "resolved_citations": 1},
            "cost_micros": 1000,
            "provider_latencies_seconds": [1.0],
            "active_seconds": 1.0,
            "used_tokens": 10,
        },
        {
            "arm": "deepseek",
            "scenario_id": "normal",
            "attempted": False,
            "status": "NOT_RUN",
            "end_to_end_success": False,
            "supervisor_success": False,
            "cost_micros": None,
        },
    ]
    group = aggregate_records(records, planned)["deepseek/normal"]
    assert group["planned"] == 2
    assert group["attempted"] == 1
    assert group["not_run"] == 1
    assert group["end_to_end_success_rate_planned_denominator"] == 0.5
    assert group["end_to_end_success_wilson95"] == wilson(1, 2)


def test_planned_records_require_the_exact_identity_multiset() -> None:
    manifest = load_evaluation_manifest(MANIFEST)
    planned = _planned(manifest.campaign("reliability"), "deepseek")
    records = [{**item, "attempted": True} for item in planned]
    assert _planned_records_match(planned, records) is True

    duplicate = [dict(item) for item in records]
    duplicate[4] = dict(duplicate[0])
    assert _planned_records_match(planned, duplicate) is False

    wrong_campaign = [dict(item) for item in records]
    wrong_campaign[0]["campaign"] = "another-campaign"
    assert _planned_records_match(planned, wrong_campaign) is False

    wrong_case_id = [dict(item) for item in records]
    wrong_case_id[0]["case_id"] = "another-case"
    assert _planned_records_match(planned, wrong_case_id) is False


def test_reliability_thresholds_are_loaded_from_the_manifest() -> None:
    manifest = load_evaluation_manifest(MANIFEST)
    campaign = manifest.campaign("reliability")
    planned = _planned(campaign, "deepseek")
    records = [{**item, "attempted": True} for item in planned]
    aggregates = {}
    for scenario_id in campaign.scenarios:
        has_one_safety_finding = int(scenario_id == "normal")
        aggregates[f"deepseek/{scenario_id}"] = {
            "audit_incomplete_records": 0,
            "invalid_applied_schedules": has_one_safety_finding,
            "invariant_failures": has_one_safety_finding,
            "truth_leaks": has_one_safety_finding,
            "claim_audit_contradictions": has_one_safety_finding,
            "citation_validity_numerator": 1,
            "citation_validity_denominator": 1,
            "end_to_end_success_rate_planned_denominator": 0.90,
            "cost_usd_mean_planned_denominator": 0.025,
        }

    assert (
        _thresholds(
            campaign,
            planned,
            records,
            aggregates,
            manifest.thresholds,
        )["passed"]
        is False
    )

    relaxed = replace(
        manifest.thresholds,
        normal_success_rate=Decimal("0.90"),
        urgent_order_success_rate=Decimal("0.90"),
        machine_breakdown_success_rate=Decimal("0.90"),
        no_feasible_recovery_safe_block_rate=Decimal("0.90"),
        invalid_applied_schedules=1,
        invariant_failures=1,
        truth_leaks=1,
        persisted_claim_contradictions=1,
        mean_cost_usd_per_workflow=Decimal("0.025"),
    )
    result = _thresholds(campaign, planned, records, aggregates, relaxed)
    assert result["configured"] == relaxed.public_dict()
    assert result["passed"] is True


@pytest.mark.parametrize(
    "artifact,error_code",
    [
        ("agent_trace.sqlite3", "agent_trace_missing"),
        ("lifecycle.json", "lifecycle_missing"),
        ("invariants.json", "invariants_missing"),
        ("metrics.json", "metrics_missing"),
        ("final_snapshot.json", "final_snapshot_missing"),
        ("provider_ledger.sqlite3", "provider_ledger_missing"),
    ],
)
def test_collect_run_fails_closed_when_an_audit_artifact_is_missing(
    audited_baseline_output: Path,
    tmp_path: Path,
    artifact: str,
    error_code: str,
) -> None:
    output = tmp_path / "missing-artifact"
    shutil.copytree(audited_baseline_output, output)
    (output / artifact).unlink()

    record = collect_run(
        arm="baseline",
        campaign_id="test",
        scenario_id="normal",
        seed=0,
        repetition=0,
        output=output,
        wall_seconds=0.0,
    )

    assert record["audit_complete"] is False
    assert error_code in record["audit_errors"]
    assert record["end_to_end_success"] is False
    group = aggregate_records([record], [{"arm": "baseline", "scenario_id": "normal"}])[
        "baseline/normal"
    ]
    assert group["audit_incomplete_records"] == 1


def test_collect_run_uses_the_independent_invariant_result(
    audited_baseline_output: Path, tmp_path: Path
) -> None:
    output = tmp_path / "failed-independent-invariant"
    shutil.copytree(audited_baseline_output, output)
    invariants_path = output / "invariants.json"
    invariants = json.loads(invariants_path.read_text(encoding="utf-8"))
    independent = next(
        item for item in invariants if item["code"] == "applied_plans_pass_independent_validator"
    )
    independent["passed"] = False
    independent["details"]["failure_count"] = 1
    independent["details"]["failures"] = ["synthetic independent-audit failure"]
    invariants_path.write_text(json.dumps(invariants), encoding="utf-8")

    record = collect_run(
        arm="baseline",
        campaign_id="test",
        scenario_id="normal",
        seed=0,
        repetition=0,
        output=output,
        wall_seconds=0.0,
    )

    assert record["invariants_passed"] is False
    assert record["hard_constraint_violations"] == 1
    assert record["end_to_end_success"] is False
