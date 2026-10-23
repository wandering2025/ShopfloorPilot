"""Independent, denominator-preserving scoring of one recovery-run artifact set."""

from __future__ import annotations

import json
import math
import re
import sqlite3
import statistics
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from shopfloor_pilot.agent.harness import load_request_fixtures
from shopfloor_pilot.domain.canonical import canonical_hash
from shopfloor_pilot.domain.fixtures import load_scenario
from shopfloor_pilot.services import ApplicationServices
from shopfloor_pilot.storage import SQLiteStateRepository
from shopfloor_pilot.workflows.claim_audit import audit_claims
from shopfloor_pilot.workflows.scenario_runner import EXPECTED_TERMINALS

SUPERVISOR_SUCCESS = {
    "normal": "awaiting_approval",
    "urgent_order": "awaiting_approval",
    "machine_breakdown": "awaiting_approval",
    "no_feasible_recovery": "infeasible",
}
REQUEST_FIXTURE = {
    "normal": "normal-en-plate-today",
    "urgent_order": "urgent-zh-plate-90min",
}
SUMMARY_PROVIDER = {
    "baseline": "deterministic-baseline",
    "scripted": "scripted-fake",
    "deepseek": "deepseek",
}
EXPECTED_INVARIANT_CODES = {
    "applied_plans_pass_independent_validator",
    "one_plan_application_per_consumed_approval",
    "approvals_by_human_or_named_policy_only",
    "completed_work_never_changes",
    "unaffected_running_work_fixed",
    "interrupted_attempts_restart_full_duration",
    "wasted_work_counted_once",
    "replay_reproduces_current_checkpoint",
    "decisions_bound_to_recorded_boundary",
}
REQUIRED_METRIC_FIELDS = {
    "actual_outcome_basis",
    "actual_makespan_seconds",
    "actual_makespan_lower_bound_seconds",
    "actual_weighted_tardiness_seconds",
    "weighted_tardiness_lower_bound_seconds",
    "predicted_makespan_seconds",
    "predicted_weighted_tardiness_seconds",
    "unfinished_order_count",
    "wasted_processing_seconds",
}
REQUIRED_SNAPSHOT_FIELDS = {
    "schema_version",
    "simulated_time_seconds",
    "hard_horizon_seconds",
    "orders",
    "machines",
    "operations",
    "attempts",
}
TRUTH_MARKERS = ("EVAL_ONLY_SENTINEL", "private_truth_ref", "fault_truth")
_CAUSAL = re.compile(
    r"\b(?:root cause is|caused by|confirmed cause|definitively due to|proves? that .* caused)\b",
    re.IGNORECASE,
)
_QUALIFIED = re.compile(
    r"\b(?:possible|plausible|hypothesis|unverified|not verified|not confirmed|"
    r"no cause is confirmed|cannot confirm)\b",
    re.IGNORECASE,
)
_SUPPORT_PREFIXES = {
    "observation": ("observation:", "snapshot:", "event_log:"),
    "hypothesis": ("hypothesis:", "guidance:", "diagnosis:", "observation:"),
    "plan": ("validation:", "solver:", "interpretation:"),
    "limitation": ("snapshot:", "observation:", "guidance:", "hypothesis:"),
}


def wilson(successes: int, total: int, z: float = 1.959964) -> list[float] | None:
    if total == 0:
        return None
    proportion = successes / total
    denominator = 1 + z * z / total
    centre = (proportion + z * z / (2 * total)) / denominator
    margin = (
        z
        * math.sqrt(proportion * (1 - proportion) / total + z * z / (4 * total * total))
        / denominator
    )
    return [round(max(0.0, centre - margin), 4), round(min(1.0, centre + margin), 4)]


def percentile(values: Iterable[float], fraction: float) -> float | None:
    ordered = sorted(values)
    if not ordered:
        return None
    index = min(len(ordered) - 1, max(0, math.ceil(fraction * len(ordered)) - 1))
    return round(ordered[index], 6)


