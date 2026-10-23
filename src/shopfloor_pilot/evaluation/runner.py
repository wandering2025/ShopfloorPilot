"""S13 matrix runner for deterministic baseline, scripted and live DeepSeek arms."""

from __future__ import annotations

import csv
import json
import os
import shutil
import time
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from shopfloor_pilot.baseline import BaselineExitCode, BaselineFailure, _prepare_output
from shopfloor_pilot.retrieval import LexicalRetriever, evaluate_retrieval_fixture
from shopfloor_pilot.workflows.scenario_runner import run_recovery_scenario

from .collector import aggregate_records, collect_run
from .manifest import (
    REPOSITORY_ROOT,
    Campaign,
    EvaluationManifest,
    EvaluationThresholds,
    load_evaluation_manifest,
)

MODE_CAMPAIGN = {
    "baseline": "offline",
    "scripted": "offline",
    "deepseek": "paired_live",
}
PROVIDER_KIND = {
    "baseline": "deterministic-baseline",
    "scripted": "scripted-fake",
    "deepseek": "deepseek",
}
LIVE_SETTING_FIELDS = {
    "DEEPSEEK_BASE_URL": ("provider", "base_url"),
    "DEEPSEEK_MODEL": ("provider", "model"),
    "DEEPSEEK_THINKING": ("provider", "thinking"),
    "SHOPFLOOR_PROVIDER_MAX_OUTPUT_TOKENS": ("provider", "max_output_tokens"),
    "SHOPFLOOR_PROVIDER_CURRENCY": ("pricing", "currency"),
    "SHOPFLOOR_PROVIDER_INPUT_CACHE_HIT_RATE": ("pricing", "input_cache_hit"),
    "SHOPFLOOR_PROVIDER_INPUT_CACHE_MISS_RATE": ("pricing", "input_cache_miss"),
    "SHOPFLOOR_PROVIDER_OUTPUT_RATE": ("pricing", "output"),
    "SHOPFLOOR_PROVIDER_TARIFF_DATE": ("pricing", "tariff_date"),
    "SHOPFLOOR_PROVIDER_TARIFF_SOURCE": ("pricing", "tariff_source"),
}
FROZEN_LIMIT_FIELDS = {
    "SHOPFLOOR_PROVIDER_MAX_ATTEMPTS": "max_provider_attempts",
    "SHOPFLOOR_PROVIDER_MAX_TOOL_EXECUTIONS": "max_tools",
    "SHOPFLOOR_PROVIDER_MAX_RETRIES_PER_REQUEST": "max_retries_per_request",
    "SHOPFLOOR_PROVIDER_ATTEMPT_TIMEOUT_SECONDS": "attempt_timeout_seconds",
    "SHOPFLOOR_PROVIDER_ACTIVE_SECONDS": "max_active_seconds",
    "SHOPFLOOR_PROVIDER_MAX_TOKENS": "max_total_tokens",
}
TERMINAL_LIMIT_FIELDS = {
    "max_provider_attempts": "max_provider_attempts",
    "max_tools": "max_tool_executions",
    "max_retries_per_request": "max_retries_per_request",
    "attempt_timeout_seconds": "attempt_timeout_seconds",
    "max_active_seconds": "max_active_seconds",
    "max_total_tokens": "max_tokens",
}


def _write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


@dataclass(frozen=True, slots=True)
class EvaluationResult:
    status: str
    exit_code: int
    report: dict[str, Any]
    output: Path


def _planned(campaign: Campaign, mode: str) -> list[dict[str, Any]]:
    if mode == "deepseek":
        arms = campaign.arms
    else:
        if mode not in campaign.arms:
            raise BaselineFailure(
                "mode_not_in_campaign",
                f"mode {mode} is not part of campaign {campaign.campaign_id}",
                BaselineExitCode.USAGE,
            )
        arms = (mode,)
    return [
        {
            "campaign": campaign.campaign_id,
            "case_id": f"{scenario_id}-seed-{seed}-repeat-{repetition}",
            "scenario_id": scenario_id,
            "seed": seed,
            "repetition": repetition,
            "arm": arm,
        }
        for repetition in range(campaign.repetitions_per_seed)
        for seed in campaign.seeds
        for scenario_id in campaign.scenarios
        for arm in arms
    ]


