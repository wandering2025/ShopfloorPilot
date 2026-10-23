"""Scenario harness and offline demonstration entry point for the supervisor.

The harness is operator/scenario-side code. It may read evaluator fixtures to
bootstrap a scenario through the S06 baseline helpers and an explicitly named
deterministic policy; the supervisor itself sees only the model-readable
facade. Unless ``approval_policy="deterministic"`` is passed explicitly, the
run stops at awaiting human approval.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, JsonValue

from shopfloor_pilot import baseline
from shopfloor_pilot.baseline import BaselineExitCode, BaselineFailure
from shopfloor_pilot.budgets import BudgetLedger
from shopfloor_pilot.domain.base import Identifier, StrictModel
from shopfloor_pilot.domain.fixtures import (
    REPOSITORY_ROOT,
    InterventionKind,
    ScenarioBundle,
    load_scenario,
    materialize_initial_snapshot,
)
from shopfloor_pilot.domain.models import ApprovalDecision
from shopfloor_pilot.providers.base import Provider
from shopfloor_pilot.providers.clock import SystemClock
from shopfloor_pilot.providers.config import load_paid_run_settings, merged_environment
from shopfloor_pilot.providers.costs import TokenRates
from shopfloor_pilot.providers.errors import ConfigurationError
from shopfloor_pilot.scheduling import SolverConfig
from shopfloor_pilot.services import ApplicationServices
from shopfloor_pilot.storage import SQLiteStateRepository
from shopfloor_pilot.tools import WorkflowKind, WorkflowPhase

from .fake import ScriptedCall, ScriptedTurn, TemplateScriptedProvider
from .trace import TraceStore, project_trace
from .workflow import (
    SupervisorBudget,
    SupervisorConfig,
    SupervisorOutcome,
    SupervisorTask,
    SupervisorWorkflow,
)

DEFAULT_REQUEST_FIXTURES = REPOSITORY_ROOT / "data" / "agent_requests" / "requests.json"
AGENT_SCENARIOS = ("normal", "urgent_order", "machine_breakdown")
BOOTSTRAP_SCENARIOS = frozenset({"urgent_order", "machine_breakdown"})
DEFAULT_REQUEST_BY_SCENARIO = {
    "normal": "normal-en-plate-today",
    "urgent_order": "urgent-zh-plate-90min",
}
ABNORMALITY_TASKS: dict[str, tuple[str, tuple[str, ...], str]] = {
    "machine_breakdown": (
        "Operator report: CNC02 stopped after temperature and vibration alarms. Diagnose "
        "from observable evidence and propose a recovery schedule.",
        ("CNC02",),
        "elevated temperature and vibration alarm on a milling and slotting machine",
    ),
}
# Offline ledger mechanics still need dated rates. These copy the S09-verified
# conservative peak deepseek-flash tariff; the scripted fake never spends money.
OFFLINE_ACCOUNTING_RATES = TokenRates(
    currency="USD",
    input_cache_hit_per_million=Decimal("0.006"),
    input_cache_miss_per_million=Decimal("0.30"),
    output_per_million=Decimal("1.20"),
    tariff_date=date(2026, 10, 21),
    source_url="https://api-docs.deepseek.com/quick_start/pricing",
    model="scripted-fake-model",
)
OFFLINE_BUDGET = SupervisorBudget(currency="USD", run_cap_micros=50_000, workflow_cap_micros=50_000)

WallClock = Callable[[], datetime]


class RequestFixture(StrictModel):
    fixture_id: Identifier
    category: Literal["normal", "urgent", "ambiguous", "unsupported"]
    scenario: Literal["normal", "urgent_order"]
    request_text: str = Field(min_length=1, max_length=500)
    scripted_draft: dict[str, JsonValue]
    expected: dict[str, JsonValue]


class RequestFixtureSet(StrictModel):
    schema_version: Literal[1]
    fixture_set_id: Identifier
    description: str = Field(min_length=1, max_length=1_000)
    fixtures: tuple[RequestFixture, ...] = Field(min_length=8)


def load_request_fixtures(path: Path = DEFAULT_REQUEST_FIXTURES) -> RequestFixtureSet:
    return RequestFixtureSet.model_validate_json(path.read_bytes())


# ------------------------------------------------------------- scripted turns


def scripted_order_turns(draft: Mapping[str, Any]) -> list[ScriptedTurn]:
    """The labeled offline fake's order-request behaviour."""

    pinned = {"run_id": "$run_id", "state_version": "$state_version"}
    return [
        ScriptedTurn(
            calls=(ScriptedCall("submit_order_interpretation", {**draft, "run_id": "$run_id"}),)
        ),
        ScriptedTurn(calls=(ScriptedCall("get_observable_snapshot", {"run_id": "$run_id"}),)),
        ScriptedTurn(calls=(ScriptedCall("solve_schedule", pinned),)),
        ScriptedTurn(
            calls=(ScriptedCall("validate_candidate", {**pinned, "candidate_id": "$candidate_id"}),)
        ),
        ScriptedTurn(
            calls=(
                ScriptedCall(
                    "persist_proposal",
                    {
                        **pinned,
                        "candidate_id": "$candidate_id",
                        "claims": [
                            {
                                "kind": "observation",
                                "text": "The pinned snapshot lists current orders and machines.",
                                "evidence_ids": ["$evidence_first:snapshot"],
                            },
                            {
                                "kind": "plan",
                                "text": (
                                    "The independently validated candidate schedules the drafted "
                                    "order with all existing work; it awaits human approval."
                                ),
                                "evidence_ids": [
                                    "$evidence_first:validation",
                                    "$evidence_first:interpretation",
                                    "$evidence_first:solver",
                                ],
                            },
                        ],
                    },
                ),
            )
        ),
    ]


