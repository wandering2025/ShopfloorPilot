"""S04 known answers, calendars, dynamic recovery, status handling and purity."""

from dataclasses import replace

import pytest
from cases import Case, copy, priority_case, scenario_case, tiny_case

from shopfloor_pilot.domain.canonical import canonical_hash
from shopfloor_pilot.domain.models import (
    AttemptStatus,
    MachineStatus,
    ResourceDelta,
    ResourceDeltaKind,
    TimeWindow,
)
from shopfloor_pilot.scheduling import NativeStatus, SolveOutcome, SolverConfig, solve_schedule
from shopfloor_pilot.scheduling.models import AdapterResult
from shopfloor_pilot.scheduling.solver import CpSatAdapter
from shopfloor_pilot.validation import ViolationCode, validate_candidate


def solve(case: Case, **kwargs):
    before = canonical_hash(case.snapshot)
    result = solve_schedule(
        case.snapshot,
        case.catalog,
        proposal_id="proposal-test",
        workflow_id="workflow-test",
        proposed_order_deltas=case.order_deltas,
        **kwargs,
    )
    assert canonical_hash(case.snapshot) == before
    if result.candidate:
        assert result.validation.is_valid
        assert validate_candidate(
            case.snapshot,
            case.catalog,
            result.candidate,
            projected_order_deltas=case.order_deltas,
            projected_resource_deltas=kwargs.get("proposed_resource_deltas", ()),
        ).is_valid
    return result


def test_a01_tiny_known_optimum():
    # Two units: mill [0,3), [3,6); QC [3,5), [6,8).
    # Last unit cannot finish before 2*3+2=8. T=2*(8-6)=4.
    result = solve(tiny_case())
    assert result.outcome is SolveOutcome.OPTIMAL
    assert result.report.native_status is NativeStatus.OPTIMAL
    assert result.candidate.objective.weighted_total_tardiness_seconds == 4
    assert result.candidate.objective.makespan_seconds == 8
    assert result.report.objective_value == result.report.best_objective_bound == 92


@pytest.mark.parametrize(
    "name", ("normal", "urgent_order", "machine_breakdown", "no_feasible_recovery")
)
def test_a02_s01_intended_boundaries(name):
    case = scenario_case(name)
    result = solve(case)
    if name == "no_feasible_recovery":
        assert result.outcome is SolveOutcome.INFEASIBLE
        assert result.report.native_status is NativeStatus.INFEASIBLE
        assert result.candidate is None
        assert result.report.objective_value is None
    else:
        assert result.is_usable
        if name == "urgent_order":
            assert result.candidate.proposed_order_deltas == case.order_deltas
        assert all(
            a.window.start_seconds >= case.snapshot.simulated_time_seconds
            for a in result.candidate.assignments
        )


def test_a03_alternative_calendars_and_qc_contention():
    case = tiny_case(quantity=3, due=20, horizon=30)
    mill, qc = case.catalog.machines
    alternative = copy(
        mill,
        machine_id="alternate",
        availability_windows=(
            TimeWindow(start_seconds=2, end_seconds=5),
            TimeWindow(start_seconds=10, end_seconds=30),
        ),
    )
    qc = copy(
        qc,
        availability_windows=(
            TimeWindow(start_seconds=0, end_seconds=5),
            TimeWindow(start_seconds=12, end_seconds=30),
        ),
    )
    catalog = copy(case.catalog, machines=(mill, alternative, qc))
    case = replace(
        case,
        catalog=catalog,
        snapshot=copy(
            case.snapshot,
            machines=(mill, alternative, qc),
            orders=(copy(case.snapshot.orders[0], release_time_seconds=2),),
        ),
    )
    delta = ResourceDelta(
        kind=ResourceDeltaKind.MARK_DOWN,
        machine_id="mill",
        effective_time_seconds=0,
        new_status=MachineStatus.DOWN,
    )
    result = solve(case, proposed_resource_deltas=(delta,))
    assert result.is_usable
    assert result.candidate.proposed_resource_deltas == (delta,)
    assert not any(a.machine_id == "mill" for a in result.candidate.assignments)
    qc_assignments = [a for a in result.candidate.assignments if a.machine_id == "qc"]
    assert len(qc_assignments) == 3
    assert all(a.window.start_seconds >= 12 for a in qc_assignments)
    assert all(
        not left.window.overlaps(right.window)
        for i, left in enumerate(qc_assignments)
        for right in qc_assignments[i + 1 :]
    )