def _configured_value(manifest: EvaluationManifest, path: tuple[str, str]) -> str:
    value = manifest.payload["live_configuration"][path[0]][path[1]]
    return str(value)


def _live_preflight(manifest: EvaluationManifest) -> list[str]:
    failures = []
    for variable, path in LIVE_SETTING_FIELDS.items():
        expected = _configured_value(manifest, path)
        actual = os.environ.get(variable)
        if actual != expected:
            failures.append(variable)
    provider = manifest.payload["live_configuration"]["provider"]
    for variable, field in FROZEN_LIMIT_FIELDS.items():
        actual = os.environ.get(variable)
        if actual is None:
            continue
        try:
            matches = Decimal(actual) == Decimal(str(provider[field]))
        except (InvalidOperation, ValueError):
            matches = False
        if not matches:
            failures.append(variable)
    if os.environ.get("DEEPSEEK_REASONING_EFFORT", "").strip():
        failures.append("DEEPSEEK_REASONING_EFFORT")
    key_file = os.environ.get("DEEPSEEK_API_KEY_FILE")
    if not key_file or not Path(key_file).is_file():
        failures.append("DEEPSEEK_API_KEY_FILE")
    return sorted(set(failures))


@contextmanager
def _frozen_workflow_settings(
    manifest: EvaluationManifest, campaign: Campaign, *, live: bool
) -> Any:
    values: dict[str, str] = {}
    if campaign.workflow_cap_usd is not None:
        cap = str(campaign.workflow_cap_usd)
        values.update(
            {
                "SHOPFLOOR_PROVIDER_WORKFLOW_BUDGET": cap,
                "SHOPFLOOR_PROVIDER_RUN_BUDGET": cap,
            }
        )
    if live:
        provider = manifest.payload["live_configuration"]["provider"]
        values.update(
            {variable: str(provider[field]) for variable, field in FROZEN_LIMIT_FIELDS.items()}
        )
        # Thinking is frozen off, so a reasoning effort must not leak in from .env.
        values["DEEPSEEK_REASONING_EFFORT"] = ""
    previous = {name: os.environ.get(name) for name in values}
    try:
        os.environ.update(values)
        yield
    finally:
        for name, old in previous.items():
            if old is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = old


def _not_run(
    item: dict[str, Any], output: Path, reason: str, details: Any = None
) -> dict[str, Any]:
    return {
        **item,
        "output": str(output),
        "attempted": False,
        "status": "NOT_RUN",
        "reason_code": reason,
        "details": details,
        "end_to_end_success": False,
        "supervisor_success": False,
        "counts_as_llm_capability": False,
        "cost_micros": None,
    }


def _failed_attempt(
    item: dict[str, Any],
    output: Path,
    error: Exception,
    *,
    audit_error: str = "runner_exception_without_complete_audit",
) -> dict[str, Any]:
    return {
        **item,
        "output": str(output),
        "attempted": True,
        "audit_complete": False,
        "audit_errors": [audit_error],
        "status": "error",
        "reason_code": type(error).__name__,
        "message": str(error)[:300],
        "end_to_end_success": False,
        "supervisor_success": False,
        "counts_as_llm_capability": False,
        "cost_micros": None,
        "cost_accounting": "unknown_conservatively_reserved_at_workflow_cap",
    }


def _mark_raised_after_summary(record: dict[str, Any], error: Exception) -> dict[str, Any]:
    audit_errors = list(record.get("audit_errors") or [])
    audit_errors.append("runner_raised_after_summary")
    observed_cost = record.get("cost_micros")
    return {
        **record,
        "raised_after_summary": type(error).__name__,
        "observed_status": record.get("status"),
        "observed_reason_code": record.get("reason_code"),
        "status": "error",
        "reason_code": type(error).__name__,
        "audit_complete": False,
        "audit_errors": sorted(set(audit_errors)),
        "end_to_end_success": False,
        "counts_as_llm_capability": False,
        "observed_cost_micros": observed_cost,
        "cost_micros": None,
        "cost_accounting": "unknown_conservatively_reserved_at_workflow_cap",
    }


