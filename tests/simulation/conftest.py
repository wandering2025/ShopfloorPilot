"""Independent, hand-built fixtures for the S05 simulator acceptance suite."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

import pytest

from shopfloor_pilot.domain.canonical import canonical_hash
from shopfloor_pilot.domain.models import (
    CandidateProposal,
    Capability,
    DomainCatalog,
    Machine,
    Operation,
    OperationalSnapshot,
    OperationTemplate,
    Order,
    ProductTemplate,
    ResourceKind,
    ScheduleAssignment,
    ScheduleObjective,
    TimeWindow,
)
from shopfloor_pilot.planning import expand_process_route
from shopfloor_pilot.storage import SQLiteStateRepository


@dataclass(frozen=True)
class TinyCell:
    """A deliberately small cell whose schedule has a hand-calculated oracle."""

    catalog: DomainCatalog
    snapshot: OperationalSnapshot
    operations: tuple[Operation, ...]

    def operation(self, unit: int, sequence: int) -> Operation:
        return next(
            item
            for item in self.operations
            if item.unit_index == unit and item.sequence_index == sequence
        )


@pytest.fixture
def tiny_cell_factory() -> Callable[..., TinyCell]:
    def build(
        *,
        run_id: str = "run-s05-tiny",
        quantity: int = 2,
        milling_seconds: int = 3,
        inspection_seconds: int = 2,
        horizon: int = 30,
        second_mill: bool = False,
    ) -> TinyCell:
        full_window = (TimeWindow(start_seconds=0, end_seconds=horizon),)
        machines = [
            Machine(
                machine_id="mill-a",
                display_name="Mill A",
                kind=ResourceKind.PROCESSING,
                capabilities=(Capability.MILLING,),
                availability_windows=full_window,
            )
        ]
        if second_mill:
            machines.append(
                Machine(
                    machine_id="mill-b",
                    display_name="Mill B",
                    kind=ResourceKind.PROCESSING,
                    capabilities=(Capability.MILLING,),
                    availability_windows=full_window,
                )
            )
        machines.extend(
            (
                Machine(
                    machine_id="drill-only",
                    display_name="Drill Only",
                    kind=ResourceKind.PROCESSING,
                    capabilities=(Capability.DRILLING,),
                    availability_windows=full_window,
                ),
                Machine(
                    machine_id="qc",
                    display_name="Quality Control",
                    kind=ResourceKind.INSPECTION,
                    capabilities=(Capability.INSPECTION,),
                    availability_windows=full_window,
                ),
            )
        )
        product = ProductTemplate(
            product_id="tiny-part",
            canonical_name="Tiny Test Part",
            route_id="route-tiny-part",
            operations=(
                OperationTemplate(
                    operation_template_id="tiny-mill",
                    sequence_index=0,
                    name="Mill",
                    required_capability=Capability.MILLING,
                    duration_seconds=milling_seconds,
                ),
                OperationTemplate(
                    operation_template_id="tiny-inspect",
                    sequence_index=1,
                    name="Inspect",
                    required_capability=Capability.INSPECTION,
                    duration_seconds=inspection_seconds,
                ),
            ),
        )
        catalog = DomainCatalog(
            schema_version=1,
            machines=tuple(machines),
            products=(product,),
        )
        order = Order(
            order_id="order-tiny",
            product_id=product.product_id,
            quantity=quantity,
            release_time_seconds=0,
            due_time_seconds=horizon,
            priority_weight=2,
        )
        expansion = expand_process_route(order, catalog)
        assert expansion.order == order
        assert expansion.operations
        snapshot = OperationalSnapshot(
            schema_version=1,
            run_id=run_id,
            state_version=0,
            plan_revision=0,
            simulated_time_seconds=0,
            hard_horizon_seconds=horizon,
            machines=catalog.machines,
            orders=(order,),
            operations=(),
            attempts=(),
        )
        return TinyCell(catalog=catalog, snapshot=snapshot, operations=expansion.operations)

    return build


@pytest.fixture
def candidate_factory() -> Callable[..., CandidateProposal]:
    def build(
        snapshot: OperationalSnapshot,
        assignments: Iterable[tuple[Operation, str, int, int] | ScheduleAssignment],
        *,
        proposal_id: str,
        workflow_id: str = "workflow-s05-tests",
        weighted_tardiness: int = 0,
        makespan: int | None = None,
    ) -> CandidateProposal:
        projected: list[ScheduleAssignment] = []
        for index, item in enumerate(assignments, start=1):
            if isinstance(item, ScheduleAssignment):
                projected.append(item)
                continue
            operation, machine_id, start, end = item
            projected.append(
                ScheduleAssignment(
                    operation_id=operation.operation_id,
                    attempt_id=f"attempt-{operation.unit_index}-{operation.sequence_index}-{index}",
                    machine_id=machine_id,
                    window=TimeWindow(start_seconds=start, end_seconds=end),
                )
            )
        assignment_tuple = tuple(projected)
        if makespan is None:
            makespan = max(
                (item.window.end_seconds for item in assignment_tuple),
                default=snapshot.simulated_time_seconds,
            )
        return CandidateProposal.create(
            schema_version=1,
            proposal_id=proposal_id,
            run_id=snapshot.run_id,
            workflow_id=workflow_id,
            base_state_version=snapshot.state_version,
            base_snapshot_hash=canonical_hash(snapshot),
            proposed_plan_revision=snapshot.plan_revision + 1,
            assignments=assignment_tuple,
            objective=ScheduleObjective(
                weighted_total_tardiness_seconds=weighted_tardiness,
                makespan_seconds=makespan,
            ),
        )

    return build


@pytest.fixture
def repository_factory(
    tmp_path: Path,
) -> Callable[[OperationalSnapshot, str], SQLiteStateRepository]:
    counter = 0

    def build(snapshot: OperationalSnapshot, label: str = "runtime") -> SQLiteStateRepository:
        nonlocal counter
        counter += 1
        repository = SQLiteStateRepository(tmp_path / f"{counter:02d}-{label}.sqlite3")
        repository.initialize(applied_at_utc="2026-10-20T20:00:00+02:00")
        repository.create_run(snapshot, created_at_utc="2026-10-20T20:00:00+02:00")
        return repository

    return build
