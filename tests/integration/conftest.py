"""Offline S11 integration fixtures: no credentials, no network, labeled fake only."""

from __future__ import annotations

import os
import socket

import pytest


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    del config
    if os.environ.get("SHOPFLOOR_RUN_LIVE") == "1":
        return
    skip_live = pytest.mark.skip(
        reason="live recovery test requires SHOPFLOOR_RUN_LIVE=1, paid settings and a human"
    )
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip_live)


@pytest.fixture(autouse=True)
def deny_network(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    if request.node.get_closest_marker("live") is not None:
        return
    for name in ("DEEPSEEK_API_KEY", "DEEPSEEK_API_KEY_FILE", "OPENAI_API_KEY"):
        monkeypatch.delenv(name, raising=False)

    def blocked(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("S11 offline integration tests must not access the network")

    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(socket.socket, "connect", blocked)