def test_a04_completed_running_interrupted_preserved():
    case = scenario_case("machine_breakdown")
    result = solve(case)
    assert result.is_usable
    projected = {a.attempt_id: a for a in result.validation.projected_attempts}
    assert all(projected[a.attempt_id] == a for a in case.snapshot.attempts)
    frozen = {
        a.operation_id
        for a in case.snapshot.attempts
        if a.status in (AttemptStatus.COMPLETED, AttemptStatus.RUNNING)
    }
    assert not frozen.intersection(a.operation_id for a in result.candidate.assignments)
    interrupted = next(a for a in case.snapshot.attempts if a.status is AttemptStatus.INTERRUPTED)
    restart = next(a for a in result.validation.projected_attempts if a.restart_of_attempt_id)
    assert restart.operation_id == interrupted.operation_id
    assert restart.attempt_number == 2
    assert restart.restart_of_attempt_id == interrupted.attempt_id
    assert restart.machine_id == "CNC03"
    assert restart.planned_duration_seconds == restart.scheduled_window.duration_seconds == 900
    assert restart.scheduled_window.start_seconds >= 2700
    assert interrupted.consumed_seconds == 180
    assert all(
        a.window.start_seconds >= 2780
        for a in result.candidate.assignments
        if a.machine_id == "QC01"
    )
    # An applied but unstarted restart retains its identity and generation on replan.
    applied = replace(
        case,
        snapshot=copy(
            case.snapshot,
            operations=result.validation.projected_operations,
            attempts=result.validation.projected_attempts,
            state_version=8,
            plan_revision=3,
        ),
    )
    replanned = solve(applied)
    assert replanned.is_usable
    new_restart = next(
        a
        for a in replanned.validation.projected_attempts
        if a.status is AttemptStatus.SCHEDULED and a.operation_id == restart.operation_id
    )
    assert new_restart.attempt_id == restart.attempt_id
    assert new_restart.attempt_number == 2


def test_a05_due_dates_soft_horizon_hard():
    tardy = solve(tiny_case(due=0))
    assert tardy.is_usable
    assert tardy.candidate.objective.weighted_total_tardiness_seconds == 16
    impossible = solve(tiny_case(due=0, horizon=7))
    assert impossible.outcome is SolveOutcome.INFEASIBLE
    assert not impossible.is_usable


class ControlledAdapter:
    def __init__(self, status, *, corrupt=False, scalar_error=False):
        self.status, self.corrupt, self.scalar_error = status, corrupt, scalar_error

    def solve(self, problem, config):
        if self.status not in (NativeStatus.FEASIBLE, NativeStatus.OPTIMAL):
            return AdapterResult(status=self.status, best_objective_bound=0.0)
        result = CpSatAdapter().solve(problem, config)
        assert result.status is NativeStatus.OPTIMAL
        changes = {"status": self.status}
        if self.corrupt:
            assignment = result.assignments[0]
            changes["assignments"] = (
                copy(assignment, machine_id="qc"),
                *result.assignments[1:],
            )
        if self.scalar_error:
            changes["objective_value"] = result.objective_value + 1
        return copy(result, **changes)


def test_a06_controlled_feasible_incumbent_timeout():
    result = solve(tiny_case(), _adapter=ControlledAdapter(NativeStatus.FEASIBLE))
    assert result.outcome is SolveOutcome.FEASIBLE
    assert result.report.native_status_name == "FEASIBLE"
    assert result.is_usable


@pytest.mark.parametrize(
    "status,outcome",
    (
        (NativeStatus.UNKNOWN, SolveOutcome.UNKNOWN),
        (NativeStatus.MODEL_INVALID, SolveOutcome.MODEL_INVALID),
        (NativeStatus.INFEASIBLE, SolveOutcome.INFEASIBLE),
    ),
)
def test_a06_controlled_no_incumbent_statuses(status, outcome):
    result = solve(tiny_case(), _adapter=ControlledAdapter(status))
    assert result.outcome is outcome
    assert result.candidate is None
    assert not result.is_usable
    assert result.report.native_status is status
    assert result.report.objective_value is None