def scripted_abnormality_turns(query: str) -> list[ScriptedTurn]:
    """The labeled offline fake's abnormality behaviour."""

    pinned = {"run_id": "$run_id", "state_version": "$state_version"}
    return [
        ScriptedTurn(calls=(ScriptedCall("get_observable_snapshot", {"run_id": "$run_id"}),)),
        ScriptedTurn(
            calls=(
                ScriptedCall(
                    "summarize_observations", {**pinned, "machine_ids": ["$focus_machine_ids"]}
                ),
                ScriptedCall("retrieve_guidance", {**pinned, "query": query, "top_k": 3}),
            )
        ),
        ScriptedTurn(
            calls=(
                ScriptedCall(
                    "diagnose_machine",
                    {**pinned, "machine_ids": ["$focus_machine_ids"], "query": query},
                ),
            )
        ),
        ScriptedTurn(calls=(ScriptedCall("solve_schedule", pinned),)),
        ScriptedTurn(
            calls=(ScriptedCall("validate_candidate", {**pinned, "candidate_id": "$candidate_id"}),)
        ),
        ScriptedTurn(
            calls=(
                ScriptedCall(
                    "persist_proposal",
                    {
                        **pinned,
                        "candidate_id": "$candidate_id",
                        "claims": [
                            {
                                "kind": "observation",
                                "text": "Persisted telemetry shows elevated temperature and "
                                "vibration on the focus machine, which is unavailable.",
                                "evidence_ids": [
                                    "$evidence_first:observation",
                                    "$evidence_first:snapshot",
                                ],
                            },
                            {
                                "kind": "hypothesis",
                                "text": "Possible, uncalibrated explanations are listed in the "
                                "cited hypotheses; none is a verified cause.",
                                "evidence_ids": ["$evidence:hypothesis"],
                            },
                            {
                                "kind": "plan",
                                "text": "The independently validated recovery candidate replans "
                                "remaining work; it awaits human approval.",
                                "evidence_ids": [
                                    "$evidence_first:validation",
                                    "$evidence_first:solver",
                                ],
                            },
                        ],
                    },
                ),
            )
        ),
    ]


# ------------------------------------------------------------------ bootstrap


def _wall(clock: WallClock) -> str:
    return clock().astimezone(UTC).isoformat(timespec="milliseconds")


