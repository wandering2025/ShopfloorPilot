"""S01-A01, A03, A07, and A09 fixture acceptance."""

from __future__ import annotations

import random
import shutil
from pathlib import Path

import pytest

from shopfloor_pilot.domain.aliases import resolve_order_request
from shopfloor_pilot.domain.canonical import canonical_hash
from shopfloor_pilot.domain.fixtures import (
    DEFAULT_SCENARIO_ROOT,
    ScenarioBundle,
    ScenarioOutcome,
    load_all_scenarios,
    load_manifest,
    project_public_view,
)
from shopfloor_pilot.domain.models import Capability, ResourceKind, ResultStatus


def test_a01_manifest_loads_every_typed_fixture_and_records_contract() -> None:
    manifest = load_manifest()
    bundles = load_all_scenarios()

    assert [item.scenario_id for item in manifest.scenarios] == [
        "normal",
        "urgent_order",
        "machine_breakdown",
        "no_feasible_recovery",
    ]
    assert [item.scenario.scenario_id for item in bundles] == [
        item.scenario_id for item in manifest.scenarios
    ]
    assert all(len(item.public_sha256) == 64 for item in manifest.scenarios)
    assert all(len(item.evaluator_sha256) == 64 for item in manifest.scenarios)
    assert all(len(item.expectation_sha256) == 64 for item in manifest.scenarios)
    assert [item.seed for item in manifest.scenarios] == [101, 202, 303, 404]
    assert manifest.units.simulated_time == "integer_seconds"
    assert manifest.units.intervals == "half_open"
    assert manifest.units.quantity == "integer_units_1_to_10_one_route_chain_per_unit"
    assert manifest.units.telemetry == "scaled_integers_with_explicit_units"
    assert [item.expected_outcome for item in manifest.scenarios] == [
        ScenarioOutcome.COMPLETES,
        ScenarioOutcome.REPLAN_REQUIRED,
        ScenarioOutcome.RECOVERY_FEASIBLE,
        ScenarioOutcome.NO_FEASIBLE_RECOVERY,
    ]


def test_a01_only_registered_evaluation_manifests_are_excluded_from_domain_coverage(
    tmp_path: Path,
) -> None:
    scenario_root = tmp_path / "scenarios"
    shutil.copytree(DEFAULT_SCENARIO_ROOT, scenario_root)
    (scenario_root / "unlisted.json").write_text("{}", encoding="utf-8")

    with pytest.raises(ValueError, match=r"unlisted=\['unlisted.json'\]"):
        load_all_scenarios(scenario_root)


def test_a01_ids_are_unique_and_references_resolve(bundles: dict[str, ScenarioBundle]) -> None:
    for bundle in bundles.values():
        catalog = bundle.catalog
        assert len({item.machine_id for item in catalog.machines}) == len(catalog.machines)
        assert len({item.product_id for item in catalog.products}) == len(catalog.products)
        product_ids = {item.product_id for item in catalog.products}
        machine_ids = {item.machine_id for item in catalog.machines}
        assert all(item.product_id in product_ids for item in bundle.scenario.orders)
        assert all(
            item.target_machine_id is None or item.target_machine_id in machine_ids
            for item in bundle.evaluator.interventions
        )


def test_a03_cell_routes_orders_and_alternative_machine(
    bundles: dict[str, ScenarioBundle],
) -> None:
    catalog = bundles["normal"].catalog
    processing = [item for item in catalog.machines if item.kind is ResourceKind.PROCESSING]
    inspection = [item for item in catalog.machines if item.kind is ResourceKind.INSPECTION]

    assert [item.machine_id for item in processing] == ["CNC01", "CNC02", "CNC03"]
    assert [item.machine_id for item in inspection] == ["QC01"]
    assert len(catalog.products) == 3
    assert all(len(bundle.scenario.orders) == 8 for bundle in bundles.values())
    milling_machines = [
        item.machine_id for item in processing if Capability.MILLING in item.capabilities
    ]
    assert milling_machines == ["CNC02", "CNC03"]
    assert all(
        product.operations[-1].required_capability is Capability.INSPECTION
        for product in catalog.products
    )


