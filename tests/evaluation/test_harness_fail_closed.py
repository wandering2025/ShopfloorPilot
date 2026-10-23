from __future__ import annotations

import json
import shutil
import sqlite3
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from shopfloor_pilot.evaluation import runner as evaluation_runner
from shopfloor_pilot.evaluation.collector import collect_run
from shopfloor_pilot.evaluation.manifest import REPOSITORY_ROOT, load_evaluation_manifest
from shopfloor_pilot.workflows.scenario_runner import run_recovery_scenario

MANIFEST = REPOSITORY_ROOT / "scenarios/evaluation.json"


@pytest.fixture(scope="module")
def audited_baseline_output(tmp_path_factory: pytest.TempPathFactory) -> Path:
    output = tmp_path_factory.mktemp("fail-closed-baseline") / "run"
    result = run_recovery_scenario(
        provider_kind="deterministic-baseline",
        scenario_id="normal",
        seed=0,
        output=output,
        approval_policy="deterministic",
    )
    assert result.exit_code == 0
    return output


@pytest.mark.parametrize(
    "mutation",
    [
        (
            "DELETE FROM provider_attempts WHERE attempt_id = "
            "(SELECT attempt_id FROM provider_attempts ORDER BY started_at_utc LIMIT 1)"
        ),
        (
            "UPDATE provider_attempts SET used_tokens = used_tokens + 1 WHERE attempt_id = "
            "(SELECT attempt_id FROM provider_attempts ORDER BY started_at_utc LIMIT 1)"
        ),
        (
            "UPDATE provider_attempts SET actual_cost_micros = actual_cost_micros + 1 "
            "WHERE attempt_id = "
            "(SELECT attempt_id FROM provider_attempts ORDER BY started_at_utc LIMIT 1)"
        ),
    ],
    ids=["attempts", "tokens", "cost"],
)
def test_deepseek_accounting_is_unknown_when_ledger_disagrees_with_terminal_budget(
    tmp_path: Path,
    audited_baseline_output: Path,
    mutation: str,
) -> None:
    output = tmp_path / "run"
    shutil.copytree(audited_baseline_output, output)
    with sqlite3.connect(output / "provider_ledger.sqlite3") as connection:
        connection.execute(mutation)

    record = collect_run(
        arm="deepseek",
        campaign_id="paired_live",
        scenario_id="normal",
        seed=0,
        repetition=0,
        output=output,
        wall_seconds=0.0,
    )

    assert record["audit_complete"] is False
    assert record["end_to_end_success"] is False
    assert record["budget_accounting_verified"] is False
    assert record["cost_micros"] is None
    assert "provider_ledger_terminal_budget_mismatch" in record["audit_errors"]


def test_deepseek_accounting_is_unknown_when_ledger_is_corrupt(
    tmp_path: Path,
    audited_baseline_output: Path,
) -> None:
    output = tmp_path / "run"
    shutil.copytree(audited_baseline_output, output)
    (output / "provider_ledger.sqlite3").write_bytes(b"not a sqlite database")

    record = collect_run(
        arm="deepseek",
        campaign_id="paired_live",
        scenario_id="normal",
        seed=0,
        repetition=0,
        output=output,
        wall_seconds=0.0,
    )

    assert record["audit_complete"] is False
    assert record["end_to_end_success"] is False
    assert record["budget_accounting_verified"] is False
    assert record["cost_micros"] is None
    assert "provider_ledger_invalid" in record["audit_errors"]


def test_trace_payload_decode_error_fails_closed(
    tmp_path: Path,
    audited_baseline_output: Path,
) -> None:
    output = tmp_path / "run"
    shutil.copytree(audited_baseline_output, output)
    with sqlite3.connect(output / "agent_trace.sqlite3") as connection:
        connection.execute(
            "UPDATE agent_trace SET payload_json = ? WHERE (workflow_id, sequence) = "
            "(SELECT workflow_id, sequence FROM agent_trace WHERE kind != 'terminal' LIMIT 1)",
            ("{",),
        )

    record = collect_run(
        arm="baseline",
        campaign_id="offline",
        scenario_id="normal",
        seed=0,
        repetition=0,
        output=output,
        wall_seconds=0.0,
    )

    assert record["audit_complete"] is False
    assert record["end_to_end_success"] is False
    assert "trace_payload_decode_error" in record["audit_errors"]


