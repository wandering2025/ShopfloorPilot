"""Small deterministic fixtures for the S06 application-service boundary."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest

from shopfloor_pilot.domain.models import (
    Capability,
    DomainCatalog,
    Machine,
    OperationalSnapshot,
    OperationTemplate,
    Order,
    ProductTemplate,
    ResourceKind,
    TimeWindow,
)
from shopfloor_pilot.services import ApplicationServices
from shopfloor_pilot.storage import SQLiteStateRepository

FIXED_UTC = "2026-10-20T20:45:00+02:00"


@dataclass(frozen=True)
class ServiceCase:
    catalog: DomainCatalog
    snapshot: OperationalSnapshot


@pytest.fixture
def service_case_factory() -> Callable[[str], ServiceCase]:
    def build(run_id: str = "run-s06-services") -> ServiceCase:
        window = (TimeWindow(start_seconds=0, end_seconds=30),)
        machines = (
            Machine(
                machine_id="mill",
                display_name="Mill",
                kind=ResourceKind.PROCESSING,
                capabilities=(Capability.MILLING,),
                availability_windows=window,
            ),
            Machine(
                machine_id="qc",
                display_name="Quality Control",
                kind=ResourceKind.INSPECTION,
                capabilities=(Capability.INSPECTION,),
                availability_windows=window,
            ),
        )
        product = ProductTemplate(
            product_id="service-part",
            canonical_name="Service Test Part",
            route_id="route-service-part",
            operations=(
                OperationTemplate(
                    operation_template_id="service-mill",
                    sequence_index=0,
                    name="Mill",
                    required_capability=Capability.MILLING,
                    duration_seconds=3,
                ),
                OperationTemplate(
                    operation_template_id="service-qc",
                    sequence_index=1,
                    name="Inspect",
                    required_capability=Capability.INSPECTION,
                    duration_seconds=2,
                ),
            ),
        )
        catalog = DomainCatalog(schema_version=1, machines=machines, products=(product,))
        order = Order(
            order_id="order-base",
            product_id=product.product_id,
            quantity=1,
            release_time_seconds=0,
            due_time_seconds=15,
            priority_weight=1,
        )
        snapshot = OperationalSnapshot(
            schema_version=1,
            run_id=run_id,
            state_version=0,
            plan_revision=0,
            simulated_time_seconds=0,
            hard_horizon_seconds=30,
            machines=machines,
            orders=(order,),
            operations=(),
            attempts=(),
        )
        return ServiceCase(catalog=catalog, snapshot=snapshot)

    return build


@pytest.fixture
def service_factory(
    tmp_path: Path,
    service_case_factory: Callable[[str], ServiceCase],
) -> Callable[[str], tuple[ApplicationServices, ServiceCase, Path]]:
    counter = 0

    def build(run_id: str = "run-s06-services") -> tuple[ApplicationServices, ServiceCase, Path]:
        nonlocal counter
        counter += 1
        case = service_case_factory(run_id)
        path = tmp_path / f"{counter:02d}-services.sqlite3"
        repository = SQLiteStateRepository(path, busy_timeout_seconds=2.0)
        repository.initialize(applied_at_utc=FIXED_UTC)
        services = ApplicationServices(repository, case.catalog, seed=0)
        services.create_run(case.snapshot, created_at_utc=FIXED_UTC)
        return services, case, path

    return build
