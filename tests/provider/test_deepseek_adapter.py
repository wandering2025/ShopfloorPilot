"""DeepSeek adapter transport mapping and response parsing tests."""

from __future__ import annotations

from types import SimpleNamespace

import httpx2
import pytest

from shopfloor_pilot.providers.config import DeepSeekSettings
from shopfloor_pilot.providers.deepseek import DeepSeekProvider
from shopfloor_pilot.providers.errors import (
    ConfigurationError,
    ProviderError,
    ProviderErrorCode,
    ProviderResponseValidationError,
)
from shopfloor_pilot.providers.models import (
    MessageRole,
    ProviderMessage,
    ProviderRequest,
    TokenUsage,
    ToolChoice,
    ToolSchema,
)


def _settings(*, thinking: bool = False, limits=None) -> DeepSeekSettings:
    from shopfloor_pilot.budgets import ProviderLimits

    return DeepSeekSettings(
        api_key="dummy-s09-test-key",
        base_url="https://api.deepseek.com",
        model="deepseek-flash",
        thinking=thinking,
        reasoning_effort="high" if thinking else None,
        limits=limits or ProviderLimits(),
    )


class _Completions:
    def __init__(self, outcome: object | Exception) -> None:
        self.outcome = outcome
        self.kwargs: dict[str, object] | None = None
        self.calls = 0

    def create(self, **kwargs: object) -> object:
        self.calls += 1
        self.kwargs = kwargs
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


class _Client:
    def __init__(self, outcome: object | Exception) -> None:
        self.completions = _Completions(outcome)
        self.chat = SimpleNamespace(completions=self.completions)
        self.closed = False

    def close(self) -> None:
        self.closed = True


def _completion_dict(
    *,
    content: str | None = "done",
    tool_calls: list[dict[str, object]] | None = None,
    usage: object = None,
    model: str = "deepseek-flash",
    reasoning: str | None = None,
) -> dict[str, object]:
    message: dict[str, object] = {"role": "assistant", "content": content}
    if reasoning is not None:
        message["reasoning_content"] = reasoning
    if tool_calls is not None:
        message["tool_calls"] = tool_calls
    choice: dict[str, object] = {
        "message": message,
        "finish_reason": "tool_calls" if tool_calls else "stop",
        "index": 0,
    }
    return {
        "id": "completion-1",
        "choices": [choice],
        "model": model,
        "usage": usage,
    }


def test_adapter_parses_tool_call_and_usage() -> None:
    usage = {
        "prompt_tokens": 100,
        "completion_tokens": 20,
        "total_tokens": 120,
        "prompt_cache_hit_tokens": 40,
        "prompt_cache_miss_tokens": 60,
    }
    client = _Client(
        _completion_dict(
            content=None,
            tool_calls=[
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {
                        "name": "fixture_machine_status",
                        "arguments": '{"machine_id":"M1"}',
                    },
                }
            ],
            usage=usage,
        )
    )
    provider = DeepSeekProvider(_settings(), client=client)
    request = ProviderRequest(
        messages=(ProviderMessage(role=MessageRole.USER, content="check"),),
        tools=(
            ToolSchema(
                name="fixture_machine_status",
                description="status",
                parameters={
                    "type": "object",
                    "properties": {"machine_id": {"type": "string"}},
                    "required": ["machine_id"],
                    "additionalProperties": False,
                },
            ),
        ),
        max_output_tokens=64,
        tool_choice=ToolChoice.REQUIRED,
    )
    response = provider.complete(request, timeout_seconds=12.5)
    assert response.usage == TokenUsage(100, 20, 120, 40, 60)
    assert response.message.tool_calls[0].id == "call-1"
    assert response.finish_reason == "tool_calls"
    assert client.completions.kwargs is not None
    kwargs = client.completions.kwargs
    assert kwargs["max_tokens"] == 64
    assert kwargs["stream"] is False
    assert kwargs["extra_body"] == {"thinking": {"type": "disabled"}}
    assert kwargs["timeout"] == 12.5
    assert "thinking" not in kwargs


def test_adapter_preserves_reasoning_only_in_wire_projection() -> None:
    client = _Client(_completion_dict(content="answer", reasoning="hidden"))
    provider = DeepSeekProvider(_settings(thinking=True), client=client)
    request = ProviderRequest(
        messages=(ProviderMessage(role=MessageRole.USER, content="hi"),),
        max_output_tokens=16,
    )
    response = provider.complete(request, timeout_seconds=5)
    assert response.message.reasoning == "hidden"
    assert response.message.to_wire()["reasoning_content"] == "hidden"
    assert "reasoning_content" not in response.message.public_dict()


def test_forced_tool_choice_with_thinking_is_rejected_before_request() -> None:
    client = _Client(_completion_dict())
    provider = DeepSeekProvider(_settings(thinking=True), client=client)
    request = ProviderRequest(
        messages=(ProviderMessage(role=MessageRole.USER, content="hi"),),
        tools=(
            ToolSchema(
                name="fixture",
                description="fixture",
                parameters={
                    "type": "object",
                    "properties": {},
                    "additionalProperties": False,
                },
            ),
        ),
        max_output_tokens=16,
        tool_choice=ToolChoice.REQUIRED,
    )
    with pytest.raises(ConfigurationError):
        provider.complete(request, timeout_seconds=5)
    assert client.completions.calls == 0


