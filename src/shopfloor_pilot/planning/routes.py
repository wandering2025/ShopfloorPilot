"""Pure expansion of structured orders into finite, predefined process routes."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from enum import StrEnum
from typing import Self

from pydantic import Field, model_validator

from shopfloor_pilot.domain.base import Identifier, StrictModel
from shopfloor_pilot.domain.canonical import canonical_json_bytes
from shopfloor_pilot.domain.models import (
    Capability,
    DomainCatalog,
    Operation,
    OperationStatus,
    Order,
    OrderStatus,
    ProductTemplate,
    ResultStatus,
)

_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]{0,63}$")
_REQUIRED_FIELDS = (
    "order_id",
    "product_id",
    "quantity",
    "release_time_seconds",
    "due_time_seconds",
    "priority_weight",
)
_ALLOWED_FIELDS = frozenset(
    {
        *_REQUIRED_FIELDS,
        "status",
        "requested_route_id",
        "operation_parameters",
    }
)
_PARAMETER_FIELDS = frozenset({"operation_template_id", "required_capability", "duration_seconds"})


class RouteIssueCode(StrEnum):
    """Stable finite route-boundary failure vocabulary."""

    MISSING_ORDER_ID = "missing_order_id"
    INVALID_ORDER_ID = "invalid_order_id"
    MISSING_PRODUCT = "missing_product"
    UNSUPPORTED_PRODUCT = "unsupported_product"
    MISSING_QUANTITY = "missing_quantity"
    INVALID_QUANTITY = "invalid_quantity"
    MISSING_RELEASE_TIME = "missing_release_time"
    INVALID_RELEASE_TIME = "invalid_release_time"
    MISSING_DUE_TIME = "missing_due_time"
    INVALID_DUE_TIME = "invalid_due_time"
    MISSING_PRIORITY = "missing_priority"
    INVALID_PRIORITY = "invalid_priority"
    INVALID_ORDER_STATUS = "invalid_order_status"
    UNSUPPORTED_REQUEST_FIELD = "unsupported_request_field"
    ROUTE_MISMATCH = "route_mismatch"
    INVALID_ROUTE_PARAMETERS = "invalid_route_parameters"
    DUPLICATE_ROUTE_PARAMETER = "duplicate_route_parameter"
    MISSING_ROUTE_PARAMETER = "missing_route_parameter"
    UNKNOWN_ROUTE_PARAMETER = "unknown_route_parameter"
    UNKNOWN_CAPABILITY = "unknown_capability"
    CAPABILITY_MISMATCH = "capability_mismatch"
    INVALID_DURATION = "invalid_duration"
    DURATION_MISMATCH = "duration_mismatch"


class RouteIssue(StrictModel):
    code: RouteIssueCode
    field: str
    message: str = Field(min_length=1, max_length=300)
    references: dict[str, str] = Field(default_factory=dict)


class RouteExpansionResult(StrictModel):
    """Typed route outcome; failures never return a partial invented route."""

    status: ResultStatus
    route_id: Identifier | None = None
    order: Order | None = None
    operations: tuple[Operation, ...] = ()
    failures: tuple[RouteIssue, ...] = ()

    @model_validator(mode="after")
    def validate_outcome(self) -> Self:
        if self.status is ResultStatus.SUCCESS:
            if self.failures:
                raise ValueError("successful route expansion cannot contain failures")
            if self.route_id is None or self.order is None or not self.operations:
                raise ValueError("successful route expansion requires route, order, and operations")
        else:
            if not self.failures:
                raise ValueError("failed route expansion requires typed failures")
            if self.route_id is not None or self.order is not None or self.operations:
                raise ValueError("failed route expansion cannot return partial route data")
        return self


def _issue(
    code: RouteIssueCode,
    field: str,
    message: str,
    **references: str,
) -> RouteIssue:
    return RouteIssue(code=code, field=field, message=message, references=references)


def _is_int(value: object) -> bool:
    return type(value) is int


def _operation_id(
    *,
    order_id: str,
    unit_index: int,
    operation_template_id: str,
) -> str:
    identity = canonical_json_bytes(
        {
            "namespace": "shopfloor-pilot-operation-v1",
            "order_id": order_id,
            "unit_index": unit_index,
            "operation_template_id": operation_template_id,
        },
        exclude_wall_clock=False,
    )
    return f"op-{hashlib.sha256(identity).hexdigest()[:40]}"


def _request_data(request: Order | Mapping[str, object]) -> Mapping[str, object]:
    if isinstance(request, Order):
        return request.model_dump(mode="python")
    return request


def _validate_scalar_fields(
    data: Mapping[str, object],
    products: Mapping[str, ProductTemplate],
) -> tuple[list[RouteIssue], ProductTemplate | None]:
    failures: list[RouteIssue] = []
    extra_fields = sorted(str(key) for key in data if key not in _ALLOWED_FIELDS)
    for field in extra_fields:
        failures.append(
            _issue(
                RouteIssueCode.UNSUPPORTED_REQUEST_FIELD,
                field,
                "field is not part of the structured process-route request",
            )
        )

    order_id = data.get("order_id")
    if order_id is None:
        failures.append(_issue(RouteIssueCode.MISSING_ORDER_ID, "order_id", "order_id is required"))
    elif not isinstance(order_id, str) or _IDENTIFIER_PATTERN.fullmatch(order_id) is None:
        failures.append(
            _issue(
                RouteIssueCode.INVALID_ORDER_ID,
                "order_id",
                "order_id must match the stable identifier contract",
            )
        )

    product: ProductTemplate | None = None
    product_id = data.get("product_id")
    if product_id is None:
        failures.append(
            _issue(RouteIssueCode.MISSING_PRODUCT, "product_id", "product_id is required")
        )
    elif not isinstance(product_id, str) or product_id not in products:
        failures.append(
            _issue(
                RouteIssueCode.UNSUPPORTED_PRODUCT,
                "product_id",
                "product_id is not in the finite supported catalog",
                product_id=str(product_id),
            )
        )
    else:
        product = products[product_id]

    quantity = data.get("quantity")
    if quantity is None:
        failures.append(_issue(RouteIssueCode.MISSING_QUANTITY, "quantity", "quantity is required"))
    elif not _is_int(quantity) or not 1 <= quantity <= 10:
        failures.append(
            _issue(
                RouteIssueCode.INVALID_QUANTITY,
                "quantity",
                "quantity must be an integer in the inclusive range 1..10",
            )
        )

    release = data.get("release_time_seconds")
    if release is None:
        failures.append(
            _issue(
                RouteIssueCode.MISSING_RELEASE_TIME,
                "release_time_seconds",
                "release_time_seconds is required",
            )
        )
    elif not _is_int(release) or release < 0:
        failures.append(
            _issue(
                RouteIssueCode.INVALID_RELEASE_TIME,
                "release_time_seconds",
                "release_time_seconds must be a non-negative integer",
            )
        )

    due = data.get("due_time_seconds")
    if due is None:
        failures.append(
            _issue(
                RouteIssueCode.MISSING_DUE_TIME,
                "due_time_seconds",
                "due_time_seconds is required",
            )
        )
    elif not _is_int(due) or due < 0:
        failures.append(
            _issue(
                RouteIssueCode.INVALID_DUE_TIME,
                "due_time_seconds",
                "due_time_seconds must be a non-negative integer",
            )
        )
    elif _is_int(release) and release >= 0 and due < release:
        failures.append(
            _issue(
                RouteIssueCode.INVALID_DUE_TIME,
                "due_time_seconds",
                "due_time_seconds must not precede release_time_seconds",
            )
        )

    priority = data.get("priority_weight")
    if priority is None:
        failures.append(
            _issue(
                RouteIssueCode.MISSING_PRIORITY,
                "priority_weight",
                "priority_weight is required",
            )
        )
    elif not _is_int(priority) or not 1 <= priority <= 100:
        failures.append(
            _issue(
                RouteIssueCode.INVALID_PRIORITY,
                "priority_weight",
                "priority_weight must be an integer in the inclusive range 1..100",
            )
        )

    status = data.get("status", OrderStatus.PENDING)
    if isinstance(status, str):
        try:
            OrderStatus(status)
        except ValueError:
            failures.append(
                _issue(
                    RouteIssueCode.INVALID_ORDER_STATUS,
                    "status",
                    "status must be a supported order lifecycle value",
                )
            )
    elif not isinstance(status, OrderStatus):
        failures.append(
            _issue(
                RouteIssueCode.INVALID_ORDER_STATUS,
                "status",
                "status must be a supported order lifecycle value",
            )
        )

    requested_route_id = data.get("requested_route_id")
    if requested_route_id is not None and (
        product is None
        or not isinstance(requested_route_id, str)
        or requested_route_id != product.route_id
    ):
        failures.append(
            _issue(
                RouteIssueCode.ROUTE_MISMATCH,
                "requested_route_id",
                "requested_route_id must exactly match the predefined product route",
                requested_route_id=str(requested_route_id),
                expected_route_id=product.route_id if product is not None else "unknown",
            )
        )
    return failures, product


def _validate_operation_parameters(
    raw_parameters: object,
    product: ProductTemplate,
) -> list[RouteIssue]:
    if raw_parameters is None:
        return []
    if isinstance(raw_parameters, (str, bytes, bytearray)) or not isinstance(
        raw_parameters, Sequence
    ):
        return [
            _issue(
                RouteIssueCode.INVALID_ROUTE_PARAMETERS,
                "operation_parameters",
                "operation_parameters must be a sequence of structured parameter records",
            )
        ]

    failures: list[RouteIssue] = []
    by_id: dict[str, Mapping[str, object]] = {}
    for index, raw in enumerate(raw_parameters):
        field = f"operation_parameters[{index}]"
        if not isinstance(raw, Mapping):
            failures.append(
                _issue(
                    RouteIssueCode.INVALID_ROUTE_PARAMETERS,
                    field,
                    "route parameter must be a mapping",
                )
            )
            continue
        extras = sorted(str(key) for key in raw if key not in _PARAMETER_FIELDS)
        if extras:
            failures.append(
                _issue(
                    RouteIssueCode.INVALID_ROUTE_PARAMETERS,
                    field,
                    "route parameter contains unsupported fields",
                    unsupported_fields=",".join(extras),
                )
            )
        template_id = raw.get("operation_template_id")
        if not isinstance(template_id, str):
            failures.append(
                _issue(
                    RouteIssueCode.INVALID_ROUTE_PARAMETERS,
                    f"{field}.operation_template_id",
                    "operation_template_id is required and must be a string",
                )
            )
            continue
        if template_id in by_id:
            failures.append(
                _issue(
                    RouteIssueCode.DUPLICATE_ROUTE_PARAMETER,
                    f"{field}.operation_template_id",
                    "operation_template_id appears more than once",
                    operation_template_id=template_id,
                )
            )
            continue
        by_id[template_id] = raw

    templates = {item.operation_template_id: item for item in product.operations}
    for template in product.operations:
        raw = by_id.get(template.operation_template_id)
        if raw is None:
            failures.append(
                _issue(
                    RouteIssueCode.MISSING_ROUTE_PARAMETER,
                    "operation_parameters",
                    "an explicit parameter set must cover every predefined operation",
                    operation_template_id=template.operation_template_id,
                )
            )
            continue
        field = f"operation_parameters.{template.operation_template_id}"
        capability = raw.get("required_capability")
        known_capability: Capability | None = None
        if isinstance(capability, Capability):
            known_capability = capability
        elif isinstance(capability, str):
            try:
                known_capability = Capability(capability)
            except ValueError:
                pass
        if known_capability is None:
            failures.append(
                _issue(
                    RouteIssueCode.UNKNOWN_CAPABILITY,
                    f"{field}.required_capability",
                    "required_capability is not in the supported capability vocabulary",
                    operation_template_id=template.operation_template_id,
                    required_capability=str(capability),
                )
            )
        elif known_capability is not template.required_capability:
            failures.append(
                _issue(
                    RouteIssueCode.CAPABILITY_MISMATCH,
                    f"{field}.required_capability",
                    "capability cannot override the predefined route",
                    operation_template_id=template.operation_template_id,
                    expected_capability=template.required_capability.value,
                    actual_capability=known_capability.value,
                )
            )

        duration = raw.get("duration_seconds")
        if not _is_int(duration) or duration <= 0:
            failures.append(
                _issue(
                    RouteIssueCode.INVALID_DURATION,
                    f"{field}.duration_seconds",
                    "duration_seconds must be a positive integer",
                    operation_template_id=template.operation_template_id,
                )
            )
        elif duration != template.duration_seconds:
            failures.append(
                _issue(
                    RouteIssueCode.DURATION_MISMATCH,
                    f"{field}.duration_seconds",
                    "duration cannot override the predefined route",
                    operation_template_id=template.operation_template_id,
                    expected_duration_seconds=str(template.duration_seconds),
                    actual_duration_seconds=str(duration),
                )
            )

    for unknown_id in sorted(set(by_id) - set(templates)):
        failures.append(
            _issue(
                RouteIssueCode.UNKNOWN_ROUTE_PARAMETER,
                "operation_parameters",
                "operation parameter is not part of the predefined product route",
                operation_template_id=unknown_id,
            )
        )
    return failures


def _failure_status(failures: Sequence[RouteIssue]) -> ResultStatus:
    clarification_codes = {
        RouteIssueCode.MISSING_ORDER_ID,
        RouteIssueCode.MISSING_PRODUCT,
        RouteIssueCode.MISSING_QUANTITY,
        RouteIssueCode.MISSING_RELEASE_TIME,
        RouteIssueCode.MISSING_DUE_TIME,
        RouteIssueCode.MISSING_PRIORITY,
    }
    if all(item.code in clarification_codes for item in failures):
        return ResultStatus.CLARIFICATION_REQUIRED
    return ResultStatus.REJECTED


def expand_process_route(
    request: Order | Mapping[str, object],
    catalog: DomainCatalog,
) -> RouteExpansionResult:
    """Validate and expand one structured request without mutating either input.

    The catalog is authoritative. Explicit operation parameters are assertions of the
    selected predefined route, never overrides. Natural-language interpretation is
    intentionally outside this boundary.
    """

    if not isinstance(request, (Order, Mapping)):
        failure = _issue(
            RouteIssueCode.INVALID_ROUTE_PARAMETERS,
            "request",
            "request must be an Order or a structured mapping",
        )
        return RouteExpansionResult(status=ResultStatus.REJECTED, failures=(failure,))

    data = _request_data(request)
    products = {item.product_id: item for item in catalog.products}
    failures, product = _validate_scalar_fields(data, products)
    if product is not None:
        failures.extend(_validate_operation_parameters(data.get("operation_parameters"), product))
    if failures:
        return RouteExpansionResult(
            status=_failure_status(failures),
            failures=tuple(failures),
        )

    status = data.get("status", OrderStatus.PENDING)
    order = Order(
        order_id=data["order_id"],
        product_id=data["product_id"],
        quantity=data["quantity"],
        release_time_seconds=data["release_time_seconds"],
        due_time_seconds=data["due_time_seconds"],
        priority_weight=data["priority_weight"],
        status=OrderStatus(status) if isinstance(status, str) else status,
    )
    assert product is not None
    operations: list[Operation] = []
    for unit_index in range(1, order.quantity + 1):
        predecessor_id: str | None = None
        for template in product.operations:
            operation_id = _operation_id(
                order_id=order.order_id,
                unit_index=unit_index,
                operation_template_id=template.operation_template_id,
            )
            operations.append(
                Operation(
                    operation_id=operation_id,
                    order_id=order.order_id,
                    unit_index=unit_index,
                    operation_template_id=template.operation_template_id,
                    sequence_index=template.sequence_index,
                    required_capability=template.required_capability,
                    duration_seconds=template.duration_seconds,
                    predecessor_operation_ids=(predecessor_id,) if predecessor_id else (),
                    status=OperationStatus.PENDING,
                )
            )
            predecessor_id = operation_id
    return RouteExpansionResult(
        status=ResultStatus.SUCCESS,
        route_id=product.route_id,
        order=order,
        operations=tuple(operations),
    )
