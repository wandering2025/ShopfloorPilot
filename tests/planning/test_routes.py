"""S03-A01/A02/A10 finite route expansion acceptance tests."""

from __future__ import annotations

from copy import deepcopy

import pytest

from shopfloor_pilot.domain.canonical import canonical_hash, canonical_json_bytes
from shopfloor_pilot.domain.models import Capability, DomainCatalog, Order, ResultStatus
from shopfloor_pilot.planning import RouteIssueCode, expand_process_route


def _request(**updates: object) -> dict[str, object]:
    request: dict[str, object] = {
        "order_id": "order-route-test",
        "product_id": "prod_aluminum_mounting_plate",
        "quantity": 1,
        "release_time_seconds": 10,
        "due_time_seconds": 1000,
        "priority_weight": 3,
    }
    request.update(updates)
    return request


@pytest.mark.parametrize(
    ("product_id", "quantity", "expected_capabilities", "expected_durations"),
    [
        (
            "prod_aluminum_mounting_plate",
            2,
            (Capability.MILLING, Capability.DRILLING, Capability.INSPECTION),
            (900, 420, 180),
        ),
        (
            "prod_steel_bracket",
            1,
            (Capability.SLOTTING, Capability.DRILLING, Capability.INSPECTION),
            (720, 360, 180),
        ),
        (
            "prod_actuator_base",
            1,
            (Capability.MILLING, Capability.TAPPING, Capability.INSPECTION),
            (1080, 480, 240),
        ),
    ],
)
def test_a01_each_supported_product_expands_to_stable_complete_unit_chains(
    catalog: DomainCatalog,
    product_id: str,
    quantity: int,
    expected_capabilities: tuple[Capability, ...],
    expected_durations: tuple[int, ...],
) -> None:
    request = _request(product_id=product_id, quantity=quantity)
    first = expand_process_route(request, catalog)
    second = expand_process_route(deepcopy(request), catalog)

    assert first.status is ResultStatus.SUCCESS
    assert first == second
    assert canonical_json_bytes(first) == canonical_json_bytes(second)
    assert len(first.operations) == quantity * len(expected_capabilities)
    assert len({operation.operation_id for operation in first.operations}) == len(first.operations)
    for unit_index in range(1, quantity + 1):
        unit = tuple(item for item in first.operations if item.unit_index == unit_index)
        assert tuple(item.sequence_index for item in unit) == tuple(
            range(len(expected_capabilities))
        )
        assert tuple(item.required_capability for item in unit) == expected_capabilities
        assert tuple(item.duration_seconds for item in unit) == expected_durations
        assert unit[0].predecessor_operation_ids == ()
        for predecessor, operation in zip(unit, unit[1:], strict=False):
            assert operation.predecessor_operation_ids == (predecessor.operation_id,)
        assert unit[-1].required_capability is Capability.INSPECTION


def test_a01_matching_explicit_route_parameters_are_assertions_not_overrides(
    catalog: DomainCatalog,
) -> None:
    product = next(
        item for item in catalog.products if item.product_id == "prod_aluminum_mounting_plate"
    )
    parameters = [
        {
            "operation_template_id": item.operation_template_id,
            "required_capability": item.required_capability.value,
            "duration_seconds": item.duration_seconds,
        }
        for item in product.operations
    ]
    result = expand_process_route(
        _request(requested_route_id=product.route_id, operation_parameters=parameters),
        catalog,
    )
    assert result.status is ResultStatus.SUCCESS
    assert result.route_id == product.route_id


@pytest.mark.parametrize(
    ("updates", "expected_code"),
    [
        ({"product_id": "prod_not_supported"}, RouteIssueCode.UNSUPPORTED_PRODUCT),
        ({"quantity": 0}, RouteIssueCode.INVALID_QUANTITY),
        ({"quantity": True}, RouteIssueCode.INVALID_QUANTITY),
        ({"quantity": 11}, RouteIssueCode.INVALID_QUANTITY),
        ({"priority_weight": 0}, RouteIssueCode.INVALID_PRIORITY),
        ({"release_time_seconds": -1}, RouteIssueCode.INVALID_RELEASE_TIME),
        ({"due_time_seconds": 9}, RouteIssueCode.INVALID_DUE_TIME),
        ({"requested_route_id": "route_wrong"}, RouteIssueCode.ROUTE_MISMATCH),
        ({"free_text": "please invent a route"}, RouteIssueCode.UNSUPPORTED_REQUEST_FIELD),
    ],
)
def test_a02_invalid_structured_values_are_rejected_without_a_route(
    catalog: DomainCatalog,
    updates: dict[str, object],
    expected_code: RouteIssueCode,
) -> None:
    before = canonical_hash(catalog)
    result = expand_process_route(_request(**updates), catalog)
    assert result.status is ResultStatus.REJECTED
    assert expected_code in {failure.code for failure in result.failures}
    assert result.route_id is None
    assert result.order is None
    assert result.operations == ()
    assert canonical_hash(catalog) == before


