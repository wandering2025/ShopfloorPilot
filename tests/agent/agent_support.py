"""Shared offline helpers for S10 supervisor tests and evidence recording."""

from __future__ import annotations

import sqlite3
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from shopfloor_pilot.agent.fake import ScriptedTurn, TemplateScriptedProvider
from shopfloor_pilot.agent.harness import (
    OFFLINE_ACCOUNTING_RATES,
    bootstrap_scenario,
    load_request_fixtures,
)
from shopfloor_pilot.agent.trace import TraceRecord, TraceStore, project_trace
from shopfloor_pilot.agent.workflow import (
    SupervisorBudget,
    SupervisorConfig,
    SupervisorOutcome,
    SupervisorTask,
    SupervisorWorkflow,
)
from shopfloor_pilot.budgets import BudgetLedger, ProviderLimits
from shopfloor_pilot.domain.fixtures import (
    ScenarioBundle,
    load_scenario,
    materialize_initial_snapshot,
)
from shopfloor_pilot.providers.clock import FakeClock
from shopfloor_pilot.services import ApplicationServices
from shopfloor_pilot.storage import SQLiteStateRepository

FIXED_UTC = "2026-10-21T12:00:00.000+00:00"
FAULT_SENTINEL = "EVAL_ONLY_SENTINEL_SPINDLE_BEARING_DAMAGE"
FAULT_TRUTH_REF = "truth-breakdown-cnc02"
_TEMPLATE_ROOT = Path(tempfile.mkdtemp(prefix="s10-agent-templates-"))
_TEMPLATES: dict[str, Path] = {}


def _fixed_wall() -> datetime:
    return datetime(2026, 10, 21, 12, 0, tzinfo=UTC)


@dataclass
class RunHandle:
    services: ApplicationServices
    bundle: ScenarioBundle
    run_id: str
    root: Path


def _template(scenario_id: str) -> Path:
    cached = _TEMPLATES.get(scenario_id)
    if cached is not None:
        return cached
    root = _TEMPLATE_ROOT / scenario_id
    root.mkdir(parents=True)
    bundle = load_scenario(scenario_id)
    repository = SQLiteStateRepository(root / "state.sqlite3")
    repository.initialize()
    services = ApplicationServices(repository, bundle.catalog, seed=0)
    services.create_run(materialize_initial_snapshot(bundle))
    bootstrap_scenario(
        services, bundle, seed=0, output=root, policy="deterministic", wall_clock=_fixed_wall
    )
    _TEMPLATES[scenario_id] = root / "state.sqlite3"
    return _TEMPLATES[scenario_id]


def fresh_run(tmp: Path, scenario_id: str) -> RunHandle:
    """A private copy of the scenario run at the supervisor decision point."""

    tmp.mkdir(parents=True, exist_ok=True)
    target = tmp / "state.sqlite3"
    source = sqlite3.connect(_template(scenario_id))
    destination = sqlite3.connect(target)
    with destination:
        source.backup(destination)
    source.close()
    destination.close()
    bundle = load_scenario(scenario_id)
    services = ApplicationServices(SQLiteStateRepository(target), bundle.catalog, seed=0)
    return RunHandle(services, bundle, bundle.scenario.run_id, tmp)


def identity(services: ApplicationServices, run_id: str) -> dict[str, Any]:
    """Operational identity: everything a supervisor must never change."""

    checkpoint = services.get_checkpoint(run_id)
    snapshot = checkpoint.snapshot
    return {
        "state_version": snapshot.state_version,
        "plan_revision": snapshot.plan_revision,
        "simulated_time_seconds": snapshot.simulated_time_seconds,
        "snapshot_hash": checkpoint.snapshot_hash,
        "orders": [item.model_dump(mode="json") for item in snapshot.orders],
        "machines": [item.model_dump(mode="json") for item in snapshot.machines],
        "attempts": [item.model_dump(mode="json") for item in snapshot.attempts],
        "event_count": checkpoint.last_sequence,
    }


@dataclass
class SupervisorRun:
    outcome: SupervisorOutcome
    workflow: SupervisorWorkflow
    provider: TemplateScriptedProvider
    records: tuple[TraceRecord, ...]
    trace_view: dict[str, Any]
    ledger_path: Path
    trace_path: Path


def run_supervisor(
    handle: RunHandle,
    task: SupervisorTask,
    turns: Sequence[ScriptedTurn],
    *,
    workflow_id: str = "workflow-s10-test",
    limits: ProviderLimits | None = None,
    config: SupervisorConfig | None = None,
    secrets: Sequence[str] = (),
    ledger_path: Path | None = None,
    workflow_hook: Callable[[SupervisorWorkflow], None] | None = None,
) -> SupervisorRun:
    provider = TemplateScriptedProvider(turns)
    ledger_file = ledger_path or handle.root / "provider_ledger.sqlite3"
    trace_file = handle.root / f"trace-{workflow_id}.sqlite3"
    ledger = BudgetLedger(ledger_file)
    trace = TraceStore(trace_file, secrets=secrets)
    try:
        workflow = SupervisorWorkflow(
            facade=handle.services.for_model(),
            catalog=handle.bundle.catalog,
            scenario_clock=handle.bundle.scenario.clock,
            seed=0,
            provider=provider,
            ledger=ledger,
            pricing=OFFLINE_ACCOUNTING_RATES,
            budget=SupervisorBudget(
                currency="USD",
                run_cap_micros=50_000,
                workflow_cap_micros=50_000,
                limits=limits or ProviderLimits(),
            ),
            trace=trace,
            run_id=handle.run_id,
            workflow_id=workflow_id,
            config=config,
            clock=FakeClock(),
            wall_clock=lambda: FIXED_UTC,
        )
        if workflow_hook is not None:
            workflow_hook(workflow)
        outcome = workflow.run(task)
        records = trace.records(workflow_id)
    finally:
        trace.close()
        ledger.close()
    return SupervisorRun(
        outcome=outcome,
        workflow=workflow,
        provider=provider,
        records=records,
        trace_view=project_trace(records),
        ledger_path=ledger_file,
        trace_path=trace_file,
    )


def fixtures_by_id() -> dict[str, Any]:
    return {item.fixture_id: item for item in load_request_fixtures().fixtures}


def tool_results(run: SupervisorRun, tool: str) -> list[dict[str, Any]]:
    return [
        item.payload["result"]
        for item in run.records
        if item.kind == "tool_result" and item.payload["tool"] == tool
    ]
