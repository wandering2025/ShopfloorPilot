"""Hand-calculated, solver-free schedule fixtures for S03 acceptance."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from shopfloor_pilot.domain.canonical import canonical_hash
from shopfloor_pilot.domain.fixtures import load_scenario
from shopfloor_pilot.domain.models import (
    AttemptStatus,
    CandidateProposal,
    DomainCatalog,
    Machine,
    MachineStatus,
    Operation,
    OperationalSnapshot,
    OperationAttempt,
    OperationStatus,
    Order,
    OrderDelta,
    OrderDeltaKind,
    ScheduleAssignment,
    ScheduleObjective,
    TimeWindow,
)
from shopfloor_pilot.planning import expand_process_route

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "hand_calculated_cases.json"


@dataclass(frozen=True)
class ValidationCase:
    catalog: DomainCatalog
    snapshot: OperationalSnapshot
    operations: tuple[Operation, ...]
    assignments: tuple[ScheduleAssignment, ...]
    candidate: CandidateProposal
    order_deltas: tuple[OrderDelta, ...] = ()


def validated_copy[ModelT](model: ModelT, **updates: Any) -> ModelT:
    data = model.model_dump(mode="python")  # type: ignore[attr-defined]
    data.update(updates)
    return type(model).model_validate(data)


def operation_at(
    operations: tuple[Operation, ...],
    unit_index: int,
    sequence_index: int,
) -> Operation:
    return next(
        operation
        for operation in operations
        if operation.unit_index == unit_index and operation.sequence_index == sequence_index
    )


def order_from_json(raw: dict[str, Any]) -> Order:
    return Order.model_validate_json(json.dumps(raw, ensure_ascii=False))


def assignment_for(
    assignments: tuple[ScheduleAssignment, ...],
    unit_index: int,
    sequence_index: int,
    operations: tuple[Operation, ...],
) -> ScheduleAssignment:
    operation = operation_at(operations, unit_index, sequence_index)
    return next(item for item in assignments if item.operation_id == operation.operation_id)


def replace_assignment(
    assignments: tuple[ScheduleAssignment, ...],
    operation_id: str,
    **updates: Any,
) -> tuple[ScheduleAssignment, ...]:
    return tuple(
        validated_copy(item, **updates) if item.operation_id == operation_id else item
        for item in assignments
    )


@pytest.fixture(scope="session")
def hand_cases() -> dict[str, Any]:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def catalog() -> DomainCatalog:
    return load_scenario("normal").catalog


@pytest.fixture
def case_factory(
    catalog: DomainCatalog,
    hand_cases: dict[str, Any],
) -> Callable[..., ValidationCase]:
    def build(
        case_name: str,
        *,
        include_operations: bool = False,
        operation_statuses: dict[int, OperationStatus] | None = None,
        attempts: tuple[OperationAttempt, ...] = (),
        machines: tuple[Machine, ...] | None = None,
        state_version: int = 0,
        plan_revision: int = 0,
        order_as_delta: bool = False,
    ) -> ValidationCase:
        raw = hand_cases[case_name]
        order = order_from_json(raw["order"])
        route = expand_process_route(order, catalog)
        assert route.order == order
        operations = route.operations
        if operation_statuses:
            operations = tuple(
                validated_copy(
                    operation,
                    status=operation_statuses.get(operation.sequence_index, operation.status),
                )
                for operation in operations
            )
        assignment_specs = raw["assignments"]
        assignments = tuple(
            ScheduleAssignment(
                operation_id=operation_at(
                    operations,
                    item["unit_index"],
                    item["sequence_index"],
                ).operation_id,
                attempt_id=item["attempt_id"],
                machine_id=item["machine_id"],
                window=TimeWindow(
                    start_seconds=item["start_seconds"],
                    end_seconds=item["end_seconds"],
                ),
            )
            for item in assignment_specs
        )
        deltas = (OrderDelta(kind=OrderDeltaKind.ADD, order=order),) if order_as_delta else ()
        snapshot = OperationalSnapshot(
            schema_version=1,
            run_id=raw["run_id"],
            state_version=state_version,
            plan_revision=plan_revision,
            simulated_time_seconds=raw["simulated_time_seconds"],
            hard_horizon_seconds=raw["hard_horizon_seconds"],
            machines=machines or catalog.machines,
            orders=() if order_as_delta else (order,),
            operations=operations if include_operations else (),
            attempts=attempts,
        )
        candidate = CandidateProposal.create(
            schema_version=1,
            proposal_id=f"proposal-{case_name.replace('_', '-')}",
            run_id=snapshot.run_id,
            workflow_id=f"workflow-{case_name.replace('_', '-')}",
            base_state_version=snapshot.state_version,
            base_snapshot_hash=canonical_hash(snapshot),
            proposed_plan_revision=snapshot.plan_revision + 1,
            assignments=assignments,
            objective=ScheduleObjective(
                weighted_total_tardiness_seconds=raw["weighted_total_tardiness_seconds"],
                makespan_seconds=raw["makespan_seconds"],
            ),
            proposed_order_deltas=deltas,
            created_at_utc="2026-10-20T19:00:00+02:00",
        )
        return ValidationCase(
            catalog=catalog,
            snapshot=snapshot,
            operations=operations,
            assignments=assignments,
            candidate=candidate,
            order_deltas=deltas,
        )

    return build


@pytest.fixture
def legal_case(case_factory: Callable[..., ValidationCase]) -> ValidationCase:
    return case_factory("legal_quantity_two")


@pytest.fixture
def recovery_case(
    case_factory: Callable[..., ValidationCase],
    catalog: DomainCatalog,
    hand_cases: dict[str, Any],
) -> ValidationCase:
    raw = hand_cases["interrupted_recovery"]
    order = order_from_json(raw["order"])
    route = expand_process_route(order, catalog)
    mill = operation_at(route.operations, 1, 0)
    interrupted = OperationAttempt(
        attempt_id="attempt-recovery-mill-1",
        operation_id=mill.operation_id,
        attempt_number=1,
        machine_id="CNC02",
        planned_duration_seconds=mill.duration_seconds,
        status=AttemptStatus.INTERRUPTED,
        scheduled_window=TimeWindow(start_seconds=0, end_seconds=900),
        actual_start_seconds=0,
        actual_end_seconds=300,
        consumed_seconds=300,
    )
    machines = tuple(
        validated_copy(
            machine,
            status=MachineStatus.DOWN,
            availability_windows=(TimeWindow(start_seconds=0, end_seconds=300),),
        )
        if machine.machine_id == "CNC02"
        else machine
        for machine in catalog.machines
    )
    return case_factory(
        "interrupted_recovery",
        include_operations=True,
        operation_statuses={0: OperationStatus.INTERRUPTED},
        attempts=(interrupted,),
        machines=machines,
        state_version=5,
        plan_revision=1,
    )


@pytest.fixture
def history_case(
    case_factory: Callable[..., ValidationCase],
    catalog: DomainCatalog,
    hand_cases: dict[str, Any],
) -> ValidationCase:
    raw = hand_cases["fixed_history"]
    order = order_from_json(raw["order"])
    route = expand_process_route(order, catalog)
    mill = operation_at(route.operations, 1, 0)
    drill = operation_at(route.operations, 1, 1)
    completed = OperationAttempt(
        attempt_id="attempt-history-mill-1",
        operation_id=mill.operation_id,
        attempt_number=1,
        machine_id="CNC02",
        planned_duration_seconds=mill.duration_seconds,
        status=AttemptStatus.COMPLETED,
        scheduled_window=TimeWindow(start_seconds=0, end_seconds=900),
        actual_start_seconds=0,
        actual_end_seconds=900,
        consumed_seconds=900,
    )
    running = OperationAttempt(
        attempt_id="attempt-history-drill-1",
        operation_id=drill.operation_id,
        attempt_number=1,
        machine_id="CNC01",
        planned_duration_seconds=drill.duration_seconds,
        status=AttemptStatus.RUNNING,
        scheduled_window=TimeWindow(start_seconds=900, end_seconds=1320),
        actual_start_seconds=900,
        consumed_seconds=100,
    )
    machines = tuple(
        validated_copy(machine, status=MachineStatus.RUNNING)
        if machine.machine_id == "CNC01"
        else machine
        for machine in catalog.machines
    )
    return case_factory(
        "fixed_history",
        include_operations=True,
        operation_statuses={
            0: OperationStatus.COMPLETED,
            1: OperationStatus.RUNNING,
        },
        attempts=(completed, running),
        machines=machines,
        state_version=8,
        plan_revision=2,
    )


@pytest.fixture
def urgent_case(case_factory: Callable[..., ValidationCase]) -> ValidationCase:
    return case_factory("urgent_order_delta", order_as_delta=True)
