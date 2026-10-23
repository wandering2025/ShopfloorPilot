"""Local request, response, and tool-argument validation.

Provider output is untrusted.  Even when a provider advertises strict schemas,
the application validates the response shape, tool names, IDs, JSON syntax, and
the declared JSON Schema before any tool can execute.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from .errors import (
    ProviderError,
    ProviderErrorCode,
    ProviderResponseValidationError,
    ToolCallValidationError,
)
from .models import (
    ALLOWED_FINISH_REASONS,
    MAX_REQUEST_BYTES,
    MAX_TOOL_CALLS_PER_TURN,
    MessageRole,
    ProviderMessage,
    ProviderRequest,
    ProviderResponse,
    ProviderTurn,
    TokenUsage,
    ToolSchema,
    ValidatedToolCall,
)


def _request_error(reason: str, message: str) -> ProviderError:
    return ProviderError(
        ProviderErrorCode.INVALID_REQUEST,
        message,
        retryable=False,
        details={"reason": reason},
    )


def _response_error(reason: str, message: str) -> ProviderResponseValidationError:
    return ProviderResponseValidationError(reason, message)


def validate_request(request: ProviderRequest) -> None:
    """Reject an incomplete or oversized conversation before any network call."""

    if not isinstance(request, ProviderRequest):
        raise _request_error("unsupported_shape", "request is not a ProviderRequest")
    _validate_conversation(request.messages)
    size = len(request.estimated_payload_text().encode("utf-8"))
    if size > MAX_REQUEST_BYTES:
        raise _request_error(
            "request_too_large",
            f"serialized request is {size} bytes, above the {MAX_REQUEST_BYTES} byte limit",
        )


def _validate_conversation(messages: tuple[ProviderMessage, ...]) -> None:
    known: dict[str, str] = {}
    unresolved: set[str] = set()
    for index, message in enumerate(messages):
        if message.role is MessageRole.ASSISTANT:
            if unresolved:
                raise _request_error(
                    "unanswered_tool_call",
                    f"assistant message at index {index} leaves tool calls without results",
                )
            for call in message.tool_calls:
                if call.id in known:
                    raise _request_error(
                        "duplicate_tool_call_id",
                        f"tool call id {call.id!r} appears more than once in the conversation",
                    )
                known[call.id] = call.name
                unresolved.add(call.id)
        elif message.role is MessageRole.TOOL:
            assert message.tool_call_id is not None
            if message.tool_call_id not in known:
                raise _request_error(
                    "orphan_tool_result",
                    f"tool result references unknown call id {message.tool_call_id!r}",
                )
            if message.tool_call_id not in unresolved:
                raise _request_error(
                    "duplicate_tool_result",
                    f"tool call id {message.tool_call_id!r} already has a result",
                )
            if message.name is not None and message.name != known[message.tool_call_id]:
                raise _request_error(
                    "tool_result_name_mismatch",
                    f"tool result name {message.name!r} does not match the original call",
                )
            unresolved.remove(message.tool_call_id)
    if unresolved:
        raise _request_error(
            "unanswered_tool_call",
            "the request contains tool calls without matching tool results",
        )


def validate_response_turn(response: object, request: ProviderRequest) -> ProviderTurn:
    """Validate response shape, correlation, and every tool argument."""

    if not isinstance(response, ProviderResponse):
        raise _response_error("unsupported_shape", "provider returned a non-ProviderResponse value")
    if response.usage is not None and not isinstance(response.usage, TokenUsage):
        raise _response_error("unsupported_usage", "provider usage is not a TokenUsage value")
    if response.message.role is not MessageRole.ASSISTANT:
        raise _response_error("unsupported_shape", "provider response is not an assistant message")
    if response.finish_reason not in ALLOWED_FINISH_REASONS:
        raise _response_error(
            "unsupported_shape", "provider response has an unsupported finish reason"
        )
    if len(response.tool_calls) > MAX_TOOL_CALLS_PER_TURN:
        raise ToolCallValidationError(
            "too_many_tool_calls",
            f"provider returned more than {MAX_TOOL_CALLS_PER_TURN} tool calls",
        )
    if response.message.content is None and not response.tool_calls:
        raise _response_error(
            "unsupported_shape", "provider response has no content and no tool calls"
        )
    if request.tool_choice is not None:
        if request.tool_choice.value == "none" and response.tool_calls:
            raise ToolCallValidationError(
                "tool_choice_violation",
                "provider returned tool calls when tool_choice was none",
            )
        if request.tool_choice.value == "required" and not response.tool_calls:
            raise ToolCallValidationError(
                "tool_choice_violation",
                "provider returned no tool call when tool_choice was required",
            )

    tools = {tool.name: tool for tool in request.tools}
    history_ids = {
        call.id
        for message in request.messages
        if message.role is MessageRole.ASSISTANT
        for call in message.tool_calls
    }
    seen: set[str] = set()
    validated: list[ValidatedToolCall] = []
    for call in response.tool_calls:
        if call.id in seen or call.id in history_ids:
            raise ToolCallValidationError(
                "duplicate_tool_call_id",
                f"provider reused tool call id {call.id!r}",
                tool_name=call.name,
                call_id=call.id,
            )
        seen.add(call.id)
        tool = tools.get(call.name)
        if tool is None:
            raise ToolCallValidationError(
                "unknown_tool",
                f"provider called unknown tool {call.name!r}",
                tool_name=call.name,
                call_id=call.id,
            )
        arguments = _parse_arguments(call.arguments_json, call.name, call.id)
        _validate_arguments(tool, arguments, call)
        validated.append(ValidatedToolCall(call.id, call.name, arguments))
    return ProviderTurn(response=response, tool_calls=tuple(validated))


def _parse_arguments(raw: str, tool_name: str, call_id: str) -> dict[str, Any]:
    try:
        parsed = json.loads(
            raw,
            parse_constant=_reject_json_constant,
        )
    except (json.JSONDecodeError, ValueError) as exc:
        raise ToolCallValidationError(
            "malformed_json",
            f"tool {tool_name!r} returned arguments that are not valid JSON: {exc}",
            tool_name=tool_name,
            call_id=call_id,
        ) from None
    if not isinstance(parsed, dict):
        raise ToolCallValidationError(
            "arguments_not_object",
            f"tool {tool_name!r} returned arguments that are not a JSON object",
            tool_name=tool_name,
            call_id=call_id,
        )
    return parsed


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON constant {value!r} is not supported")


def _validate_arguments(
    tool: ToolSchema,
    arguments: Mapping[str, Any],
    call: Any,
) -> None:
    validator = Draft202012Validator(tool.parameters)
    error = next(validator.iter_errors(dict(arguments)), None)
    if error is None:
        return
    reason = _schema_reason(error)
    path = ".".join(str(part) for part in error.absolute_path) or "<root>"
    raise ToolCallValidationError(
        reason,
        f"tool {tool.name!r} arguments failed local schema validation at {path}: {error.message}",
        tool_name=tool.name,
        call_id=call.id,
    )


def _schema_reason(error: ValidationError) -> str:
    if error.validator == "required":
        return "missing_field"
    if error.validator == "additionalProperties":
        return "unexpected_field"
    return "schema_violation"


def validate_tool_result_content(content: object) -> str:
    """Tool results sent back to the model must be strings."""

    if not isinstance(content, str):
        raise ToolCallValidationError(
            "tool_result_not_string",
            "tool results submitted to the provider must be strings",
        )
    if not content:
        raise ToolCallValidationError("tool_result_empty", "tool results must not be empty")
    return content