def bootstrap_scenario(
    services: ApplicationServices,
    bundle: ScenarioBundle,
    *,
    seed: int,
    output: Path,
    policy: str,
    wall_clock: WallClock,
) -> dict[str, Any]:
    """Apply the scenario's initial plan via an explicit named policy and advance.

    This is the S06 baseline's own bootstrap (same planning policies, same
    approval/apply boundary, actor ``policy:deterministic``), stopped at the
    supervisor decision point instead of continuing to the scenario end.
    """

    scenario_id = bundle.scenario.scenario_id
    if scenario_id not in BOOTSTRAP_SCENARIOS:
        return {"bootstrap": "none", "scenario_id": scenario_id}
    if policy != "deterministic":
        raise BaselineFailure(
            "explicit_bootstrap_policy_required",
            f"scenario {scenario_id} needs --bootstrap-policy deterministic for its initial plan",
            BaselineExitCode.USAGE,
        )
    run_id = bundle.scenario.run_id
    if scenario_id == "urgent_order":
        initial = baseline._generate(  # noqa: SLF001 - reuse the accepted S06 bootstrap
            services,
            run_id,
            "initial",
            seed=seed,
            not_before_seconds=1,
            planning_policy="positive_time_origin_floor",
            wall_clock=wall_clock,
        )
    else:
        initial = baseline._scarce_capability_first_initial(  # noqa: SLF001
            services,
            bundle.catalog,
            materialize_initial_snapshot(bundle),
            seed=seed,
            output=output,
            wall_clock=wall_clock,
        )
    candidate = baseline._require_candidate(initial, "initial")  # noqa: SLF001
    approval, _latency = baseline._approve(services, run_id, "initial", initial, wall_clock)  # noqa: SLF001
    baseline._apply(services, run_id, "initial", initial, approval)  # noqa: SLF001
    if scenario_id == "urgent_order":
        at_seconds = next(
            item.at_seconds
            for item in bundle.evaluator.interventions
            if item.kind is InterventionKind.ADD_ORDER
        )
        target = at_seconds - 1
        services.advance_simulation(run_id, target, "advance-agent-before-decision")
    else:
        target = bundle.evaluator.interventions[0].at_seconds
        services.advance_simulation(
            run_id,
            target,
            "advance-agent-disruption",
            interventions=bundle.evaluator.interventions,
            observations=bundle.evaluator.observation_templates,
        )
    snapshot = services.get_snapshot(run_id)
    return {
        "bootstrap": "initial_plan_applied_then_advanced",
        "scenario_id": scenario_id,
        "policy_actor": approval.actor,
        "initial_proposal_id": candidate.proposal_id,
        "initial_proposal_hash": candidate.proposal_hash,
        "approval_id": approval.approval_id,
        "advanced_to_seconds": target,
        "state_version_after_bootstrap": snapshot.state_version,
        "plan_revision_after_bootstrap": snapshot.plan_revision,
        "harness_note": "evaluator interventions are consumed only by this harness",
    }


# ----------------------------------------------------------------------- CLI


@dataclass(frozen=True, slots=True)
class AgentRunResult:
    status: str
    exit_code: int
    summary: dict[str, Any]


def _operational_identity(services: ApplicationServices, run_id: str) -> dict[str, Any]:
    checkpoint = services.get_checkpoint(run_id)
    snapshot = checkpoint.snapshot
    return {
        "state_version": snapshot.state_version,
        "plan_revision": snapshot.plan_revision,
        "simulated_time_seconds": snapshot.simulated_time_seconds,
        "snapshot_hash": checkpoint.snapshot_hash,
        "order_ids": sorted(item.order_id for item in snapshot.orders),
        "machine_statuses": {item.machine_id: item.status.value for item in snapshot.machines},
        "event_count": checkpoint.last_sequence,
    }


