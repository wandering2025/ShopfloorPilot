"""Typed provider messages, tools, usage, and responses.

These models are the only shape the supervisor sees.  Hidden reasoning is kept
in a private field for the required thinking-mode round trip but is excluded
from representations and audit dictionaries.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Final

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

TOOL_NAME_PATTERN: Final = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
CALL_ID_PATTERN: Final = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
MAX_MESSAGES: Final = 64
MAX_TOOLS: Final = 32
MAX_TOOL_CALLS_PER_TURN: Final = 8
MAX_TEXT_CHARS: Final = 100_000
MAX_REASONING_CHARS: Final = 200_000
MAX_OUTPUT_TOKENS: Final = 32_768
MAX_REQUEST_BYTES: Final = 256_000
ALLOWED_FINISH_REASONS: Final = frozenset(
    {"stop", "length", "content_filter", "tool_calls", "insufficient_system_resource", "aborted"}
)


class MessageRole(StrEnum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class ToolChoice(StrEnum):
    AUTO = "auto"
    NONE = "none"
    REQUIRED = "required"


def _require_text(value: object, name: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    if not allow_empty and not value.strip():
        raise ValueError(f"{name} must not be blank")
    if len(value) > MAX_TEXT_CHARS:
        raise ValueError(f"{name} exceeds {MAX_TEXT_CHARS} characters")
    return value


def _require_identifier(value: object, name: str, pattern: re.Pattern[str]) -> str:
    text = _require_text(value, name)
    if pattern.fullmatch(text) is None:
        raise ValueError(f"{name} contains unsupported characters")
    return text


@dataclass(frozen=True, slots=True)
class ToolSchema:
    """One function tool exposed to the model; local validation is mandatory."""

    name: str
    description: str
    parameters: Mapping[str, Any]

    def __post_init__(self) -> None:
        _require_identifier(self.name, "tool name", TOOL_NAME_PATTERN)
        _require_text(self.description, "tool description")
        if len(self.description) > 2_000:
            raise ValueError("tool description exceeds 2000 characters")
        if not isinstance(self.parameters, Mapping):
            raise TypeError("tool parameters must be a mapping")
        parameters = dict(self.parameters)
        if parameters.get("type") != "object":
            raise ValueError("tool parameters must be a JSON object schema")
        properties = parameters.get("properties", {})
        if not isinstance(properties, Mapping):
            raise TypeError("tool properties must be a mapping")
        if parameters.get("additionalProperties") is not False:
            raise ValueError("tool parameters must set additionalProperties to false")
        try:
            Draft202012Validator.check_schema(parameters)
        except SchemaError as exc:
            raise ValueError(f"tool parameters are not valid JSON Schema: {exc.message}") from None
        object.__setattr__(self, "parameters", parameters)

    def to_wire(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": dict(self.parameters),
            },
        }

    def public_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": dict(self.parameters),
        }


@dataclass(frozen=True, slots=True)
class ProviderToolCall:
    """An unvalidated tool call emitted by the model."""

    id: str
    name: str
    arguments_json: str

    def __post_init__(self) -> None:
        _require_identifier(self.id, "tool call id", CALL_ID_PATTERN)
        _require_identifier(self.name, "tool name", TOOL_NAME_PATTERN)
        if not isinstance(self.arguments_json, str):
            raise TypeError("tool arguments must be a JSON string")
        if len(self.arguments_json) > 16_384:
            raise ValueError("tool arguments exceed 16384 characters")

    def to_wire(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": "function",
            "function": {"name": self.name, "arguments": self.arguments_json},
        }

    def public_dict(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "arguments": self.arguments_json}


@dataclass(frozen=True, slots=True)
class ProviderMessage:
    """One conversation message with optional private reasoning text."""

    role: MessageRole
    content: str | None = None
    tool_calls: tuple[ProviderToolCall, ...] = ()
    tool_call_id: str | None = None
    name: str | None = None
    reasoning: str | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not isinstance(self.role, MessageRole):
            try:
                object.__setattr__(self, "role", MessageRole(self.role))
            except ValueError:
                raise ValueError("unsupported message role") from None
        if self.content is not None:
            _require_text(self.content, "message content", allow_empty=True)
        calls = tuple(self.tool_calls)
        if len(calls) > MAX_TOOL_CALLS_PER_TURN:
            raise ValueError(f"a message may carry at most {MAX_TOOL_CALLS_PER_TURN} tool calls")
        object.__setattr__(self, "tool_calls", calls)
        if self.tool_call_id is not None:
            _require_identifier(self.tool_call_id, "tool_call_id", CALL_ID_PATTERN)
        if self.name is not None:
            _require_text(self.name, "message name")
        if self.reasoning is not None:
            if not isinstance(self.reasoning, str):
                raise TypeError("reasoning must be a string")
            if len(self.reasoning) > MAX_REASONING_CHARS:
                raise ValueError(f"reasoning exceeds {MAX_REASONING_CHARS} characters")

        if self.role in (MessageRole.SYSTEM, MessageRole.USER):
            if self.content is None:
                raise ValueError(f"{self.role.value} messages require content")
            if calls or self.tool_call_id is not None or self.reasoning is not None:
                raise ValueError(f"{self.role.value} messages cannot carry tool fields")
        elif self.role is MessageRole.ASSISTANT:
            if self.tool_call_id is not None:
                raise ValueError("assistant messages cannot carry tool_call_id")
            if self.content is None and not calls:
                raise ValueError("assistant messages require content or tool calls")
        else:
            if self.content is None:
                raise ValueError("tool messages require content")
            if calls or self.reasoning is not None:
                raise ValueError("tool messages cannot carry tool calls or reasoning")
            if self.tool_call_id is None:
                raise ValueError("tool messages require tool_call_id")

    def to_wire(self) -> dict[str, Any]:
        wire: dict[str, Any] = {"role": self.role.value}
        if self.role is MessageRole.ASSISTANT:
            wire["content"] = self.content
        else:
            wire["content"] = self.content
        if self.name is not None:
            wire["name"] = self.name
        if self.tool_calls:
            wire["tool_calls"] = [call.to_wire() for call in self.tool_calls]
        if self.tool_call_id is not None:
            wire["tool_call_id"] = self.tool_call_id
        if self.role is MessageRole.ASSISTANT and self.reasoning is not None:
            # Required by DeepSeek thinking mode whenever tools are present.
            # This value must never enter audit artifacts or logs.
            wire["reasoning_content"] = self.reasoning
        return wire

    def public_dict(self) -> dict[str, Any]:
        """Audit projection that deliberately omits hidden reasoning."""

        payload: dict[str, Any] = {"role": self.role.value, "content": self.content}
        if self.name is not None:
            payload["name"] = self.name
        if self.tool_calls:
            payload["tool_calls"] = [call.public_dict() for call in self.tool_calls]
        if self.tool_call_id is not None:
            payload["tool_call_id"] = self.tool_call_id
        return payload


@dataclass(frozen=True, slots=True)
class ProviderRequest:
    """One non-streaming completion request."""

    messages: tuple[ProviderMessage, ...]
    tools: tuple[ToolSchema, ...] = ()
    max_output_tokens: int = 1_024
    tool_choice: ToolChoice | None = None

    def __post_init__(self) -> None:
        messages = tuple(self.messages)
        tools = tuple(self.tools)
        if not 1 <= len(messages) <= MAX_MESSAGES:
            raise ValueError(f"provider requests require 1..{MAX_MESSAGES} messages")
        if len(tools) > MAX_TOOLS:
            raise ValueError(f"provider requests support at most {MAX_TOOLS} tools")
        names = [tool.name for tool in tools]
        if len(names) != len(set(names)):
            raise ValueError("tool names must be unique")
        if not isinstance(self.max_output_tokens, int) or isinstance(self.max_output_tokens, bool):
            raise TypeError("max_output_tokens must be an integer")
        if not 1 <= self.max_output_tokens <= MAX_OUTPUT_TOKENS:
            raise ValueError(f"max_output_tokens must be between 1 and {MAX_OUTPUT_TOKENS}")
        choice = self.tool_choice
        if choice is not None and not isinstance(choice, ToolChoice):
            choice = ToolChoice(choice)
        if not tools and choice is not None:
            raise ValueError("tool_choice requires at least one tool")
        object.__setattr__(self, "messages", messages)
        object.__setattr__(self, "tools", tools)
        object.__setattr__(self, "tool_choice", choice)

    def to_wire(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "messages": [message.to_wire() for message in self.messages],
            "max_output_tokens": self.max_output_tokens,
        }
        if self.tools:
            payload["tools"] = [tool.to_wire() for tool in self.tools]
        if self.tool_choice is not None:
            payload["tool_choice"] = self.tool_choice.value
        return payload

    def estimated_payload_text(self) -> str:
        return json.dumps(
            self.to_wire(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )


@dataclass(frozen=True, slots=True)
class TokenUsage:
    """Provider-reported token usage; absent usage is represented by ``None``."""

    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    cache_hit_tokens: int | None = None
    cache_miss_tokens: int | None = None

    def __post_init__(self) -> None:
        for name in ("prompt_tokens", "completion_tokens", "total_tokens"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if self.total_tokens != self.prompt_tokens + self.completion_tokens:
            raise ValueError("total_tokens must equal prompt_tokens + completion_tokens")
        for name in ("cache_hit_tokens", "cache_miss_tokens"):
            value = getattr(self, name)
            if value is not None and (
                not isinstance(value, int) or isinstance(value, bool) or value < 0
            ):
                raise ValueError(f"{name} must be a non-negative integer or None")
        if self.cache_hit_tokens is not None and self.cache_miss_tokens is not None:
            if self.cache_hit_tokens + self.cache_miss_tokens != self.prompt_tokens:
                raise ValueError("cache hit/miss tokens must sum to prompt_tokens")

    @property
    def cache_breakdown_known(self) -> bool:
        return self.cache_hit_tokens is not None and self.cache_miss_tokens is not None

    def public_dict(self) -> dict[str, Any]:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "cache_hit_tokens": self.cache_hit_tokens,
            "cache_miss_tokens": self.cache_miss_tokens,
        }


@dataclass(frozen=True, slots=True)
class ProviderResponse:
    """A normalized provider turn with optional usage and latency."""

    message: ProviderMessage
    model: str
    provider: str
    finish_reason: str
    usage: TokenUsage | None = None
    response_id: str | None = None
    latency_seconds: float | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.message, ProviderMessage):
            raise TypeError("provider responses require a ProviderMessage")
        if self.message.role is not MessageRole.ASSISTANT:
            raise ValueError("provider responses must carry an assistant message")
        _require_text(self.model, "model")
        _require_text(self.provider, "provider")
        if self.finish_reason not in ALLOWED_FINISH_REASONS:
            raise ValueError("unsupported finish reason")
        if self.usage is not None and not isinstance(self.usage, TokenUsage):
            raise TypeError("provider usage must be a TokenUsage or None")
        if self.latency_seconds is not None:
            if not isinstance(self.latency_seconds, (int, float)) or isinstance(
                self.latency_seconds, bool
            ):
                raise TypeError("latency_seconds must be numeric or None")
            if self.latency_seconds < 0:
                raise ValueError("latency_seconds must be non-negative")

    @property
    def tool_calls(self) -> tuple[ProviderToolCall, ...]:
        return self.message.tool_calls

    def public_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "provider": self.provider,
            "model": self.model,
            "finish_reason": self.finish_reason,
            "response_id": self.response_id,
            "message": self.message.public_dict(),
            "usage": None if self.usage is None else self.usage.public_dict(),
            "latency_seconds": self.latency_seconds,
        }
        return payload


@dataclass(frozen=True, slots=True)
class ValidatedToolCall:
    """A locally validated tool call safe to hand to a tool registry."""

    call_id: str
    name: str
    arguments: Mapping[str, Any]

    def public_dict(self) -> dict[str, Any]:
        return {"call_id": self.call_id, "name": self.name, "arguments": dict(self.arguments)}


@dataclass(frozen=True, slots=True)
class ProviderTurn:
    """A response plus the tool calls that passed local validation."""

    response: ProviderResponse
    tool_calls: tuple[ValidatedToolCall, ...]

    def public_dict(self) -> dict[str, Any]:
        return {
            "response": self.response.public_dict(),
            "validated_tool_calls": [call.public_dict() for call in self.tool_calls],
        }


def serialize_tool_arguments(arguments: Mapping[str, Any]) -> str:
    """Serialize a local tool result/arguments mapping deterministically."""

    if not isinstance(arguments, Mapping):
        raise TypeError("arguments must be a mapping")
    return json.dumps(dict(arguments), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sequence_of_messages(messages: Sequence[ProviderMessage]) -> tuple[ProviderMessage, ...]:
    return tuple(messages)