def test_missing_declared_summary_artifact_fails_closed(
    tmp_path: Path,
    audited_baseline_output: Path,
) -> None:
    output = tmp_path / "run"
    shutil.copytree(audited_baseline_output, output)
    (output / "decisions.json").unlink()

    record = collect_run(
        arm="baseline",
        campaign_id="offline",
        scenario_id="normal",
        seed=0,
        repetition=0,
        output=output,
        wall_seconds=0.0,
    )

    assert record["audit_complete"] is False
    assert record["end_to_end_success"] is False
    assert "summary_artifact_missing" in record["audit_errors"]


def test_truncated_invariant_set_fails_closed(
    tmp_path: Path,
    audited_baseline_output: Path,
) -> None:
    output = tmp_path / "run"
    shutil.copytree(audited_baseline_output, output)
    invariants_path = output / "invariants.json"
    invariants = json.loads(invariants_path.read_text(encoding="utf-8"))
    invariants_path.write_text(json.dumps(invariants[:-1]), encoding="utf-8")

    record = collect_run(
        arm="baseline",
        campaign_id="offline",
        scenario_id="normal",
        seed=0,
        repetition=0,
        output=output,
        wall_seconds=0.0,
    )

    assert record["audit_complete"] is False
    assert record["end_to_end_success"] is False
    assert "invariant_set_mismatch" in record["audit_errors"]


def test_truncated_metrics_fail_closed(
    tmp_path: Path,
    audited_baseline_output: Path,
) -> None:
    output = tmp_path / "run"
    shutil.copytree(audited_baseline_output, output)
    metrics_path = output / "metrics.json"
    metrics = json.loads(metrics_path.read_text(encoding="utf-8"))
    del metrics["unfinished_order_count"]
    metrics_path.write_text(json.dumps(metrics), encoding="utf-8")

    record = collect_run(
        arm="baseline",
        campaign_id="offline",
        scenario_id="normal",
        seed=0,
        repetition=0,
        output=output,
        wall_seconds=0.0,
    )

    assert record["audit_complete"] is False
    assert record["end_to_end_success"] is False
    assert "metrics_invalid" in record["audit_errors"]


def test_snapshot_hash_mismatch_fails_closed(
    tmp_path: Path,
    audited_baseline_output: Path,
) -> None:
    output = tmp_path / "run"
    shutil.copytree(audited_baseline_output, output)
    snapshot_path = output / "final_snapshot.json"
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    snapshot["simulated_time_seconds"] += 1
    snapshot_path.write_text(json.dumps(snapshot), encoding="utf-8")

    record = collect_run(
        arm="baseline",
        campaign_id="offline",
        scenario_id="normal",
        seed=0,
        repetition=0,
        output=output,
        wall_seconds=0.0,
    )

    assert record["audit_complete"] is False
    assert record["end_to_end_success"] is False
    assert "final_snapshot_hash_mismatch" in record["audit_errors"]


@pytest.mark.parametrize("mutation", ["missing", "wrong"], ids=["missing", "wrong"])
def test_missing_or_wrong_supervisor_terminal_cannot_score_as_success(
    tmp_path: Path,
    audited_baseline_output: Path,
    mutation: str,
) -> None:
    output = tmp_path / "run"
    shutil.copytree(audited_baseline_output, output)
    with sqlite3.connect(output / "agent_trace.sqlite3") as connection:
        if mutation == "missing":
            connection.execute("DELETE FROM agent_trace WHERE kind = 'terminal'")
        else:
            rowid, encoded = connection.execute(
                "SELECT rowid, payload_json FROM agent_trace WHERE kind = 'terminal'"
            ).fetchone()
            payload = json.loads(encoded)
            payload["terminal"] = "completed"
            connection.execute(
                "UPDATE agent_trace SET payload_json = ? WHERE rowid = ?",
                (json.dumps(payload), rowid),
            )

    record = collect_run(
        arm="baseline",
        campaign_id="offline",
        scenario_id="normal",
        seed=0,
        repetition=0,
        output=output,
        wall_seconds=0.0,
    )

    assert record["end_to_end_success"] is False
    assert record["supervisor_success"] is False