@pytest.mark.parametrize(
    ("status", "code", "retryable"),
    [
        (401, ProviderErrorCode.AUTHENTICATION, False),
        (402, ProviderErrorCode.INSUFFICIENT_BALANCE, False),
        (400, ProviderErrorCode.INVALID_REQUEST, False),
        (422, ProviderErrorCode.INVALID_REQUEST, False),
        (429, ProviderErrorCode.RATE_LIMITED, True),
        (500, ProviderErrorCode.SERVER_ERROR, True),
        (503, ProviderErrorCode.SERVER_ERROR, True),
    ],
)
def test_status_errors_are_normalized_and_redacted(
    status: int, code: ProviderErrorCode, retryable: bool
) -> None:
    request = httpx2.Request("POST", "https://api.deepseek.com/chat/completions")
    response = httpx2.Response(
        status,
        request=request,
        headers={"retry-after": "5"} if status == 429 else None,
    )
    # Compose the sentinel at runtime so the repository secret scanner does not
    # treat a dummy test value as a leaked production credential.
    fake_key = "sk-" + "leakedcredentialmarker123456"
    # Build the SDK exception type without relying on private request internals.
    from openai import APIStatusError

    class FakeStatusError(APIStatusError):
        pass

    exc = FakeStatusError(
        f"server said {fake_key} and status {status}",
        response=response,
        body={"error": {"message": "boom"}},
    )
    client = _Client(exc)
    provider = DeepSeekProvider(_settings(), client=client)
    req = ProviderRequest(
        messages=(ProviderMessage(role=MessageRole.USER, content="hi"),),
        max_output_tokens=16,
    )
    with pytest.raises(ProviderError) as raised:
        provider.complete(req, timeout_seconds=5)
    assert raised.value.code is code
    assert raised.value.retryable is retryable
    assert raised.value.status_code == status
    assert fake_key not in raised.value.message
    assert fake_key not in str(raised.value)
    if status == 429:
        assert raised.value.retry_after_seconds == 5.0


def test_transport_timeout_and_connection_errors_are_transient() -> None:
    from openai import APIConnectionError, APITimeoutError

    http_request = httpx2.Request("POST", "https://api.deepseek.com/chat/completions")
    request = ProviderRequest(
        messages=(ProviderMessage(role=MessageRole.USER, content="hi"),),
        max_output_tokens=16,
    )
    for exception, expected in (
        (APITimeoutError(http_request), ProviderErrorCode.TIMEOUT),
        (APIConnectionError(request=http_request), ProviderErrorCode.CONNECTION),
    ):
        provider = DeepSeekProvider(_settings(), client=_Client(exception))
        with pytest.raises(ProviderError) as raised:
            provider.complete(request, timeout_seconds=5)
        assert raised.value.code is expected
        assert raised.value.retryable is True


def test_missing_usage_is_supported() -> None:
    client = _Client(_completion_dict(content="no usage", usage=None))
    provider = DeepSeekProvider(_settings(), client=client)
    response = provider.complete(
        ProviderRequest(
            messages=(ProviderMessage(role=MessageRole.USER, content="hi"),),
            max_output_tokens=16,
        ),
        timeout_seconds=5,
    )
    assert response.usage is None


@pytest.mark.parametrize(
    "usage",
    [
        {"prompt_tokens": 10, "completion_tokens": 5},
        {"prompt_tokens": -1, "completion_tokens": 5, "total_tokens": 4},
        {
            "prompt_tokens": 10,
            "completion_tokens": 5,
            "total_tokens": 15,
            "prompt_cache_hit_tokens": 7,
            "prompt_cache_miss_tokens": 7,
        },
    ],
)
def test_malformed_usage_is_rejected(usage: dict[str, int]) -> None:
    client = _Client(_completion_dict(usage=usage))
    provider = DeepSeekProvider(_settings(), client=client)
    with pytest.raises(ProviderResponseValidationError):
        provider.complete(
            ProviderRequest(
                messages=(ProviderMessage(role=MessageRole.USER, content="hi"),),
                max_output_tokens=16,
            ),
            timeout_seconds=5,
        )


def test_unsupported_tool_call_shape_is_rejected() -> None:
    client = _Client(
        _completion_dict(
            content=None,
            tool_calls=[
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {"name": "fixture", "arguments": {"machine_id": "M1"}},
                }
            ],
        )
    )
    provider = DeepSeekProvider(_settings(), client=client)
    with pytest.raises(ProviderResponseValidationError):
        provider.complete(
            ProviderRequest(
                messages=(ProviderMessage(role=MessageRole.USER, content="hi"),),
                tools=(
                    ToolSchema(
                        name="fixture",
                        description="fixture",
                        parameters={
                            "type": "object",
                            "properties": {"machine_id": {"type": "string"}},
                            "required": ["machine_id"],
                            "additionalProperties": False,
                        },
                    ),
                ),
                max_output_tokens=16,
            ),
            timeout_seconds=5,
        )


def test_adapter_close_releases_client() -> None:
    client = _Client(_completion_dict())
    provider = DeepSeekProvider(_settings(), client=client)
    provider.close()
    assert client.closed is True
