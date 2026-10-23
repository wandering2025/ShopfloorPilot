"""S11 backend scenario runner: initial planning → disruption → supervisor → approval → apply.

The runner is a scenario harness. It may read evaluator interventions and inject
them through explicit coordinator advances; the supervisor sees only the
model-readable facade. Without an explicit offline batch approval policy the
runner stops at the first proposal (including initial planning) and writes an
``approval_request.json``; :func:`decide_recovery_scenario` records a human
decision and resumes. Every stop and resume is reconstructed from SQLite.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from shopfloor_pilot import baseline
from shopfloor_pilot.agent.fake import DETERMINISTIC_BASELINE_LABEL, ScriptedTurn
from shopfloor_pilot.agent.harness import (
    ABNORMALITY_TASKS,
    _live_settings,
    load_request_fixtures,
    scripted_abnormality_turns,
    scripted_order_turns,
)
from shopfloor_pilot.agent.trace import TraceStore, project_trace
from shopfloor_pilot.agent.workflow import SupervisorTask
from shopfloor_pilot.baseline import BaselineExitCode, BaselineFailure
from shopfloor_pilot.domain.fixtures import (
    InterventionKind,
    ScenarioBundle,
    load_scenario,
    materialize_initial_snapshot,
)
from shopfloor_pilot.domain.models import OrderStatus
from shopfloor_pilot.providers.errors import ConfigurationError
from shopfloor_pilot.scheduling import SchedulingResult
from shopfloor_pilot.services import ApplicationServices
from shopfloor_pilot.storage import SQLiteStateRepository
from shopfloor_pilot.tools import WorkflowKind

from .coordinator import RecoveryCoordinator, SessionFactory
from .invariants import check_run_invariants, invariants_passed
from .lifecycle_view import lifecycle_view
from .models import ActionStatus, DecisionKind, DecisionRecord, DecisionSource, DecisionStatus
from .review import approval_request
from .sessions import supervisor_session_factory

RECOVERY_SCENARIOS = ("normal", "urgent_order", "machine_breakdown", "no_feasible_recovery")
EXPECTED_TERMINALS = {
    "normal": ("completed", "scenario_completed"),
    "urgent_order": ("completed", "scenario_completed"),
    "machine_breakdown": ("completed", "scenario_completed"),
    "no_feasible_recovery": ("blocked", "no_feasible_schedule"),
}
NO_FEASIBLE_TASK = (
    "Operator report: CNC02 and CNC03 stopped with drive and lubrication alarms. Diagnose "
    "from observable evidence and propose a recovery schedule.",
    ("CNC02", "CNC03"),
    "zero drive current and low lubrication pressure on milling machines",
)
# Safe stops chosen by an operator rather than a scenario failure.
OPERATOR_STOP_REASONS = frozenset({"proposal_rejected", "operator_cancelled"})
MANIFEST = "recovery_manifest.json"
WallClock = Callable[[], datetime]


@dataclass(frozen=True, slots=True)
class PhaseSpec:
    name: str
    kind: DecisionKind
    source: DecisionSource
    with_order_delta: bool = False
    pre_advance: tuple[int, str, tuple[Any, ...], tuple[Any, ...]] | None = None
    task: SupervisorTask | None = None
    turns: tuple[ScriptedTurn, ...] = ()
    generator: Callable[[DecisionRecord], SchedulingResult] | None = None

    def workflow_id(self, scenario_id: str, seed: int) -> str:
        return f"workflow-s11-{scenario_id}-{seed}-{self.name}"


@dataclass(slots=True)
class RunnerContext:
    scenario_id: str
    seed: int
    provider_kind: str
    output: Path
    bundle: ScenarioBundle
    services: ApplicationServices
    coordinator: RecoveryCoordinator
    approval_policy: str | None
    clock: WallClock
    settings: Any = None
    supervisor_timeout_seconds: float | None = None
    step_log: list[dict[str, Any]] = field(default_factory=list)

    @property
    def run_id(self) -> str:
        return self.bundle.scenario.run_id

    def wall(self) -> str:
        return self.clock().astimezone(UTC).isoformat(timespec="milliseconds")


@dataclass(frozen=True, slots=True)
class RecoveryRunResult:
    status: str
    exit_code: int
    summary: dict[str, Any]


def _write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


# ------------------------------------------------------------------- phases


def _phases(ctx: RunnerContext) -> tuple[PhaseSpec, ...]:
    bundle = ctx.bundle
    scenario_id = ctx.scenario_id
    fixtures = {item.fixture_id: item for item in load_request_fixtures().fixtures}

    def baseline_initial(policy: str) -> Callable[[DecisionRecord], SchedulingResult]:
        def generate(decision: DecisionRecord) -> SchedulingResult:
            if policy == "positive_time_origin_floor":
                return baseline._generate(  # noqa: SLF001 - accepted S06 bootstrap policy
                    ctx.services,
                    ctx.run_id,
                    "initial",
                    seed=ctx.seed,
                    not_before_seconds=1,
                    planning_policy=policy,
                    wall_clock=ctx.clock,
                    workflow_id=decision.workflow_id,
                )
            return baseline._scarce_capability_first_initial(  # noqa: SLF001
                ctx.services,
                bundle.catalog,
                ctx.services.get_snapshot(ctx.run_id),
                seed=ctx.seed,
                output=ctx.output,
                wall_clock=ctx.clock,
                workflow_id=decision.workflow_id,
            )

        return generate

    def order_phase(name: str, kind: DecisionKind, fixture_id: str, pre: Any) -> PhaseSpec:
        fixture = fixtures[fixture_id]
        return PhaseSpec(
            name=name,
            kind=kind,
            source=DecisionSource.SUPERVISOR,
            with_order_delta=True,
            pre_advance=pre,
            task=SupervisorTask(WorkflowKind.ORDER_REQUEST, fixture.request_text),
            turns=tuple(scripted_order_turns(fixture.scripted_draft)),
        )

    def fault_phase(task: tuple[str, tuple[str, ...], str]) -> PhaseSpec:
        text, machines, query = task
        disruption = bundle.evaluator.interventions[0].at_seconds
        return PhaseSpec(
            name="recovery",
            kind=DecisionKind.FAULT_RECOVERY,
            source=DecisionSource.SUPERVISOR,
            pre_advance=(
                disruption,
                f"advance-s11-{scenario_id}-disruption",
                tuple(bundle.evaluator.interventions),
                tuple(bundle.evaluator.observation_templates),
            ),
            task=SupervisorTask(WorkflowKind.ABNORMALITY, text, machines),
            turns=tuple(scripted_abnormality_turns(query)),
        )

    if scenario_id == "normal":
        return (
            order_phase("initial", DecisionKind.INITIAL_PLANNING, "normal-en-plate-today", None),
        )
    if scenario_id == "urgent_order":
        arrival = next(
            item.at_seconds
            for item in bundle.evaluator.interventions
            if item.kind is InterventionKind.ADD_ORDER
        )
        return (
            PhaseSpec(
                name="initial",
                kind=DecisionKind.INITIAL_PLANNING,
                source=DecisionSource.DETERMINISTIC_BASELINE,
                generator=baseline_initial("positive_time_origin_floor"),
            ),
            order_phase(
                "urgent",
                DecisionKind.URGENT_ORDER,
                "urgent-zh-plate-90min",
                (arrival - 1, "advance-s11-urgent-before-request", (), ()),
            ),
        )
    initial = PhaseSpec(
        name="initial",
        kind=DecisionKind.INITIAL_PLANNING,
        source=DecisionSource.DETERMINISTIC_BASELINE,
        generator=baseline_initial("scarce_capability_first"),
    )
    if scenario_id == "machine_breakdown":
        return (initial, fault_phase(ABNORMALITY_TASKS["machine_breakdown"]))
    return (initial, fault_phase(NO_FEASIBLE_TASK))


# --------------------------------------------------------- supervisor session


def _session_factory(
    ctx: RunnerContext, decision: DecisionRecord, phase: PhaseSpec
) -> SessionFactory:
    assert phase.task is not None

    def write_trace(trace: TraceStore) -> None:
        _write(
            ctx.output / "traces" / f"{decision.workflow_id}.json",
            project_trace(trace.records(decision.workflow_id)),
        )

    return supervisor_session_factory(
        services=ctx.services,
        bundle=ctx.bundle,
        seed=ctx.seed,
        decision=decision,
        task=phase.task,
        ledger_path=ctx.output / "provider_ledger.sqlite3",
        trace_path=ctx.output / "agent_trace.sqlite3",
        wall_clock=ctx.wall,
        settings=ctx.settings,
        turns=phase.turns,
        offline_provider_kind=ctx.provider_kind,
        on_close=write_trace,
    )


# ---------------------------------------------------------------- the driver


def _approve_and_apply(ctx: RunnerContext, decision: DecisionRecord) -> dict[str, Any]:
    assert decision.pending_proposal_id is not None and decision.pending_proposal_hash is not None
    approval = ctx.coordinator.approve_by_policy(
        ctx.run_id,
        decision.workflow_id,
        proposal_id=decision.pending_proposal_id,
        proposal_hash=decision.pending_proposal_hash,
    )
    record: dict[str, Any] = {"approval": approval.public_dict()}
    if approval.status in (ActionStatus.APPROVED, ActionStatus.ALREADY_APPROVED):
        assert approval.approval_id is not None
        applied = ctx.coordinator.apply(
            ctx.run_id,
            decision.workflow_id,
            proposal_id=decision.pending_proposal_id,
            proposal_hash=decision.pending_proposal_hash,
            approval_id=approval.approval_id,
        )
        record["application"] = applied.public_dict()
    return record


def _run_phase(ctx: RunnerContext, phase: PhaseSpec) -> DecisionRecord:
    workflow_id = phase.workflow_id(ctx.scenario_id, ctx.seed)
    existing = {item.workflow_id: item for item in ctx.coordinator.decisions(ctx.run_id)}
    decision = existing.get(workflow_id)
    if decision is not None:
        return decision
    if phase.pre_advance is not None:
        target, advance_id, interventions, observations = phase.pre_advance
        ctx.coordinator.advance(
            ctx.run_id, target, advance_id, interventions=interventions, observations=observations
        )
        ctx.step_log.append({"step": "advance", "advance_id": advance_id, "target_seconds": target})
    settled = ctx.coordinator.settle_decision_boundary(
        ctx.run_id,
        f"advance-s11-{ctx.scenario_id}-{phase.name}",
        with_order_delta=phase.with_order_delta,
    )
    if settled:
        ctx.step_log.append({"step": "boundary_settle", "advanced_to_seconds": settled})
    provider = None if phase.source is not DecisionSource.SUPERVISOR else ctx.provider_kind
    decision = ctx.coordinator.open_decision(
        ctx.run_id,
        workflow_id,
        kind=phase.kind,
        source=phase.source,
        provider=provider,
        focus_machine_ids=() if phase.task is None else phase.task.focus_machine_ids,
        with_order_delta=phase.with_order_delta,
    )
    ctx.step_log.append({"step": "decision_opened", "workflow_id": workflow_id})
    if phase.source is DecisionSource.SUPERVISOR:
        step = ctx.coordinator.run_supervisor(
            decision,
            _session_factory(ctx, decision, phase),
            timeout_seconds=ctx.supervisor_timeout_seconds,
        )
        ctx.step_log.append({"step": "supervisor", **step.public_dict()})
        return step.decision
    assert phase.generator is not None
    settled, result = ctx.coordinator.propose_deterministic(decision, phase.generator)
    ctx.step_log.append(
        {
            "step": "deterministic_proposal",
            "workflow_id": workflow_id,
            "solver_outcome": result.outcome.value,
        }
    )
    return settled


def _final_target(ctx: RunnerContext) -> int:
    lifecycle = ctx.services.export_lifecycle(ctx.run_id)
    proposals = {item.proposal_id: item for item in lifecycle.proposals}
    latest = max(lifecycle.approval_consumptions, key=lambda item: item.resulting_state_version)
    target = proposals[latest.proposal_id].objective.makespan_seconds
    if ctx.scenario_id == "normal":
        target = max(target, max(item.at_seconds for item in ctx.bundle.evaluator.interventions))
    return max(target, ctx.services.get_snapshot(ctx.run_id).simulated_time_seconds)


def _drive(ctx: RunnerContext) -> tuple[str, str, DecisionRecord | None]:
    """Advance the loop until it completes, blocks, or needs a human decision."""

    for phase in _phases(ctx):
        decision = _run_phase(ctx, phase)
        if decision.status is DecisionStatus.AWAITING_APPROVAL:
            if ctx.approval_policy is None:
                return "awaiting_approval", "human_approval_required", decision
            ctx.step_log.append({"step": "policy_decision", **_approve_and_apply(ctx, decision)})
            decision = ctx.coordinator.decision(ctx.run_id, decision.workflow_id)
        if decision.status is not DecisionStatus.APPLIED:
            return "blocked", decision.reason_code or decision.status.value, decision
    target = _final_target(ctx)
    advance_id = f"advance-s11-{ctx.scenario_id}-final"
    final_kwargs: dict[str, Any] = {}
    if ctx.scenario_id == "normal":
        final_kwargs = {
            "interventions": ctx.bundle.evaluator.interventions,
            "observations": ctx.bundle.evaluator.observation_templates,
        }
    elif ctx.scenario_id == "urgent_order":
        final_kwargs = {"observations": ctx.bundle.evaluator.observation_templates}
    ctx.coordinator.advance(ctx.run_id, target, advance_id, **final_kwargs)
    ctx.step_log.append({"step": "advance", "advance_id": advance_id, "target_seconds": target})
    final = ctx.services.get_snapshot(ctx.run_id)
    incomplete = sorted(
        item.order_id for item in final.orders if item.status is not OrderStatus.COMPLETED
    )
    if incomplete:
        return "failed", "incomplete_execution", None
    return "completed", "scenario_completed", None


def _finalize(
    ctx: RunnerContext, status: str, reason: str, stopped: DecisionRecord | None
) -> RecoveryRunResult:
    run_id = ctx.run_id
    services = ctx.services
    decisions = ctx.coordinator.decisions(run_id)
    invariants = check_run_invariants(services, run_id)
    replay_header = services.export_replay_bundle(run_id, ctx.output / "events.jsonl")
    lifecycle = services.export_lifecycle(run_id)
    _write(ctx.output / "lifecycle.json", lifecycle.model_dump(mode="json"))
    _write(ctx.output / "lifecycle_view.json", lifecycle_view(lifecycle))
    _write(
        ctx.output / "final_snapshot.json", services.get_snapshot(run_id).model_dump(mode="json")
    )
    _write(
        ctx.output / "metrics.json", services.calculate_run_metrics(run_id).model_dump(mode="json")
    )
    _write(ctx.output / "decisions.json", [item.public_dict() for item in decisions])
    _write(ctx.output / "invariants.json", [item.model_dump(mode="json") for item in invariants])
    request_path = ctx.output / "approval_request.json"
    if stopped is not None and status == "awaiting_approval":
        _write(request_path, approval_request(services, stopped))
    elif request_path.exists():
        request_path.unlink()
    expected = EXPECTED_TERMINALS[ctx.scenario_id]
    checkpoint = services.get_checkpoint(run_id)
    supervisor_decisions = [item for item in decisions if item.source is DecisionSource.SUPERVISOR]
    summary = {
        "schema_version": 1,
        "command": "recovery",
        "status": status,
        "reason_code": reason,
        "scenario_id": ctx.scenario_id,
        "seed": ctx.seed,
        "run_id": run_id,
        "provider": ctx.provider_kind,
        "live_provider": ctx.settings is not None,
        "evidence_label": (
            "LIVE DeepSeek provider evidence"
            if ctx.settings is not None
            else (
                DETERMINISTIC_BASELINE_LABEL
                if ctx.provider_kind == "deterministic-baseline"
                else "OFFLINE CONTRACT EVIDENCE ONLY - scripted fake provider"
            )
        ),
        "approval_policy": ctx.approval_policy or "none_await_human",
        "expected_terminal": {"status": expected[0], "reason_code": expected[1]},
        "terminal_matches_expectation": (status, reason) == expected
        or (status == "awaiting_approval" and ctx.approval_policy is None),
        "invariants_passed": invariants_passed(invariants),
        "failed_invariants": [item.code for item in invariants if not item.passed],
        "decisions": [
            {
                "workflow_id": item.workflow_id,
                "kind": item.kind.value,
                "source": item.source.value,
                "provider": item.provider,
                "status": item.status.value,
                "outcome": item.outcome,
                "reason_code": item.reason_code,
                "decision_time_seconds": item.decision_time_seconds,
                "decision_boundary_seconds": item.decision_boundary_seconds,
                "proposal_id": item.pending_proposal_id,
                "consumed_approval_id": item.consumed_approval_id,
            }
            for item in decisions
        ],
        "supervisor_decision_count": len(supervisor_decisions),
        "awaiting": None
        if stopped is None or status != "awaiting_approval"
        else stopped.workflow_id,
        "final": {
            "state_version": checkpoint.snapshot.state_version,
            "plan_revision": checkpoint.snapshot.plan_revision,
            "simulated_time_seconds": checkpoint.snapshot.simulated_time_seconds,
            "snapshot_hash": checkpoint.snapshot_hash,
            "event_count": checkpoint.last_sequence,
            "replay_final_snapshot_hash": replay_header.final_snapshot_hash,
        },
        "steps": ctx.step_log,
        "artifacts": sorted(
            str(item.relative_to(ctx.output))
            for item in ctx.output.rglob("*.json")
            if item.name != "summary.json"
        )
        + ["events.jsonl", "summary.json"],
    }
    _write(ctx.output / "summary.json", summary)
    operator_stop = status == "blocked" and reason in OPERATOR_STOP_REASONS
    ok = summary["invariants_passed"] and (summary["terminal_matches_expectation"] or operator_stop)
    exit_code = 0 if ok else int(BaselineExitCode.WORKFLOW_FAILURE)
    return RecoveryRunResult(status, exit_code, summary)


def _context(
    *,
    provider_kind: str,
    scenario_id: str,
    seed: int,
    output: Path,
    approval_policy: str | None,
    clock: WallClock,
    settings: Any,
    create: bool,
    supervisor_timeout_seconds: float | None = None,
) -> RunnerContext:
    bundle = load_scenario(scenario_id)
    repository = SQLiteStateRepository(output / "state.sqlite3")
    if create:
        repository.initialize()
    services = ApplicationServices(repository, bundle.catalog, seed=seed)
    if create:
        services.create_run(materialize_initial_snapshot(bundle))
    coordinator = RecoveryCoordinator(
        services,
        wall_clock=lambda: clock().astimezone(UTC).isoformat(timespec="milliseconds"),
        batch_policy=approval_policy,
    )
    return RunnerContext(
        scenario_id=scenario_id,
        seed=seed,
        provider_kind=provider_kind,
        output=output,
        bundle=bundle,
        services=services,
        coordinator=coordinator,
        approval_policy=approval_policy,
        clock=clock,
        settings=settings,
        supervisor_timeout_seconds=supervisor_timeout_seconds,
    )


def _settings(provider_kind: str, env_file: Path | None) -> Any:
    if provider_kind != "deepseek":
        return None
    return _live_settings(env_file)


def run_recovery_scenario(
    *,
    provider_kind: str,
    scenario_id: str,
    seed: int,
    output: Path,
    approval_policy: str | None = None,
    env_file: Path | None = None,
    wall_clock: WallClock | None = None,
    supervisor_timeout_seconds: float | None = None,
) -> RecoveryRunResult:
    """Run one canonical S11 scenario on a fresh run in an empty ``output``."""

    clock = wall_clock or (lambda: datetime.now(UTC))
    if provider_kind not in {"deterministic-baseline", "scripted-fake", "deepseek"}:
        raise BaselineFailure("invalid_provider", "unsupported provider", BaselineExitCode.USAGE)
    if scenario_id not in RECOVERY_SCENARIOS:
        raise BaselineFailure("invalid_scenario", "unsupported scenario", BaselineExitCode.USAGE)
    if approval_policy not in (None, "deterministic"):
        raise BaselineFailure(
            "invalid_approval_policy", "only deterministic is supported", BaselineExitCode.USAGE
        )
    if isinstance(seed, bool) or not isinstance(seed, int) or not 0 <= seed <= 2**31 - 1:
        raise BaselineFailure("invalid_seed", "seed out of range", BaselineExitCode.INPUT_ERROR)
    try:
        settings = _settings(provider_kind, env_file)
    except ConfigurationError as exc:
        target = baseline._prepare_output(output)  # noqa: SLF001
        summary = {
            "schema_version": 1,
            "command": "recovery",
            "status": "SKIPPED",
            "gate": "S11-A11",
            "reason": exc.message,
            "missing": exc.details.get("missing", []),
            "message": "No DeepSeek request was attempted; paid-run settings are incomplete.",
        }
        _write(target / "summary.json", summary)
        return RecoveryRunResult("SKIPPED", int(BaselineExitCode.USAGE), summary)
    target = baseline._prepare_output(output)  # noqa: SLF001
    _write(
        target / MANIFEST,
        {
            "schema_version": 1,
            "scenario_id": scenario_id,
            "seed": seed,
            "provider": provider_kind,
            "approval_policy": approval_policy,
        },
    )
    ctx = _context(
        provider_kind=provider_kind,
        scenario_id=scenario_id,
        seed=seed,
        output=target,
        approval_policy=approval_policy,
        clock=clock,
        settings=settings,
        create=True,
        supervisor_timeout_seconds=supervisor_timeout_seconds,
    )
    status, reason, stopped = _drive(ctx)
    return _finalize(ctx, status, reason, stopped)


def decide_recovery_scenario(
    *,
    output: Path,
    workflow_id: str,
    proposal_hash: str,
    actor: str,
    approve: bool,
    reason: str | None = None,
    env_file: Path | None = None,
    wall_clock: WallClock | None = None,
) -> RecoveryRunResult:
    """Record one explicit human decision on a paused run, apply it, and resume."""

    clock = wall_clock or (lambda: datetime.now(UTC))
    manifest_path = output / MANIFEST
    if not manifest_path.is_file():
        raise BaselineFailure(
            "not_a_recovery_run", "output has no recovery manifest", BaselineExitCode.INPUT_ERROR
        )
    manifest = json.loads(manifest_path.read_text())
    if manifest.get("approval_policy") is not None:
        raise BaselineFailure(
            "batch_policy_run",
            "this run uses an offline batch policy and takes no human decisions",
            BaselineExitCode.USAGE,
        )
    try:
        settings = _settings(manifest["provider"], env_file)
    except ConfigurationError as exc:
        raise BaselineFailure(
            "live_settings_missing", exc.message, BaselineExitCode.USAGE, dict(exc.details)
        ) from exc
    ctx = _context(
        provider_kind=manifest["provider"],
        scenario_id=manifest["scenario_id"],
        seed=manifest["seed"],
        output=output,
        approval_policy=None,
        clock=clock,
        settings=settings,
        create=False,
    )
    decision = ctx.coordinator.decision(ctx.run_id, workflow_id)
    if decision.pending_proposal_id is None:
        raise BaselineFailure(
            "no_pending_proposal", "decision has no pending proposal", BaselineExitCode.INPUT_ERROR
        )
    if approve:
        action = ctx.coordinator.approve(
            ctx.run_id,
            workflow_id,
            proposal_id=decision.pending_proposal_id,
            proposal_hash=proposal_hash,
            actor=actor,
            reason=reason,
        )
    else:
        action = ctx.coordinator.reject(
            ctx.run_id,
            workflow_id,
            proposal_id=decision.pending_proposal_id,
            proposal_hash=proposal_hash,
            actor=actor,
            reason=reason,
        )
    ctx.step_log.append({"step": "human_decision", **action.public_dict()})
    if action.status in (ActionStatus.APPROVED, ActionStatus.ALREADY_APPROVED):
        assert action.approval_id is not None
        applied = ctx.coordinator.apply(
            ctx.run_id,
            workflow_id,
            proposal_id=decision.pending_proposal_id,
            proposal_hash=proposal_hash,
            approval_id=action.approval_id,
        )
        ctx.step_log.append({"step": "application", **applied.public_dict()})
    elif not action.ok:
        current = ctx.coordinator.decision(ctx.run_id, workflow_id)
        still_waiting = current.status is DecisionStatus.AWAITING_APPROVAL
        result = _finalize(
            ctx,
            "awaiting_approval" if still_waiting else "blocked",
            action.reason_code or action.status.value,
            current,
        )
        return RecoveryRunResult(
            "decision_refused", int(BaselineExitCode.WORKFLOW_FAILURE), result.summary
        )
    status, reason_code, stopped = _drive(ctx)
    return _finalize(ctx, status, reason_code, stopped)


__all__ = [
    "EXPECTED_TERMINALS",
    "RECOVERY_SCENARIOS",
    "RecoveryRunResult",
    "decide_recovery_scenario",
    "run_recovery_scenario",
]
