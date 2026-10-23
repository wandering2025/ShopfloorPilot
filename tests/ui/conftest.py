from __future__ import annotations

from collections.abc import Iterator

import pytest

from shopfloor_pilot.ui import DashboardController


@pytest.fixture
def run_id_factory() -> Iterator[object]:
    counter = iter(range(1, 100))
    yield lambda scenario_id: f"ui-{scenario_id}-{next(counter):03d}"


@pytest.fixture
def controller(tmp_path, run_id_factory) -> DashboardController:
    return DashboardController(tmp_path / "dashboard", run_id_factory=run_id_factory)
