"""Shared typed scenario fixtures for S01 acceptance tests."""

from __future__ import annotations

import pytest

from shopfloor_pilot.domain.fixtures import ScenarioBundle, load_all_scenarios


@pytest.fixture(scope="session")
def bundles() -> dict[str, ScenarioBundle]:
    return {bundle.scenario.scenario_id: bundle for bundle in load_all_scenarios()}
