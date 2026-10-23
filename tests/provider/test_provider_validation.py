"""S09-A03: malformed JSON, schema, name, ID, and response-shape rejection."""

from __future__ import annotations

import pytest

from shopfloor_pilot.budgets import BudgetLedger
from shopfloor_pilot.providers.bounded import BoundedProvider
from shopfloor_pilot.providers.clock import FakeClock
from shopfloor_pilot.providers.costs import TokenRates
from shopfloor_pilot.providers.errors import (
    ProviderError,
    ProviderErrorCode,
    ProviderResponseValidationError,
    ToolCallValidationError,
)
from shopfloor_pilot.providers.fake import FakeReply, ScriptedFakeProvider, scripted_text
from shopfloor_pilot.providers.models import (
    MessageRole,
    ProviderMessage,
    ProviderRequest,
    ProviderResponse,
    ProviderToolCall,
    ToolChoice,
)
from shopfloor_pilot.providers.retry import RetryPolicy
from shopfloor_pilot.providers.validation import validate_request, validate_tool_result_content


def _provider(
    ledger: BudgetLedger,
    workflow_id: str,
    pricing: TokenRates,
    clock: FakeClock,
    script: list[FakeReply],
) -> BoundedProvider:
    fake = ScriptedFakeProvider(script, clock=clock)
    return BoundedProvider(
        fake,
        ledger,
        workflow_id,
        pricing,
        clock=clock,
        estimator=lambda _request: 100,
        retry_policy=RetryPolicy(max_retries_per_request=0),
    )


def _tool_response(
    *,
    calls: tuple[ProviderToolCall, ...],
    content: str | None = None,
) -> ProviderResponse:
    return ProviderResponse(
        message=ProviderMessage(role=MessageRole.ASSISTANT, content=content, tool_calls=calls),
        model="fake",
        provider="scripted-fake",
        finish_reason="tool_calls",
    )


def _call(arguments_json: str, *, name: str = "fixture_machine_status", call_id: str = "call-1"):
    return ProviderToolCall(id=call_id, name=name, arguments_json=arguments_json)


@pytest.mark.parametrize(
    ("arguments_json", "reason"),
    [
        ("{", "malformed_json"),
        ("[]", "arguments_not_object"),
        ("{}", "missing_field"),
        ('{"machine_id":"M1","extra":1}', "unexpected_field"),
        ('{"machine_id":"M2"}', "schema_violation"),
        ('{"machine_id":NaN}', "malformed_json"),
    ],
)
def test_bad_arguments_are_typed_and_do_not_execute(
    ledger: BudgetLedger,
    workflow_id: str,
    pricing: TokenRates,
    clock: FakeClock,
    tool_request: ProviderRequest,
    arguments_json: str,
    reason: str,
) -> None:
    bounded = _provider(
        ledger,
        workflow_id,
        pricing,
        clock,
        [FakeReply(_tool_response(calls=(_call(arguments_json),)))],
    )
    with pytest.raises(ToolCallValidationError) as exc:
        bounded.complete(tool_request, request_id=f"request-bad-arguments-{reason}")
    assert exc.value.reason == reason
    assert exc.value.code is ProviderErrorCode.INVALID_TOOL_CALL
    snapshot = ledger.snapshot(workflow_id)
    assert snapshot.provider_attempts == 1
    assert snapshot.tool_executions == 0
    assert snapshot.used_tokens > 0


def test_unknown_tool_is_rejected_before_execution(
    ledger: BudgetLedger,
    workflow_id: str,
    pricing: TokenRates,
    clock: FakeClock,
    tool_request: ProviderRequest,
) -> None:
    call = _call('{"machine_id":"M1"}', name="delete_everything")
    bounded = _provider(
        ledger,
        workflow_id,
        pricing,
        clock,
        [FakeReply(_tool_response(calls=(call,)))],
    )
    with pytest.raises(ToolCallValidationError) as exc:
        bounded.complete(tool_request, request_id="request-unknown-tool")
    assert exc.value.reason == "unknown_tool"
    assert ledger.snapshot(workflow_id).tool_executions == 0


def test_duplicate_tool_call_ids_are_rejected(
    ledger: BudgetLedger,
    workflow_id: str,
    pricing: TokenRates,
    clock: FakeClock,
    tool_request: ProviderRequest,
) -> None:
    call = _call('{"machine_id":"M1"}')
    bounded = _provider(
        ledger,
        workflow_id,
        pricing,
        clock,
        [FakeReply(_tool_response(calls=(call, call)))],
    )
    with pytest.raises(ToolCallValidationError) as exc:
        bounded.complete(tool_request, request_id="request-duplicate-call-id")
    assert exc.value.reason == "duplicate_tool_call_id"
    assert ledger.snapshot(workflow_id).tool_executions == 0


