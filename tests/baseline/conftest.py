"""Shared no-network executions for the S06 baseline acceptance tests."""

from __future__ import annotations

import hashlib
import os
import socket
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from shopfloor_pilot.baseline import BaselineRunResult, run_baseline
from shopfloor_pilot.domain.fixtures import DEFAULT_SCENARIO_ROOT

SCENARIOS = (
    "normal",
    "urgent_order",
    "machine_breakdown",
    "no_feasible_recovery",
)


class _SteppedWallClock:
    """Deterministic test clock; production defaults to live timezone-aware UTC."""

    def __init__(self) -> None:
        self._next = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        current = self._next
        self._next += timedelta(milliseconds=125)
        return current


def _fixture_hashes() -> dict[str, str]:
    return {
        path.relative_to(DEFAULT_SCENARIO_ROOT).as_posix(): hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
        for path in sorted(DEFAULT_SCENARIO_ROOT.rglob("*"))
        if path.is_file()
    }


@pytest.fixture(scope="session")
def baseline_runs(
    tmp_path_factory: pytest.TempPathFactory,
) -> dict[str, tuple[BaselineRunResult, Path]]:
    """Execute each public command flow once with socket connects forbidden."""

    root = tmp_path_factory.mktemp("s06-baselines")
    before = _fixture_hashes()
    results: dict[str, tuple[BaselineRunResult, Path]] = {}

    def reject_network(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("the deterministic baseline attempted network access")

    with (
        patch.dict(os.environ, {}, clear=False),
        patch.object(socket.socket, "connect", reject_network),
    ):
        os.environ.pop("DEEPSEEK_API_KEY", None)
        for scenario_id in SCENARIOS:
            output = root / scenario_id
            result = run_baseline(
                scenario_id,
                seed=0,
                approval_policy="deterministic",
                output=output,
                wall_clock=_SteppedWallClock(),
            )
            results[scenario_id] = (result, output)

    assert _fixture_hashes() == before
    return results
