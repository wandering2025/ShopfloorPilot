"""S01-A08 and A10 public projection and input-boundary acceptance."""

from __future__ import annotations

from shopfloor_pilot.domain.aliases import resolve_order_request
from shopfloor_pilot.domain.fixtures import (
    PublicScenarioView,
    ScenarioBundle,
    project_public_view,
)
from shopfloor_pilot.domain.models import FailureCode, ResultStatus


def test_a08_public_projection_is_allowlisted_and_hides_truth_and_future_values(
    bundles: dict[str, ScenarioBundle],
) -> None:
    bundle = bundles["machine_breakdown"]
    private_json = bundle.evaluator.model_dump_json()
    assert "EVAL_ONLY_SENTINEL_SPINDLE_BEARING_DAMAGE" in private_json

    before = project_public_view(bundle, 2699)
    after = project_public_view(bundle, 2700)
    assert before.observations == ()
    assert len(after.observations) == 1
    assert set(PublicScenarioView.model_fields) == {
        "schema_version",
        "scenario_id",
        "run_id",
        "simulated_time_seconds",
        "observations",
    }
    for projection in (before.model_dump_json(), after.model_dump_json()):
        assert "EVAL_ONLY_SENTINEL" not in projection
        assert "cause_label" not in projection
        assert "fault_truth" not in projection
        assert "interventions" not in projection
        assert "private_truth_ref" not in projection
        assert "unavailable_until_seconds" not in projection
        assert "evaluator_only" not in projection
    assert "cause" not in after.observations[0].summary.casefold()


def test_a08_no_feasible_projection_does_not_reveal_both_future_faults(
    bundles: dict[str, ScenarioBundle],
) -> None:
    bundle = bundles["no_feasible_recovery"]
    before = project_public_view(bundle, 2999).model_dump_json()
    assert "down" not in before
    assert "CNC02_DRIVE_LOCK" not in before
    assert "CNC03_LUBE_PRESSURE" not in before
    assert "14400" not in before


def test_a10_supported_english_and_chinese_aliases_are_canonical(
    bundles: dict[str, ScenarioBundle],
) -> None:
    clock = bundles["normal"].scenario.clock
    english = resolve_order_request(
        product="Aluminum Mounting Plate", quantity=2, due="today", scenario_clock=clock
    )
    chinese = resolve_order_request(
        product="铝制安装板", quantity=2, due="today", scenario_clock=clock
    )
    historical_short = resolve_order_request(
        product="铝安装板", quantity=2, due="today", scenario_clock=clock
    )
    assert english.status is ResultStatus.SUCCESS
    assert english == chinese == historical_short
    assert english.product_id == "prod_aluminum_mounting_plate"
    assert english.canonical_product_name == "Aluminum Mounting Plate"
    assert english.route_id == "route_aluminum_plate"
    assert english.due_time_seconds == clock.shift_boundary_seconds


def test_a10_today_without_clock_requires_clarification() -> None:
    result = resolve_order_request(
        product="steel bracket", quantity=1, due="today", scenario_clock=None
    )
    assert result.status is ResultStatus.CLARIFICATION_REQUIRED
    assert result.failures[0].code is FailureCode.AMBIGUOUS_TIME


def test_a10_missing_fields_unknown_routes_and_fuzzy_products_are_not_inferred(
    bundles: dict[str, ScenarioBundle],
) -> None:
    clock = bundles["normal"].scenario.clock
    missing = resolve_order_request(product=None, quantity=None, due=100, scenario_clock=clock)
    assert missing.status is ResultStatus.CLARIFICATION_REQUIRED
    assert {item.code for item in missing.failures} == {
        FailureCode.MISSING_PRODUCT,
        FailureCode.MISSING_QUANTITY,
    }

    wrong_route = resolve_order_request(
        product="Actuator Base",
        quantity=1,
        due=100,
        scenario_clock=clock,
        requested_route_id="route-invented",
    )
    assert wrong_route.status is ResultStatus.REJECTED
    assert wrong_route.failures[0].code is FailureCode.UNKNOWN_ROUTE

    fuzzy = resolve_order_request(
        product="please make something vaguely plate-like",
        quantity=1,
        due=100,
        scenario_clock=clock,
    )
    assert fuzzy.status is ResultStatus.REJECTED
    assert fuzzy.failures[0].code is FailureCode.UNSUPPORTED_PRODUCT
