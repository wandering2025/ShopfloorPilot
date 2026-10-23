"""Catalog fixture for finite process-route tests."""

from __future__ import annotations

import pytest

from shopfloor_pilot.domain.fixtures import ScenarioBundle, load_scenario
from shopfloor_pilot.domain.models import DomainCatalog


@pytest.fixture(scope="session")
def normal_bundle() -> ScenarioBundle:
    return load_scenario("normal")


@pytest.fixture(scope="session")
def catalog(normal_bundle: ScenarioBundle) -> DomainCatalog:
    return normal_bundle.catalog