def test_missing_supervisor_decision_makes_claim_audit_incomplete(
    tmp_path: Path,
    audited_baseline_output: Path,
) -> None:
    output = tmp_path / "run"
    shutil.copytree(audited_baseline_output, output)
    summary_path = output / "summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    summary["decisions"] = []
    summary["supervisor_decision_count"] = 0
    summary_path.write_text(json.dumps(summary), encoding="utf-8")

    record = collect_run(
        arm="baseline",
        campaign_id="offline",
        scenario_id="normal",
        seed=0,
        repetition=0,
        output=output,
        wall_seconds=0.0,
    )

    assert record["audit_complete"] is False
    assert record["end_to_end_success"] is False
    assert record["claim_audit"]["error"] == "supervisor_decision_not_found"
    assert "claim_audit_incomplete" in record["audit_errors"]


@pytest.mark.parametrize(
    ("field", "value"),
    [("scenario_id", "urgent_order"), ("seed", 999), ("provider", "deepseek")],
)
def test_summary_identity_must_match_the_planned_record(
    tmp_path: Path,
    audited_baseline_output: Path,
    field: str,
    value: object,
) -> None:
    output = tmp_path / "run"
    shutil.copytree(audited_baseline_output, output)
    summary_path = output / "summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    summary[field] = value
    summary_path.write_text(json.dumps(summary), encoding="utf-8")

    record = collect_run(
        arm="baseline",
        campaign_id="offline",
        scenario_id="normal",
        seed=0,
        repetition=0,
        output=output,
        wall_seconds=0.0,
    )

    assert record["audit_complete"] is False
    assert record["end_to_end_success"] is False
    assert "summary_identity_mismatch" in record["audit_errors"]


