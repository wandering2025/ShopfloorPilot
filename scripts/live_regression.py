"""Bounded live regression over the frozen evaluation manifest (5 runs per scenario).

The default is a no-network preflight. ``--execute`` launches the public recovery
CLI 20 times with deterministic batch approval, then reuses the evaluation
collector and reliability thresholds. This is never human-approval evidence.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from shopfloor_pilot.baseline import _prepare_output
from shopfloor_pilot.evaluation.collector import aggregate_records, collect_run
from shopfloor_pilot.evaluation.manifest import (
    REPOSITORY_ROOT,
    Campaign,
    EvaluationManifest,
    load_evaluation_manifest,
)
from shopfloor_pilot.evaluation.runner import (
    FROZEN_LIMIT_FIELDS,
    LIVE_SETTING_FIELDS,
    _failed_attempt,
    _not_run,
    _planned,
    _planned_records_match,
    _thresholds,
    _write,
    _write_csv,
)

MANIFEST_SHA256 = "9394f48a9ea29eddebb65be50a3152a210eb87a33a6222fa60c63e9fd28ce9a4"
AGGREGATE_CAP = Decimal("0.30")
WORKFLOW_CAP = Decimal("0.0125")


def release_campaign(manifest: EvaluationManifest) -> Campaign:
    """Freeze five independent repetitions of seed zero for every core scenario."""

    if manifest.file_sha256 != MANIFEST_SHA256:
        raise ValueError("S13 accepted manifest hash differs; reassess the freeze before running")
    original = manifest.campaign("reliability")
    if original.workflow_cap_usd != WORKFLOW_CAP:
        raise ValueError("S13 workflow cap differs")
    campaign = replace(
        original,
        campaign_id="s14-release",
        seeds=(0,),
        aggregate_cap_usd=AGGREGATE_CAP,
    )
    if campaign.planned_paid_runs != 20:
        raise ValueError("S14 requires exactly twenty paid workflows")
    if campaign.planned_paid_runs * WORKFLOW_CAP > AGGREGATE_CAP:
        raise ValueError("worst-case cost exceeds the release aggregate cap")
    return campaign


def frozen_environment(
    manifest: EvaluationManifest, key_file: Path, inherited: Mapping[str, str]
) -> dict[str, str]:
    """Prevent ambient provider overrides; the key file is read only by the backend."""

    environment = {
        name: value
        for name, value in inherited.items()
        if not name.startswith(("DEEPSEEK_", "SHOPFLOOR_PROVIDER_"))
    }
    config = manifest.payload["live_configuration"]
    environment.update(
        {
            name: str(config[section][field])
            for name, (section, field) in LIVE_SETTING_FIELDS.items()
        }
    )
    environment.update(
        {name: str(config["provider"][field]) for name, field in FROZEN_LIMIT_FIELDS.items()}
    )
    environment.update(
        {
            "DEEPSEEK_API_KEY_FILE": str(key_file.resolve()),
            "DEEPSEEK_REASONING_EFFORT": "",
            "SHOPFLOOR_PROVIDER_WORKFLOW_BUDGET": str(WORKFLOW_CAP),
            "SHOPFLOOR_PROVIDER_RUN_BUDGET": str(WORKFLOW_CAP),
            # S13 used the unchanged provider defaults; pin them against ambient overrides.
            "SHOPFLOOR_PROVIDER_RETRY_BACKOFF_SECONDS": "1.0",
            "SHOPFLOOR_PROVIDER_RETRY_MAX_BACKOFF_SECONDS": "4.0",
        }
    )
    return environment


def accounted_cost(record: Mapping[str, Any]) -> Decimal:
    """Unknown or unverified usage reserves the entire workflow allowance."""

    cost = record.get("cost_micros")
    if (
        record.get("budget_accounting_verified") is not True
        or isinstance(cost, bool)
        or not isinstance(cost, int)
        or cost < 0
    ):
        return WORKFLOW_CAP
    return Decimal(cost) / Decimal(1_000_000)


def score(
    manifest: EvaluationManifest,
    campaign: Campaign,
    planned: list[dict[str, Any]],
    records: list[dict[str, Any]],
    spent: Decimal,
) -> tuple[dict[str, Any], dict[str, Any]]:
    # Invalid identities must fail before rates/Wilson intervals are computed.
    aggregates = (
        aggregate_records(records, planned) if _planned_records_match(planned, records) else {}
    )
    # Reuse every S13 reliability check, retaining the actual S14 row identities.
    thresholds = _thresholds(
        replace(campaign, campaign_id="reliability"),
        planned,
        records,
        aggregates,
        manifest.thresholds,
        manifest.payload["live_configuration"]["provider"],
    )
    thresholds["checks"].update(
        {
            "exactly_twenty_release_rows": len(planned) == len(records) == 20,
            "all_release_expected_outcomes": all(
                item.get("end_to_end_success") is True for item in records
            ),
            "aggregate_cost_within_release_cap": spent <= AGGREGATE_CAP,
            "all_cost_accounting_verified": all(
                item.get("budget_accounting_verified") is True for item in records
            ),
            "each_workflow_cost_within_frozen_cap": all(
                accounted_cost(item) <= WORKFLOW_CAP for item in records
            ),
        }
    )
    thresholds["passed"] = all(thresholds["checks"].values())
    return aggregates, thresholds


def execute(
    manifest: EvaluationManifest,
    campaign: Campaign,
    environment: dict[str, str],
    output: Path,
    preflight: dict[str, Any],
) -> dict[str, Any]:
    target = _prepare_output(output)
    planned = _planned(campaign, "deepseek")
    _write(target / "preflight.json", preflight)
    (target / "frozen_manifest.json").write_bytes(manifest.path.read_bytes())
    records: list[dict[str, Any]] = []
    spent = Decimal(0)
    started = datetime.now(UTC).isoformat(timespec="seconds")
    for index, item in enumerate(planned, start=1):
        run_output = target / "runs" / item["case_id"]
        if spent + WORKFLOW_CAP > AGGREGATE_CAP:
            record = _not_run(item, run_output, "aggregate_cap_guard")
        else:
            began = time.monotonic()
            command = [
                sys.executable,
                "-m",
                "shopfloor_pilot.cli",
                "recovery",
                "--provider",
                "deepseek",
                "--scenario",
                item["scenario_id"],
                "--seed",
                str(item["seed"]),
                "--output",
                str(run_output),
                "--approval-policy",
                "deterministic",
            ]
            try:
                process = subprocess.run(
                    command,
                    cwd=REPOSITORY_ROOT,
                    env=environment,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                    timeout=240,
                )
                record = {
                    **item,
                    **collect_run(
                        arm="deepseek",
                        campaign_id=campaign.campaign_id,
                        scenario_id=item["scenario_id"],
                        seed=item["seed"],
                        repetition=item["repetition"],
                        output=run_output,
                        wall_seconds=time.monotonic() - began,
                    ),
                    "cli_exit_code": process.returncode,
                }
                if process.returncode != 0:
                    record["end_to_end_success"] = False
                    record["audit_complete"] = False
                    record["audit_errors"] = [
                        *record.get("audit_errors", []),
                        "recovery_cli_nonzero_exit",
                    ]
            except Exception as exc:
                # Record only the exception type, never a command/environment/provider body.
                record = _failed_attempt(item, run_output, RuntimeError(type(exc).__name__))
            spent += accounted_cost(record)
        records.append(record)
        _write(target / "records.json", records)
        _write_csv(target / "runs.csv", records)
        print(
            json.dumps(
                {
                    "run": index,
                    "of": len(planned),
                    "scenario": item["scenario_id"],
                    "repetition": item["repetition"],
                    "success": record.get("end_to_end_success", False),
                    "status": record.get("status"),
                    "spent_usd_conservative": str(spent),
                },
                sort_keys=True,
            ),
            flush=True,
        )
    aggregates, thresholds = score(manifest, campaign, planned, records, spent)
    report = {
        "schema_version": 1,
        "evaluation": "S14 release-candidate regression; deterministic batch approval",
        "status": "PASS" if thresholds["passed"] else "FAIL",
        "started_at_utc": started,
        "finished_at_utc": datetime.now(UTC).isoformat(timespec="seconds"),
        "preflight": preflight,
        "planned_rows": planned,
        "records": records,
        "aggregate": aggregates,
        "thresholds": thresholds,
        "spent_usd_conservative": str(spent),
    }
    _write(target / "report.json", report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", help="make the 20 paid recovery runs")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--key-file", type=Path, default=os.environ.get("DEEPSEEK_API_KEY_FILE"))
    parser.add_argument(
        "--tariff-checked-date", required=True, help="date of official tariff review"
    )
    args = parser.parse_args()
    manifest = load_evaluation_manifest(REPOSITORY_ROOT / "scenarios/evaluation.json")
    campaign = release_campaign(manifest)
    if args.tariff_checked_date != manifest.payload["live_configuration"]["pricing"]["tariff_date"]:
        parser.error("tariff review date differs from the frozen tariff; reassess before running")
    if args.key_file is None or not args.key_file.is_file():
        parser.error(
            "provide an existing private key file using --key-file or DEEPSEEK_API_KEY_FILE"
        )
    if (REPOSITORY_ROOT / ".env").exists():
        parser.error("run from an isolated candidate without .env to avoid implicit settings")
    commit = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=REPOSITORY_ROOT, text=True
    ).strip()
    dirty = subprocess.check_output(
        ["git", "status", "--porcelain", "--untracked-files=no"], cwd=REPOSITORY_ROOT, text=True
    ).strip()
    if args.execute and dirty:
        parser.error("paid release gate requires a clean committed candidate")
    environment = frozen_environment(manifest, args.key_file, os.environ)
    preflight = {
        "candidate_commit": commit,
        "tracked_tree_clean": not dirty,
        "manifest_sha256": manifest.file_sha256,
        "manifest_canonical_sha256": manifest.canonical_sha256,
        "prompt_contract_sha256": manifest.payload["prompt_contract_sha256"],
        "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "live_configuration": manifest.payload["live_configuration"],
        "retry_backoff_seconds": [1.0, 2.0, 4.0],
        "tariff_checked_date": args.tariff_checked_date,
        "scenarios": campaign.scenarios,
        "seeds": campaign.seeds,
        "repetitions_per_seed": campaign.repetitions_per_seed,
        "planned_workflows": campaign.planned_paid_runs,
        "workflow_and_run_cap_usd": str(WORKFLOW_CAP),
        "aggregate_cap_usd": str(AGGREGATE_CAP),
        "worst_case_usd": str(campaign.planned_paid_runs * WORKFLOW_CAP),
        "approval_actor": "policy:deterministic",
        "key_file_exists": True,
        "no_paid_calls_during_preflight": True,
    }
    print(json.dumps(preflight, indent=2, sort_keys=True), flush=True)
    if not args.execute:
        return 0
    if args.output is None:
        parser.error("--execute requires a new --output directory")
    report = execute(manifest, campaign, environment, args.output.resolve(), preflight)
    print(json.dumps({"status": report["status"], "output": str(args.output)}, sort_keys=True))
    return 0 if report["status"] == "PASS" else 4


if __name__ == "__main__":
    raise SystemExit(main())
