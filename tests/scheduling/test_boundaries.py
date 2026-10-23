"""Additional boundary checks found during the S04 implementation review."""

import ast
import json
from dataclasses import replace
from pathlib import Path

import pytest
from cases import copy, priority_case, scenario_case, tiny_case
from test_solver import solve

import shopfloor_pilot.scheduling.solver as implementation
from shopfloor_pilot.domain.canonical import canonical_hash
from shopfloor_pilot.domain.models import (
    AttemptStatus,
    MachineStatus,
    OperationalSnapshot,
    OperationAttempt,
    OperationStatus,
    OrderDelta,
    OrderDeltaKind,
    OrderStatus,
    ResourceDelta,
    ResourceDeltaKind,
    ScheduleAssignment,
    TimeWindow,
)
from shopfloor_pilot.planning import expand_process_route
from shopfloor_pilot.scheduling import NativeStatus, SolveOutcome, SolverConfig, solve_schedule
from shopfloor_pilot.validation import validate_candidate, validate_schedule


def test_a01_committed_hand_answer():
    expected = json.loads((Path(__file__).parent / "fixtures/known_answers.json").read_text())[
        "tiny_two_units"
    ]
    result = solve(tiny_case())
    assert result.candidate.objective.makespan_seconds == expected["optimal_makespan"]
    assert (
        result.candidate.objective.weighted_total_tardiness_seconds
        == (expected["optimal_weighted_tardiness"])
    )
    assert result.report.objective_value == expected["optimal_scalar"]


def test_a04_actual_times_override_obsolete_planned_windows():
    case = tiny_case(quantity=1, due=0, horizon=20)
    mill, qc = expand_process_route(case.snapshot.orders[0], case.catalog).operations
    completed = OperationAttempt(
        attempt_id="actual-completed",
        operation_id=mill.operation_id,
        attempt_number=1,
        machine_id="mill",
        planned_duration_seconds=3,
        status=AttemptStatus.COMPLETED,
        scheduled_window=TimeWindow(start_seconds=0, end_seconds=3),
        actual_start_seconds=1,
        actual_end_seconds=4,
        consumed_seconds=3,
    )
    running = OperationAttempt(
        attempt_id="actual-running",
        operation_id=qc.operation_id,
        attempt_number=1,
        machine_id="qc",
        planned_duration_seconds=2,
        status=AttemptStatus.RUNNING,
        scheduled_window=TimeWindow(start_seconds=3, end_seconds=5),
        actual_start_seconds=5,
        consumed_seconds=1,
    )
    snapshot = copy(
        case.snapshot,
        simulated_time_seconds=6,
        operations=(
            copy(mill, status=OperationStatus.COMPLETED),
            copy(qc, status=OperationStatus.RUNNING),
        ),
        attempts=(completed, running),
        machines=(
            case.snapshot.machines[0],
            copy(case.snapshot.machines[1], status=MachineStatus.RUNNING),
        ),
    )
    result = solve(replace(case, snapshot=snapshot))
    assert result.outcome is SolveOutcome.OPTIMAL
    assert result.candidate.assignments == ()
    assert result.candidate.objective.makespan_seconds == 7
    assert result.candidate.objective.weighted_total_tardiness_seconds == 14
    assert result.validation.projected_attempts == snapshot.attempts
    # All-completed state still includes historical tardiness and absolute completion.
    completed_qc = copy(
        running,
        status=AttemptStatus.COMPLETED,
        actual_end_seconds=7,
        consumed_seconds=2,
    )
    final = copy(
        snapshot,
        simulated_time_seconds=10,
        orders=(copy(snapshot.orders[0], status=OrderStatus.COMPLETED),),
        operations=tuple(copy(op, status=OperationStatus.COMPLETED) for op in snapshot.operations),
        attempts=(completed, completed_qc),
        machines=case.snapshot.machines,
    )
    result = solve(replace(case, snapshot=final))
    assert result.is_usable
    assert result.candidate.objective.makespan_seconds == 7
    assert result.validation.projected_attempts == final.attempts