def _pairs(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    indexed = {
        (item["scenario_id"], item["seed"], item["repetition"], item["arm"]): item
        for item in records
    }
    pairs = []
    case_keys = sorted(
        {(item["scenario_id"], item["seed"], item["repetition"]) for item in records}
    )
    for scenario_id, seed, repetition in case_keys:
        available = {
            arm: indexed.get((scenario_id, seed, repetition, arm))
            for arm in ("baseline", "scripted", "deepseek")
        }
        arms = [arm for arm, row in available.items() if row is not None]
        if len(arms) < 2:
            continue
        reference = available["baseline"] or available[arms[0]]
        assert reference is not None
        for arm in arms:
            if arm == reference["arm"]:
                continue
            compared = available[arm]
            assert compared is not None
            pairs.append(
                {
                    "scenario_id": scenario_id,
                    "seed": seed,
                    "repetition": repetition,
                    "reference_arm": reference["arm"],
                    "comparison_arm": arm,
                    "both_attempted": bool(
                        reference.get("attempted") and compared.get("attempted")
                    ),
                    "semantic_snapshot_equal": (
                        reference.get("semantic_snapshot_sha256") is not None
                        and reference.get("semantic_snapshot_sha256")
                        == compared.get("semantic_snapshot_sha256")
                    ),
                    "actual_metrics_equal": reference.get("metrics") == compared.get("metrics"),
                    "solver_outcomes": {
                        reference["arm"]: reference.get("solver_outcomes"),
                        arm: compared.get("solver_outcomes"),
                    },
                    "input_difference": {
                        "baseline": "frozen structured request or observable-alarm rule",
                        "comparison": (
                            "semantically equivalent natural-language request"
                            if arm == "deepseek"
                            else "pre-recorded infrastructure tool-call fixture"
                        ),
                    },
                }
            )
    return pairs


def _case_key(item: dict[str, Any]) -> tuple[str, str, str, int, int, str] | None:
    try:
        campaign = item["campaign"]
        case_id = item["case_id"]
        scenario_id = item["scenario_id"]
        seed = item["seed"]
        repetition = item["repetition"]
        arm = item["arm"]
    except KeyError:
        return None
    if (
        not isinstance(campaign, str)
        or not isinstance(case_id, str)
        or not isinstance(scenario_id, str)
        or isinstance(seed, bool)
        or not isinstance(seed, int)
        or isinstance(repetition, bool)
        or not isinstance(repetition, int)
        or not isinstance(arm, str)
    ):
        return None
    return campaign, case_id, scenario_id, seed, repetition, arm


def _planned_records_match(planned: list[dict[str, Any]], records: list[dict[str, Any]]) -> bool:
    planned_keys = [_case_key(item) for item in planned]
    record_keys = [_case_key(item) for item in records]
    return bool(
        None not in planned_keys
        and None not in record_keys
        and len(set(planned_keys)) == len(planned_keys)
        and Counter(record_keys) == Counter(planned_keys)
    )


def _metric_at_least(value: Any, threshold: Decimal) -> bool:
    try:
        return Decimal(str(value)) >= threshold
    except (InvalidOperation, ValueError):
        return False


def _metric_at_most(value: Any, threshold: Decimal) -> bool:
    try:
        return Decimal(str(value)) <= threshold
    except (InvalidOperation, ValueError):
        return False


def _provider_contract_matches(
    records: list[dict[str, Any]], expected_provider: dict[str, Any] | None
) -> bool:
    if expected_provider is None:
        return True
    expected_limits = {
        terminal_field: expected_provider[manifest_field]
        for manifest_field, terminal_field in TERMINAL_LIMIT_FIELDS.items()
    }
    expected_model = expected_provider["model"]
    for record in records:
        if record.get("arm") != "deepseek" or record.get("attempted") is not True:
            continue
        actual_limits = record.get("provider_limits")
        if not isinstance(actual_limits, dict) or set(actual_limits) != set(expected_limits):
            return False
        try:
            limits_match = all(
                Decimal(str(actual_limits[field])) == Decimal(str(expected))
                for field, expected in expected_limits.items()
            )
        except (InvalidOperation, ValueError):
            return False
        if not limits_match:
            return False
        model_ids = record.get("model_ids")
        if (
            not isinstance(model_ids, list)
            or (record.get("end_to_end_success") is True and not model_ids)
            or any(
                not isinstance(model_id, str) or model_id != expected_model
                for model_id in model_ids
            )
        ):
            return False
    return True


def _thresholds(
    campaign: Campaign,
    planned: list[dict[str, Any]],
    records: list[dict[str, Any]],
    aggregates: dict[str, Any],
    configured: EvaluationThresholds,
    expected_provider: dict[str, Any] | None = None,
) -> dict[str, Any]:
    invalid_schedules = sum(
        int(group.get("invalid_applied_schedules", 0)) for group in aggregates.values()
    )
    invariant_failures = sum(
        int(group.get("invariant_failures", 0)) for group in aggregates.values()
    )
    truth_leaks = sum(int(group.get("truth_leaks", 0)) for group in aggregates.values())
    contradictions = sum(
        int(group.get("claim_audit_contradictions", 0)) for group in aggregates.values()
    )
    checks: dict[str, bool] = {
        "all_planned_rows_retained": _planned_records_match(planned, records),
        "all_planned_rows_attempted": all(record.get("attempted") is True for record in records),
        "frozen_provider_contract_matches": _provider_contract_matches(records, expected_provider),
        "all_attempted_records_audited": all(
            group.get("audit_incomplete_records") == 0 for group in aggregates.values()
        ),
        "invalid_applied_schedules_within_manifest_threshold": (
            invalid_schedules <= configured.invalid_applied_schedules
        ),
        "invariant_failures_within_manifest_threshold": (
            invariant_failures <= configured.invariant_failures
        ),
        "truth_leaks_within_manifest_threshold": truth_leaks <= configured.truth_leaks,
        "claim_audit_contradictions_within_manifest_threshold": (
            contradictions <= configured.persisted_claim_contradictions
        ),
        "all_citations_resolve": all(
            group.get("citation_validity_numerator") == group.get("citation_validity_denominator")
            for group in aggregates.values()
        ),
    }
    if campaign.campaign_id == "offline":
        checks["all_expected_offline_terminals"] = all(
            item.get("end_to_end_success") for item in records
        )
    elif campaign.campaign_id == "paired_live":
        for scenario_id in campaign.scenarios:
            group = aggregates.get(f"deepseek/{scenario_id}", {})
            checks[f"deepseek_at_least_one_{scenario_id}"] = group.get("end_to_end_success", 0) >= 1
    elif campaign.campaign_id == "reliability":
        scenario_thresholds = {
            "normal": configured.normal_success_rate,
            "urgent_order": configured.urgent_order_success_rate,
            "machine_breakdown": configured.machine_breakdown_success_rate,
        }
        for scenario_id, threshold in scenario_thresholds.items():
            group = aggregates.get(f"deepseek/{scenario_id}", {})
            checks[f"success_rate_{scenario_id}_meets_manifest_threshold"] = _metric_at_least(
                group.get("end_to_end_success_rate_planned_denominator"), threshold
            )
        impossible = aggregates.get("deepseek/no_feasible_recovery", {})
        checks["safe_block_no_feasible_recovery_meets_manifest_threshold"] = _metric_at_least(
            impossible.get("end_to_end_success_rate_planned_denominator"),
            configured.no_feasible_recovery_safe_block_rate,
        )
        checks["mean_cost_each_scenario_meets_manifest_threshold"] = all(
            group.get("cost_usd_mean_planned_denominator") is not None
            and _metric_at_most(
                group["cost_usd_mean_planned_denominator"],
                configured.mean_cost_usd_per_workflow,
            )
            for key, group in aggregates.items()
            if key.startswith("deepseek/")
        )
    return {
        "configured": configured.public_dict(),
        "checks": checks,
        "passed": all(checks.values()),
    }


def _write_csv(path: Path, records: list[dict[str, Any]]) -> None:
    fields = (
        "campaign",
        "case_id",
        "arm",
        "scenario_id",
        "seed",
        "repetition",
        "attempted",
        "audit_complete",
        "audit_errors",
        "status",
        "reason_code",
        "end_to_end_success",
        "supervisor_terminal",
        "supervisor_reason_code",
        "invariants_passed",
        "hard_constraint_violations",
        "truth_leak",
        "tool_schema_rejections",
        "tool_result_rejections",
        "budget_accounting_verified",
        "provider_attempts",
        "used_tokens",
        "cost_micros",
        "active_seconds",
        "wall_seconds",
        "semantic_snapshot_sha256",
    )
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(records)


def run_evaluation(
    *,
    mode: str,
    manifest_path: Path,
    output: Path,
    campaign_id: str | None = None,
) -> EvaluationResult:
    if mode not in MODE_CAMPAIGN:
        raise BaselineFailure(
            "invalid_evaluation_mode",
            "mode must be baseline, scripted or deepseek",
            BaselineExitCode.USAGE,
        )
    manifest = load_evaluation_manifest(manifest_path)
    selected_campaign = campaign_id or MODE_CAMPAIGN[mode]
    campaign = manifest.campaign(selected_campaign)
    if mode != "deepseek" and selected_campaign != "offline":
        raise BaselineFailure(
            "invalid_mode_campaign",
            "baseline/scripted CLI modes select the offline campaign",
            BaselineExitCode.USAGE,
        )
    if mode == "deepseek" and selected_campaign not in {"paired_live", "reliability"}:
        raise BaselineFailure(
            "invalid_mode_campaign",
            "deepseek mode selects paired_live or reliability",
            BaselineExitCode.USAGE,
        )
    target = _prepare_output(output)
    planned = _planned(campaign, mode)
    _write(
        target / "manifest_snapshot.json",
        {
            "schema_version": 1,
            "manifest_id": manifest.manifest_id,
            "manifest_file_sha256": manifest.file_sha256,
            "manifest_canonical_sha256": manifest.canonical_sha256,
            "payload": manifest.payload,
            "selected_campaign": selected_campaign,
            "selected_mode": mode,
            "planned_rows": planned,
        },
    )
    retrieval = evaluate_retrieval_fixture(
        LexicalRetriever.from_default_corpus(),
        REPOSITORY_ROOT / "tests/retrieval/fixtures/frozen_queries.json",
    ).model_dump(mode="json")
    _write(target / "retrieval_fixture_results.json", retrieval)
    live_failures = _live_preflight(manifest) if mode == "deepseek" else []
    started = datetime.now(UTC).isoformat(timespec="seconds")
    records: list[dict[str, Any]] = []
    spent = Decimal(0)
    with _frozen_workflow_settings(manifest, campaign, live=mode == "deepseek"):
        for index, item in enumerate(planned, start=1):
            arm = item["arm"]
            run_output = (
                target
                / "runs"
                / item["scenario_id"]
                / f"seed-{item['seed']}"
                / f"repeat-{item['repetition']}"
                / arm
            )
            if arm == "deepseek" and live_failures:
                record = _not_run(item, run_output, "live_preflight_failed", live_failures)
                records.append(record)
                continue
            if (
                arm == "deepseek"
                and campaign.aggregate_cap_usd is not None
                and campaign.workflow_cap_usd is not None
                and spent + campaign.workflow_cap_usd > campaign.aggregate_cap_usd
            ):
                record = _not_run(item, run_output, "aggregate_cap_guard")
                records.append(record)
                continue
            began = time.monotonic()
            try:
                run_recovery_scenario(
                    provider_kind=PROVIDER_KIND[arm],
                    scenario_id=item["scenario_id"],
                    seed=item["seed"],
                    output=run_output,
                    approval_policy="deterministic",
                )
                record = {
                    **item,
                    **collect_run(
                        arm=arm,
                        campaign_id=campaign.campaign_id,
                        scenario_id=item["scenario_id"],
                        seed=item["seed"],
                        repetition=item["repetition"],
                        output=run_output,
                        wall_seconds=time.monotonic() - began,
                    ),
                }
            except Exception as exc:  # every failed denominator is retained
                if (run_output / "summary.json").is_file():
                    try:
                        collected = collect_run(
                            arm=arm,
                            campaign_id=campaign.campaign_id,
                            scenario_id=item["scenario_id"],
                            seed=item["seed"],
                            repetition=item["repetition"],
                            output=run_output,
                            wall_seconds=time.monotonic() - began,
                        )
                    except Exception as collect_exc:
                        record = _failed_attempt(
                            item,
                            run_output,
                            collect_exc,
                            audit_error="collector_failed_after_runner_exception",
                        )
                        record["raised_after_summary"] = type(exc).__name__
                    else:
                        record = _mark_raised_after_summary({**item, **collected}, exc)
                else:
                    record = _failed_attempt(item, run_output, exc)
            if arm == "deepseek":
                cost = record.get("cost_micros")
                spent += (
                    Decimal(cost) / Decimal(1_000_000)
                    if cost is not None
                    else campaign.workflow_cap_usd or Decimal(0)
                )
            records.append(record)
            print(
                json.dumps(
                    {
                        "run": index,
                        "of": len(planned),
                        "arm": arm,
                        "scenario": item["scenario_id"],
                        "seed": item["seed"],
                        "repetition": item["repetition"],
                        "status": record.get("status"),
                        "reason": record.get("supervisor_reason_code") or record.get("reason_code"),
                        "spent_usd_conservative": str(spent),
                    },
                    sort_keys=True,
                ),
                flush=True,
            )
    aggregates = aggregate_records(records, planned)
    paired = _pairs(records)
    threshold_results = _thresholds(
        campaign,
        planned,
        records,
        aggregates,
        manifest.thresholds,
        manifest.payload["live_configuration"]["provider"] if mode == "deepseek" else None,
    )
    if campaign.campaign_id == "offline" and mode in {"baseline", "scripted"}:
        # A single-arm invocation cannot compare arms but must still preserve its 20 rows.
        threshold_results["checks"]["minimum_20_rows_for_selected_offline_arm"] = len(records) == 20
        threshold_results["passed"] = all(threshold_results["checks"].values())
    report = {
        "schema_version": 1,
        "evaluation": "S13 fair evaluation and regression matrix",
        "status": "PASS" if threshold_results["passed"] else "FAIL",
        "started_at_utc": started,
        "finished_at_utc": datetime.now(UTC).isoformat(timespec="seconds"),
        "mode": mode,
        "campaign": campaign.campaign_id,
        "manifest": {
            "id": manifest.manifest_id,
            "file_sha256": manifest.file_sha256,
            "canonical_sha256": manifest.canonical_sha256,
        },
        "denominators": {
            "planned_rows": len(planned),
            "attempted_rows": sum(1 for item in records if item.get("attempted")),
            "not_run_rows": sum(1 for item in records if not item.get("attempted")),
            "failed_or_blocked_rows": sum(
                1 for item in records if not item.get("end_to_end_success")
            ),
        },
        "budget": {
            "currency": "USD" if campaign.workflow_cap_usd is not None else None,
            "workflow_cap_usd": None
            if campaign.workflow_cap_usd is None
            else str(campaign.workflow_cap_usd),
            "aggregate_cap_usd": None
            if campaign.aggregate_cap_usd is None
            else str(campaign.aggregate_cap_usd),
            "worst_case_usd": None
            if campaign.workflow_cap_usd is None
            else str(campaign.workflow_cap_usd * campaign.planned_paid_runs),
            "spent_usd_conservative": str(spent),
            "unknown_cost_is_reserved_at_full_workflow_cap": True,
        },
        "aggregate": aggregates,
        "thresholds": threshold_results,
        "paired_comparisons": paired,
        "retrieval": retrieval["summary"],
        "attribution": {
            "solver": "Shared deterministic CP-SAT engine owns all schedule objectives/outcomes.",
            "baseline": (
                "Fixed rule policy over observable alarms or frozen structured input; no LLM."
            ),
            "scripted": "Infrastructure contract evidence only; never counted as LLM capability.",
            "deepseek": (
                "Capability is scored only for interpretation, evidence selection, tool/schema "
                "orchestration and supported claims; solver gains are not model gains."
            ),
        },
        "live_preflight_failures": live_failures,
        "records": records,
    }
    _write(target / "report.json", report)
    _write_csv(target / "runs.csv", records)
    shutil.copyfile(manifest.path, target / "frozen_manifest.json")
    return EvaluationResult(
        report["status"],
        int(
            BaselineExitCode.SUCCESS
            if threshold_results["passed"]
            else BaselineExitCode.WORKFLOW_FAILURE
        ),
        report,
        target,
    )