def _patch_local_campaign_dependencies(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(evaluation_runner, "_live_preflight", lambda manifest: [])
    monkeypatch.setattr(
        evaluation_runner,
        "LexicalRetriever",
        SimpleNamespace(from_default_corpus=lambda: object()),
    )
    monkeypatch.setattr(
        evaluation_runner,
        "evaluate_retrieval_fixture",
        lambda *args, **kwargs: SimpleNamespace(
            model_dump=lambda **options: {"summary": {"passed": True}}
        ),
    )


def _planned_deepseek_row(*, scenario_id: str = "normal", seed: int = 0) -> dict[str, Any]:
    return {
        "campaign": "paired_live",
        "case_id": f"{scenario_id}-seed-{seed}-repeat-0",
        "scenario_id": scenario_id,
        "seed": seed,
        "repetition": 0,
        "arm": "deepseek",
    }


def _successful_deepseek_record(
    *,
    manifest_provider: dict[str, Any],
    arm: str,
    campaign_id: str,
    scenario_id: str,
    seed: int,
    repetition: int,
    output: Path,
    wall_seconds: float,
) -> dict[str, Any]:
    limits = {
        "max_provider_attempts": manifest_provider["max_provider_attempts"],
        "max_tool_executions": manifest_provider["max_tools"],
        "max_retries_per_request": manifest_provider["max_retries_per_request"],
        "attempt_timeout_seconds": manifest_provider["attempt_timeout_seconds"],
        "max_active_seconds": manifest_provider["max_active_seconds"],
        "max_tokens": manifest_provider["max_total_tokens"],
    }
    return {
        "campaign": campaign_id,
        "arm": arm,
        "scenario_id": scenario_id,
        "seed": seed,
        "repetition": repetition,
        "output": str(output),
        "attempted": True,
        "audit_complete": True,
        "audit_errors": [],
        "status": "completed",
        "reason_code": "scenario_completed",
        "end_to_end_success": True,
        "supervisor_success": True,
        "invariants_passed": True,
        "hard_constraint_violations": 0,
        "truth_leak": False,
        "claim_audit": {"has_contradiction": False},
        "claims": {
            "citations": 0,
            "resolved_citations": 0,
            "citation_validity_passed": True,
        },
        "interpretation": {"applicable": False, "correct": None},
        "provider_latencies_seconds": [],
        "active_seconds": 0.0,
        "used_tokens": 1,
        "cost_micros": 1_000,
        "budget_accounting_verified": True,
        "provider_limits": limits,
        "model_ids": [manifest_provider["model"]],
        "wall_seconds": wall_seconds,
        "counts_as_llm_capability": True,
    }


def test_exception_after_summary_cannot_retain_a_successful_audit_or_known_cost(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_local_campaign_dependencies(monkeypatch)
    manifest = load_evaluation_manifest(MANIFEST)
    provider = manifest.payload["live_configuration"]["provider"]
    monkeypatch.setattr(
        evaluation_runner,
        "_planned",
        lambda campaign, mode: [_planned_deepseek_row()],
    )

    def fail_after_summary(**kwargs: Any) -> None:
        output = kwargs["output"]
        output.mkdir(parents=True)
        (output / "summary.json").write_text("{}\n", encoding="utf-8")
        raise RuntimeError("failure after summary write")

    monkeypatch.setattr(evaluation_runner, "run_recovery_scenario", fail_after_summary)
    monkeypatch.setattr(
        evaluation_runner,
        "collect_run",
        lambda **kwargs: _successful_deepseek_record(
            manifest_provider=provider,
            **kwargs,
        ),
    )

    result = evaluation_runner.run_evaluation(
        mode="deepseek",
        campaign_id="paired_live",
        manifest_path=MANIFEST,
        output=tmp_path / "evaluation",
    )

    record = result.report["records"][0]
    assert record["attempted"] is True
    assert record["status"] == "error"
    assert record["raised_after_summary"] == "RuntimeError"
    assert record["audit_complete"] is False
    assert record["end_to_end_success"] is False
    assert record["cost_micros"] is None
    assert Decimal(result.report["budget"]["spent_usd_conservative"]) == Decimal("0.0125")


def test_collector_exception_after_run_exception_does_not_abort_campaign(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_local_campaign_dependencies(monkeypatch)
    planned = [_planned_deepseek_row(seed=0), _planned_deepseek_row(seed=1)]
    monkeypatch.setattr(evaluation_runner, "_planned", lambda campaign, mode: planned)

    def fail_after_summary(**kwargs: Any) -> None:
        output = kwargs["output"]
        output.mkdir(parents=True)
        (output / "summary.json").write_text("{}\n", encoding="utf-8")
        raise RuntimeError("failure after summary write")

    def fail_collection(**kwargs: Any) -> dict[str, Any]:
        raise ValueError("collector failed")

    monkeypatch.setattr(evaluation_runner, "run_recovery_scenario", fail_after_summary)
    monkeypatch.setattr(evaluation_runner, "collect_run", fail_collection)

    result = evaluation_runner.run_evaluation(
        mode="deepseek",
        campaign_id="paired_live",
        manifest_path=MANIFEST,
        output=tmp_path / "evaluation",
    )

    assert len(result.report["records"]) == len(planned)
    assert result.status == "FAIL"
    assert Decimal(result.report["budget"]["spent_usd_conservative"]) == Decimal("0.0250")
    for record in result.report["records"]:
        assert record["attempted"] is True
        assert record["status"] == "error"
        assert record["audit_complete"] is False
        assert record["end_to_end_success"] is False
        assert record["cost_micros"] is None


@pytest.mark.parametrize(
    "drift",
    [None, "provider_limits", "model_ids", "missing_model_id_on_success"],
)
def test_thresholds_enforce_deepseek_provider_identity_and_limits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    drift: str | None,
) -> None:
    _patch_local_campaign_dependencies(monkeypatch)
    manifest = load_evaluation_manifest(MANIFEST)
    provider = manifest.payload["live_configuration"]["provider"]
    planned = [
        _planned_deepseek_row(scenario_id=scenario_id)
        for scenario_id in (
            "normal",
            "urgent_order",
            "machine_breakdown",
            "no_feasible_recovery",
        )
    ]
    monkeypatch.setattr(evaluation_runner, "_planned", lambda campaign, mode: planned)
    monkeypatch.setattr(evaluation_runner, "run_recovery_scenario", lambda **kwargs: None)

    def collect_with_optional_drift(**kwargs: Any) -> dict[str, Any]:
        record = _successful_deepseek_record(manifest_provider=provider, **kwargs)
        if drift == "provider_limits":
            record["provider_limits"] = {
                **record["provider_limits"],
                "max_tokens": record["provider_limits"]["max_tokens"] + 1,
            }
        elif drift == "model_ids":
            record["model_ids"] = ["unexpected-model"]
        elif drift == "missing_model_id_on_success":
            record["model_ids"] = []
        return record

    monkeypatch.setattr(evaluation_runner, "collect_run", collect_with_optional_drift)

    result = evaluation_runner.run_evaluation(
        mode="deepseek",
        campaign_id="paired_live",
        manifest_path=MANIFEST,
        output=tmp_path / f"evaluation-{drift or 'matching'}",
    )

    assert result.status == ("PASS" if drift is None else "FAIL")
