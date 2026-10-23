"""Write sanitized, measured S04 fixture evidence; no production state or API."""

import argparse
import json
import platform
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import ortools
from cases import scenario_case, tiny_case

from shopfloor_pilot.domain.canonical import canonical_hash
from shopfloor_pilot.scheduling import SolveOutcome, solve_schedule
from shopfloor_pilot.validation import validate_candidate


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    output = parser.parse_args().output
    cpu = next(
        line.split(":", 1)[1].strip()
        for line in Path("/proc/cpuinfo").read_text().splitlines()
        if line.startswith("model name")
    )
    rows = []
    for name in ("tiny", "normal", "urgent_order", "machine_breakdown", "no_feasible_recovery"):
        case = tiny_case() if name == "tiny" else scenario_case(name)
        runs = []
        for number in range(1, 6 if name in ("tiny", "normal") else 2):
            before = canonical_hash(case.snapshot)
            result = solve_schedule(
                case.snapshot,
                case.catalog,
                proposal_id="proposal-evidence",
                workflow_id="workflow-evidence",
                proposed_order_deltas=case.order_deltas,
            )
            unchanged = before == canonical_hash(case.snapshot)
            validation = (
                validate_candidate(
                    case.snapshot,
                    case.catalog,
                    result.candidate,
                    projected_order_deltas=case.order_deltas,
                )
                if result.candidate
                else None
            )
            assert unchanged
            if name == "no_feasible_recovery":
                assert result.outcome is SolveOutcome.INFEASIBLE
                assert result.candidate is None
            else:
                assert validation is not None and validation.is_valid
            if name in ("tiny", "normal"):
                assert (
                    result.report.total_elapsed_seconds <= result.report.config.max_time_seconds + 2
                )
            runs.append(
                {
                    "run": number,
                    "outcome": result.outcome.value,
                    "candidate_hash": canonical_hash(result.candidate)
                    if result.candidate
                    else None,
                    "objective": (
                        result.candidate.objective.model_dump(mode="json")
                        if result.candidate
                        else None
                    ),
                    "independently_valid": validation.is_valid if validation else None,
                    "snapshot_unchanged": unchanged,
                    "report": result.report.model_dump(mode="json", exclude={"response_stats"}),
                }
            )
        hashes = {run["candidate_hash"] for run in runs}
        assert len(hashes) == 1
        rows.append(
            {
                "case": name,
                "snapshot_hash": canonical_hash(case.snapshot),
                "catalog_hash": canonical_hash(case.catalog),
                "order_delta_hash": canonical_hash(case.order_deltas),
                "simulated_time_seconds": case.snapshot.simulated_time_seconds,
                "hard_horizon_seconds": case.snapshot.hard_horizon_seconds,
                "fixed_attempts": [a.model_dump(mode="json") for a in case.snapshot.attempts],
                "all_candidate_hashes_equal": len(hashes) == 1,
                "runs": runs,
            }
        )
    evidence = {
        "recorded_at": datetime.now(ZoneInfo("Europe/Berlin")).isoformat(timespec="seconds"),
        "environment": {
            "cpu": cpu,
            "platform": platform.platform(),
            "python": platform.python_version(),
            "ortools": ortools.__version__,
            "gpu_workload": False,
        },
        "cases": rows,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(evidence, indent=2) + "\n")
    print(f"PASS: {len(rows)} cases, {sum(len(row['runs']) for row in rows)} solves; {output}")


if __name__ == "__main__":
    main()
