"""Offline provider fixtures and network denial for S09 tests."""

from __future__ import annotations

import os
import socket
from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from shopfloor_pilot.budgets import BudgetLedger, ProviderLimits
from shopfloor_pilot.providers.clock import FakeClock
from shopfloor_pilot.providers.config import DeepSeekSettings
from shopfloor_pilot.providers.costs import TokenRates
from shopfloor_pilot.providers.models import (
    MessageRole,
    ProviderMessage,
    ProviderRequest,
    ToolChoice,
    ToolSchema,
)

TARIFF_URL = "https://api-docs.deepseek.com/quick_start/pricing"


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    del config
    if os.environ.get("SHOPFLOOR_RUN_LIVE") == "1":
        return
    skip_live = pytest.mark.skip(
        reason="live provider test requires SHOPFLOOR_RUN_LIVE=1 and paid settings"
    )
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip_live)


@pytest.fixture(autouse=True)
def deny_network(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every ordinary provider test is credential-free and network-free."""

    if request.node.get_closest_marker("live") is not None:
        return
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY_FILE", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    def blocked(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("S09 provider tests must not access the network")

    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(socket.socket, "connect", blocked)


@pytest.fixture
def limits() -> ProviderLimits:
    return ProviderLimits()


@pytest.fixture
def pricing() -> TokenRates:
    return TokenRates(
        currency="USD",
        input_cache_hit_per_million=Decimal("0.006"),
        input_cache_miss_per_million=Decimal("0.30"),
        output_per_million=Decimal("1.20"),
        tariff_date=date(2026, 10, 21),
        source_url=TARIFF_URL,
        model="deepseek-flash",
    )


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def workflow_id() -> str:
    return "workflow-s09-test"


@pytest.fixture
def run_id() -> str:
    return "run-s09-test"


@pytest.fixture
def ledger(
    tmp_path: Path,
    limits: ProviderLimits,
    workflow_id: str,
    run_id: str,
) -> Iterator[BudgetLedger]:
    instance = BudgetLedger(tmp_path / "provider_ledger.sqlite3")
    instance.open_run(run_id=run_id, currency="USD", cap_micros=50_000)
    instance.open_workflow(
        workflow_id=workflow_id,
        run_id=run_id,
        limits=limits,
        workflow_cap_micros=25_000,
    )
    try:
        yield instance
    finally:
        instance.close()


@pytest.fixture
def settings(limits: ProviderLimits) -> DeepSeekSettings:
    return DeepSeekSettings(
        api_key="dummy-s09-test-key",
        base_url="https://api.deepseek.com",
        model="deepseek-flash",
        thinking=False,
        reasoning_effort=None,
        limits=limits,
    )


@pytest.fixture
def machine_tool() -> ToolSchema:
    return ToolSchema(
        name="fixture_machine_status",
        description="Return the synthetic status of one fixture machine.",
        parameters={
            "type": "object",
            "properties": {
                "machine_id": {
                    "type": "string",
                    "enum": ["M1"],
                    "description": "Fixture machine id.",
                }
            },
            "required": ["machine_id"],
            "additionalProperties": False,
        },
    )


@pytest.fixture
def tool_request(machine_tool: ToolSchema) -> ProviderRequest:
    return ProviderRequest(
        messages=(
            ProviderMessage(role=MessageRole.SYSTEM, content="Readiness probe."),
            ProviderMessage(role=MessageRole.USER, content="Check machine M1."),
        ),
        tools=(machine_tool,),
        max_output_tokens=64,
        tool_choice=ToolChoice.REQUIRED,
    )


@pytest.fixture
def text_request() -> ProviderRequest:
    return ProviderRequest(
        messages=(ProviderMessage(role=MessageRole.USER, content="Say hello."),),
        max_output_tokens=64,
    )
