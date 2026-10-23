"""S01-A02 and A05 strict-value and time-boundary acceptance."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from shopfloor_pilot.domain.fixtures import ScenarioBundle
from shopfloor_pilot.domain.models import (
    AttemptStatus,
    Capability,
    DomainCatalog,
    FailureCode,
    FailureDetail,
    FeasibilityResult,
    FeasibilityStatus,
    OperationAttempt,
    OperationTemplate,
    Order,
    OrderStatus,
    TimeWindow,
)


@pytest.mark.parametrize(
    ("factory", "expected_field", "expected_reason"),
    [
        (
            lambda: OperationTemplate(
                operation_template_id="invalid-duration",
                sequence_index=0,
                name="Invalid",
                required_capability=Capability.DRILLING,
                duration_seconds=-1,
            ),
            "duration_seconds",
            "greater than 0",
        ),
        (
            lambda: Order(
                order_id="invalid-quantity",
                product_id="prod_aluminum_mounting_plate",
                quantity=0,
                release_time_seconds=0,
                due_time_seconds=10,
                priority_weight=1,
                status=OrderStatus.PENDING,
            ),
            "quantity",
            "greater than or equal to 1",
        ),
        (
            lambda: TimeWindow(start_seconds=-1, end_seconds=10),
            "start_seconds",
            "greater than or equal to 0",
        ),
        (
            lambda: TimeWindow(start_seconds=10, end_seconds=10),
            "",
            "end_seconds must be greater",
        ),
    ],
)
def test_a02_invalid_planning_values_fail_with_field_and_reason(
    factory: object, expected_field: str, expected_reason: str
) -> None:
    with pytest.raises(ValidationError) as caught:
        factory()  # type: ignore[operator]
    errors = caught.value.errors()
    assert expected_field in ".".join(str(part) for error in errors for part in error["loc"])
    assert expected_reason in " ".join(error["msg"] for error in errors)


def test_a02_duplicate_ids_are_rejected(bundles: dict[str, ScenarioBundle]) -> None:
    catalog = bundles["normal"].catalog
    with pytest.raises(ValidationError, match="machine_id values must be unique"):
        DomainCatalog(
            schema_version=1,
            machines=(catalog.machines[0], catalog.machines[0]),
            products=catalog.products,
        )


def test_a02_invalid_enum_unknown_capability_and_malformed_window_are_rejected() -> None:
    with pytest.raises(ValidationError) as invalid_state:
        Order.model_validate_json(
            '{"order_id":"invalid-state","product_id":"prod_actuator_base",'
            '"quantity":1,"release_time_seconds":0,"due_time_seconds":10,'
            '"priority_weight":1,"status":"done-ish"}'
        )
    assert "status" in str(invalid_state.value)
    assert "done-ish" in str(invalid_state.value)

    with pytest.raises(ValidationError) as unknown_capability:
        OperationTemplate.model_validate_json(
            '{"operation_template_id":"laser-op","sequence_index":0,"name":"Laser",'
            '"required_capability":"laser_cutting","duration_seconds":10}'
        )
    assert "required_capability" in str(unknown_capability.value)
    assert "laser_cutting" in str(unknown_capability.value)

    with pytest.raises(ValidationError, match="end_seconds must be greater"):
        TimeWindow.model_validate_json('{"start_seconds":20,"end_seconds":10}')


def test_a02_strict_types_do_not_silently_coerce_planning_values() -> None:
    with pytest.raises(ValidationError):
        Order(
            order_id="string-quantity",
            product_id="prod_actuator_base",
            quantity="2",  # type: ignore[arg-type]
            release_time_seconds=0,
            due_time_seconds=10,
            priority_weight=1,
        )
    with pytest.raises(ValidationError):
        OperationTemplate(
            operation_template_id="float-duration",
            sequence_index=0,
            name="Float duration",
            required_capability=Capability.MILLING,
            duration_seconds=3.5,  # type: ignore[arg-type]
        )


def test_a05_half_open_adjacent_windows_are_legal() -> None:
    first = TimeWindow(start_seconds=0, end_seconds=10)
    second = TimeWindow(start_seconds=10, end_seconds=20)
    assert first.duration_seconds == second.duration_seconds == 10
    assert not first.overlaps(second)
    assert not second.overlaps(first)


def test_a05_zero_length_operations_and_attempts_are_invalid() -> None:
    with pytest.raises(ValidationError):
        OperationTemplate(
            operation_template_id="zero-duration",
            sequence_index=0,
            name="Zero duration",
            required_capability=Capability.MILLING,
            duration_seconds=0,
        )
    with pytest.raises(ValidationError):
        OperationAttempt(
            attempt_id="zero-attempt",
            operation_id="zero-operation",
            attempt_number=1,
            machine_id="CNC02",
            planned_duration_seconds=0,
            status=AttemptStatus.SCHEDULED,
        )


def test_a05_soft_tardiness_and_hard_horizon_are_distinct() -> None:
    tardy = FeasibilityResult(
        status=FeasibilityStatus.FEASIBLE_TARDY,
        total_tardiness_seconds=30,
        makespan_seconds=90,
        hard_horizon_seconds=100,
    )
    horizon_failure = FeasibilityResult(
        status=FeasibilityStatus.HARD_HORIZON_INFEASIBLE,
        total_tardiness_seconds=0,
        makespan_seconds=101,
        hard_horizon_seconds=100,
        failures=(
            FailureDetail(
                code=FailureCode.HARD_HORIZON,
                field="hard_horizon_seconds",
                message="required work extends beyond the hard horizon",
            ),
        ),
    )
    assert tardy.status is FeasibilityStatus.FEASIBLE_TARDY
    assert not tardy.failures
    assert horizon_failure.status is FeasibilityStatus.HARD_HORIZON_INFEASIBLE
    assert horizon_failure.failures[0].code is FailureCode.HARD_HORIZON

    with pytest.raises(ValidationError, match="makespan beyond hard horizon"):
        FeasibilityResult(
            status=FeasibilityStatus.FEASIBLE_TARDY,
            total_tardiness_seconds=30,
            makespan_seconds=101,
            hard_horizon_seconds=100,
        )
