"""Write sanitized S10 acceptance evidence; offline only, no paid provider."""

from __future__ import annotations

import argparse
import hashlib
import json
import tempfile
from pathlib import Path
from typing import Any

from agent_support import (
    FAULT_SENTINEL,
    FAULT_TRUTH_REF,
    fixtures_by_id,
    fresh_run,
    run_supervisor,
)
from test_interpretation import field_results

from shopfloor_pilot.agent.harness import (
    ABNORMALITY_TASKS,
    load_request_fixtures,
    scripted_abnormality_turns,
    scripted_order_turns,
)
from shopfloor_pilot.agent.trace import normalize_for_comparison
from shopfloor_pilot.agent.workflow import OFFLINE_EVIDENCE_LABEL, SupervisorTask
from shopfloor_pilot.tools import WorkflowKind, build_supervisor_registry


def _write(path: Path, value: Any) -> str:
    text = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    path.write_text(text)
    return hashlib.sha256(text.encode()).hexdigest()


def _scripted(tmp: Path, scenario: str) -> Any:
    handle = fresh_run(tmp, scenario)
    if scenario == "machine_breakdown":
        text, machines, query = ABNORMALITY_TASKS[scenario]
        return run_supervisor(
            handle,
            SupervisorTask(WorkflowKind.ABNORMALITY, text, machines),
            scripted_abnormality_turns(query),
        )
    fixture_id = "normal-en-plate-today" if scenario == "normal" else "urgent-zh-plate-90min"
    fixture = fixtures_by_id()[fixture_id]
    return run_supervisor(
        handle,
        SupervisorTask(WorkflowKind.ORDER_REQUEST, fixture.request_text),
        scripted_order_turns(fixture.scripted_draft),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    output = parser.parse_args().output
    output.mkdir(parents=True, exist_ok=True)
    hashes: dict[str, str] = {}
    with tempfile.TemporaryDirectory(prefix="s10-evidence-") as raw:
        tmp = Path(raw)
        rows = [
            field_results(item.fixture_id, tmp / item.fixture_id)
            for item in load_request_fixtures().fixtures
        ]
        hashes["a01_interpretation_fields.json"] = _write(
            output / "a01_interpretation_fields.json",
            {
                "label": OFFLINE_EVIDENCE_LABEL,
                "fixture_set": "data/agent_requests/requests.json",
                "normalization": "NFKC + casefold + collapsed whitespace on spans only",
                "supported_matching": sum(
                    row["all_match"] and row["fields"][0]["expected"] == "success" for row in rows
                ),
                "clarify_or_reject_matching": sum(
                    row["all_match"] and row["fields"][0]["expected"] != "success" for row in rows
                ),
                "results": rows,
            },
        )
        hashes["tool_registry.json"] = _write(
            output / "tool_registry.json",
            {"tools": [item.public_dict() for item in build_supervisor_registry().specs()]},
        )
        determinism = []
        for scenario in ("normal", "urgent_order", "machine_breakdown"):
            runs = [_scripted(tmp / f"det-{scenario}-{n}", scenario) for n in (1, 2)]
            digests = [
                hashlib.sha256(
                    json.dumps(normalize_for_comparison(run.trace_view), sort_keys=True).encode()
                ).hexdigest()
                for run in runs
            ]
            solver = [
                next(
                    item.payload["result"]["solver_outcome"]
                    for item in run.records
                    if item.kind == "tool_result" and item.payload["tool"] == "solve_schedule"
                )
                for run in runs
            ]
            determinism.append(
                {
                    "scenario": scenario,
                    "normalized_trace_sha256": digests,
                    "traces_match": digests[0] == digests[1],
                    "proposal_hashes": [run.outcome.proposal["proposal_hash"] for run in runs],
                    "proposals_match": runs[0].outcome.proposal == runs[1].outcome.proposal,
                    "solver_outcomes": solver,
                }
            )
        hashes["a11_determinism.json"] = _write(
            output / "a11_determinism.json",
            {
                "label": OFFLINE_EVIDENCE_LABEL,
                "excluded_fields": sorted(
                    [
                        "recorded_at_utc",
                        "latency_seconds",
                        "active_seconds",
                        "created_at_utc",
                        "budget",
                    ]
                ),
                "note": (
                    "normal hits the 5 s CP-SAT wall limit (feasible, not proven optimal); "
                    "S04 documents that wall-limited incumbents are not guaranteed identical "
                    "across hosts or load."
                ),
                "runs": determinism,
            },
        )
        fault = _scripted(tmp / "fault", "machine_breakdown")
        prompts = "\n".join(request.estimated_payload_text() for request in fault.provider.requests)
        trace_text = json.dumps([item.public_dict() for item in fault.records], ensure_ascii=False)
        hashes["a09_fault_trace.json"] = _write(
            output / "a09_fault_trace.json",
            {
                "label": OFFLINE_EVIDENCE_LABEL,
                "terminal": fault.outcome.terminal.value,
                "claims": list(fault.outcome.claims),
                "evidence": list(fault.outcome.evidence),
                "scan": {
                    "evaluator_sentinel_in_prompts": FAULT_SENTINEL in prompts,
                    "evaluator_sentinel_in_trace": FAULT_SENTINEL in trace_text,
                    "truth_ref_in_prompts_or_trace": FAULT_TRUTH_REF in prompts + trace_text,
                    "reasoning_key_in_trace": "reasoning" in trace_text,
                    "provider_requests": len(fault.provider.requests),
                    "trace_records": len(fault.records),
                },
                "trace_projection": fault.trace_view,
            },
        )
    _write(output / "offline_evidence_hashes.json", hashes)


if __name__ == "__main__":
    main()
