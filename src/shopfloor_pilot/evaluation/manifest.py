"""Frozen S13 manifest validation and prompt/tool contract fingerprinting."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from shopfloor_pilot.agent.harness import ABNORMALITY_TASKS, load_request_fixtures
from shopfloor_pilot.agent.prompts import SYSTEM_PROMPT
from shopfloor_pilot.domain.canonical import canonical_hash
from shopfloor_pilot.tools import build_supervisor_registry
from shopfloor_pilot.workflows.scenario_runner import NO_FEASIBLE_TASK, RECOVERY_SCENARIOS

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
EXPECTED_SCENARIOS = tuple(RECOVERY_SCENARIOS)
EXPECTED_ARMS = ("baseline", "scripted", "deepseek")


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(128 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def prompt_contract_sha256() -> str:
    """Hash the exact static prompt, tool schemas and fixed evaluation requests."""

    fixtures = load_request_fixtures()
    request_texts = {
        item.fixture_id: item.request_text
        for item in fixtures.fixtures
        if item.fixture_id in {"normal-en-plate-today", "urgent-zh-plate-90min"}
    }
    abnormality = {
        "machine_breakdown": ABNORMALITY_TASKS["machine_breakdown"][0],
        "no_feasible_recovery": NO_FEASIBLE_TASK[0],
    }
    prompts_source = REPOSITORY_ROOT / "src/shopfloor_pilot/agent/prompts.py"
    return canonical_hash(
        {
            "contract": "s13-static-prompt-tool-request-v1",
            "system_prompt": SYSTEM_PROMPT,
            "prompt_builder_source_sha256": _file_sha256(prompts_source),
            "tool_schemas": [item.public_dict() for item in build_supervisor_registry().specs()],
            "order_requests": request_texts,
            "abnormality_requests": abnormality,
        }
    )


def _object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ValueError(f"{label} must be a JSON object")
    return value


def _strings(value: Any, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError(f"{label} must be a JSON string array")
    result = tuple(value)
    if len(result) != len(set(result)):
        raise ValueError(f"{label} values must be unique")
    return result


def _integers(value: Any, label: str) -> tuple[int, ...]:
    if not isinstance(value, list) or any(
        isinstance(item, bool) or not isinstance(item, int) or item < 0 for item in value
    ):
        raise ValueError(f"{label} must be a non-negative integer array")
    result = tuple(value)
    if len(result) != len(set(result)):
        raise ValueError(f"{label} values must be unique")
    return result


def _decimal(value: Any, label: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{label} must be a decimal") from exc
    if not result.is_finite() or result <= 0:
        raise ValueError(f"{label} must be finite and positive")
    return result


@dataclass(frozen=True, slots=True)
class Campaign:
    campaign_id: str
    scenarios: tuple[str, ...]
    seeds: tuple[int, ...]
    arms: tuple[str, ...]
    repetitions_per_seed: int
    aggregate_cap_usd: Decimal | None
    workflow_cap_usd: Decimal | None

    @property
    def planned_cases(self) -> int:
        return len(self.scenarios) * len(self.seeds) * self.repetitions_per_seed

    @property
    def planned_paid_runs(self) -> int:
        return self.planned_cases * int("deepseek" in self.arms)


@dataclass(frozen=True, slots=True)
class EvaluationThresholds:
    """Frozen acceptance thresholds loaded from the evaluation manifest."""

    normal_success_rate: Decimal
    urgent_order_success_rate: Decimal
    machine_breakdown_success_rate: Decimal
    no_feasible_recovery_safe_block_rate: Decimal
    invalid_applied_schedules: int
    invariant_failures: int
    truth_leaks: int
    persisted_claim_contradictions: int
    mean_cost_usd_per_workflow: Decimal

    def public_dict(self) -> dict[str, str | int]:
        return {
            "normal_success_rate": str(self.normal_success_rate),
            "urgent_order_success_rate": str(self.urgent_order_success_rate),
            "machine_breakdown_success_rate": str(self.machine_breakdown_success_rate),
            "no_feasible_recovery_safe_block_rate": str(self.no_feasible_recovery_safe_block_rate),
            "invalid_applied_schedules": self.invalid_applied_schedules,
            "invariant_failures": self.invariant_failures,
            "truth_leaks": self.truth_leaks,
            "persisted_claim_contradictions": self.persisted_claim_contradictions,
            "mean_cost_usd_per_workflow": str(self.mean_cost_usd_per_workflow),
        }


@dataclass(frozen=True, slots=True)
class EvaluationManifest:
    path: Path
    payload: dict[str, Any]
    file_sha256: str
    canonical_sha256: str
    campaigns: dict[str, Campaign]
    thresholds: EvaluationThresholds

    @property
    def manifest_id(self) -> str:
        return str(self.payload["manifest_id"])

    @property
    def scenarios(self) -> tuple[dict[str, Any], ...]:
        return tuple(self.payload["scenario_contracts"])

    def campaign(self, campaign_id: str) -> Campaign:
        try:
            return self.campaigns[campaign_id]
        except KeyError as exc:
            raise ValueError(f"unknown evaluation campaign: {campaign_id}") from exc


def _validate_source_hashes(payload: dict[str, Any]) -> None:
    sources = _object(payload.get("sources"), "sources")
    for label, item in sources.items():
        entry = _object(item, f"sources.{label}")
        relative = entry.get("path")
        expected = entry.get("sha256")
        if not isinstance(relative, str) or not isinstance(expected, str):
            raise ValueError(f"sources.{label} needs path and sha256 strings")
        path = (REPOSITORY_ROOT / relative).resolve()
        if REPOSITORY_ROOT not in path.parents or not path.is_file():
            raise ValueError(f"sources.{label} path is not a repository file")
        actual = _file_sha256(path)
        if actual != expected:
            raise ValueError(f"sources.{label} hash changed: expected {expected}, got {actual}")
    scenario_manifest = json.loads(
        (REPOSITORY_ROOT / sources["scenario_manifest"]["path"]).read_text(encoding="utf-8")
    )
    indexed = {item["scenario_id"]: item for item in scenario_manifest["scenarios"]}
    for contract in payload["scenario_contracts"]:
        source = indexed[contract["scenario_id"]]
        for contract_key, source_key in (
            ("public_sha256", "public_sha256"),
            ("evaluator_sha256", "evaluator_sha256"),
            ("expectation_sha256", "expectation_sha256"),
        ):
            if contract.get(contract_key) != source[source_key]:
                raise ValueError(
                    f"scenario {contract['scenario_id']} {contract_key} disagrees with source"
                )


def _campaign(name: str, raw: Any) -> Campaign:
    item = _object(raw, f"campaigns.{name}")
    scenarios = _strings(item.get("scenarios"), f"campaigns.{name}.scenarios")
    seeds = _integers(item.get("seeds"), f"campaigns.{name}.seeds")
    arms = _strings(item.get("arms"), f"campaigns.{name}.arms")
    repetitions = item.get("repetitions_per_seed")
    if isinstance(repetitions, bool) or not isinstance(repetitions, int) or repetitions < 1:
        raise ValueError(f"campaigns.{name}.repetitions_per_seed must be positive")
    unknown = set(scenarios) - set(EXPECTED_SCENARIOS)
    if unknown:
        raise ValueError(f"campaigns.{name} has unknown scenarios: {sorted(unknown)}")
    unknown_arms = set(arms) - set(EXPECTED_ARMS)
    if unknown_arms:
        raise ValueError(f"campaigns.{name} has unknown arms: {sorted(unknown_arms)}")
    aggregate = item.get("aggregate_cap_usd")
    workflow = item.get("workflow_cap_usd")
    aggregate_cap = None if aggregate is None else _decimal(aggregate, f"{name} aggregate cap")
    workflow_cap = None if workflow is None else _decimal(workflow, f"{name} workflow cap")
    campaign = Campaign(
        campaign_id=name,
        scenarios=scenarios,
        seeds=seeds,
        arms=arms,
        repetitions_per_seed=repetitions,
        aggregate_cap_usd=aggregate_cap,
        workflow_cap_usd=workflow_cap,
    )
    if "deepseek" in arms:
        if aggregate_cap is None or workflow_cap is None:
            raise ValueError(f"paid campaign {name} requires aggregate and workflow caps")
        worst_case = workflow_cap * campaign.planned_paid_runs
        if worst_case > aggregate_cap:
            raise ValueError(
                f"paid campaign {name} worst case {worst_case} exceeds cap {aggregate_cap}"
            )
    elif aggregate_cap is not None or workflow_cap is not None:
        raise ValueError(f"offline campaign {name} cannot carry monetary caps")
    return campaign


def _probability(value: Any, label: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{label} must be a decimal probability") from exc
    if not result.is_finite() or not Decimal(0) <= result <= Decimal(1):
        raise ValueError(f"{label} must be between 0 and 1")
    return result


def _non_negative_decimal(value: Any, label: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{label} must be a decimal") from exc
    if not result.is_finite() or result < 0:
        raise ValueError(f"{label} must be finite and non-negative")
    return result


def _non_negative_integer(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{label} must be a non-negative integer")
    return value


def _evaluation_thresholds(raw: Any) -> EvaluationThresholds:
    item = _object(raw, "thresholds")
    expected = {
        "normal_success_rate",
        "urgent_order_success_rate",
        "machine_breakdown_success_rate",
        "no_feasible_recovery_safe_block_rate",
        "invalid_applied_schedules",
        "invariant_failures",
        "truth_leaks",
        "persisted_claim_contradictions",
        "mean_cost_usd_per_workflow",
    }
    if set(item) != expected:
        missing = sorted(expected - set(item))
        extra = sorted(set(item) - expected)
        raise ValueError(f"thresholds keys differ: missing={missing}, extra={extra}")
    return EvaluationThresholds(
        normal_success_rate=_probability(
            item["normal_success_rate"], "thresholds.normal_success_rate"
        ),
        urgent_order_success_rate=_probability(
            item["urgent_order_success_rate"], "thresholds.urgent_order_success_rate"
        ),
        machine_breakdown_success_rate=_probability(
            item["machine_breakdown_success_rate"],
            "thresholds.machine_breakdown_success_rate",
        ),
        no_feasible_recovery_safe_block_rate=_probability(
            item["no_feasible_recovery_safe_block_rate"],
            "thresholds.no_feasible_recovery_safe_block_rate",
        ),
        invalid_applied_schedules=_non_negative_integer(
            item["invalid_applied_schedules"], "thresholds.invalid_applied_schedules"
        ),
        invariant_failures=_non_negative_integer(
            item["invariant_failures"], "thresholds.invariant_failures"
        ),
        truth_leaks=_non_negative_integer(item["truth_leaks"], "thresholds.truth_leaks"),
        persisted_claim_contradictions=_non_negative_integer(
            item["persisted_claim_contradictions"],
            "thresholds.persisted_claim_contradictions",
        ),
        mean_cost_usd_per_workflow=_non_negative_decimal(
            item["mean_cost_usd_per_workflow"], "thresholds.mean_cost_usd_per_workflow"
        ),
    )


def _validate_live_configuration(raw: Any) -> None:
    configuration = _object(raw, "live_configuration")
    if set(configuration) != {"provider", "pricing"}:
        raise ValueError("live_configuration must contain exactly provider and pricing")
    provider = _object(configuration.get("provider"), "live_configuration.provider")
    provider_keys = {
        "base_url",
        "model",
        "served_model_version",
        "thinking",
        "max_output_tokens",
        "max_provider_attempts",
        "max_tools",
        "max_retries_per_request",
        "attempt_timeout_seconds",
        "max_active_seconds",
        "max_total_tokens",
    }
    if set(provider) != provider_keys:
        raise ValueError("live_configuration.provider keys differ from the frozen schema")
    for field in ("base_url", "model", "served_model_version"):
        if not isinstance(provider.get(field), str) or not provider[field].strip():
            raise ValueError(f"live_configuration.provider.{field} must be a non-empty string")
    if provider.get("thinking") != "disabled":
        raise ValueError("live_configuration.provider.thinking must be disabled")
    for field in ("max_output_tokens", "max_provider_attempts", "max_tools", "max_total_tokens"):
        value = provider.get(field)
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError(f"live_configuration.provider.{field} must be a positive integer")
    _non_negative_integer(
        provider.get("max_retries_per_request"),
        "live_configuration.provider.max_retries_per_request",
    )
    for field in ("attempt_timeout_seconds", "max_active_seconds"):
        _decimal(provider.get(field), f"live_configuration.provider.{field}")
    if provider["max_output_tokens"] > provider["max_total_tokens"]:
        raise ValueError("live max_output_tokens cannot exceed max_total_tokens")

    pricing = _object(configuration.get("pricing"), "live_configuration.pricing")
    pricing_keys = {
        "currency",
        "tariff_date",
        "tariff_source",
        "pricing_basis",
        "input_cache_hit",
        "input_cache_miss",
        "output",
    }
    if set(pricing) != pricing_keys:
        raise ValueError("live_configuration.pricing keys differ from the frozen schema")
    for field in ("currency", "tariff_date", "tariff_source", "pricing_basis"):
        if not isinstance(pricing.get(field), str) or not pricing[field].strip():
            raise ValueError(f"live_configuration.pricing.{field} must be a non-empty string")
    if pricing["currency"] != "USD" or not pricing["tariff_source"].startswith("https://"):
        raise ValueError("live pricing must use USD and an HTTPS tariff source")
    for field in ("input_cache_hit", "input_cache_miss", "output"):
        _non_negative_decimal(pricing.get(field), f"live_configuration.pricing.{field}")


def load_evaluation_manifest(path: Path, *, verify_sources: bool = True) -> EvaluationManifest:
    resolved = path.resolve()
    try:
        raw = resolved.read_bytes()
        payload = json.loads(raw)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot load evaluation manifest {resolved}: {exc}") from exc
    root = _object(payload, "evaluation manifest")
    if root.get("schema_version") != 1 or root.get("frozen_before_runs") is not True:
        raise ValueError("evaluation manifest must be schema 1 and frozen_before_runs=true")
    if not isinstance(root.get("manifest_id"), str):
        raise ValueError("evaluation manifest_id must be a string")
    contracts = root.get("scenario_contracts")
    if not isinstance(contracts, list):
        raise ValueError("scenario_contracts must be an array")
    contract_ids = tuple(
        _object(item, "scenario contract").get("scenario_id") for item in contracts
    )
    if contract_ids != EXPECTED_SCENARIOS:
        raise ValueError(f"scenario contracts must be ordered exactly as {EXPECTED_SCENARIOS}")
    arms = _object(root.get("arms"), "arms")
    if tuple(arms) != EXPECTED_ARMS:
        raise ValueError(f"arms must be ordered exactly as {EXPECTED_ARMS}")
    campaigns_raw = _object(root.get("campaigns"), "campaigns")
    campaigns = {name: _campaign(name, value) for name, value in campaigns_raw.items()}
    required = {"offline", "paired_live", "reliability"}
    if set(campaigns) != required:
        raise ValueError(f"campaigns must be exactly {sorted(required)}")
    offline = campaigns["offline"]
    paired = campaigns["paired_live"]
    reliability = campaigns["reliability"]
    for campaign_id, campaign in campaigns.items():
        if campaign.scenarios != EXPECTED_SCENARIOS:
            raise ValueError(
                f"campaigns.{campaign_id}.scenarios must be ordered exactly as {EXPECTED_SCENARIOS}"
            )
    if (
        offline.seeds != (0, 1, 2, 3, 4)
        or offline.arms != ("baseline", "scripted")
        or offline.repetitions_per_seed != 1
    ):
        raise ValueError(
            "offline campaign must freeze seeds 0..4, one repetition and baseline/scripted arms"
        )
    if (
        paired.seeds != (0, 1, 2)
        or paired.arms != ("baseline", "deepseek")
        or paired.repetitions_per_seed != 1
    ):
        raise ValueError(
            "paired_live must freeze seeds 0..2, one repetition and baseline/deepseek arms"
        )
    if (
        reliability.seeds != (0, 1, 2, 3, 4)
        or reliability.arms != ("deepseek",)
        or reliability.repetitions_per_seed != 5
    ):
        raise ValueError(
            "reliability must freeze five repetitions of seeds 0..4 and the deepseek arm"
        )
    thresholds = _evaluation_thresholds(root.get("thresholds"))
    _validate_live_configuration(root.get("live_configuration"))
    solver = _object(root.get("solver"), "solver")
    if solver.get("num_search_workers") != 1 or solver.get("max_time_seconds") != 5.0:
        raise ValueError("solver must freeze one worker and a five-second search budget")
    if root.get("prompt_contract_sha256") != prompt_contract_sha256():
        raise ValueError("static prompt/tool/request contract hash changed")
    if verify_sources:
        _validate_source_hashes(root)
    return EvaluationManifest(
        path=resolved,
        payload=root,
        file_sha256=hashlib.sha256(raw).hexdigest(),
        canonical_sha256=canonical_hash(root),
        campaigns=campaigns,
        thresholds=thresholds,
    )