def test_a09_shorter_makespan_alternative_has_worse_primary_objective():
    case = priority_case()
    operations = {
        (order.order_id, op.sequence_index): op
        for order in case.snapshot.orders
        for op in expand_process_route(order, case.catalog).operations
    }
    # Construct the competing schedule independently of the solver.
    assignments = tuple(
        ScheduleAssignment(
            operation_id=operations[order_id, index].operation_id,
            attempt_id=f"competing-{order_id}-{index}",
            machine_id=machine,
            window=TimeWindow(start_seconds=start, end_seconds=end),
        )
        for order_id, index, machine, start, end in (
            ("long", 0, "mill", 0, 3),
            ("long", 1, "qc", 3, 13),
            ("short", 0, "mill", 3, 5),
            ("short", 1, "qc", 13, 14),
        )
    )
    alternate = validate_schedule(case.snapshot, case.catalog, assignments)
    assert alternate.is_valid
    assert (alternate.total_tardiness_seconds, alternate.makespan_seconds) == (110, 14)
    chosen = solve(case)
    assert chosen.candidate.objective.makespan_seconds > alternate.makespan_seconds
    assert chosen.candidate.objective.weighted_total_tardiness_seconds == 0


@pytest.mark.parametrize(
    "kind",
    (
        ResourceDeltaKind.RESTORE,
        ResourceDeltaKind.REPLACE_AVAILABILITY,
    ),
)
def test_unsupported_resource_deltas_rejected_before_solver(kind):
    case = tiny_case()
    delta = (
        ResourceDelta(
            kind=kind,
            machine_id="mill",
            effective_time_seconds=0,
            new_status=MachineStatus.AVAILABLE,
        )
        if kind is ResourceDeltaKind.RESTORE
        else ResourceDelta(
            kind=kind,
            machine_id="mill",
            effective_time_seconds=0,
            availability_windows=(TimeWindow(start_seconds=0, end_seconds=100),),
        )
    )
    result = solve(case, proposed_resource_deltas=(delta,))
    assert result.outcome is SolveOutcome.INVALID_INPUT
    assert result.report.native_status is None


def test_order_replacement_duplicate_and_stale_mark_down_rejected():
    case = tiny_case()
    for deltas in (
        (OrderDelta(kind=OrderDeltaKind.REPLACE, order=case.snapshot.orders[0]),),
        (OrderDelta(kind=OrderDeltaKind.ADD, order=case.snapshot.orders[0]),),
    ):
        result = solve(replace(case, order_deltas=deltas))
        assert result.outcome is SolveOutcome.INVALID_INPUT
        assert result.report.native_status is None
    delta = ResourceDelta(
        kind=ResourceDeltaKind.MARK_DOWN,
        machine_id="mill",
        effective_time_seconds=1,
        new_status=MachineStatus.DOWN,
    )
    assert solve(case, proposed_resource_deltas=(delta,)).outcome is SolveOutcome.INVALID_INPUT


def test_large_soft_due_date_and_safe_numeric_objective_limit():
    case = tiny_case()
    late = copy(case.snapshot.orders[0], due_time_seconds=10**30)
    result = solve(replace(case, snapshot=copy(case.snapshot, orders=(late,))))
    assert result.is_usable
    assert result.candidate.objective.weighted_total_tardiness_seconds == 0
    # H itself is permitted; the scalar bound exceeds the separate conservative cap.
    case = replace(case, snapshot=copy(case.snapshot, hard_horizon_seconds=10**9))
    result = solve(case)
    assert result.outcome is SolveOutcome.INVALID_INPUT
    assert "numeric_bounds_exceeded" in result.report.diagnostics[0]


def test_no_incumbent_never_reads_native_variable_values(monkeypatch):
    class NoIncumbentSolver:
        class Parameters:
            pass

        parameters = Parameters()
        wall_time = 0.01
        best_objective_bound = 0.0

        def solve(self, model):
            return int(NativeStatus.UNKNOWN)

        def response_stats(self):
            return "Controlled no-incumbent response"

        def value(self, variable):
            pytest.fail("A no-incumbent response must not read any variable")

    monkeypatch.setattr(implementation.cp_model, "CpSolver", NoIncumbentSolver)
    result = solve(tiny_case())
    assert result.outcome is SolveOutcome.UNKNOWN
    assert not result.is_usable


def test_a11_scheduler_dependency_boundary():
    source = Path(implementation.__file__).parent
    imported = {
        node.module
        for path in source.glob("*.py")
        for node in ast.walk(ast.parse(path.read_text()))
        if isinstance(node, ast.ImportFrom) and node.module
    }
    assert not any(
        module.startswith(
            (
                "shopfloor_pilot.storage",
                "shopfloor_pilot.events",
                "shopfloor_pilot.domain.fixtures",
                "requests",
                "httpx",
                "socket",
            )
        )
        for module in imported
    )
    assert not any("Approval" in path.read_text() for path in source.glob("*.py"))


