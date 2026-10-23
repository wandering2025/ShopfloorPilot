"""S09-A09: explicit live readiness gate; skipped unless deliberately enabled."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from shopfloor_pilot.providers.config import load_paid_run_settings
from shopfloor_pilot.providers.errors import ConfigurationError
from shopfloor_pilot.providers.models import (
    MessageRole,
    ProviderMessage,
    ProviderResponse,
    ProviderToolCall,
    TokenUsage,
)
from shopfloor_pilot.providers.readiness import _live_check, run_provider_check


def _paid_environment() -> dict[str, str]:
    return {
        "DEEPSEEK_API_KEY": "dummy-readiness-api-key",
        "DEEPSEEK_BASE_URL": "https://api.deepseek.com",
        "DEEPSEEK_MODEL": "deepseek-flash",
        "DEEPSEEK_THINKING": "disabled",
        "SHOPFLOOR_PROVIDER_CURRENCY": "USD",
        "SHOPFLOOR_PROVIDER_WORKFLOW_BUDGET": "0.01",
        "SHOPFLOOR_PROVIDER_RUN_BUDGET": "0.05",
        "SHOPFLOOR_PROVIDER_INPUT_CACHE_HIT_RATE": "0.006",
        "SHOPFLOOR_PROVIDER_INPUT_CACHE_MISS_RATE": "0.30",
        "SHOPFLOOR_PROVIDER_OUTPUT_RATE": "1.20",
        "SHOPFLOOR_PROVIDER_TARIFF_DATE": "2026-10-21",
        "SHOPFLOOR_PROVIDER_TARIFF_SOURCE": ("https://api-docs.deepseek.com/quick_start/pricing"),
    }


class _StubDeepSeekProvider:
    provider_name = "deepseek"

    def __init__(self, responses: list[ProviderResponse]) -> None:
        self._responses = list(responses)

    def complete(self, *_args: object, **_kwargs: object) -> ProviderResponse:
        return self._responses.pop(0)

    def close(self) -> None:
        return None


def _response(
    *,
    content: str | None = None,
    tool_call: bool = False,
    usage: TokenUsage | None,
    provider: str = "deepseek",
    model: str = "deepseek-flash",
) -> ProviderResponse:
    calls = (
        (
            ProviderToolCall(
                id="call-readiness-test",
                name="fixture_machine_status",
                arguments_json='{"machine_id":"M1"}',
            ),
        )
        if tool_call
        else ()
    )
    return ProviderResponse(
        message=ProviderMessage(
            role=MessageRole.ASSISTANT,
            content=content,
            tool_calls=calls,
        ),
        model=model,
        provider=provider,
        finish_reason="tool_calls" if tool_call else "stop",
        usage=usage,
    )


def test_live_check_fails_when_either_turn_omits_usage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    usage = TokenUsage(prompt_tokens=20, completion_tokens=5, total_tokens=25)
    stub = _StubDeepSeekProvider(
        [
            _response(tool_call=True, usage=usage),
            _response(content="M1 is idle.", usage=None),
        ]
    )
    import shopfloor_pilot.providers.readiness as readiness

    monkeypatch.setattr(readiness, "DeepSeekProvider", lambda _settings: stub)
    outcome = _live_check(load_paid_run_settings(_paid_environment()), tmp_path)
    assert outcome.status == "FAILED"
    assert outcome.report["live_readiness"] is False
    assert outcome.report["error"]["code"] == "invalid_response"
    assert outcome.report["checks"]["first_usage_reported"] is True
    assert "second_usage_reported" not in outcome.report["checks"]


@pytest.mark.parametrize(
    ("provider", "model"),
    (("not-deepseek", "deepseek-flash"), ("deepseek", "different-model")),
)
def test_live_check_fails_on_response_identity_mismatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
    model: str,
) -> None:
    usage = TokenUsage(prompt_tokens=20, completion_tokens=5, total_tokens=25)
    stub = _StubDeepSeekProvider(
        [_response(tool_call=True, usage=usage, provider=provider, model=model)]
    )
    import shopfloor_pilot.providers.readiness as readiness

    monkeypatch.setattr(readiness, "DeepSeekProvider", lambda _settings: stub)
    outcome = _live_check(load_paid_run_settings(_paid_environment()), tmp_path)
    assert outcome.status == "FAILED"
    assert outcome.report["live_readiness"] is False
    assert outcome.report["error"]["code"] == "invalid_response"


@pytest.mark.live
def test_live_readiness_gate(tmp_path: Path) -> None:
    if os.environ.get("SHOPFLOOR_RUN_LIVE") != "1":
        pytest.skip("set SHOPFLOOR_RUN_LIVE=1 and configure a paid run to enable")
    try:
        load_paid_run_settings(os.environ)
    except ConfigurationError as exc:
        pytest.skip(f"live prerequisites are absent: {exc.message}")
    outcome = run_provider_check(live=True, output=tmp_path)
    assert outcome.status == "PASS", json.dumps(outcome.report, indent=2)
    report = json.loads((tmp_path / "readiness.json").read_text(encoding="utf-8"))
    assert report["live_readiness"] is True
    assert report["attempts"] <= 8
    assert report["tool_executions"] == 1
    assert report["provider"] == "deepseek"
    assert report["model"] == os.environ["DEEPSEEK_MODEL"]
    assert report["usage_known"] is True
    assert report["checks"]["first_usage_reported"] is True
    assert report["checks"]["second_usage_reported"] is True
    assert report["checks"]["first_response_identity_valid"] is True
    assert report["checks"]["second_response_identity_valid"] is True
