"""No-network checks for release-gate denominators, accounting and fail-closed scoring."""

from __future__ import annotations

import importlib.util
import json
import subprocess
from collections import Counter
from decimal import Decimal

import pytest

from shopfloor_pilot.evaluation.manifest import REPOSITORY_ROOT, load_evaluation_manifest
from shopfloor_pilot.evaluation.runner import TERMINAL_LIMIT_FIELDS, _planned
from shopfloor_pilot.providers.config import load_paid_run_settings

SPEC = importlib.util.spec_from_file_location(
    "live_regression", REPOSITORY_ROOT / "scripts/live_regression.py"
)
assert SPEC is not None and SPEC.loader is not None
gate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gate)


@pytest.fixture
def frozen():
    manifest = load_evaluation_manifest(REPOSITORY_ROOT / "scenarios/evaluation.json")
    campaign = gate.release_campaign(manifest)
    return manifest, campaign, _planned(campaign, "deepseek")


def successful_record(item, manifest):
    provider = manifest.payload["live_configuration"]["provider"]
    return {
        **item,
        "attempted": True,
        "audit_complete": True,
        "audit_errors": [],
        "end_to_end_success": True,
        "invariants_passed": True,
        "hard_constraint_violations": 0,
        "truth_leak": False,
        "claim_audit": {"has_contradiction": False},
        "claims": {"citation_validity_passed": True},
        "budget_accounting_verified": True,
        "cost_micros": 1000,
        "model_ids": [provider["model"]],
        "provider_limits": {
            target: provider[source] for source, target in TERMINAL_LIMIT_FIELDS.items()
        },
    }


def test_release_subset_retains_original_manifest_and_cost_caps(frozen):
    manifest, campaign, planned = frozen
    assert manifest.campaign("reliability").planned_paid_runs == 100
    assert Counter(item["scenario_id"] for item in planned) == dict.fromkeys(campaign.scenarios, 5)
    assert {(item["seed"], item["repetition"]) for item in planned} == {
        (0, repetition) for repetition in range(5)
    }
    assert len({item["case_id"] for item in planned}) == 20
    assert campaign.workflow_cap_usd * campaign.planned_paid_runs == Decimal("0.2500")
    assert campaign.aggregate_cap_usd == Decimal("0.30")


def test_runtime_settings_override_ambient_provider_drift(frozen, tmp_path):
    manifest, _, _ = frozen
    dummy_key = tmp_path / "dummy-key"
    dummy_key.write_text("dummy-test-key-never-a-real-credential")
    environment = gate.frozen_environment(
        manifest,
        dummy_key,
        {
            "DEEPSEEK_API_KEY": "unwanted-inline-test-key",
            "DEEPSEEK_MODEL": "wrong-model",
            "DEEPSEEK_THINKING": "enabled",
            "SHOPFLOOR_PROVIDER_MAX_TOKENS": "999999",
            "SHOPFLOOR_PROVIDER_RETRY_BACKOFF_SECONDS": "99",
            "SHOPFLOOR_PROVIDER_RUN_BUDGET": "999",
            "SHOPFLOOR_PROVIDER_LEDGER_PATH": "/must/not/reuse.sqlite3",
            "PATH": "/usr/bin",
        },
    )
    assert "DEEPSEEK_API_KEY" not in environment
    assert "SHOPFLOOR_PROVIDER_LEDGER_PATH" not in environment
    assert environment["PATH"] == "/usr/bin"
    settings = load_paid_run_settings(environment)
    assert settings.model == "deepseek-flash"
    assert settings.connection.thinking is False
    assert settings.workflow_cap_micros == settings.run_cap_micros == 12500
    assert settings.max_output_tokens == 1024
    assert settings.retry_policy.max_retries_per_request == 2
    assert settings.retry_policy.initial_backoff_seconds == 1.0
    assert settings.retry_policy.max_backoff_seconds == 4.0
    assert settings.retry_policy.multiplier == 2.0


@pytest.mark.parametrize("cost", [None, -1, True, "1000", 1.5])
def test_unknown_or_malformed_cost_reserves_full_workflow_cap(cost):
    assert gate.accounted_cost({"cost_micros": cost, "budget_accounting_verified": True}) == (
        gate.WORKFLOW_CAP
    )


def test_unverified_cost_is_not_trusted():
    assert gate.accounted_cost({"cost_micros": 1}) == gate.WORKFLOW_CAP
    assert gate.accounted_cost({"cost_micros": 1000, "budget_accounting_verified": True}) == (
        Decimal("0.001")
    )


@pytest.mark.parametrize("defect", ["failure", "unaudited", "missing", "duplicate", "overspend"])
def test_one_bad_row_fails_release_gate(frozen, defect):
    manifest, campaign, planned = frozen
    records = [successful_record(item, manifest) for item in planned]
    assert gate.score(manifest, campaign, planned, records, Decimal("0.02"))[1]["passed"] is True
    if defect == "failure":
        records[0]["end_to_end_success"] = False
    elif defect == "unaudited":
        records[0]["audit_complete"] = False
    elif defect == "missing":
        records.pop()
    elif defect == "duplicate":
        records[-1] = records[0]
    else:
        records[0]["cost_micros"] = 13000
    assert gate.score(manifest, campaign, planned, records, Decimal("0.02"))[1]["passed"] is False


def test_failed_launches_keep_all_twenty_denominators(frozen, tmp_path, monkeypatch):
    manifest, campaign, _ = frozen
    commands = []

    def fail(command, **kwargs):
        commands.append(command)
        raise subprocess.TimeoutExpired(command, 240)

    monkeypatch.setattr(gate.subprocess, "run", fail)
    output = tmp_path / "release"
    report = gate.execute(manifest, campaign, {}, output, {"test_only": True})
    assert len(commands) == len(report["records"]) == 20
    assert report["spent_usd_conservative"] == "0.2500"
    assert report["status"] == "FAIL"
    assert all(record["attempted"] for record in report["records"])
    assert len(json.loads((output / "records.json").read_text())) == 20
    assert all(command[-2:] == ["--approval-policy", "deterministic"] for command in commands)


def test_aggregate_guard_stops_new_calls_and_retains_not_run_rows(frozen, tmp_path, monkeypatch):
    manifest, campaign, planned = frozen
    calls = []

    def launch(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0)

    record = successful_record(planned[0], manifest)
    record["cost_micros"] = 290000
    monkeypatch.setattr(gate.subprocess, "run", launch)
    monkeypatch.setattr(gate, "collect_run", lambda **kwargs: record)
    report = gate.execute(manifest, campaign, {}, tmp_path / "release", {"test_only": True})
    assert len(calls) == 1
    assert len(report["records"]) == 20
    assert sum(row["status"] == "NOT_RUN" for row in report["records"][1:]) == 19
    assert report["spent_usd_conservative"] == "0.29"
    assert report["status"] == "FAIL"