@pytest.mark.parametrize(
    "kwargs",
    (
        {"max_time_seconds": 0.0},
        {"max_time_seconds": float("inf")},
        {"max_time_seconds": float("nan")},
        {"num_search_workers": 2},
        {"relative_gap_limit": 0.1},
        {"random_seed": -1},
    ),
)
def test_configuration_rejects_unbounded_or_nonreproducible_settings(kwargs):
    with pytest.raises(ValueError):
        SolverConfig(**kwargs)


def test_release_beyond_horizon_and_missing_capacity_are_hard_infeasible():
    case = tiny_case()
    future_order = copy(case.snapshot.orders[0], release_time_seconds=21, due_time_seconds=21)
    for updates in ({"orders": (future_order,)}, {"machines": ()}):
        snapshot = OperationalSnapshot.model_validate(
            {
                **case.snapshot.model_dump(mode="python"),
                **updates,
            }
        )
        result = solve(replace(case, snapshot=snapshot))
        assert result.outcome is SolveOutcome.INFEASIBLE


def test_s05_post_fault_not_before_floor_uses_actual_boundary_snapshot():
    case = scenario_case("machine_breakdown")
    result = solve(case, not_before_seconds=2701)

    assert case.snapshot.simulated_time_seconds == 2700
    assert result.is_usable
    assert result.report.not_before_seconds == 2701
    assert "not_before_seconds" not in result.report.config.model_dump()
    assert result.candidate.base_state_version == case.snapshot.state_version
    assert result.candidate.base_snapshot_hash == canonical_hash(case.snapshot)
    assert all(
        assignment.window.start_seconds >= 2701 for assignment in result.candidate.assignments
    )
    restart = next(
        attempt
        for attempt in result.validation.projected_attempts
        if attempt.restart_of_attempt_id is not None
    )
    assert restart.scheduled_window.start_seconds >= 2701
    assert restart.scheduled_window.duration_seconds == restart.planned_duration_seconds == 900
    assert validate_candidate(case.snapshot, case.catalog, result.candidate).is_valid


@pytest.mark.parametrize(
    "invalid_kind",
    ("negative", "before_snapshot", "after_horizon", "bool", "float", "string"),
)
def test_not_before_rejects_invalid_boundaries_before_solver(invalid_kind):
    class SolverMustNotRun:
        def solve(self, problem, config):
            pytest.fail("invalid not_before_seconds reached the solver adapter")

    case = scenario_case("machine_breakdown")
    not_before_seconds = {
        "negative": -1,
        "before_snapshot": case.snapshot.simulated_time_seconds - 1,
        "after_horizon": case.snapshot.hard_horizon_seconds + 1,
        "bool": True,
        "float": 2700.0,
        "string": "2700",
    }[invalid_kind]
    result = solve(
        case,
        not_before_seconds=not_before_seconds,
        _adapter=SolverMustNotRun(),
    )

    assert result.outcome is SolveOutcome.INVALID_INPUT
    assert result.report.native_status is None
    assert result.candidate is None
    assert "not_before_seconds" in result.report.diagnostics[0]


def test_not_before_none_preserves_default_candidate_and_stays_out_of_solver_config():
    case = scenario_case("machine_breakdown")
    kwargs = {
        "proposal_id": "proposal-evidence",
        "workflow_id": "workflow-evidence",
        "proposed_order_deltas": case.order_deltas,
    }
    implicit = solve_schedule(case.snapshot, case.catalog, **kwargs)
    explicit = solve_schedule(case.snapshot, case.catalog, not_before_seconds=None, **kwargs)

    assert implicit.outcome is explicit.outcome
    assert implicit.candidate == explicit.candidate
    assert canonical_hash(implicit.candidate) == canonical_hash(explicit.candidate)
    assert canonical_hash(implicit.candidate) == (
        "d064e5a71ea03605a86a259e73468856039521671418c1bed13ac2ae0ea2d768"
    )
    assert implicit.report.not_before_seconds is explicit.report.not_before_seconds is None
    assert "not_before_seconds" not in implicit.report.config.model_dump()


@pytest.mark.parametrize("boundary", ("snapshot", "horizon"))
def test_not_before_inclusive_bounds_reach_solver(boundary):
    case = scenario_case("machine_breakdown")
    value = (
        case.snapshot.simulated_time_seconds
        if boundary == "snapshot"
        else case.snapshot.hard_horizon_seconds
    )

    result = solve(case, not_before_seconds=value)

    assert result.outcome is not SolveOutcome.INVALID_INPUT
    assert result.report.not_before_seconds == value
