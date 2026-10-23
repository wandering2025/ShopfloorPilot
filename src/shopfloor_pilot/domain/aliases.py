"""Small explicit bilingual alias and relative-time resolution boundary."""

from __future__ import annotations

from typing import Self

from pydantic import Field, model_validator

from .base import Identifier, PositiveQuantity, Seconds, StrictModel
from .fixtures import ScenarioClock
from .models import FailureCode, FailureDetail, ResultStatus

PRODUCT_ALIASES: dict[str, tuple[str, str]] = {
    "aluminum mounting plate": ("prod_aluminum_mounting_plate", "Aluminum Mounting Plate"),
    "mounting plate": ("prod_aluminum_mounting_plate", "Aluminum Mounting Plate"),
    "铝制安装板": ("prod_aluminum_mounting_plate", "Aluminum Mounting Plate"),
    "铝安装板": ("prod_aluminum_mounting_plate", "Aluminum Mounting Plate"),
    "steel support bracket": ("prod_steel_bracket", "Steel Support Bracket"),
    "steel bracket": ("prod_steel_bracket", "Steel Support Bracket"),
    "钢制支架": ("prod_steel_bracket", "Steel Support Bracket"),
    "actuator base": ("prod_actuator_base", "Actuator Base"),
    "执行器底座": ("prod_actuator_base", "Actuator Base"),
}

PRODUCT_ROUTES = {
    "prod_aluminum_mounting_plate": "route_aluminum_plate",
    "prod_steel_bracket": "route_steel_bracket",
    "prod_actuator_base": "route_actuator_base",
}


class OrderRequestResolution(StrictModel):
    status: ResultStatus
    product_id: Identifier | None = None
    canonical_product_name: str | None = Field(default=None, max_length=100)
    route_id: Identifier | None = None
    quantity: PositiveQuantity | None = None
    due_time_seconds: Seconds | None = None
    failures: tuple[FailureDetail, ...] = ()

    @model_validator(mode="after")
    def validate_resolution(self) -> Self:
        if self.status is ResultStatus.SUCCESS:
            if self.failures:
                raise ValueError("successful resolution cannot contain failures")
            if None in (
                self.product_id,
                self.canonical_product_name,
                self.route_id,
                self.quantity,
                self.due_time_seconds,
            ):
                raise ValueError("successful resolution requires every canonical field")
        elif not self.failures:
            raise ValueError("non-success resolution requires typed failures")
        return self


def _failure(code: FailureCode, field: str, message: str) -> FailureDetail:
    return FailureDetail(code=code, field=field, message=message)


def resolve_order_request(
    *,
    product: str | None,
    quantity: int | None,
    due: int | str | None,
    scenario_clock: ScenarioClock | None,
    requested_route_id: str | None = None,
) -> OrderRequestResolution:
    """Resolve only documented aliases and ``today``; never infer arbitrary text."""

    failures: list[FailureDetail] = []
    if product is None or not product.strip():
        failures.append(_failure(FailureCode.MISSING_PRODUCT, "product", "product is required"))
        resolved = None
    else:
        resolved = PRODUCT_ALIASES.get(product.strip().casefold())
        if resolved is None:
            failures.append(
                _failure(
                    FailureCode.UNSUPPORTED_PRODUCT,
                    "product",
                    "product is not in the supported alias table",
                )
            )

    if quantity is None:
        failures.append(_failure(FailureCode.MISSING_QUANTITY, "quantity", "quantity is required"))
    elif type(quantity) is not int or not 1 <= quantity <= 10:
        failures.append(
            _failure(
                FailureCode.INVALID_QUANTITY,
                "quantity",
                "quantity must be an integer in the inclusive range 1..10",
            )
        )

    due_seconds: int | None = None
    if due is None:
        failures.append(_failure(FailureCode.AMBIGUOUS_TIME, "due", "due time is required"))
    elif type(due) is int:
        if due < 0:
            failures.append(
                _failure(
                    FailureCode.AMBIGUOUS_TIME,
                    "due",
                    "due time must be a non-negative simulated second",
                )
            )
        else:
            due_seconds = due
    elif isinstance(due, str) and due.strip().casefold() == "today":
        if scenario_clock is None:
            failures.append(
                _failure(
                    FailureCode.AMBIGUOUS_TIME,
                    "due",
                    "today requires an explicit scenario clock and shift boundary",
                )
            )
        else:
            due_seconds = scenario_clock.shift_boundary_seconds
    else:
        failures.append(
            _failure(
                FailureCode.AMBIGUOUS_TIME,
                "due",
                "only an integer simulated second or the exact relative phrase today is supported",
            )
        )

    route_id: str | None = None
    product_id: str | None = None
    canonical_name: str | None = None
    if resolved is not None:
        product_id, canonical_name = resolved
        route_id = PRODUCT_ROUTES[product_id]
        if requested_route_id is not None and requested_route_id != route_id:
            failures.append(
                _failure(
                    FailureCode.UNKNOWN_ROUTE,
                    "requested_route_id",
                    "requested route does not match the predefined product route",
                )
            )

    if failures:
        clarification_codes = {
            FailureCode.MISSING_PRODUCT,
            FailureCode.MISSING_QUANTITY,
            FailureCode.AMBIGUOUS_TIME,
        }
        status = (
            ResultStatus.CLARIFICATION_REQUIRED
            if all(item.code in clarification_codes for item in failures)
            else ResultStatus.REJECTED
        )
        return OrderRequestResolution(status=status, failures=tuple(failures))

    return OrderRequestResolution(
        status=ResultStatus.SUCCESS,
        product_id=product_id,
        canonical_product_name=canonical_name,
        route_id=route_id,
        quantity=quantity,
        due_time_seconds=due_seconds,
    )
