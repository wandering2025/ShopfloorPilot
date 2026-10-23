"""Public typed domain contract for ShopfloorPilot."""

from .aliases import OrderRequestResolution, resolve_order_request
from .canonical import (
    canonical_data,
    canonical_hash,
    canonical_json_bytes,
    semantic_data,
    semantic_hash,
    semantic_json_bytes,
)
from .fixtures import (
    EvaluatorScenario,
    FixtureManifest,
    PublicScenarioView,
    ScenarioBundle,
    ScenarioDefinition,
    ScenarioExpectation,
    load_all_scenarios,
    load_scenario,
    materialize_initial_snapshot,
    project_public_view,
)
from .models import *  # noqa: F403 - the domain package intentionally re-exports contracts

__all__ = [
    "EvaluatorScenario",
    "FixtureManifest",
    "OrderRequestResolution",
    "PublicScenarioView",
    "ScenarioBundle",
    "ScenarioDefinition",
    "ScenarioExpectation",
    "canonical_data",
    "canonical_hash",
    "canonical_json_bytes",
    "load_all_scenarios",
    "load_scenario",
    "materialize_initial_snapshot",
    "project_public_view",
    "resolve_order_request",
    "semantic_data",
    "semantic_hash",
    "semantic_json_bytes",
]
