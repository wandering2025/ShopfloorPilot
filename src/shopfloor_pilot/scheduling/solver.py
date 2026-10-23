"""CPU-only CP-SAT scheduling over explicit snapshots and declared deltas."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from time import perf_counter
from typing import Protocol

import ortools
from ortools.sat.python import cp_model
from pydantic import ValidationError

from shopfloor_pilot.domain.canonical import canonical_hash
from shopfloor_pilot.domain.models import (
    AttemptStatus,
    CandidateProposal,
    DomainCatalog,
    MachineStatus,
    Operation,
    OperationalSnapshot,
    OperationStatus,
    Order,
    OrderDelta,
    OrderStatus,
    ResourceDelta,
    ScheduleAssignment,
    ScheduleObjective,
    TimeWindow,
)
from shopfloor_pilot.planning import expand_process_route
from shopfloor_pilot.validation import (
    ScheduleViolation,
    ValidationReport,
    ViolationCode,
    validate_candidate,
    validate_schedule,
)

from .models import (
    AdapterResult,
    NativeStatus,
    SchedulingResult,
    SolveOutcome,
    SolverConfig,
    SolverReport,
)

# Conservative limits keep arithmetic far below int64 and objective floats exact.
MAX_HORIZON_SECONDS = 10**9
MAX_SCALAR_OBJECTIVE = 2**50


@dataclass(frozen=True)
class _Task:
    operation: Operation
    start: cp_model.IntVar
    end: cp_model.IntVar
    alternatives: tuple[tuple[str, cp_model.IntVar], ...]
    attempt_id: str


@dataclass(frozen=True)
class _Problem:
    model: cp_model.CpModel
    tasks: tuple[_Task, ...]
    tardiness: cp_model.IntVar
    makespan: cp_model.IntVar
    multiplier: int
    upper_bound: int


class SolverAdapter(Protocol):
    def solve(self, problem: _Problem, config: SolverConfig) -> AdapterResult: ...


class CpSatAdapter:
    def solve(self, problem: _Problem, config: SolverConfig) -> AdapterResult:
        solver = cp_model.CpSolver()
        for name, value in config.model_dump().items():
            target = "max_time_in_seconds" if name == "max_time_seconds" else name
            setattr(solver.parameters, target, value)
        status = NativeStatus(int(solver.solve(problem.model)))
        common = {
            "status": status,
            "wall_time_seconds": solver.wall_time,
            "response_stats": solver.response_stats(),
        }
        if status not in (NativeStatus.OPTIMAL, NativeStatus.FEASIBLE):
            # Never read variable values without a feasible incumbent.
            return AdapterResult(
                **common,
                best_objective_bound=(
                    solver.best_objective_bound if status is NativeStatus.UNKNOWN else None
                ),
            )
        assignments = tuple(
            ScheduleAssignment(
                operation_id=task.operation.operation_id,
                attempt_id=task.attempt_id,
                machine_id=next(
                    machine for machine, present in task.alternatives if solver.value(present)
                ),
                window=TimeWindow(
                    start_seconds=solver.value(task.start),
                    end_seconds=solver.value(task.end),
                ),
            )
            for task in problem.tasks
        )
        return AdapterResult(
            **common,
            assignments=assignments,
            weighted_tardiness_seconds=solver.value(problem.tardiness),
            makespan_seconds=solver.value(problem.makespan),
            objective_value=solver.objective_value,
            best_objective_bound=solver.best_objective_bound,
        )


def _attempt_id(snapshot: OperationalSnapshot, operation: Operation) -> str:
    history = [a for a in snapshot.attempts if a.operation_id == operation.operation_id]
    scheduled = [a for a in history if a.status is AttemptStatus.SCHEDULED]
    if scheduled:
        return scheduled[0].attempt_id
    generation = max((a.attempt_number for a in history), default=0) + 1
    return (
        "attempt-"
        + canonical_hash(("s04-attempt-v1", snapshot.run_id, operation.operation_id, generation))[
            :48
        ]
    )


def _build_problem(
    snapshot: OperationalSnapshot,
    catalog: DomainCatalog,
    order_deltas: tuple[OrderDelta, ...],
    resource_deltas: tuple[ResourceDelta, ...],
    not_before_seconds: int | None,
) -> _Problem:
    orders = tuple(
        sorted(
            (
                o
                for o in (*snapshot.orders, *(d.order for d in order_deltas))
                if o.status is not OrderStatus.REJECTED
            ),
            key=lambda o: o.order_id,
        )
    )
    operations = tuple(
        op for order in orders for op in expand_process_route(order, catalog).operations
    )
    horizon = snapshot.hard_horizon_seconds
    multiplier = horizon + 1
    tardiness_bound = sum(
        order.priority_weight * max(0, horizon - order.due_time_seconds) for order in orders
    )
    objective_bound = multiplier * tardiness_bound + horizon
    if horizon > MAX_HORIZON_SECONDS or objective_bound > MAX_SCALAR_OBJECTIVE:
        raise ValueError("numeric_bounds_exceeded: horizon or scalar objective exceeds safe limits")

    model = cp_model.CpModel()
    machine_intervals: dict[str, list[cp_model.IntervalVar]] = {
        m.machine_id: [] for m in snapshot.machines
    }
    starts: dict[str, cp_model.IntVar | int] = {}
    ends: dict[str, cp_model.IntVar | int] = {}
    existing = {op.operation_id: op for op in snapshot.operations}
    catalog_machines = {m.machine_id: m for m in catalog.machines}
    order_by_id: dict[str, Order] = {o.order_id: o for o in orders}
    unavailable = {d.machine_id for d in resource_deltas}

    for attempt in sorted(snapshot.attempts, key=lambda a: a.attempt_id):
        if attempt.status is AttemptStatus.SCHEDULED or attempt.actual_start_seconds is None:
            continue
        start = attempt.actual_start_seconds
        end = (
            start + attempt.planned_duration_seconds
            if attempt.status is AttemptStatus.RUNNING
            else attempt.actual_end_seconds
        )
        if end is not None:
            machine_intervals[attempt.machine_id].append(
                model.new_fixed_size_interval_var(start, end - start, attempt.attempt_id)
            )
        if attempt.status in (AttemptStatus.COMPLETED, AttemptStatus.RUNNING):
            starts[attempt.operation_id] = start
            ends[attempt.operation_id] = end

    tasks: list[_Task] = []
    for operation in operations:
        previous = existing.get(operation.operation_id)
        if previous and previous.status in (OperationStatus.COMPLETED, OperationStatus.RUNNING):
            continue
        name = operation.operation_id
        start = model.new_int_var(0, horizon, f"start:{name}")
        end = model.new_int_var(0, horizon, f"end:{name}")
        starts[name], ends[name] = start, end
        duration = operation.duration_seconds
        earliest = max(
            snapshot.simulated_time_seconds,
            not_before_seconds
            if not_before_seconds is not None
            else snapshot.simulated_time_seconds,
            order_by_id[operation.order_id].release_time_seconds,
        )
        alternatives: list[tuple[str, cp_model.IntVar]] = []
        for machine in sorted(snapshot.machines, key=lambda m: m.machine_id):
            catalog_machine = catalog_machines.get(machine.machine_id)
            if (
                machine.machine_id in unavailable
                or machine.status in (MachineStatus.DOWN, MachineStatus.MAINTENANCE)
                or operation.required_capability not in machine.capabilities
                or catalog_machine is None
                or operation.required_capability not in catalog_machine.capabilities
            ):
                continue
            for index, window in enumerate(machine.availability_windows):
                lower = max(earliest, window.start_seconds)
                upper = min(horizon, window.end_seconds)
                if upper - lower < duration:
                    continue
                label = f"{name}:{machine.machine_id}:{index}"
                present = model.new_bool_var(f"selected:{label}")
                interval = model.new_optional_interval_var(start, duration, end, present, label)
                model.add(start >= lower).only_enforce_if(present)
                model.add(end <= upper).only_enforce_if(present)
                machine_intervals[machine.machine_id].append(interval)
                alternatives.append((machine.machine_id, present))
        # An empty set is a hard contradiction, proved by CP-SAT rather than guessed.
        model.add_exactly_one([present for _, present in alternatives])
        tasks.append(
            _Task(operation, start, end, tuple(alternatives), _attempt_id(snapshot, operation))
        )

    for operation in operations:
        for predecessor in operation.predecessor_operation_ids:
            model.add(starts[operation.operation_id] >= ends[predecessor])
    for intervals in machine_intervals.values():
        model.add_no_overlap(intervals)

    weighted_terms = []
    for order in orders:
        final_operations = [op for op in operations if op.order_id == order.order_id]
        last_sequence = max(op.sequence_index for op in final_operations)
        completion = model.new_int_var(0, horizon, f"completion:{order.order_id}")
        model.add_max_equality(
            completion,
            [
                ends[op.operation_id]
                for op in final_operations
                if op.sequence_index == last_sequence
            ],
        )
        tardiness = model.new_int_var(
            0, max(0, horizon - order.due_time_seconds), f"tardiness:{order.order_id}"
        )
        if order.due_time_seconds >= horizon:
            model.add(tardiness == 0)
        else:
            model.add_max_equality(tardiness, [0, completion - order.due_time_seconds])
        weighted_terms.append(order.priority_weight * tardiness)
    total = model.new_int_var(0, tardiness_bound, "weighted_tardiness")
    model.add(total == sum(weighted_terms))
    makespan = model.new_int_var(0, horizon, "makespan")
    model.add_max_equality(makespan, list(ends.values()) or [snapshot.simulated_time_seconds])
    model.minimize(multiplier * total + makespan)
    return _Problem(model, tuple(tasks), total, makespan, multiplier, objective_bound)


def solve_schedule(
    snapshot: OperationalSnapshot,
    catalog: DomainCatalog,
    *,
    proposal_id: str,
    workflow_id: str,
    proposed_order_deltas: Sequence[OrderDelta] = (),
    proposed_resource_deltas: Sequence[ResourceDelta] = (),
    not_before_seconds: int | None = None,
    config: SolverConfig | None = None,
    _adapter: SolverAdapter | None = None,
) -> SchedulingResult:
    """Plan initial/remaining work without mutation, approval, or hidden reads.

    Identity is supplied by the caller. ``_adapter`` is a private testing seam;
    production callers use the default CP-SAT adapter.
    """
    began = perf_counter()
    settings = config or SolverConfig()
    report = SolverReport(
        config=settings,
        solver_version=ortools.__version__,
        not_before_seconds=(
            not_before_seconds
            if type(not_before_seconds) is int and not_before_seconds >= 0
            else None
        ),
    )

    def finish(
        outcome: SolveOutcome,
        *,
        candidate: CandidateProposal | None = None,
        validation: ValidationReport | None = None,
        diagnostics: tuple[str, ...] = (),
    ) -> SchedulingResult:
        return SchedulingResult(
            outcome=outcome,
            candidate=candidate,
            validation=validation,
            report=report.model_copy(
                update={
                    "total_elapsed_seconds": perf_counter() - began,
                    "diagnostics": diagnostics,
                }
            ),
        )

    if type(not_before_seconds) is not int and not_before_seconds is not None:
        return finish(
            SolveOutcome.INVALID_INPUT,
            diagnostics=("not_before_seconds must be a strict non-negative integer or None.",),
        )
    if not_before_seconds is not None and not (
        snapshot.simulated_time_seconds <= not_before_seconds <= snapshot.hard_horizon_seconds
    ):
        return finish(
            SolveOutcome.INVALID_INPUT,
            diagnostics=(
                "not_before_seconds must be within the inclusive snapshot-to-horizon range.",
            ),
        )

    order_deltas, resource_deltas = tuple(proposed_order_deltas), tuple(proposed_resource_deltas)
    # Public S03 API only: missing future coverage is expected before solving.
    # All other findings describe malformed input/history/deltas, not infeasibility.
    preflight = validate_schedule(
        snapshot,
        catalog,
        (),
        projected_order_deltas=order_deltas,
        projected_resource_deltas=resource_deltas,
    )
    input_violations = tuple(
        v for v in preflight.violations if v.code is not ViolationCode.MISSING_OPERATION
    )
    if input_violations:
        return finish(
            SolveOutcome.INVALID_INPUT,
            validation=ValidationReport(is_valid=False, violations=input_violations),
            diagnostics=("Snapshot/history/deltas failed independent input validation.",),
        )
    try:
        # Validate caller identity before spending the solve budget.
        CandidateProposal.create(
            schema_version=1,
            proposal_id=proposal_id,
            run_id=snapshot.run_id,
            workflow_id=workflow_id,
            base_state_version=snapshot.state_version,
            base_snapshot_hash=canonical_hash(snapshot),
            proposed_plan_revision=snapshot.plan_revision + 1,
            assignments=(),
            objective=ScheduleObjective(weighted_total_tardiness_seconds=0, makespan_seconds=0),
        )
        problem = _build_problem(
            snapshot,
            catalog,
            order_deltas,
            resource_deltas,
            not_before_seconds,
        )
    except ValueError as exc:
        return finish(SolveOutcome.INVALID_INPUT, diagnostics=(str(exc),))
    report = report.model_copy(
        update={
            "objective_multiplier": problem.multiplier,
            "objective_upper_bound": problem.upper_bound,
        }
    )
    model_error = problem.model.validate()
    if model_error:
        return finish(SolveOutcome.MODEL_INVALID, diagnostics=(model_error,))
    result = (_adapter or CpSatAdapter()).solve(problem, settings)
    report = report.model_copy(
        update={
            "native_status": result.status,
            "native_status_name": result.status.name,
            "objective_value": result.objective_value,
            "best_objective_bound": result.best_objective_bound,
            "solver_wall_time_seconds": result.wall_time_seconds,
            "response_stats": result.response_stats,
        }
    )
    if result.status not in (NativeStatus.OPTIMAL, NativeStatus.FEASIBLE):
        return finish(
            {
                NativeStatus.INFEASIBLE: SolveOutcome.INFEASIBLE,
                NativeStatus.UNKNOWN: SolveOutcome.UNKNOWN,
                NativeStatus.MODEL_INVALID: SolveOutcome.MODEL_INVALID,
            }[result.status]
        )
    try:
        candidate = CandidateProposal.create(
            schema_version=1,
            proposal_id=proposal_id,
            run_id=snapshot.run_id,
            workflow_id=workflow_id,
            base_state_version=snapshot.state_version,
            base_snapshot_hash=canonical_hash(snapshot),
            proposed_plan_revision=snapshot.plan_revision + 1,
            assignments=result.assignments,
            objective=ScheduleObjective(
                weighted_total_tardiness_seconds=result.weighted_tardiness_seconds,
                makespan_seconds=result.makespan_seconds,
            ),
            proposed_order_deltas=order_deltas,
            proposed_resource_deltas=resource_deltas,
        )
    except ValidationError:
        return finish(
            SolveOutcome.VALIDATION_FAILED,
            diagnostics=("Solver incumbent could not form a typed candidate.",),
        )
    validation = validate_candidate(
        snapshot,
        catalog,
        candidate,
        projected_order_deltas=order_deltas,
        projected_resource_deltas=resource_deltas,
    )
    if not validation.is_valid:
        return finish(SolveOutcome.VALIDATION_FAILED, validation=validation)
    expected_scalar = (
        problem.multiplier * validation.total_tardiness_seconds + validation.makespan_seconds
    )
    if result.objective_value != expected_scalar:
        return finish(
            SolveOutcome.VALIDATION_FAILED,
            validation=ValidationReport(
                is_valid=False,
                violations=(
                    ScheduleViolation(
                        code=ViolationCode.OBJECTIVE_MISMATCH,
                        message="Native scalar objective disagrees with independent metrics.",
                        field="solver_report.objective_value",
                    ),
                ),
                total_tardiness_seconds=validation.total_tardiness_seconds,
                makespan_seconds=validation.makespan_seconds,
            ),
            diagnostics=("Solver scalar objective differs from independently checked metrics.",),
        )
    return finish(
        SolveOutcome.OPTIMAL if result.status is NativeStatus.OPTIMAL else SolveOutcome.FEASIBLE,
        candidate=candidate,
        validation=validation,
    )