def test_a03_unsupported_product_has_explicit_rejection(
    bundles: dict[str, ScenarioBundle],
) -> None:
    result = resolve_order_request(
        product="unobtainium turbine",
        quantity=1,
        due=100,
        scenario_clock=bundles["normal"].scenario.clock,
    )
    assert result.status is ResultStatus.REJECTED
    assert result.failures[0].code.value == "unsupported_product"


def _signal_values(view: object) -> tuple[int, ...]:
    return tuple(
        signal.value
        for observation in view.observations  # type: ignore[attr-defined]
        for signal in observation.signals
    )


def test_a07_same_seed_is_deterministic_and_local_rng_isolated(
    bundles: dict[str, ScenarioBundle],
) -> None:
    first_load = load_all_scenarios()
    second_load = load_all_scenarios()
    assert canonical_hash(first_load) == canonical_hash(second_load)

    bundle = bundles["machine_breakdown"]
    at_time = bundle.evaluator.interventions[0].at_seconds
    random.seed(90210)
    global_state = random.getstate()
    first = project_public_view(bundle, at_time)
    second = project_public_view(bundle, at_time)
    assert first == second
    assert random.getstate() == global_state
    assert bundle.evaluator == bundles["machine_breakdown"].evaluator


def test_a07_seed_override_changes_only_documented_signal_values(
    bundles: dict[str, ScenarioBundle],
) -> None:
    bundle = bundles["machine_breakdown"]
    at_time = bundle.evaluator.interventions[0].at_seconds
    first = project_public_view(bundle, at_time, seed_override=303)
    second = project_public_view(bundle, at_time, seed_override=304)

    assert first.scenario_id == second.scenario_id
    assert first.run_id == second.run_id
    assert first.simulated_time_seconds == second.simulated_time_seconds
    assert [item.observation_id for item in first.observations] == [
        item.observation_id for item in second.observations
    ]
    assert [item.machine_id for item in first.observations] == [
        item.machine_id for item in second.observations
    ]
    assert [item.summary for item in first.observations] == [
        item.summary for item in second.observations
    ]
    assert _signal_values(first) != _signal_values(second)
    assert bundle.scenario.orders == bundles["machine_breakdown"].scenario.orders
    assert bundle.evaluator.interventions == bundles["machine_breakdown"].evaluator.interventions


def test_a09_all_scenario_outcomes_have_required_evidence(
    bundles: dict[str, ScenarioBundle],
) -> None:
    assert bundles["normal"].expectation.expected_outcome is ScenarioOutcome.COMPLETES
    assert bundles["urgent_order"].expectation.expected_outcome is ScenarioOutcome.REPLAN_REQUIRED

    breakdown = bundles["machine_breakdown"].expectation
    assert breakdown.expected_outcome is ScenarioOutcome.RECOVERY_FEASIBLE
    assert breakdown.alternate_recovery is not None
    assert breakdown.alternate_recovery.failed_machine_id == "CNC02"
    assert breakdown.alternate_recovery.alternate_machine_id == "CNC03"
    assert breakdown.alternate_recovery.capability is Capability.MILLING
    assert breakdown.alternate_recovery.restart_full_duration_seconds == 900

    impossible = bundles["no_feasible_recovery"].expectation
    assert impossible.expected_outcome is ScenarioOutcome.NO_FEASIBLE_RECOVERY
    assert impossible.hard_contradiction is not None
    assert impossible.hard_contradiction.capability is Capability.MILLING
    assert set(impossible.hard_contradiction.capable_machine_ids) == {"CNC02", "CNC03"}
    assert impossible.hard_contradiction.required_duration_seconds == 900
    assert all(
        window.end_seconds == impossible.hard_contradiction.hard_horizon_seconds
        for window in impossible.hard_contradiction.blocked_windows
    )
    assert "due" in impossible.hard_contradiction.explanation
    assert bundles["machine_breakdown"].evaluator.diagnostic_variant is not None
    assert len(bundles["machine_breakdown"].evaluator.diagnostic_variant.plausible_hypotheses) >= 2
