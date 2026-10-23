"""Explicitly labeled scripted fake provider for offline contract evidence only."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .clock import FakeClock
from .errors import ProviderError
from .models import (
    ProviderMessage,
    ProviderRequest,
    ProviderResponse,
    ProviderToolCall,
    TokenUsage,
    serialize_tool_arguments,
)

FAKE_PROVIDER_LABEL = (
    "OFFLINE CONTRACT EVIDENCE ONLY — ScriptedFakeProvider. "
    "This is not DeepSeek quality evidence and not live-readiness evidence."
)


@dataclass(frozen=True, slots=True)
class FakeReply:
    response: object
    advance_seconds: float = 0.0


@dataclass(frozen=True, slots=True)
class FakeFailure:
    error: ProviderError
    advance_seconds: float = 0.0


FakeStep = FakeReply | FakeFailure


class ScriptedFakeProvider:
    """Deterministic transport that replays scripted replies or failures."""

    provider_name = "scripted-fake"
    label = FAKE_PROVIDER_LABEL

    def __init__(
        self,
        script: Sequence[FakeStep],
        *,
        clock: FakeClock | None = None,
    ) -> None:
        self._script = list(script)
        self._clock = clock
        self.requests: list[ProviderRequest] = []
        self.calls = 0

    def complete(self, request: ProviderRequest, *, timeout_seconds: float) -> ProviderResponse:
        del timeout_seconds
        self.requests.append(request)
        self.calls += 1
        if not self._script:
            raise AssertionError("ScriptedFakeProvider script exhausted")
        step = self._script.pop(0)
        if step.advance_seconds and self._clock is not None:
            self._clock.advance(step.advance_seconds)
        if isinstance(step, FakeFailure):
            raise step.error
        return step.response  # type: ignore[return-value]

    @property
    def remaining_steps(self) -> int:
        return len(self._script)

    def artifact_label(self) -> dict[str, Any]:
        return {
            "provider": self.provider_name,
            "evidence_kind": "offline_contract_evidence",
            "label": self.label,
            "live_readiness": False,
            "calls": self.calls,
        }


def scripted_tool_call(
    *,
    call_id: str,
    tool_name: str,
    arguments: Mapping[str, Any],
    content: str | None = None,
    usage: TokenUsage | None = None,
    reasoning: str | None = None,
    model: str = "scripted-fake-model",
) -> ProviderResponse:
    """Build a valid fake tool-call turn for offline tests."""

    message = ProviderMessage(
        role="assistant",
        content=content,
        tool_calls=(
            ProviderToolCall(
                id=call_id,
                name=tool_name,
                arguments_json=serialize_tool_arguments(arguments),
            ),
        ),
        reasoning=reasoning,
    )
    return ProviderResponse(
        message=message,
        model=model,
        provider=ScriptedFakeProvider.provider_name,
        finish_reason="tool_calls",
        usage=usage,
    )


def scripted_text(
    *,
    content: str,
    usage: TokenUsage | None = None,
    reasoning: str | None = None,
    model: str = "scripted-fake-model",
) -> ProviderResponse:
    """Build a valid fake final-answer turn for offline tests."""

    message = ProviderMessage(role="assistant", content=content, reasoning=reasoning)
    return ProviderResponse(
        message=message,
        model=model,
        provider=ScriptedFakeProvider.provider_name,
        finish_reason="stop",
        usage=usage,
    )