@pytest.mark.parametrize(
    ("missing_field", "expected_code"),
    [
        ("order_id", RouteIssueCode.MISSING_ORDER_ID),
        ("product_id", RouteIssueCode.MISSING_PRODUCT),
        ("quantity", RouteIssueCode.MISSING_QUANTITY),
        ("release_time_seconds", RouteIssueCode.MISSING_RELEASE_TIME),
        ("due_time_seconds", RouteIssueCode.MISSING_DUE_TIME),
        ("priority_weight", RouteIssueCode.MISSING_PRIORITY),
    ],
)
def test_a02_missing_required_fields_request_clarification(
    catalog: DomainCatalog,
    missing_field: str,
    expected_code: RouteIssueCode,
) -> None:
    request = _request()
    del request[missing_field]
    result = expand_process_route(request, catalog)
    assert result.status is ResultStatus.CLARIFICATION_REQUIRED
    assert tuple(failure.code for failure in result.failures) == (expected_code,)
    assert result.operations == ()


def test_a02_capability_duration_and_parameter_coverage_cannot_change_route(
    catalog: DomainCatalog,
) -> None:
    base = [
        {
            "operation_template_id": "plate-mill",
            "required_capability": "milling",
            "duration_seconds": 900,
        },
        {
            "operation_template_id": "plate-drill",
            "required_capability": "drilling",
            "duration_seconds": 420,
        },
        {
            "operation_template_id": "plate-inspect",
            "required_capability": "inspection",
            "duration_seconds": 180,
        },
    ]

    unknown_capability = deepcopy(base)
    unknown_capability[0]["required_capability"] = "laser_cutting"
    mismatch = deepcopy(base)
    mismatch[0]["required_capability"] = "drilling"
    wrong_duration = deepcopy(base)
    wrong_duration[0]["duration_seconds"] = 901
    impossible_duration = deepcopy(base)
    impossible_duration[0]["duration_seconds"] = 0
    incomplete = deepcopy(base[:-1])
    extra = deepcopy(base)
    extra.append(
        {
            "operation_template_id": "invented-op",
            "required_capability": "milling",
            "duration_seconds": 1,
        }
    )

    cases = (
        (unknown_capability, RouteIssueCode.UNKNOWN_CAPABILITY),
        (mismatch, RouteIssueCode.CAPABILITY_MISMATCH),
        (wrong_duration, RouteIssueCode.DURATION_MISMATCH),
        (impossible_duration, RouteIssueCode.INVALID_DURATION),
        (incomplete, RouteIssueCode.MISSING_ROUTE_PARAMETER),
        (extra, RouteIssueCode.UNKNOWN_ROUTE_PARAMETER),
    )
    for parameters, expected_code in cases:
        result = expand_process_route(_request(operation_parameters=parameters), catalog)
        assert result.status is ResultStatus.REJECTED
        assert expected_code in {failure.code for failure in result.failures}
        assert result.operations == ()


def test_a10_order_and_mapping_inputs_remain_unchanged_and_repeatable(
    catalog: DomainCatalog,
) -> None:
    request = _request(quantity=2)
    raw_before = canonical_hash(request)
    catalog_before = canonical_hash(catalog)
    mapping_results = tuple(expand_process_route(request, catalog) for _ in range(3))
    assert mapping_results[0] == mapping_results[1] == mapping_results[2]
    assert canonical_hash(request) == raw_before
    assert canonical_hash(catalog) == catalog_before

    order = mapping_results[0].order
    assert isinstance(order, Order)
    order_before = canonical_hash(order)
    order_results = tuple(expand_process_route(order, catalog) for _ in range(3))
    assert order_results[0] == order_results[1] == order_results[2]
    assert canonical_hash(order) == order_before
    assert order_results[0].operations == mapping_results[0].operations