def _write(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def _live_settings(env_file: Path | None) -> Any:
    selected = env_file
    if selected is None and Path(".env").is_file():
        selected = Path(".env")
    return load_paid_run_settings(merged_environment(os.environ, env_file=selected))


SAFE_TERMINALS = frozenset(
    {
        WorkflowPhase.AWAITING_APPROVAL,
        WorkflowPhase.CLARIFICATION_REQUIRED,
        WorkflowPhase.REJECTED,
    }
)


def run_agent_scenario(
    *,
    provider_kind: str,
    scenario_id: str,
    seed: int,
    output: Path,
    request_fixture_id: str | None = None,
    approval_policy: str | None = None,
    bootstrap_policy: str | None = None,
    env_file: Path | None = None,
    wall_clock: WallClock | None = None,
) -> AgentRunResult:
    """Run one supervisor workflow on a fresh scenario run in ``output``."""

    clock = wall_clock or (lambda: datetime.now(UTC))
    if provider_kind not in {"scripted-fake", "deepseek"}:
        raise BaselineFailure("invalid_provider", "unsupported provider", BaselineExitCode.USAGE)
    if scenario_id not in AGENT_SCENARIOS:
        raise BaselineFailure("invalid_scenario", "unsupported scenario", BaselineExitCode.USAGE)
    if approval_policy not in (None, "deterministic"):
        raise BaselineFailure(
            "invalid_approval_policy", "only deterministic is supported", BaselineExitCode.USAGE
        )
    if scenario_id in BOOTSTRAP_SCENARIOS and bootstrap_policy != "deterministic":
        raise BaselineFailure(
            "explicit_bootstrap_policy_required",
            f"scenario {scenario_id} needs --bootstrap-policy deterministic for its initial plan",
            BaselineExitCode.USAGE,
        )
    if isinstance(seed, bool) or not isinstance(seed, int) or not 0 <= seed <= 2**31 - 1:
        raise BaselineFailure("invalid_seed", "seed out of range", BaselineExitCode.INPUT_ERROR)

    fixture = None
    if scenario_id in DEFAULT_REQUEST_BY_SCENARIO:
        fixture_id = request_fixture_id or DEFAULT_REQUEST_BY_SCENARIO[scenario_id]
        fixtures = {item.fixture_id: item for item in load_request_fixtures().fixtures}
        fixture = fixtures.get(fixture_id)
        if fixture is None or fixture.scenario != scenario_id:
            raise BaselineFailure(
                "invalid_request_fixture",
                "request fixture is unknown or belongs to another scenario",
                BaselineExitCode.INPUT_ERROR,
                {"fixture_id": fixture_id, "scenario_id": scenario_id},
            )
    elif request_fixture_id is not None:
        raise BaselineFailure(
            "invalid_request_fixture",
            "abnormality scenarios do not take an order request fixture",
            BaselineExitCode.USAGE,
        )

    settings = None
    if provider_kind == "deepseek":
        try:
            settings = _live_settings(env_file)
        except ConfigurationError as exc:
            target = baseline._prepare_output(output)  # noqa: SLF001
            summary = {
                "status": "SKIPPED",
                "gate": "S10-A10",
                "evidence_kind": "live_supervisor_skipped",
                "reason": exc.message,
                "missing": exc.details.get("missing", []),
                "message": "No DeepSeek request was attempted; paid-run settings are incomplete.",
            }
            _write(target / "summary.json", summary)
            return AgentRunResult("SKIPPED", int(BaselineExitCode.USAGE), summary)

    bundle = load_scenario(scenario_id)
    target = baseline._prepare_output(output)  # noqa: SLF001
    repository = SQLiteStateRepository(target / "state.sqlite3")
    repository.initialize()
    services = ApplicationServices(repository, bundle.catalog, seed=seed)
    services.create_run(materialize_initial_snapshot(bundle))
    run_id = bundle.scenario.run_id
    bootstrap = bootstrap_scenario(
        services,
        bundle,
        seed=seed,
        output=target,
        policy=bootstrap_policy or "",
        wall_clock=clock,
    )

    if fixture is not None:
        task = SupervisorTask(WorkflowKind.ORDER_REQUEST, fixture.request_text)
        turns = scripted_order_turns(fixture.scripted_draft)
    else:
        text, machines, query = ABNORMALITY_TASKS[scenario_id]
        task = SupervisorTask(WorkflowKind.ABNORMALITY, text, machines)
        turns = scripted_abnormality_turns(query)

    provider: Provider
    secrets: tuple[str, ...] = ()
    if settings is None:
        provider = TemplateScriptedProvider(turns)
        pricing = OFFLINE_ACCOUNTING_RATES
        budget = OFFLINE_BUDGET
        config = SupervisorConfig()
    else:
        from shopfloor_pilot.providers.deepseek import DeepSeekProvider

        provider = DeepSeekProvider(settings.connection)
        pricing = settings.pricing
        budget = SupervisorBudget(
            currency=settings.currency,
            run_cap_micros=settings.run_cap_micros,
            workflow_cap_micros=settings.workflow_cap_micros,
            limits=settings.connection.limits,
        )
        config = SupervisorConfig(
            max_output_tokens=settings.max_output_tokens,
            force_tool_choice=not settings.connection.thinking,
        )
        secrets = settings.secrets

    before = _operational_identity(services, run_id)
    workflow_id = f"workflow-agent-{scenario_id}-{seed}"
    ledger = BudgetLedger(target / "provider_ledger.sqlite3")
    trace = TraceStore(target / "agent_trace.sqlite3", secrets=secrets)
    try:
        workflow = SupervisorWorkflow(
            facade=services.for_model(),
            catalog=bundle.catalog,
            scenario_clock=bundle.scenario.clock,
            seed=seed,
            provider=provider,
            ledger=ledger,
            pricing=pricing,
            budget=budget,
            trace=trace,
            run_id=run_id,
            workflow_id=workflow_id,
            config=config,
            clock=SystemClock(),
            wall_clock=lambda: _wall(clock),
        )
        outcome = workflow.run(task)
        after = _operational_identity(services, run_id)
        trace_view = project_trace(trace.records(workflow_id))
    finally:
        trace.close()
        ledger.close()
        close = getattr(provider, "close", None)
        if callable(close):
            close()

    application = _maybe_apply(services, outcome, approval_policy, clock)
    final = _operational_identity(services, run_id)
    _write(target / "outcome.json", outcome.public_dict())
    _write(target / "trace.json", trace_view)
    _write(target / "lifecycle.json", services.export_lifecycle(run_id).model_dump(mode="json"))
    if outcome.terminal is WorkflowPhase.AWAITING_APPROVAL and application is None:
        _write(target / "approval_request.json", _approval_request(outcome))
    status = "applied" if application is not None else outcome.terminal.value
    summary = {
        "schema_version": 1,
        "status": status,
        "scenario_id": scenario_id,
        "seed": seed,
        "provider": provider.provider_name,
        "evidence_label": outcome.evidence_label,
        "live_provider": settings is not None,
        "run_id": run_id,
        "workflow_id": workflow_id,
        "request_fixture_id": None if fixture is None else fixture.fixture_id,
        "terminal": outcome.terminal.value,
        "reason_code": outcome.reason_code,
        "proposal": outcome.proposal,
        "bootstrap": bootstrap,
        "approval_policy": approval_policy or "none_await_human",
        "application": application,
        "operational_before_supervisor": before,
        "operational_after_supervisor": after,
        "operational_state_unchanged_by_supervisor": before == after,
        "operational_final": final,
        "budget": outcome.budget,
        "artifacts": sorted(
            item.name for item in target.iterdir() if item.is_file() and item.suffix == ".json"
        )
        + ["summary.json"],
    }
    _write(target / "summary.json", summary)
    exit_code = 0 if outcome.terminal in SAFE_TERMINALS else int(BaselineExitCode.WORKFLOW_FAILURE)
    return AgentRunResult(status, exit_code, summary)


def _approval_request(outcome: SupervisorOutcome) -> dict[str, Any]:
    assert outcome.proposal is not None
    return {
        "schema_version": 1,
        "action_required": "explicit human approval outside the supervisor",
        "run_id": outcome.run_id,
        "workflow_id": outcome.workflow_id,
        "proposal_id": outcome.proposal["proposal_id"],
        "proposal_hash": outcome.proposal["proposal_hash"],
        "base_state_version": outcome.proposal["base_state_version"],
        "proposed_plan_revision": outcome.proposal["proposed_plan_revision"],
        "order_deltas": outcome.proposal["order_deltas"],
        "resource_deltas": outcome.proposal["resource_deltas"],
        "claims": list(outcome.claims),
        "approval_api": (
            "ApplicationServices.record_human_decision(run_id, proposal_id, proposal_hash, "
            "approval_id=..., decision=approved, actor='human:<name>') then "
            "apply_approved_plan(...)"
        ),
    }


def _maybe_apply(
    services: ApplicationServices,
    outcome: SupervisorOutcome,
    approval_policy: str | None,
    clock: WallClock,
) -> dict[str, Any] | None:
    """Explicit offline batch policy only; never the default."""

    if (
        approval_policy != "deterministic"
        or outcome.terminal is not WorkflowPhase.AWAITING_APPROVAL
    ):
        return None
    assert outcome.proposal is not None
    approval = services.record_deterministic_policy_decision(
        outcome.run_id,
        outcome.proposal["proposal_id"],
        outcome.proposal["proposal_hash"],
        approval_id="approval-agent-policy",
        policy_name="deterministic",
        decision=ApprovalDecision.APPROVED,
        reason="Explicit --approval-policy deterministic offline batch decision.",
        decided_at_utc=_wall(clock),
    )
    result = services.apply_approved_plan(
        outcome.run_id,
        outcome.proposal["proposal_id"],
        outcome.proposal["proposal_hash"],
        approval.approval_id,
        "apply-agent-policy",
    )
    return {
        "actor": approval.actor,
        "approval_id": approval.approval_id,
        "status": result.status.value,
        "previous_state_version": result.previous_state_version,
        "resulting_state_version": result.resulting_state_version,
        "effective_time_seconds": result.effective_time_seconds,
    }


__all__ = [
    "AGENT_SCENARIOS",
    "DEFAULT_REQUEST_FIXTURES",
    "OFFLINE_ACCOUNTING_RATES",
    "OFFLINE_BUDGET",
    "AgentRunResult",
    "RequestFixture",
    "RequestFixtureSet",
    "SolverConfig",
    "bootstrap_scenario",
    "load_request_fixtures",
    "run_agent_scenario",
    "scripted_abnormality_turns",
    "scripted_order_turns",
]
