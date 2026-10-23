"""Deterministic, no-provider baseline scenario and replay workflows.

This module is the CLI-facing scenario harness.  It deliberately keeps batch
approval explicit and routes every proposal through :class:`ApplicationServices`.
Evaluator interventions are consumed only by this harness; they are never exposed
through the public runtime projection.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import IntEnum, StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from shopfloor_pilot.domain.canonical import canonical_hash
from shopfloor_pilot.domain.fixtures import (
    DEFAULT_SCENARIO_ROOT,
    InterventionKind,
    ScenarioBundle,
    ScenarioOutcome,
    load_scenario,
    materialize_initial_snapshot,
)
from shopfloor_pilot.domain.models import (
    ApprovalDecision,
    CandidateProposal,
    Capability,
    DomainCatalog,
    OperationalSnapshot,
    OrderDelta,
    OrderDeltaKind,
    OrderStatus,
    ScheduleObjective,
)
from shopfloor_pilot.metrics import DecisionLatency
from shopfloor_pilot.scheduling import (
    SchedulingResult,
    SolveOutcome,
    SolverConfig,
    solve_schedule,
)
from shopfloor_pilot.storage import SQLiteStateRepository
from shopfloor_pilot.validation import validate_candidate, validate_schedule

WallClock = Callable[[], datetime]


def _utc_now() -> datetime:
    return datetime.now(UTC)


class BaselineExitCode(IntEnum):
    """Stable process exits used by ``shopfloor-pilot``.

    Expected completion, including the fixture whose expected result is a safe
    blocked state, is zero.  Usage errors are two, invalid inputs are three,
    deterministic workflow failures are four, artifact/storage failures are five,
    and unexpected software errors are one.
    """

    SUCCESS = 0
    SOFTWARE_ERROR = 1
    USAGE = 2
    INPUT_ERROR = 3
    WORKFLOW_FAILURE = 4
    ARTIFACT_FAILURE = 5


class BaselineStatus(StrEnum):
    COMPLETED = "completed"
    BLOCKED = "blocked"
    REPLAYED = "replayed"
    FAILED = "failed"


@dataclass(frozen=True)
class BaselineFailure(RuntimeError):
    """Expected CLI failure with a stable reason and exit status."""

    reason_code: str
    message: str
    exit_code: BaselineExitCode
    details: dict[str, Any] | None = None

    def __str__(self) -> str:
        return f"{self.reason_code}: {self.message}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "status": BaselineStatus.FAILED.value,
            "reason_code": self.reason_code,
            "message": self.message,
            "exit_code": int(self.exit_code),
            "details": self.details or {},
        }


@dataclass(frozen=True)
class BaselineRunResult:
    scenario_id: str
    run_id: str
    status: BaselineStatus
    reason_code: str
    output_directory: Path
    state_version: int
    plan_revision: int
    simulated_time_seconds: int
    snapshot_hash: str
    event_count: int
    artifacts: dict[str, str]

    @property
    def exit_code(self) -> BaselineExitCode:
        return BaselineExitCode.SUCCESS

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "command": "baseline",
            "scenario_id": self.scenario_id,
            "run_id": self.run_id,
            "status": self.status.value,
            "reason_code": self.reason_code,
            "exit_code": int(self.exit_code),
            "output_directory": str(self.output_directory),
            "state_version": self.state_version,
            "plan_revision": self.plan_revision,
            "simulated_time_seconds": self.simulated_time_seconds,
            "snapshot_hash": self.snapshot_hash,
            "event_count": self.event_count,
            "artifacts": dict(sorted(self.artifacts.items())),
        }


@dataclass(frozen=True)
class ReplayRunResult:
    run_id: str
    output_directory: Path
    state_version: int
    simulated_time_seconds: int
    snapshot_hash: str
    event_count: int
    artifacts: dict[str, str]

    @property
    def exit_code(self) -> BaselineExitCode:
        return BaselineExitCode.SUCCESS

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "command": "replay",
            "run_id": self.run_id,
            "status": BaselineStatus.REPLAYED.value,
            "reason_code": "replay_verified",
            "exit_code": int(self.exit_code),
            "output_directory": str(self.output_directory),
            "state_version": self.state_version,
            "simulated_time_seconds": self.simulated_time_seconds,
            "snapshot_hash": self.snapshot_hash,
            "event_count": self.event_count,
            "artifacts": dict(sorted(self.artifacts.items())),
        }


def _json_data(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if hasattr(value, "as_dict"):
        return value.as_dict()
    return value


def _write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(_json_data(value), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _prepare_output(output: Path) -> Path:
    target = output.expanduser().resolve()
    fixture_root = DEFAULT_SCENARIO_ROOT.resolve()
    repository_root = fixture_root.parent
    if target == fixture_root or fixture_root in target.parents:
        raise BaselineFailure(
            "fixture_output_forbidden",
            "output must not be the scenario fixture tree",
            BaselineExitCode.INPUT_ERROR,
            {"output": str(target)},
        )
    if target == repository_root:
        raise BaselineFailure(
            "broad_output_forbidden",
            "output must be a dedicated directory, not the repository root",
            BaselineExitCode.INPUT_ERROR,
            {"output": str(target)},
        )
    if target.exists():
        if not target.is_dir():
            raise BaselineFailure(
                "output_not_directory",
                "output exists and is not a directory",
                BaselineExitCode.INPUT_ERROR,
                {"output": str(target)},
            )
        if any(target.iterdir()):
            raise BaselineFailure(
                "output_not_empty",
                "output directory must be absent or empty",
                BaselineExitCode.INPUT_ERROR,
                {"output": str(target)},
            )
    target.mkdir(parents=True, exist_ok=True)
    return target


def _require_candidate(result: SchedulingResult, phase: str) -> Any:
    if result.candidate is None or result.validation is None or not result.validation.is_valid:
        raise BaselineFailure(
            f"{phase}_proposal_{result.outcome.value}",
            f"{phase} scheduling did not produce a validated candidate",
            BaselineExitCode.WORKFLOW_FAILURE,
            {
                "phase": phase,
                "solver_outcome": result.outcome.value,
                "diagnostics": list(result.report.diagnostics),
            },
        )
    return result.candidate


def _wall_timestamp(wall_clock: WallClock) -> str:
    value = wall_clock()
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise BaselineFailure(
            "invalid_wall_clock",
            "wall clock must return a timezone-aware datetime",
            BaselineExitCode.SOFTWARE_ERROR,
        )
    return value.astimezone(UTC).isoformat(timespec="milliseconds")


def _proposal_artifacts(
    services: Any,
    output: Path,
    phase: str,
    result: SchedulingResult,
    decision_latency: DecisionLatency,
) -> dict[str, str]:
    """Persist a before/after prediction without claiming realized improvement."""

    candidate = _require_candidate(result, phase)
    paths = services.export_proposal_reports(
        candidate.run_id,
        candidate.proposal_id,
        output / "proposals" / phase,
        expected_proposal_hash=candidate.proposal_hash,
        decision_latency=decision_latency,
    )
    return {
        f"{phase}_proposal_before": str(paths.before.relative_to(output)),
        f"{phase}_proposal_after": str(paths.after.relative_to(output)),
        f"{phase}_proposal_comparison": str(paths.comparison.relative_to(output)),
    }


def _approve(
    services: Any,
    run_id: str,
    phase: str,
    result: SchedulingResult,
    wall_clock: WallClock,
) -> tuple[Any, DecisionLatency]:
    candidate = _require_candidate(result, phase)
    if candidate.created_at_utc is None:
        raise BaselineFailure(
            "decision_start_missing",
            "candidate lacks the wall-clock start of its decision window",
            BaselineExitCode.SOFTWARE_ERROR,
            {"phase": phase, "proposal_id": candidate.proposal_id},
        )
    completed_at_utc = _wall_timestamp(wall_clock)
    approval = services.record_deterministic_policy_decision(
        run_id,
        candidate.proposal_id,
        candidate.proposal_hash,
        approval_id=f"approval-{phase}",
        policy_name="deterministic",
        decision=ApprovalDecision.APPROVED,
        reason=f"Explicit deterministic baseline policy approval for {phase}.",
        decided_at_utc=completed_at_utc,
    )
    base = services.get_checkpoint(run_id, state_version=candidate.base_state_version)
    latency = services.calculate_decision_latency(
        start_simulated_seconds=base.snapshot.simulated_time_seconds,
        end_simulated_seconds=base.snapshot.simulated_time_seconds,
        started_at_utc=candidate.created_at_utc,
        completed_at_utc=approval.decided_at_utc,
    )
    return approval, latency


def _apply(
    services: Any,
    run_id: str,
    phase: str,
    result: SchedulingResult,
    approval: Any,
    *,
    effective_time_seconds: int | None = None,
) -> Any:
    candidate = _require_candidate(result, phase)
    return services.apply_approved_plan(
        run_id,
        candidate.proposal_id,
        candidate.proposal_hash,
        approval.approval_id,
        f"apply-{phase}",
        effective_time_seconds=effective_time_seconds,
    )


def _generate(
    services: Any,
    run_id: str,
    phase: str,
    *,
    seed: int,
    order_deltas: tuple[OrderDelta, ...] = (),
    not_before_seconds: int | None = None,
    planning_policy: str = "original_objective",
    wall_clock: WallClock,
    workflow_id: str | None = None,
) -> SchedulingResult:
    workflow_id = workflow_id or f"workflow-{phase}"
    started_at_utc = _wall_timestamp(wall_clock)
    services.ensure_workflow(
        run_id,
        workflow_id,
        payload={
            "mode": "baseline",
            "phase": phase,
            "planning_policy": planning_policy,
        },
        updated_at_utc=started_at_utc,
    )
    return services.generate_proposal(
        run_id,
        workflow_id,
        f"proposal-{phase}",
        proposed_order_deltas=order_deltas,
        not_before_seconds=not_before_seconds,
        config=SolverConfig(random_seed=seed),
        created_at_utc=started_at_utc,
    )


def _scarce_capability_first_initial(
    services: Any,
    catalog: DomainCatalog,
    snapshot: OperationalSnapshot,
    *,
    seed: int,
    output: Path,
    wall_clock: WallClock,
    workflow_id: str | None = None,
) -> SchedulingResult:
    """Build a transparent fault-scenario bootstrap through the existing solver.

    Public catalog topology and public orders identify a differentiating single-source
    bottleneck without consulting evaluator interventions.  Orders needing that
    capability receive an early soft due date and maximum weight in a *planning priority
    view* only.  CP-SAT still generates every assignment.  Assignments are rebound to
    the untouched authoritative snapshot, its objective is independently recomputed,
    and the ordinary validator and approval/apply boundary remain mandatory.

    This is scenario setup, not a claim of optimality for the original objective.
    """

    capable_machines: dict[Capability, set[str]] = {}
    for machine in catalog.machines:
        for capability in machine.capabilities:
            capable_machines.setdefault(capability, set()).add(machine.machine_id)
    single_source_capabilities = {
        capability for capability, machine_ids in capable_machines.items() if len(machine_ids) == 1
    }
    products = {product.product_id: product for product in catalog.products}
    represented_product_ids = {order.product_id for order in snapshot.orders}
    product_capabilities = {
        product_id: {item.required_capability for item in products[product_id].operations}
        for product_id in represented_product_ids
    }
    capability_coverage: dict[Capability, tuple[int, int, int]] = {}
    for capability in single_source_capabilities:
        matching_orders = tuple(
            order
            for order in snapshot.orders
            if capability in product_capabilities[order.product_id]
        )
        product_count = sum(
            capability in product_capabilities[product_id] for product_id in represented_product_ids
        )
        workload_seconds = sum(
            operation.duration_seconds
            for order in matching_orders
            for operation in products[order.product_id].operations
            if operation.required_capability is capability
        )
        capability_coverage[capability] = (
            product_count,
            len(matching_orders),
            workload_seconds,
        )
    differentiating = {
        capability: values
        for capability, values in capability_coverage.items()
        if 0 < values[0] < len(represented_product_ids) and 0 < values[1] < len(snapshot.orders)
    }
    if not differentiating:
        raise BaselineFailure(
            "bootstrap_policy_not_applicable",
            "catalog has no differentiating single-source bottleneck capability",
            BaselineExitCode.WORKFLOW_FAILURE,
        )
    bottleneck_capability = min(
        differentiating,
        key=lambda capability: (
            differentiating[capability][0],
            -differentiating[capability][2],
            -differentiating[capability][1],
            capability.value,
        ),
    )
    priority_orders = tuple(
        order
        for order in snapshot.orders
        if bottleneck_capability in product_capabilities[order.product_id]
    )
    priority_order_ids = {order.order_id for order in priority_orders}
    bottleneck_operation_seconds = max(
        operation.duration_seconds
        for order in priority_orders
        for operation in products[order.product_id].operations
        if operation.required_capability is bottleneck_capability
    )
    early_due = (
        max(order.release_time_seconds for order in priority_orders) + bottleneck_operation_seconds
    )
    priority_view_orders = tuple(
        order.model_copy(
            update={
                "due_time_seconds": (
                    max(order.release_time_seconds, early_due)
                    if order.order_id in priority_order_ids
                    else max(20_000, order.release_time_seconds)
                ),
                "priority_weight": 100 if order.order_id in priority_order_ids else 1,
            }
        )
        for order in snapshot.orders
    )
    priority_snapshot = snapshot.model_copy(update={"orders": priority_view_orders})
    phase = "initial"
    workflow_id = workflow_id or f"workflow-{phase}"
    started_at_utc = _wall_timestamp(wall_clock)
    services.ensure_workflow(
        snapshot.run_id,
        workflow_id,
        payload={
            "mode": "baseline",
            "phase": phase,
            "planning_policy": "scarce_capability_first",
            "original_objective_optimal": False,
        },
        updated_at_utc=started_at_utc,
    )
    priority_result = solve_schedule(
        priority_snapshot,
        catalog,
        proposal_id="proposal-priority-view",
        workflow_id=workflow_id,
        config=SolverConfig(random_seed=seed),
    )
    priority_candidate = _require_candidate(priority_result, "initial_priority_view")
    original_validation = validate_schedule(
        snapshot,
        catalog,
        priority_candidate.assignments,
    )
    if not original_validation.is_valid:
        raise BaselineFailure(
            "bootstrap_rebind_invalid",
            "priority-view assignments are invalid for the authoritative snapshot",
            BaselineExitCode.WORKFLOW_FAILURE,
            {"violation_codes": [item.code.value for item in original_validation.violations]},
        )
    candidate = CandidateProposal.create(
        schema_version=1,
        proposal_id="proposal-initial",
        run_id=snapshot.run_id,
        workflow_id=workflow_id,
        base_state_version=snapshot.state_version,
        base_snapshot_hash=canonical_hash(snapshot),
        proposed_plan_revision=snapshot.plan_revision + 1,
        assignments=priority_candidate.assignments,
        objective=ScheduleObjective(
            weighted_total_tardiness_seconds=original_validation.total_tardiness_seconds,
            makespan_seconds=original_validation.makespan_seconds,
        ),
        created_at_utc=started_at_utc,
    )
    rebound_validation = validate_candidate(snapshot, catalog, candidate)
    if not rebound_validation.is_valid:
        raise BaselineFailure(
            "bootstrap_candidate_invalid",
            "rebuilt authoritative candidate failed independent validation",
            BaselineExitCode.WORKFLOW_FAILURE,
            {"violation_codes": [item.code.value for item in rebound_validation.violations]},
        )
    services.save_validated_proposal(candidate, created_at_utc=started_at_utc)
    policy_path = output / "planning_policy.json"
    _write_json(
        policy_path,
        {
            "schema_version": 1,
            "policy_id": "scarce_capability_first",
            "scope": "fault_scenario_harness_bootstrap",
            "solver": "existing_cp_sat_engine",
            "uses_evaluator_future_truth": False,
            "authoritative_orders_changed": False,
            "original_objective_optimal": False,
            "selection_rule": (
                "lowest represented-product coverage among non-ubiquitous "
                "single-source capabilities; greatest order workload breaks ties"
            ),
            "bottleneck_capability": bottleneck_capability.value,
            "bottleneck_machine_ids": sorted(capable_machines[bottleneck_capability]),
            "represented_product_coverage": differentiating[bottleneck_capability][0],
            "represented_order_coverage": differentiating[bottleneck_capability][1],
            "bottleneck_workload_seconds": differentiating[bottleneck_capability][2],
            "bottleneck_operation_seconds": bottleneck_operation_seconds,
            "priority_order_ids": sorted(priority_order_ids),
            "priority_view_due_seconds": early_due,
            "priority_view_weight": 100,
            "other_view_due_seconds": 20_000,
            "other_view_weight": 1,
            "priority_view_solver_outcome": priority_result.outcome.value,
            "reported_objective": candidate.objective.model_dump(mode="json"),
            "independently_valid": rebound_validation.is_valid,
        },
    )
    return SchedulingResult(
        outcome=priority_result.outcome,
        report=priority_result.report,
        candidate=candidate,
        validation=rebound_validation,
    )


def _finalize_baseline(
    *,
    output: Path,
    bundle: ScenarioBundle,
    services: Any,
    status: BaselineStatus,
    reason_code: str,
    artifacts: dict[str, str],
) -> BaselineRunResult:
    run_id = bundle.scenario.run_id
    events = services.list_events(run_id)
    final_checkpoint = services.get_checkpoint(run_id)
    metrics = services.calculate_run_metrics(run_id)
    metrics_path = output / "metrics.json"
    _write_json(metrics_path, metrics)
    events_path = output / "events.jsonl"
    services.export_replay_bundle(run_id, events_path)
    final_path = output / "final_snapshot.json"
    _write_json(final_path, final_checkpoint.snapshot)
    lifecycle_path = output / "lifecycle.json"
    _write_json(lifecycle_path, services.export_lifecycle(run_id))
    artifacts.update(
        {
            "database": "state.sqlite3",
            "events": events_path.name,
            "final_snapshot": final_path.name,
            "lifecycle": lifecycle_path.name,
            "metrics": metrics_path.name,
        }
    )
    snapshot = final_checkpoint.snapshot
    result = BaselineRunResult(
        scenario_id=bundle.scenario.scenario_id,
        run_id=run_id,
        status=status,
        reason_code=reason_code,
        output_directory=output,
        state_version=snapshot.state_version,
        plan_revision=snapshot.plan_revision,
        simulated_time_seconds=snapshot.simulated_time_seconds,
        snapshot_hash=final_checkpoint.snapshot_hash,
        event_count=len(events),
        artifacts=artifacts,
    )
    summary_path = output / "summary.json"
    artifacts["summary"] = summary_path.name
    _write_json(summary_path, result)
    return result


def run_baseline(
    scenario_id: str,
    *,
    seed: int,
    approval_policy: str,
    output: Path,
    wall_clock: WallClock | None = None,
) -> BaselineRunResult:
    """Run one hash-verified fixture with no provider or network dependency."""

    if approval_policy != "deterministic":
        raise BaselineFailure(
            "explicit_approval_policy_required",
            "baseline mode requires --approval-policy deterministic",
            BaselineExitCode.USAGE,
        )
    if isinstance(seed, bool) or not isinstance(seed, int) or not 0 <= seed <= 2**31 - 1:
        raise BaselineFailure(
            "invalid_seed",
            "seed must be an integer between 0 and 2147483647",
            BaselineExitCode.INPUT_ERROR,
        )
    try:
        bundle = load_scenario(scenario_id)
    except (OSError, ValueError) as exc:
        raise BaselineFailure(
            "invalid_scenario",
            str(exc),
            BaselineExitCode.INPUT_ERROR,
            {"scenario_id": scenario_id},
        ) from exc

    target = _prepare_output(output)
    clock = wall_clock or _utc_now
    database_path = target / "state.sqlite3"
    repository = SQLiteStateRepository(database_path)
    repository.initialize()
    initial = materialize_initial_snapshot(bundle)

    from shopfloor_pilot.services import ApplicationServices

    services = ApplicationServices(repository, bundle.catalog, seed=seed)
    services.create_run(initial)
    artifacts: dict[str, str] = {}

    if scenario_id in {"machine_breakdown", "no_feasible_recovery"}:
        initial_result = _scarce_capability_first_initial(
            services,
            bundle.catalog,
            initial,
            seed=seed,
            output=target,
            wall_clock=clock,
        )
        artifacts["planning_policy"] = "planning_policy.json"
    elif scenario_id == "urgent_order":
        # A positive time-origin floor leaves the first instant available for later
        # external event staging while retaining the original objective and solver.
        initial_result = _generate(
            services,
            initial.run_id,
            "initial",
            seed=seed,
            not_before_seconds=1,
            planning_policy="positive_time_origin_floor",
            wall_clock=clock,
        )
        _write_json(
            target / "planning_policy.json",
            {
                "schema_version": 1,
                "policy_id": "positive_time_origin_floor",
                "scope": "deterministic_scenario_harness_bootstrap",
                "solver": "existing_cp_sat_engine",
                "uses_evaluator_future_truth": False,
                "not_before_seconds": 1,
                "reason": (
                    "Use a positive time-origin floor so later external changes can be "
                    "staged at a still-open event boundary."
                ),
            },
        )
        artifacts["planning_policy"] = "planning_policy.json"
    else:
        initial_result = _generate(
            services,
            initial.run_id,
            "initial",
            seed=seed,
            wall_clock=clock,
        )
    initial_candidate = _require_candidate(initial_result, "initial")
    initial_approval, initial_latency = _approve(
        services,
        initial.run_id,
        "initial",
        initial_result,
        clock,
    )
    artifacts.update(
        _proposal_artifacts(
            services,
            target,
            "initial",
            initial_result,
            initial_latency,
        )
    )
    _apply(services, initial.run_id, "initial", initial_result, initial_approval)

    if scenario_id == "normal":
        checkpoint_time = max(item.at_seconds for item in bundle.evaluator.interventions)
        target_time = max(initial_candidate.objective.makespan_seconds, checkpoint_time)
        services.advance_simulation(
            initial.run_id,
            target_time,
            "advance-normal-final",
            interventions=bundle.evaluator.interventions,
            observations=bundle.evaluator.observation_templates,
        )
    elif scenario_id == "urgent_order":
        intervention = next(
            item
            for item in bundle.evaluator.interventions
            if item.kind is InterventionKind.ADD_ORDER
        )
        assert intervention.order_to_add is not None
        # Keep the order a draft.  Staging the atomic registration + plan at T
        # requires pausing at T-1 because TIME_ADVANCED closes boundary T.
        services.advance_simulation(
            initial.run_id,
            intervention.at_seconds - 1,
            "advance-urgent-before-order",
        )
        urgent_order_id = intervention.order_to_add.order_id
        if any(
            item.order_id == urgent_order_id
            for item in services.get_snapshot(initial.run_id).orders
        ):
            raise BaselineFailure(
                "draft_order_mutated_state",
                "urgent order became operational before approved application",
                BaselineExitCode.WORKFLOW_FAILURE,
            )
        urgent_result = _generate(
            services,
            initial.run_id,
            "urgent",
            seed=seed,
            order_deltas=(OrderDelta(kind=OrderDeltaKind.ADD, order=intervention.order_to_add),),
            not_before_seconds=intervention.at_seconds,
            wall_clock=clock,
        )
        urgent_candidate = _require_candidate(urgent_result, "urgent")
        if any(
            item.order_id == urgent_order_id
            for item in services.get_snapshot(initial.run_id).orders
        ):
            raise BaselineFailure(
                "draft_order_mutated_state",
                "proposal generation inserted the urgent draft order",
                BaselineExitCode.WORKFLOW_FAILURE,
            )
        urgent_approval, urgent_latency = _approve(
            services,
            initial.run_id,
            "urgent",
            urgent_result,
            clock,
        )
        artifacts.update(
            _proposal_artifacts(
                services,
                target,
                "urgent",
                urgent_result,
                urgent_latency,
            )
        )
        _apply(
            services,
            initial.run_id,
            "urgent",
            urgent_result,
            urgent_approval,
            effective_time_seconds=intervention.at_seconds,
        )
        services.advance_simulation(
            initial.run_id,
            urgent_candidate.objective.makespan_seconds,
            "advance-urgent-final",
            observations=bundle.evaluator.observation_templates,
        )
    elif scenario_id in {"machine_breakdown", "no_feasible_recovery"}:
        disruption_time = bundle.evaluator.interventions[0].at_seconds
        services.advance_simulation(
            initial.run_id,
            disruption_time,
            f"advance-{scenario_id}-disruption",
            interventions=bundle.evaluator.interventions,
            observations=bundle.evaluator.observation_templates,
        )
        recovery_result = _generate(
            services,
            initial.run_id,
            "recovery",
            seed=seed,
            not_before_seconds=disruption_time + 1,
            wall_clock=clock,
        )
        if recovery_result.outcome is SolveOutcome.INFEASIBLE:
            if bundle.expectation.expected_outcome is not ScenarioOutcome.NO_FEASIBLE_RECOVERY:
                raise BaselineFailure(
                    "unexpected_no_feasible_recovery",
                    "the recoverable fixture produced no feasible recovery",
                    BaselineExitCode.WORKFLOW_FAILURE,
                    {"scenario_id": scenario_id, "at_seconds": disruption_time},
                )
            services.mark_workflow_blocked(
                initial.run_id,
                "workflow-recovery",
                reason_code="no_feasible_recovery",
                details={
                    "scenario_id": scenario_id,
                    "solver_outcome": recovery_result.outcome.value,
                    "diagnostics": list(recovery_result.report.diagnostics),
                },
                updated_at_utc=_wall_timestamp(clock),
            )
            return _finalize_baseline(
                output=target,
                bundle=bundle,
                services=services,
                status=BaselineStatus.BLOCKED,
                reason_code="no_feasible_recovery",
                artifacts=artifacts,
            )
        recovery_candidate = _require_candidate(recovery_result, "recovery")
        recovery_approval, recovery_latency = _approve(
            services,
            initial.run_id,
            "recovery",
            recovery_result,
            clock,
        )
        artifacts.update(
            _proposal_artifacts(
                services,
                target,
                "recovery",
                recovery_result,
                recovery_latency,
            )
        )
        _apply(
            services,
            initial.run_id,
            "recovery",
            recovery_result,
            recovery_approval,
            effective_time_seconds=disruption_time + 1,
        )
        services.advance_simulation(
            initial.run_id,
            recovery_candidate.objective.makespan_seconds,
            "advance-recovery-final",
        )
    else:  # load_scenario currently makes this unreachable; keep failure structured.
        raise BaselineFailure(
            "unsupported_scenario",
            f"baseline orchestration is not defined for {scenario_id!r}",
            BaselineExitCode.INPUT_ERROR,
        )

    final = services.get_snapshot(initial.run_id)
    incomplete = sorted(
        item.order_id for item in final.orders if item.status is not OrderStatus.COMPLETED
    )
    if incomplete:
        raise BaselineFailure(
            "incomplete_execution",
            "scenario stopped with unfinished orders",
            BaselineExitCode.WORKFLOW_FAILURE,
            {"order_ids": incomplete},
        )
    return _finalize_baseline(
        output=target,
        bundle=bundle,
        services=services,
        status=BaselineStatus.COMPLETED,
        reason_code="scenario_completed",
        artifacts=artifacts,
    )


def run_replay(events_path: Path, *, output: Path) -> ReplayRunResult:
    """Replay a portable JSONL bundle without simulation, solving, or randomness."""

    from shopfloor_pilot.reporting import ReportingError
    from shopfloor_pilot.services import ApplicationServices

    source = events_path.expanduser().resolve()
    if not source.is_file():
        raise BaselineFailure(
            "events_not_found",
            "portable event bundle was not found",
            BaselineExitCode.INPUT_ERROR,
            {"events": str(source)},
        )
    target = _prepare_output(output)
    try:
        bundle = ApplicationServices.load_replay_bundle(source)
        replayed = ApplicationServices.replay_bundle(bundle)
    except (OSError, ReportingError, ValueError) as exc:
        raise BaselineFailure(
            "replay_rejected",
            str(exc),
            BaselineExitCode.WORKFLOW_FAILURE,
            {"events": str(source)},
        ) from exc

    final_checkpoint = replayed.checkpoint
    metrics = replayed.actual_metrics
    copied_events = target / "events.jsonl"
    copied_events.write_bytes(source.read_bytes())
    final_path = target / "final_snapshot.json"
    metrics_path = target / "metrics.json"
    _write_json(final_path, final_checkpoint.snapshot)
    _write_json(metrics_path, metrics)
    artifacts = {
        "events": copied_events.name,
        "final_snapshot": final_path.name,
        "metrics": metrics_path.name,
    }
    result = ReplayRunResult(
        run_id=final_checkpoint.snapshot.run_id,
        output_directory=target,
        state_version=final_checkpoint.snapshot.state_version,
        simulated_time_seconds=final_checkpoint.snapshot.simulated_time_seconds,
        snapshot_hash=final_checkpoint.snapshot_hash,
        event_count=len(bundle.events),
        artifacts=artifacts,
    )
    summary_path = target / "summary.json"
    artifacts["summary"] = summary_path.name
    _write_json(summary_path, result)
    return result


def failure_payload(error: BaseException) -> dict[str, Any]:
    """Return a safe structured CLI payload for an expected or unexpected error."""

    if isinstance(error, BaselineFailure):
        return error.as_dict()
    return {
        "schema_version": 1,
        "status": BaselineStatus.FAILED.value,
        "reason_code": "internal_error",
        "message": str(error),
        "exit_code": int(BaselineExitCode.SOFTWARE_ERROR),
        "details": {},
    }


__all__ = [
    "BaselineExitCode",
    "BaselineFailure",
    "BaselineRunResult",
    "BaselineStatus",
    "ReplayRunResult",
    "failure_payload",
    "run_baseline",
    "run_replay",
]