def test_unsupported_response_shape_is_typed(
    ledger: BudgetLedger,
    workflow_id: str,
    pricing: TokenRates,
    clock: FakeClock,
    tool_request: ProviderRequest,
) -> None:
    bounded = _provider(
        ledger,
        workflow_id,
        pricing,
        clock,
        [FakeReply({"choices": "not-a-list"})],
    )
    with pytest.raises(ProviderResponseValidationError) as exc:
        bounded.complete(tool_request, request_id="request-unsupported-shape")
    assert exc.value.reason == "unsupported_shape"
    snapshot = ledger.snapshot(workflow_id)
    assert snapshot.tool_executions == 0
    assert snapshot.provider_attempts == 1


def test_duplicate_id_across_history_is_rejected(
    ledger: BudgetLedger,
    workflow_id: str,
    pricing: TokenRates,
    clock: FakeClock,
    tool_request: ProviderRequest,
) -> None:
    first = _call('{"machine_id":"M1"}')
    first_turn_request = ProviderRequest(
        messages=tool_request.messages,
        tools=tool_request.tools,
        max_output_tokens=64,
        tool_choice=None,
    )
    bounded = _provider(
        ledger,
        workflow_id,
        pricing,
        clock,
        [
            FakeReply(_tool_response(calls=(first,))),
            FakeReply(_tool_response(calls=(_call('{"machine_id":"M1"}'),))),
        ],
    )
    first_turn = bounded.complete(first_turn_request, request_id="request-history-first")
    assert first_turn.tool_calls
    follow_up = ProviderRequest(
        messages=(
            *first_turn_request.messages,
            first_turn.response.message,
            ProviderMessage(role=MessageRole.TOOL, content="{}", tool_call_id="call-1"),
        ),
        tools=tool_request.tools,
        max_output_tokens=64,
    )
    with pytest.raises(ToolCallValidationError) as exc:
        bounded.complete(follow_up, request_id="request-history-follow-up")
    assert exc.value.reason == "duplicate_tool_call_id"
    assert ledger.snapshot(workflow_id).tool_executions == 0


def test_tool_choice_none_rejects_provider_tool_call(
    ledger: BudgetLedger,
    workflow_id: str,
    pricing: TokenRates,
    clock: FakeClock,
    tool_request: ProviderRequest,
) -> None:
    request = ProviderRequest(
        messages=tool_request.messages,
        tools=tool_request.tools,
        max_output_tokens=64,
        tool_choice=ToolChoice.NONE,
    )
    bounded = _provider(
        ledger,
        workflow_id,
        pricing,
        clock,
        [FakeReply(_tool_response(calls=(_call('{"machine_id":"M1"}'),)))],
    )
    with pytest.raises(ToolCallValidationError) as exc:
        bounded.complete(request, request_id="request-tool-choice-none")
    assert exc.value.reason == "tool_choice_violation"
    assert ledger.snapshot(workflow_id).tool_executions == 0


def test_tool_choice_required_rejects_text_only_response(
    ledger: BudgetLedger,
    workflow_id: str,
    pricing: TokenRates,
    clock: FakeClock,
    tool_request: ProviderRequest,
) -> None:
    bounded = _provider(
        ledger,
        workflow_id,
        pricing,
        clock,
        [FakeReply(scripted_text(content="text without required tool call"))],
    )
    with pytest.raises(ToolCallValidationError) as exc:
        bounded.complete(tool_request, request_id="request-tool-choice-required")
    assert exc.value.reason == "tool_choice_violation"
    assert ledger.snapshot(workflow_id).tool_executions == 0


def test_tool_result_name_must_match_original_call() -> None:
    assistant = ProviderMessage(
        role=MessageRole.ASSISTANT,
        tool_calls=(_call('{"machine_id":"M1"}', name="fixture_machine_status"),),
    )
    request = ProviderRequest(
        messages=(
            assistant,
            ProviderMessage(
                role=MessageRole.TOOL,
                content="{}",
                tool_call_id="call-1",
                name="different_tool",
            ),
        ),
        max_output_tokens=16,
    )
    with pytest.raises(ProviderError) as exc:
        validate_request(request)
    assert exc.value.code is ProviderErrorCode.INVALID_REQUEST
    assert exc.value.details["reason"] == "tool_result_name_mismatch"


def test_tool_result_content_must_be_string() -> None:
    with pytest.raises(ToolCallValidationError):
        validate_tool_result_content({"status": "idle"})
    with pytest.raises(ToolCallValidationError):
        validate_tool_result_content("")
    assert validate_tool_result_content("{}") == "{}"