def _load(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return default


def _trace_payloads(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            rows = connection.execute(
                "SELECT kind, payload_json FROM agent_trace ORDER BY workflow_id, sequence"
            ).fetchall()
        finally:
            connection.close()
    except sqlite3.Error:
        return []
    payloads = []
    for kind, encoded in rows:
        try:
            payload = json.loads(encoded)
        except (json.JSONDecodeError, TypeError):
            payload = {"decode_error": True}
        payloads.append(
            {
                "kind": kind,
                "payload": payload if isinstance(payload, dict) else {"decode_error": True},
            }
        )
    return payloads


def _ledger(path: Path) -> dict[str, Any]:
    result: dict[str, Any] = {
        "available": path.is_file(),
        "read_error": False,
        "latencies": [],
        "errors": {},
        "cost_micros": 0,
        "used_tokens": 0,
        "attempts": 0,
    }
    if not path.is_file():
        return result
    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            for latency, error, cost, tokens in connection.execute(
                "SELECT latency_micros, error_code, actual_cost_micros, used_tokens "
                "FROM provider_attempts ORDER BY started_at_utc"
            ):
                result["attempts"] += 1
                result["latencies"].append(round(latency / 1_000_000, 6))
                result["cost_micros"] += int(cost)
                result["used_tokens"] += int(tokens)
                if error:
                    result["errors"][error] = result["errors"].get(error, 0) + 1
        finally:
            connection.close()
    except (sqlite3.Error, TypeError, ValueError):
        result["read_error"] = True
    return result


def _non_negative_integer(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _budget_audit_errors(terminal: Mapping[str, Any], ledger: Mapping[str, Any]) -> list[str]:
    """Cross-check the independently read attempt ledger against the terminal trace."""

    budget = terminal.get("budget")
    if not isinstance(budget, Mapping):
        return ["terminal_budget_invalid"]
    expected = {
        "provider_attempts": ledger.get("attempts"),
        "used_tokens": ledger.get("used_tokens"),
        "actual_cost_micros": ledger.get("cost_micros"),
    }
    actual = {field: _non_negative_integer(budget.get(field)) for field in expected}
    if any(value is None for value in actual.values()):
        return ["terminal_budget_invalid"]
    if ledger.get("available") is not True or ledger.get("read_error") is True:
        return []
    if actual != expected:
        return ["provider_ledger_terminal_budget_mismatch"]
    return []


def _expected_interpretation(scenario_id: str) -> dict[str, Any] | None:
    fixture_id = REQUEST_FIXTURE.get(scenario_id)
    if fixture_id is None:
        return None
    fixture = next(
        item for item in load_request_fixtures().fixtures if item.fixture_id == fixture_id
    )
    return dict(fixture.expected)


def _interpretation_score(scenario_id: str, payloads: list[dict[str, Any]]) -> dict[str, Any]:
    expected = _expected_interpretation(scenario_id)
    if expected is None:
        return {"applicable": False, "correct": None, "mismatches": []}
    actual = None
    for record in payloads:
        payload = record["payload"]
        if record["kind"] == "tool_result" and payload.get("tool") == (
            "submit_order_interpretation"
        ):
            result = payload.get("result")
            actual = result.get("interpretation") if isinstance(result, Mapping) else None
            break
    if not isinstance(actual, Mapping):
        return {
            "applicable": True,
            "fixture_id": REQUEST_FIXTURE[scenario_id],
            "correct": False,
            "mismatches": ["missing_interpretation"],
        }
    mismatches = [key for key, value in expected.items() if actual.get(key) != value]
    return {
        "applicable": True,
        "fixture_id": REQUEST_FIXTURE[scenario_id],
        "correct": not mismatches,
        "mismatches": mismatches,
        "expected": expected,
        "actual": {key: actual.get(key) for key in expected},
    }


def _claim_score(claims: list[dict[str, Any]], evidence_ids: set[str]) -> dict[str, Any]:
    citation_ids = [
        str(evidence_id) for claim in claims for evidence_id in claim.get("evidence_ids", [])
    ]
    unresolved = sorted({item for item in citation_ids if item not in evidence_ids})
    unsupported = []
    causal = []
    for index, claim in enumerate(claims):
        kind = str(claim.get("kind", ""))
        cited = [str(item) for item in claim.get("evidence_ids", [])]
        prefixes = _SUPPORT_PREFIXES.get(kind, ())
        if not cited or (prefixes and not any(item.startswith(prefixes) for item in cited)):
            unsupported.append(index)
        text = str(claim.get("text", ""))
        if _CAUSAL.search(text) and not _QUALIFIED.search(text):
            causal.append(index)
    return {
        "claims": len(claims),
        "citations": len(citation_ids),
        "resolved_citations": len(citation_ids) - len(unresolved),
        "unresolved_evidence_ids": unresolved,
        "citation_validity_passed": not unresolved,
        "kind_supported_claims": len(claims) - len(unsupported),
        "unsupported_claim_indexes": unsupported,
        "unsupported_causal_claim_indexes": causal,
    }


def _claim_audit(
    scenario_id: str,
    seed: int,
    output: Path,
    summary: Mapping[str, Any],
    claims: list[dict[str, Any]],
) -> dict[str, Any]:
    supervisor = [
        item for item in summary.get("decisions", []) if item.get("source") == "supervisor"
    ]
    if not supervisor:
        return {
            "error": "supervisor_decision_not_found",
            "claim_count": len(claims),
            "counts": {"consistent": 0, "ambiguous": 0, "contradiction": 0},
            "has_contradiction": None,
        }
    lifecycle = _load(output / "lifecycle.json", {})
    workflow = next(
        (
            item
            for item in lifecycle.get("workflows", [])
            if item.get("workflow_id") == supervisor[0].get("workflow_id")
        ),
        None,
    )
    if workflow is None:
        return {"error": "workflow_not_found", "claim_count": len(claims)}
    base_version = workflow.get("payload", {}).get("base_state_version")
    bundle = load_scenario(scenario_id)
    services = ApplicationServices(
        SQLiteStateRepository(output / "state.sqlite3"), bundle.catalog, seed=seed
    )
    snapshot = services.get_checkpoint(summary["run_id"], state_version=base_version).snapshot
    audit = audit_claims(claims, snapshot)
    return {
        "claim_count": len(claims),
        "counts": audit["counts"],
        "has_contradiction": audit["has_contradiction"],
        "non_consistent_findings": [
            finding
            for claim in audit["claims"]
            for finding in claim["findings"]
            if finding["verdict"] != "consistent"
        ],
    }


def semantic_snapshot_projection(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize generated identities but retain manufacturing facts and relationships."""

    raw_orders = list(snapshot.get("orders", []))
    ordered = sorted(
        raw_orders,
        key=lambda item: (
            item.get("product_id"),
            item.get("release_time_seconds"),
            item.get("due_time_seconds"),
            item.get("priority_weight"),
            item.get("quantity"),
            item.get("order_id"),
        ),
    )
    order_names = {item["order_id"]: f"order-{index:03d}" for index, item in enumerate(ordered)}
    operations = list(snapshot.get("operations", []))
    operation_names = {
        item["operation_id"]: (
            f"{order_names[item['order_id']]}:u{item['unit_index']}:s{item['sequence_index']}"
        )
        for item in operations
    }
    attempts = list(snapshot.get("attempts", []))
    attempt_names = {
        item["attempt_id"]: f"{operation_names[item['operation_id']]}:a{item['attempt_number']}"
        for item in attempts
    }
    return {
        "schema_version": snapshot.get("schema_version"),
        "simulated_time_seconds": snapshot.get("simulated_time_seconds"),
        "hard_horizon_seconds": snapshot.get("hard_horizon_seconds"),
        "orders": [
            {
                **{key: value for key, value in item.items() if key != "order_id"},
                "semantic_order": order_names[item["order_id"]],
            }
            for item in ordered
        ],
        "machines": sorted(snapshot.get("machines", []), key=lambda item: item["machine_id"]),
        "operations": sorted(
            (
                {
                    **{
                        key: value
                        for key, value in item.items()
                        if key not in {"operation_id", "order_id", "predecessor_operation_ids"}
                    },
                    "semantic_operation": operation_names[item["operation_id"]],
                    "semantic_order": order_names[item["order_id"]],
                    "predecessors": sorted(
                        operation_names[parent] for parent in item["predecessor_operation_ids"]
                    ),
                }
                for item in operations
            ),
            key=lambda item: item["semantic_operation"],
        ),
        "attempts": sorted(
            (
                {
                    **{
                        key: value
                        for key, value in item.items()
                        if key not in {"attempt_id", "operation_id", "restart_of_attempt_id"}
                    },
                    "semantic_attempt": attempt_names[item["attempt_id"]],
                    "semantic_operation": operation_names[item["operation_id"]],
                    "restart_of": None
                    if item.get("restart_of_attempt_id") is None
                    else attempt_names[item["restart_of_attempt_id"]],
                }
                for item in attempts
            ),
            key=lambda item: item["semantic_attempt"],
        ),
    }


def collect_run(
    *,
    arm: str,
    campaign_id: str,
    scenario_id: str,
    seed: int,
    repetition: int,
    output: Path,
    wall_seconds: float,
) -> dict[str, Any]:
    summary = _load(output / "summary.json")
    if not isinstance(summary, Mapping):
        return {
            "campaign": campaign_id,
            "arm": arm,
            "scenario_id": scenario_id,
            "seed": seed,
            "repetition": repetition,
            "output": str(output),
            "status": "error",
            "reason_code": "summary_missing_or_invalid",
            "wall_seconds": round(wall_seconds, 6),
            "attempted": True,
            "audit_complete": False,
            "audit_errors": ["summary_missing_or_invalid"],
            "counts_as_llm_capability": False,
        }
    trace_path = output / "agent_trace.sqlite3"
    lifecycle_path = output / "lifecycle.json"
    invariants_path = output / "invariants.json"
    metrics_path = output / "metrics.json"
    snapshot_path = output / "final_snapshot.json"
    ledger_path = output / "provider_ledger.sqlite3"
    payloads = _trace_payloads(trace_path)
    terminals = [item["payload"] for item in payloads if item["kind"] == "terminal"]
    terminal = terminals[-1] if terminals else {}
    tool_results = [item["payload"] for item in payloads if item["kind"] == "tool_result"]
    trace_shape_errors: list[str] = []
    if any(item["payload"].get("decode_error") is True for item in payloads):
        trace_shape_errors.append("trace_payload_decode_error")
    evidence_ids: set[str] = set()
    for item in tool_results:
        raw_evidence_ids = item.get("evidence_ids", [])
        if not isinstance(raw_evidence_ids, list) or any(
            not isinstance(evidence_id, str) for evidence_id in raw_evidence_ids
        ):
            trace_shape_errors.append("tool_evidence_ids_malformed")
            continue
        evidence_ids.update(raw_evidence_ids)
    raw_claims = terminal.get("claims", [])
    claims: list[dict[str, Any]] = []
    if not isinstance(raw_claims, list) or any(
        not isinstance(claim, Mapping)
        or not isinstance(claim.get("evidence_ids"), list)
        or any(not isinstance(item, str) for item in claim.get("evidence_ids", []))
        for claim in raw_claims
    ):
        trace_shape_errors.append("terminal_claims_malformed")
    else:
        claims = [dict(claim) for claim in raw_claims]
    claim_score = _claim_score(claims, evidence_ids)
    try:
        claim_audit = _claim_audit(scenario_id, seed, output, summary, claims)
    except (AttributeError, KeyError, LookupError, OSError, sqlite3.Error, TypeError, ValueError):
        claim_audit = {
            "error": "claim_audit_unavailable",
            "claim_count": len(claims),
            "has_contradiction": None,
        }
    interpretation = _interpretation_score(scenario_id, payloads)
    ledger = _ledger(ledger_path)
    invariants = _load(invariants_path, [])
    invariant_items = (
        [item for item in invariants if isinstance(item, Mapping)]
        if isinstance(invariants, list)
        else []
    )
    metrics_raw = _load(metrics_path, {})
    metrics = metrics_raw if isinstance(metrics_raw, Mapping) else {}
    snapshot_raw = _load(snapshot_path, {})
    snapshot = snapshot_raw if isinstance(snapshot_raw, Mapping) else {}
    solver_outcomes = []
    for item in tool_results:
        result = item.get("result")
        if item.get("tool") == "solve_schedule" and isinstance(result, Mapping):
            solver_outcomes.append(result.get("solver_outcome"))
    provider_rejections = [
        item["payload"] for item in payloads if item["kind"] == "provider_rejection"
    ]
    rejected_tools = []
    for item in tool_results:
        result = item.get("result")
        if not isinstance(result, Mapping):
            trace_shape_errors.append("tool_result_malformed")
        elif result.get("status") not in (None, "ok"):
            rejected_tools.append(item)
    lifecycle_raw = _load(lifecycle_path, {})
    lifecycle = lifecycle_raw if isinstance(lifecycle_raw, Mapping) else {}
    encoded_boundary = json.dumps(
        {"trace": payloads, "lifecycle": lifecycle},
        ensure_ascii=False,
        sort_keys=True,
    )
    truth_markers = [marker for marker in TRUTH_MARKERS if marker in encoded_boundary]
    expected = EXPECTED_TERMINALS[scenario_id]
    audit_errors: list[str] = list(trace_shape_errors)
    if (
        summary.get("scenario_id") != scenario_id
        or summary.get("seed") != seed
        or summary.get("provider") != SUMMARY_PROVIDER.get(arm)
    ):
        audit_errors.append("summary_identity_mismatch")
    declared_artifacts = summary.get("artifacts")
    if (
        not isinstance(declared_artifacts, list)
        or not declared_artifacts
        or any(not isinstance(item, str) or not item for item in declared_artifacts)
        or len(declared_artifacts) != len(set(declared_artifacts))
    ):
        audit_errors.append("summary_artifact_manifest_invalid")
    else:
        resolved_output = output.resolve()
        for relative in declared_artifacts:
            artifact = (output / relative).resolve()
            if resolved_output not in artifact.parents or not artifact.is_file():
                audit_errors.append("summary_artifact_missing")
                break
    for path, code in (
        (trace_path, "agent_trace_missing"),
        (lifecycle_path, "lifecycle_missing"),
        (invariants_path, "invariants_missing"),
        (metrics_path, "metrics_missing"),
        (snapshot_path, "final_snapshot_missing"),
        (ledger_path, "provider_ledger_missing"),
    ):
        if not path.is_file():
            audit_errors.append(code)
    if not terminals:
        audit_errors.append("terminal_trace_missing")
    if not isinstance(lifecycle_raw, Mapping) or not lifecycle:
        audit_errors.append("lifecycle_invalid")
    if not isinstance(invariants, list) or not invariants:
        audit_errors.append("invariants_invalid")
    elif any(
        not isinstance(item, Mapping)
        or not isinstance(item.get("code"), str)
        or not isinstance(item.get("passed"), bool)
        for item in invariants
    ):
        audit_errors.append("invariants_malformed")
    else:
        invariant_codes = [item["code"] for item in invariants]
        if (
            len(invariant_codes) != len(EXPECTED_INVARIANT_CODES)
            or set(invariant_codes) != EXPECTED_INVARIANT_CODES
        ):
            audit_errors.append("invariant_set_mismatch")
    invariant_results_passed = bool(invariant_items) and all(
        item.get("passed") is True for item in invariant_items
    )
    summary_invariants_passed = summary.get("invariants_passed")
    if not isinstance(summary_invariants_passed, bool):
        audit_errors.append("summary_invariants_invalid")
    elif summary_invariants_passed != invariant_results_passed:
        audit_errors.append("invariants_summary_mismatch")
    if (
        not isinstance(metrics_raw, Mapping)
        or not metrics
        or not REQUIRED_METRIC_FIELDS.issubset(metrics)
    ):
        audit_errors.append("metrics_invalid")
    if (
        not isinstance(snapshot_raw, Mapping)
        or not snapshot
        or not REQUIRED_SNAPSHOT_FIELDS.issubset(snapshot)
    ):
        audit_errors.append("final_snapshot_invalid")
    try:
        snapshot_artifact_hash = canonical_hash(snapshot) if snapshot else None
    except (TypeError, ValueError):
        snapshot_artifact_hash = None
    summary_final = summary.get("final")
    if (
        not isinstance(summary_final, Mapping)
        or snapshot_artifact_hash is None
        or summary_final.get("snapshot_hash") != snapshot_artifact_hash
        or summary_final.get("replay_final_snapshot_hash") != snapshot_artifact_hash
    ):
        audit_errors.append("final_snapshot_hash_mismatch")
    if ledger.get("available") is not True:
        audit_errors.append("provider_ledger_unavailable")
    elif ledger.get("read_error") is True:
        audit_errors.append("provider_ledger_invalid")
    budget_audit_errors = _budget_audit_errors(terminal, ledger)
    audit_errors.extend(budget_audit_errors)
    if "error" in claim_audit or not isinstance(claim_audit.get("has_contradiction"), bool):
        audit_errors.append("claim_audit_incomplete")
    if not isinstance(claim_score.get("citation_validity_passed"), bool):
        audit_errors.append("citation_audit_incomplete")
    hard_constraint_violations = 0
    for item in invariant_items:
        if item.get("code") != "applied_plans_pass_independent_validator":
            continue
        details = item.get("details")
        failure_count = details.get("failure_count") if isinstance(details, Mapping) else None
        normalized_count = _non_negative_integer(failure_count)
        if normalized_count is None:
            audit_errors.append("independent_validator_details_invalid")
        else:
            hard_constraint_violations += normalized_count
    audit_errors = sorted(set(audit_errors))
    end_to_end_success = bool(
        (summary.get("status"), summary.get("reason_code")) == expected
        and terminal.get("terminal") == SUPERVISOR_SUCCESS[scenario_id]
        and invariant_results_passed
        and not audit_errors
        and not truth_markers
        and not claim_audit.get("has_contradiction", False)
        and not claim_score["unsupported_causal_claim_indexes"]
        and claim_score["citation_validity_passed"]
    )
    try:
        semantic_digest = (
            canonical_hash(semantic_snapshot_projection(snapshot)) if snapshot else None
        )
    except (AttributeError, KeyError, TypeError, ValueError):
        semantic_digest = None
        audit_errors.append("semantic_snapshot_invalid")
        audit_errors.sort()
        end_to_end_success = False
    model_ids = sorted(
        {
            str(response.get("model"))
            for item in payloads
            if item["kind"] == "provider_response"
            and isinstance((response := item["payload"].get("response")), Mapping)
            and response.get("model") is not None
        }
    )
    budget = terminal.get("budget") if isinstance(terminal.get("budget"), Mapping) else {}
    accounting_verified = bool(
        ledger.get("available") is True
        and ledger.get("read_error") is False
        and not budget_audit_errors
    )
    audit_errors = sorted(set(audit_errors))
    return {
        "campaign": campaign_id,
        "arm": arm,
        "scenario_id": scenario_id,
        "seed": seed,
        "repetition": repetition,
        "output": str(output),
        "attempted": True,
        "audit_complete": not audit_errors,
        "audit_errors": audit_errors,
        "status": summary.get("status"),
        "reason_code": summary.get("reason_code"),
        "terminal_matches_expectation": summary.get("terminal_matches_expectation"),
        "end_to_end_success": end_to_end_success,
        "supervisor_terminal": terminal.get("terminal"),
        "supervisor_reason_code": terminal.get("reason_code"),
        "supervisor_success": terminal.get("terminal") == SUPERVISOR_SUCCESS[scenario_id],
        "solver_outcomes": solver_outcomes,
        "invariants_passed": invariant_results_passed,
        "failed_invariants": summary.get("failed_invariants", []),
        "invariant_details": invariants,
        "hard_constraint_violations": hard_constraint_violations,
        "truth_leak": bool(truth_markers),
        "truth_markers": truth_markers,
        "interpretation": interpretation,
        "claims": claim_score,
        "claim_audit": claim_audit,
        "tool_sequence": [
            item["payload"].get("tool") for item in payloads if item["kind"] == "tool_call"
        ],
        "tool_schema_rejections": len(provider_rejections),
        "tool_result_rejections": len(rejected_tools),
        "provider_attempt_errors": ledger["errors"],
        "provider_attempts": ledger["attempts"],
        "provider_latencies_seconds": ledger["latencies"],
        "active_seconds": budget.get("active_seconds"),
        "used_tokens": ledger["used_tokens"],
        "cost_micros": (
            ledger["cost_micros"] if arm == "deepseek" and accounting_verified else None
        ),
        "synthetic_accounting_cost_micros": (
            ledger["cost_micros"] if arm != "deepseek" and accounting_verified else None
        ),
        "budget_accounting_verified": accounting_verified,
        "provider_limits": dict(budget.get("limits", {}))
        if isinstance(budget.get("limits"), Mapping)
        else None,
        "model_ids": model_ids,
        "wall_seconds": round(wall_seconds, 6),
        "counts_as_llm_capability": arm == "deepseek",
        "metrics": {key: metrics.get(key) for key in sorted(REQUIRED_METRIC_FIELDS)},
        "final_snapshot_hash": (
            summary_final.get("snapshot_hash") if isinstance(summary_final, Mapping) else None
        ),
        "final_snapshot_artifact_sha256": snapshot_artifact_hash,
        "exact_replay_snapshot_hash": (
            summary_final.get("replay_final_snapshot_hash")
            if isinstance(summary_final, Mapping)
            else None
        ),
        "semantic_snapshot_sha256": semantic_digest,
    }


def _audit_record_complete(record: Mapping[str, Any]) -> bool:
    """Require explicit audit outcomes for every attempted workflow."""

    attempted = record.get("attempted")
    if attempted is False:
        return record.get("status") == "NOT_RUN"
    if attempted is not True or record.get("audit_complete") is not True:
        return False
    claim_audit = record.get("claim_audit")
    claims = record.get("claims")
    hard_violations = record.get("hard_constraint_violations")
    return bool(
        isinstance(record.get("end_to_end_success"), bool)
        and isinstance(record.get("invariants_passed"), bool)
        and isinstance(hard_violations, int)
        and not isinstance(hard_violations, bool)
        and hard_violations >= 0
        and isinstance(record.get("truth_leak"), bool)
        and isinstance(claim_audit, Mapping)
        and isinstance(claim_audit.get("has_contradiction"), bool)
        and "error" not in claim_audit
        and isinstance(claims, Mapping)
        and isinstance(claims.get("citation_validity_passed"), bool)
    )


def aggregate_records(
    records: list[dict[str, Any]], planned: list[dict[str, Any]]
) -> dict[str, Any]:
    groups: dict[str, Any] = {}
    keys = sorted({(item["arm"], item["scenario_id"]) for item in planned})
    for arm, scenario_id in keys:
        planned_rows = [
            item for item in planned if item["arm"] == arm and item["scenario_id"] == scenario_id
        ]
        rows = [
            item for item in records if item["arm"] == arm and item["scenario_id"] == scenario_id
        ]
        denominator = len(planned_rows)
        successes = sum(1 for item in rows if item.get("end_to_end_success"))
        supervisor_successes = sum(1 for item in rows if item.get("supervisor_success"))
        costs = [item["cost_micros"] / 1_000_000 for item in rows if item.get("cost_micros")]
        latencies = [value for item in rows for value in item.get("provider_latencies_seconds", [])]
        active = [
            float(item["active_seconds"]) for item in rows if item.get("active_seconds") is not None
        ]
        terminals: dict[str, int] = {}
        for item in rows:
            key = f"{item.get('supervisor_terminal')}/{item.get('supervisor_reason_code')}"
            terminals[key] = terminals.get(key, 0) + 1
        interpretations = [item["interpretation"] for item in rows if item.get("interpretation")]
        applicable = [item for item in interpretations if item.get("applicable")]
        citation_denominator = sum(item.get("claims", {}).get("citations", 0) for item in rows)
        citation_numerator = sum(
            item.get("claims", {}).get("resolved_citations", 0) for item in rows
        )
        groups[f"{arm}/{scenario_id}"] = {
            "planned": denominator,
            "attempted": sum(1 for item in rows if item.get("attempted")),
            "not_run": sum(1 for item in rows if item.get("status") == "NOT_RUN"),
            "end_to_end_success": successes,
            "end_to_end_success_rate_planned_denominator": (
                round(successes / denominator, 6) if denominator else None
            ),
            "end_to_end_success_wilson95": wilson(successes, denominator),
            "supervisor_success": supervisor_successes,
            "supervisor_success_rate_planned_denominator": (
                round(supervisor_successes / denominator, 6) if denominator else None
            ),
            "supervisor_success_wilson95": wilson(supervisor_successes, denominator),
            "supervisor_terminals": dict(sorted(terminals.items())),
            "audit_incomplete_records": sum(1 for item in rows if not _audit_record_complete(item)),
            "invariant_failures": sum(
                1
                for item in rows
                if item.get("attempted") is True and item.get("invariants_passed") is not True
            ),
            "invalid_applied_schedules": sum(
                int(item.get("hard_constraint_violations", 0)) for item in rows
            ),
            "truth_leaks": sum(1 for item in rows if item.get("truth_leak")),
            "claim_audit_contradictions": sum(
                1 for item in rows if item.get("claim_audit", {}).get("has_contradiction")
            ),
            "tool_schema_rejections": sum(
                int(item.get("tool_schema_rejections", 0)) for item in rows
            ),
            "interpretation_correct": sum(1 for item in applicable if item.get("correct")),
            "interpretation_denominator": len(applicable),
            "citation_validity_numerator": citation_numerator,
            "citation_validity_denominator": citation_denominator,
            "cost_usd_total": round(sum(costs), 6) if arm == "deepseek" else None,
            "cost_usd_mean_planned_denominator": (
                round(sum(costs) / denominator, 6) if arm == "deepseek" and denominator else None
            ),
            "provider_latency_p50_seconds": percentile(latencies, 0.5),
            "provider_latency_p95_seconds": percentile(latencies, 0.95),
            "active_seconds_p50": percentile(active, 0.5),
            "active_seconds_p95": percentile(active, 0.95),
            "tokens_mean_attempted": (
                round(statistics.fmean(item.get("used_tokens", 0) for item in rows), 2)
                if rows
                else None
            ),
        }
    return groups
