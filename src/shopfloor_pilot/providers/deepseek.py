"""DeepSeek adapter over the OpenAI-compatible chat-completions endpoint."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

from openai import APIConnectionError, APIStatusError, APITimeoutError, OpenAI

from .config import DeepSeekSettings
from .errors import (
    ConfigurationError,
    ProviderError,
    ProviderErrorCode,
    ProviderResponseValidationError,
)
from .models import (
    ProviderMessage,
    ProviderRequest,
    ProviderResponse,
    ProviderToolCall,
    TokenUsage,
    ToolChoice,
)
from .validation import validate_request

ALLOWED_FINISH_REASONS: Final = frozenset(
    {"stop", "length", "content_filter", "tool_calls", "insufficient_system_resource", "aborted"}
)


def build_outbound_payload(
    request: ProviderRequest,
    settings: DeepSeekSettings,
) -> dict[str, Any]:
    """Build the exact outbound projection; no hidden or ambient fields are added."""

    validate_request(request)
    payload: dict[str, Any] = {
        "model": settings.model,
        "messages": [message.to_wire() for message in request.messages],
        "max_tokens": request.max_output_tokens,
        "stream": False,
        "thinking": {"type": "enabled" if settings.thinking else "disabled"},
    }
    if request.tools:
        payload["tools"] = [tool.to_wire() for tool in request.tools]
    if request.tool_choice is not None:
        payload["tool_choice"] = request.tool_choice.value
    if settings.thinking and settings.reasoning_effort is not None:
        payload["reasoning_effort"] = settings.reasoning_effort
    return payload


class DeepSeekProvider:
    """One-attempt DeepSeek transport; retries and budgets live in BoundedProvider."""

    provider_name = "deepseek"

    def __init__(
        self,
        settings: DeepSeekSettings,
        *,
        client: Any | None = None,
    ) -> None:
        if not isinstance(settings, DeepSeekSettings):
            raise TypeError("settings must be DeepSeekSettings")
        self._settings = settings
        self._client = client or OpenAI(
            api_key=settings.api_key,
            base_url=settings.base_url,
            max_retries=0,
            timeout=settings.limits.attempt_timeout_seconds,
        )

    def complete(self, request: ProviderRequest, *, timeout_seconds: float) -> ProviderResponse:
        if timeout_seconds <= 0:
            raise ConfigurationError("attempt timeout must be positive")
        if request.tool_choice is ToolChoice.REQUIRED and self._settings.thinking:
            raise ConfigurationError(
                "tool_choice='required' is not supported with thinking mode; "
                "disable thinking for forced tool calls"
            )
        payload = build_outbound_payload(request, self._settings)
        thinking = payload.pop("thinking")
        kwargs = {**payload, "extra_body": {"thinking": thinking}}
        try:
            completion = self._client.chat.completions.create(
                **kwargs,
                timeout=timeout_seconds,
            )
        except APITimeoutError:
            raise ProviderError(
                ProviderErrorCode.TIMEOUT,
                "DeepSeek request timed out",
                retryable=True,
                secrets=(self._settings.api_key,),
            ) from None
        except APIConnectionError:
            raise ProviderError(
                ProviderErrorCode.CONNECTION,
                "DeepSeek connection failed",
                retryable=True,
                secrets=(self._settings.api_key,),
            ) from None
        except APIStatusError as exc:
            raise self._map_status_error(exc) from None
        except ProviderError:
            raise
        except Exception as exc:  # noqa: BLE001 - never leak SDK internals
            raise ProviderError(
                ProviderErrorCode.INVALID_RESPONSE,
                f"unexpected DeepSeek transport failure ({type(exc).__name__})",
                retryable=False,
                secrets=(self._settings.api_key,),
            ) from None
        return self._parse_completion(completion)

    def close(self) -> None:
        close = getattr(self._client, "close", None)
        if callable(close):
            close()

    def _map_status_error(self, exc: APIStatusError) -> ProviderError:
        status = exc.status_code
        code = _status_code_to_error(status)
        message = f"DeepSeek API returned status {status}"
        error_message = getattr(exc, "message", None)
        if isinstance(error_message, str) and error_message.strip():
            message = f"{message}: {error_message}"
        details: dict[str, Any] = {}
        request_id = getattr(exc, "request_id", None)
        if isinstance(request_id, str) and request_id:
            details["request_id"] = request_id
        return ProviderError(
            code,
            message,
            status_code=status,
            retryable=code
            in {
                ProviderErrorCode.RATE_LIMITED,
                ProviderErrorCode.SERVER_ERROR,
            },
            retry_after_seconds=_retry_after(exc),
            details=details,
            secrets=(self._settings.api_key,),
        )

    def _parse_completion(self, completion: object) -> ProviderResponse:
        raw = _completion_dict(completion)
        choices = raw.get("choices")
        if not isinstance(choices, list) or len(choices) != 1:
            raise ProviderResponseValidationError(
                "unsupported_shape",
                "DeepSeek response must contain exactly one choice",
            )
        choice = choices[0]
        if not isinstance(choice, Mapping):
            raise ProviderResponseValidationError(
                "unsupported_shape", "DeepSeek choice is not an object"
            )
        message_raw = choice.get("message")
        if not isinstance(message_raw, Mapping):
            raise ProviderResponseValidationError(
                "unsupported_shape", "DeepSeek response has no message object"
            )
        role = message_raw.get("role")
        if role != "assistant":
            raise ProviderResponseValidationError(
                "unsupported_shape", "DeepSeek response message is not assistant-role"
            )
        content = message_raw.get("content")
        if content is not None and not isinstance(content, str):
            raise ProviderResponseValidationError(
                "unsupported_shape", "DeepSeek message content is not text"
            )
        reasoning = message_raw.get("reasoning_content")
        if reasoning is not None and not isinstance(reasoning, str):
            raise ProviderResponseValidationError(
                "unsupported_shape", "DeepSeek reasoning_content is not text"
            )
        raw_calls = message_raw.get("tool_calls")
        calls: list[ProviderToolCall] = []
        if raw_calls is not None:
            if not isinstance(raw_calls, list):
                raise ProviderResponseValidationError(
                    "unsupported_shape", "DeepSeek tool_calls is not a list"
                )
            for index, raw_call in enumerate(raw_calls):
                calls.append(_parse_tool_call(raw_call, index))
        try:
            message = ProviderMessage(
                role="assistant",
                content=content,
                tool_calls=tuple(calls),
                reasoning=reasoning,
            )
        except (TypeError, ValueError) as exc:
            raise ProviderResponseValidationError(
                "unsupported_shape", f"DeepSeek message is invalid: {exc}"
            ) from None
        finish_reason = choice.get("finish_reason")
        if not isinstance(finish_reason, str) or finish_reason not in ALLOWED_FINISH_REASONS:
            raise ProviderResponseValidationError(
                "unsupported_shape", "DeepSeek finish_reason is missing or unsupported"
            )
        model = raw.get("model")
        if not isinstance(model, str) or not model.strip():
            raise ProviderResponseValidationError(
                "unsupported_shape", "DeepSeek response model is missing"
            )
        response_id = raw.get("id")
        if response_id is not None and not isinstance(response_id, str):
            raise ProviderResponseValidationError(
                "unsupported_shape", "DeepSeek response id is not text"
            )
        usage = _parse_usage(raw.get("usage"))
        try:
            return ProviderResponse(
                message=message,
                model=model,
                provider=self.provider_name,
                finish_reason=finish_reason,
                usage=usage,
                response_id=response_id,
            )
        except (TypeError, ValueError) as exc:
            raise ProviderResponseValidationError(
                "unsupported_shape", f"DeepSeek response is invalid: {exc}"
            ) from None


def _completion_dict(completion: object) -> dict[str, Any]:
    if isinstance(completion, Mapping):
        return dict(completion)
    dump = getattr(completion, "model_dump", None)
    if callable(dump):
        value = dump()
        if isinstance(value, Mapping):
            return dict(value)
    raise ProviderResponseValidationError(
        "unsupported_shape", "DeepSeek SDK returned an unsupported completion object"
    )


def _parse_tool_call(raw: object, index: int) -> ProviderToolCall:
    if not isinstance(raw, Mapping):
        raise ProviderResponseValidationError(
            "unsupported_shape", f"tool call at index {index} is not an object"
        )
    if raw.get("type") != "function":
        raise ProviderResponseValidationError(
            "unsupported_shape", f"tool call at index {index} is not a function call"
        )
    function = raw.get("function")
    if not isinstance(function, Mapping):
        raise ProviderResponseValidationError(
            "unsupported_shape", f"tool call at index {index} has no function object"
        )
    call_id = raw.get("id")
    name = function.get("name")
    arguments = function.get("arguments")
    if not isinstance(call_id, str) or not isinstance(name, str):
        raise ProviderResponseValidationError(
            "unsupported_shape", f"tool call at index {index} has a non-text id or name"
        )
    if not isinstance(arguments, str):
        raise ProviderResponseValidationError(
            "unsupported_shape",
            f"tool call at index {index} arguments must be a JSON string",
        )
    try:
        return ProviderToolCall(id=call_id, name=name, arguments_json=arguments)
    except (TypeError, ValueError) as exc:
        raise ProviderResponseValidationError(
            "unsupported_shape", f"tool call at index {index} is invalid: {exc}"
        ) from None


def _parse_usage(raw: object) -> TokenUsage | None:
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise ProviderResponseValidationError("unsupported_usage", "usage is not an object")
    prompt = _required_token(raw, "prompt_tokens")
    completion = _required_token(raw, "completion_tokens")
    total = _required_token(raw, "total_tokens")
    details = raw.get("prompt_tokens_details")
    hit: int | None = _optional_token(raw, "prompt_cache_hit_tokens")
    miss: int | None = _optional_token(raw, "prompt_cache_miss_tokens")
    if hit is None and isinstance(details, Mapping):
        hit = _optional_token(details, "cached_tokens")
    if hit is not None and miss is None:
        miss = max(prompt - hit, 0)
    if miss is not None and hit is None:
        hit = max(prompt - miss, 0)
    if hit is not None and miss is not None and hit + miss != prompt:
        raise ProviderResponseValidationError(
            "unsupported_usage",
            "cache hit and miss tokens do not sum to prompt_tokens",
        )
    try:
        return TokenUsage(
            prompt_tokens=prompt,
            completion_tokens=completion,
            total_tokens=total,
            cache_hit_tokens=hit,
            cache_miss_tokens=miss,
        )
    except (TypeError, ValueError) as exc:
        raise ProviderResponseValidationError(
            "unsupported_usage", f"usage is invalid: {exc}"
        ) from None


def _required_token(raw: Mapping[str, Any], name: str) -> int:
    value = raw.get(name)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ProviderResponseValidationError(
            "unsupported_usage", f"usage field {name} is missing or invalid"
        )
    return value


def _optional_token(raw: Mapping[str, Any], name: str) -> int | None:
    value = raw.get(name)
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ProviderResponseValidationError("unsupported_usage", f"usage field {name} is invalid")
    return value


def _status_code_to_error(status: int) -> ProviderErrorCode:
    if status == 401:
        return ProviderErrorCode.AUTHENTICATION
    if status == 402:
        return ProviderErrorCode.INSUFFICIENT_BALANCE
    if status == 403:
        return ProviderErrorCode.AUTHENTICATION
    if status == 429:
        return ProviderErrorCode.RATE_LIMITED
    if status >= 500:
        return ProviderErrorCode.SERVER_ERROR
    return ProviderErrorCode.INVALID_REQUEST


def _retry_after(exc: APIStatusError) -> float | None:
    headers = getattr(exc.response, "headers", None)
    if headers is None:
        return None
    raw = headers.get("retry-after")
    if not isinstance(raw, str):
        return None
    try:
        value = float(raw)
    except ValueError:
        return None
    if value < 0:
        return None
    return min(value, 60.0)
