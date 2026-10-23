"""Independent synthetic scheduling fixtures, including hand-built event boundaries."""

from dataclasses import dataclass
from typing import Any

from shopfloor_pilot.domain.fixtures import load_scenario
from shopfloor_pilot.domain.models import (
    AttemptStatus,
    Capability,
    DomainCatalog,
    Machine,
    MachineStatus,
    OperationalSnapshot,
    OperationAttempt,
    OperationStatus,
    OperationTemplate,
    Order,
    OrderDelta,
    OrderDeltaKind,
    ProductTemplate,
    ResourceKind,
    TimeWindow,
)
from shopfloor_pilot.planning import expand_process_route


def copy[ModelT](model: ModelT, **updates: Any) -> ModelT:
    data = model.model_dump(mode="python")
    data.update(updates)
    return type(model).model_validate(data)


@dataclass(frozen=True)
class Case:
    catalog: DomainCatalog
    snapshot: OperationalSnapshot
    order_deltas: tuple[OrderDelta, ...] = ()


def tiny_case(*, quantity: int = 2, due: int = 6, horizon: int = 20) -> Case:
    machines = tuple(
        Machine(
            machine_id=name,
            display_name=name,
            kind=kind,
            capabilities=(cap,),
            availability_windows=(TimeWindow(start_seconds=0, end_seconds=100),),
        )
        for name, kind, cap in (
            ("mill", ResourceKind.PROCESSING, Capability.MILLING),
            ("qc", ResourceKind.INSPECTION, Capability.INSPECTION),
        )
    )
    product = ProductTemplate(
        product_id="tiny",
        canonical_name="Tiny Test Part",
        route_id="route-tiny",
        operations=tuple(
            OperationTemplate(
                operation_template_id=f"tiny-{index}",
                sequence_index=index,
                name=cap.value,
                required_capability=cap,
                duration_seconds=duration,
            )
            for index, (cap, duration) in enumerate(
                ((Capability.MILLING, 3), (Capability.INSPECTION, 2))
            )
        ),
    )
    catalog = DomainCatalog(schema_version=1, machines=machines, products=(product,))
    order = Order(
        order_id="order-tiny",
        product_id="tiny",
        quantity=quantity,
        release_time_seconds=0,
        due_time_seconds=due,
        priority_weight=2,
    )
    snapshot = OperationalSnapshot(
        schema_version=1,
        run_id="run-tiny",
        state_version=0,
        plan_revision=0,
        simulated_time_seconds=0,
        hard_horizon_seconds=horizon,
        machines=machines,
        orders=(order,),
        operations=(),
        attempts=(),
    )
    return Case(catalog, snapshot)


def priority_case() -> Case:
    case = tiny_case(horizon=20)
    base = case.catalog.products[0]
    long = copy(
        base,
        product_id="long",
        route_id="route-long",
        operations=(base.operations[0], copy(base.operations[1], duration_seconds=10)),
    )
    short = copy(
        base,
        product_id="short",
        route_id="route-short",
        operations=(
            copy(base.operations[0], duration_seconds=2),
            copy(base.operations[1], duration_seconds=1),
        ),
    )
    orders = (
        copy(
            case.snapshot.orders[0],
            order_id="long",
            product_id="long",
            quantity=1,
            due_time_seconds=20,
            priority_weight=1,
        ),
        copy(
            case.snapshot.orders[0],
            order_id="short",
            product_id="short",
            quantity=1,
            due_time_seconds=3,
            priority_weight=10,
        ),
    )
    return Case(copy(case.catalog, products=(long, short)), copy(case.snapshot, orders=orders))


def scenario_case(name: str) -> Case:
    bundle = load_scenario(name)
    scenario, catalog = bundle.scenario, bundle.catalog
    snapshot = OperationalSnapshot(
        schema_version=1,
        run_id=scenario.run_id,
        state_version=0,
        plan_revision=0,
        simulated_time_seconds=0,
        hard_horizon_seconds=scenario.hard_horizon_seconds,
        machines=catalog.machines,
        orders=scenario.orders,
        operations=(),
        attempts=(),
    )
    if name == "normal":
        return Case(catalog, snapshot)
    if name == "urgent_order":
        intervention = next(i for i in bundle.evaluator.interventions if i.order_to_add)
        return Case(
            catalog,
            copy(snapshot, simulated_time_seconds=intervention.at_seconds, state_version=1),
            (OrderDelta(kind=OrderDeltaKind.ADD, order=intervention.order_to_add),),
        )
    if name == "no_feasible_recovery":
        # Exact hard contradiction: all milling capacity is absent from 3000 to H.
        # Pending orders retain required milling work. No soft date is a hard bound.
        return Case(
            catalog,
            copy(
                snapshot,
                simulated_time_seconds=3000,
                state_version=1,
                machines=tuple(
                    copy(m, status=MachineStatus.DOWN) if m.machine_id in ("CNC02", "CNC03") else m
                    for m in snapshot.machines
                ),
            ),
        )
    # Hand-built boundary at 2700, independent of any S04-generated schedule:
    # Completed mill/drill/slotting, running QC, and 180 wasted seconds of interrupted mill.
    operations = tuple(
        op for order in snapshot.orders for op in expand_process_route(order, catalog).operations
    )
    by_key = {(op.order_id, op.unit_index, op.sequence_index): op for op in operations}
    definitions = (
        ("order-001", 0, "CNC03", AttemptStatus.COMPLETED, 0, 900),
        ("order-001", 1, "CNC01", AttemptStatus.COMPLETED, 900, 1320),
        ("order-001", 2, "QC01", AttemptStatus.RUNNING, 2600, None),
        ("order-002", 0, "CNC02", AttemptStatus.COMPLETED, 0, 720),
        ("order-005", 0, "CNC02", AttemptStatus.COMPLETED, 720, 1440),
        ("order-008", 0, "CNC02", AttemptStatus.COMPLETED, 1800, 2520),
        ("order-004", 0, "CNC02", AttemptStatus.INTERRUPTED, 2520, 2700),
    )
    attempts = []
    statuses = {}
    for order_id, index, machine, status, start, end in definitions:
        op = by_key[order_id, 1, index]
        statuses[op.operation_id] = OperationStatus(status.value)
        attempts.append(
            OperationAttempt(
                attempt_id=f"fixed-{order_id}-{index}",
                operation_id=op.operation_id,
                attempt_number=1,
                machine_id=machine,
                planned_duration_seconds=op.duration_seconds,
                status=status,
                scheduled_window=TimeWindow(
                    start_seconds=start,
                    end_seconds=start + op.duration_seconds,
                ),
                actual_start_seconds=start,
                actual_end_seconds=end,
                consumed_seconds=(end if end is not None else 2700) - start,
            )
        )
    return Case(
        catalog,
        copy(
            snapshot,
            simulated_time_seconds=2700,
            state_version=7,
            plan_revision=2,
            machines=tuple(
                copy(m, status=MachineStatus.DOWN)
                if m.machine_id == "CNC02"
                else copy(m, status=MachineStatus.RUNNING)
                if m.machine_id == "QC01"
                else m
                for m in snapshot.machines
            ),
            operations=tuple(
                copy(op, status=statuses.get(op.operation_id, op.status)) for op in operations
            ),
            attempts=tuple(attempts),
        ),
    )