@pytest.mark.parametrize("case_factory", (tiny_case, lambda: scenario_case("normal")))
def test_a07_a10_five_reproducible_bounded_runs(case_factory):
    case = case_factory()
    results = [solve(case) for _ in range(5)]
    assert all(result.is_usable for result in results)
    assert len({canonical_hash(r.candidate) for r in results}) == 1
    assert all(r.report.total_elapsed_seconds <= 7.0 for r in results)


def test_a08_corrupted_adapter_output_cannot_escape():
    result = solve(tiny_case(), _adapter=ControlledAdapter(NativeStatus.OPTIMAL, corrupt=True))
    assert result.outcome is SolveOutcome.VALIDATION_FAILED
    assert result.candidate is None
    assert not result.validation.is_valid
    assert ViolationCode.CAPABILITY_MISMATCH in {v.code for v in result.validation.violations}


def test_a08_scalar_report_must_agree_with_independent_metrics():
    result = solve(tiny_case(), _adapter=ControlledAdapter(NativeStatus.OPTIMAL, scalar_error=True))
    assert result.outcome is SolveOutcome.VALIDATION_FAILED
    assert not result.is_usable
    assert not result.validation.is_valid
    assert result.validation.projected_attempts == ()


def test_a09_lexicographic_priority_over_makespan():
    # Short-first has T=0, Cmax=15; long-first can have Cmax=14 but T=110.
    result = solve(priority_case())
    assert result.outcome is SolveOutcome.OPTIMAL
    assert result.candidate.objective.weighted_total_tardiness_seconds == 0
    assert result.candidate.objective.makespan_seconds == 15


def test_a09_priority_weights_change_best_sequence():
    case = priority_case()
    both_due_zero = tuple(copy(o, due_time_seconds=0) for o in case.snapshot.orders)
    case = replace(case, snapshot=copy(case.snapshot, orders=both_due_zero))
    result = solve(case)
    assert result.candidate.objective.weighted_total_tardiness_seconds == 45
    reversed_weights = tuple(
        copy(o, priority_weight=100 if o.order_id == "long" else 1) for o in both_due_zero
    )
    reverse = solve(replace(case, snapshot=copy(case.snapshot, orders=reversed_weights)))
    assert reverse.candidate.objective.weighted_total_tardiness_seconds == 1314
    assert reverse.candidate.objective.makespan_seconds == 14


def test_a09_numeric_bounds_rejected_without_overflow():
    case = tiny_case()
    case = replace(case, snapshot=copy(case.snapshot, hard_horizon_seconds=10**12))
    result = solve(case)
    assert result.outcome is SolveOutcome.INVALID_INPUT
    assert result.report.native_status is None
    assert "numeric_bounds_exceeded" in result.report.diagnostics[0]


def test_a11_pure_snapshot_and_recorded_settings():
    case = tiny_case()
    before = canonical_hash((case.catalog, case.snapshot, case.order_deltas))
    config = SolverConfig(max_time_seconds=2.0, random_seed=42)
    result = solve(case, config=config)
    assert result.is_usable
    assert canonical_hash((case.catalog, case.snapshot, case.order_deltas)) == before
    assert result.report.config == config
    assert result.report.solver_version
    assert result.report.solver_wall_time_seconds >= 0
    assert result.report.total_elapsed_seconds >= result.report.solver_wall_time_seconds
    assert result.candidate.base_snapshot_hash == canonical_hash(case.snapshot)


def test_invalid_history_is_not_infeasible():
    case = scenario_case("machine_breakdown")
    case = replace(
        case,
        snapshot=copy(
            case.snapshot,
            machines=tuple(
                copy(m, status=MachineStatus.AVAILABLE) if m.machine_id == "QC01" else m
                for m in case.snapshot.machines
            ),
        ),
    )
    result = solve(case)
    assert result.outcome is SolveOutcome.INVALID_INPUT
    assert result.report.native_status is None
    assert result.candidate is None


def test_empty_work_and_calendar_exact_boundary():
    case = tiny_case(quantity=1, horizon=5)
    assert solve(case).candidate.objective.makespan_seconds == 5
    case = replace(case, snapshot=copy(case.snapshot, orders=(), simulated_time_seconds=2))
    result = solve(case)
    assert result.outcome is SolveOutcome.OPTIMAL
    assert result.candidate.assignments == ()
    assert result.candidate.objective.makespan_seconds == 2
