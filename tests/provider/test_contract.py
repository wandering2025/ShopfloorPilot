"""S09-A01/A02: offline import, fake labeling, and correlated tool round trip."""

from __future__ import annotations

import json

import pytest

from shopfloor_pilot.budgets import BudgetLedger
from shopfloor_pilot.domain.fixtures import load_scenario, materialize_initial_snapshot
from shopfloor_pilot.providers.bounded import BoundedProvider
from shopfloor_pilot.providers.clock import FakeClock
from shopfloor_pilot.providers.config import DeepSeekSettings
from shopfloor_pilot.providers.costs import TokenRates
from shopfloor_pilot.providers.deepseek import build_outbound_payload
from shopfloor_pilot.providers.errors import ProviderError
from shopfloor_pilot.providers.fake import (
    FAKE_PROVIDER_LABEL,
    FakeReply,
    ScriptedFakeProvider,
    scripted_text,
    scripted_tool_call,
)
from shopfloor_pilot.providers.models import (
    MessageRole,
    ProviderMessage,
    ProviderRequest,
    ProviderResponse,
    TokenUsage,
    ToolChoice,
    ToolSchema,
)
from shopfloor_pilot.providers.validation import validate_request
from shopfloor_pilot.storage import SQLiteStateRepository


def _bounded(
    fake: ScriptedFakeProvider,
    ledger: BudgetLedger,
    workflow_id: str,
    pricing: TokenRates,
    clock: FakeClock,
) -> BoundedProvider:
    return BoundedProvider(
        fake,
        ledger,
        workflow_id,
        pricing,
        clock=clock,
        estimator=lambda _request: 100,
    )


def test_offline_tool_round_trip_and_correlation(
    ledger: BudgetLedger,
    workflow_id: str,
    pricing: TokenRates,
    clock: FakeClock,
    tool_request: ProviderRequest,
    settings: DeepSeekSettings,
) -> None:
    first_usage = TokenUsage(100, 10, 110)
    second_usage = TokenUsage(120, 5, 125)
    fake = ScriptedFakeProvider(
        [
            FakeReply(
                scripted_tool_call(
                    call_id="call-correlated-1",
                    tool_name="fixture_machine_status",
                    arguments={"machine_id": "M1"},
                    usage=first_usage,
                )
            ),
            FakeReply(scripted_text(content="M1 is idle.", usage=second_usage)),
        ],
        clock=clock,
    )
    bounded = _bounded(fake, ledger, workflow_id, pricing, clock)
    first_turn = bounded.complete(tool_request, request_id="request-tool-round-trip-1")
    assert len(first_turn.tool_calls) == 1
    call = first_turn.tool_calls[0]
    assert call.call_id == "call-correlated-1"
    assert call.name == "fixture_machine_status"
    assert dict(call.arguments) == {"machine_id": "M1"}
    assert first_turn.response.message.tool_calls[0].arguments_json == '{"machine_id":"M1"}'

    result = json.dumps({"machine_id": "M1", "status": "idle"}, sort_keys=True)
    second_request = ProviderRequest(
        messages=(
            *tool_request.messages,
            first_turn.response.message,
            ProviderMessage(
                role=MessageRole.TOOL,
                content=result,
                tool_call_id=call.call_id,
                name=call.name,
            ),
        ),
        tools=tool_request.tools,
        max_output_tokens=64,
        tool_choice=ToolChoice.AUTO,
    )
    validate_request(second_request)
    second_turn = bounded.complete(second_request, request_id="request-tool-round-trip-2")
    assert second_turn.response.message.content == "M1 is idle."
    assert not second_turn.tool_calls
    assert fake.requests[1].messages[-1].tool_call_id == call.call_id

    payload = build_outbound_payload(second_request, settings)
    wire_messages = payload["messages"]
    assert wire_messages[-1] == {
        "role": "tool",
        "content": result,
        "name": "fixture_machine_status",
        "tool_call_id": "call-correlated-1",
    }
    assert payload["tool_choice"] == "auto"
    assert "dummy-s09-test-key" not in json.dumps(payload)
    assert "reasoning_content" not in json.dumps(payload)

    snapshot = ledger.snapshot(workflow_id)
    assert snapshot.provider_attempts == 2
    assert snapshot.used_tokens == 235
    assert snapshot.reserved_tokens == 0
    assert snapshot.actual_cost_micros == 84
    assert snapshot.reserved_cost_micros == 0
    assert snapshot.status.value == "running"


def test_fake_artifacts_are_visibly_offline() -> None:
    fake = ScriptedFakeProvider([FakeReply(scripted_text(content="offline"))])
    label = fake.artifact_label()
    assert label["evidence_kind"] == "offline_contract_evidence"
    assert label["live_readiness"] is False
    assert "OFFLINE CONTRACT EVIDENCE" in FAKE_PROVIDER_LABEL
    assert "not live-readiness evidence" in FAKE_PROVIDER_LABEL


def test_reasoning_round_trips_but_is_private(
    ledger: BudgetLedger,
    workflow_id: str,
    pricing: TokenRates,
    clock: FakeClock,
    text_request: ProviderRequest,
) -> None:
    marker = "PRIVATE-CHAIN-MARKER"
    fake = ScriptedFakeProvider(
        [FakeReply(scripted_text(content="public answer", reasoning=marker))],
        clock=clock,
    )
    bounded = _bounded(fake, ledger, workflow_id, pricing, clock)
    turn = bounded.complete(text_request, request_id="request-private-reasoning")
    message = turn.response.message
    assert message.to_wire()["reasoning_content"] == marker
    assert "reasoning_content" not in message.public_dict()
    assert "reasoning" not in repr(message)
    serialized = json.dumps(turn.public_dict())
    assert marker not in serialized
    assert marker not in repr(turn)


def test_provider_tool_proposal_cannot_mutate_authoritative_operational_state(
    tmp_path,
    ledger: BudgetLedger,
    workflow_id: str,
    pricing: TokenRates,
    clock: FakeClock,
    tool_request: ProviderRequest,
) -> None:
    repository = SQLiteStateRepository(tmp_path / "authoritative-state.sqlite3")
    repository.initialize(applied_at_utc="2026-10-21T00:00:00+00:00")
    snapshot = materialize_initial_snapshot(
        load_scenario("machine_breakdown"),
        run_id="run-provider-state-invariant",
    )
    repository.create_run(snapshot, created_at_utc="2026-10-21T00:00:00+00:00")
    before_checkpoint = repository.get_checkpoint(snapshot.run_id)
    before_lifecycle = repository.export_lifecycle(snapshot.run_id)

    fake = ScriptedFakeProvider(
        [
            FakeReply(
                scripted_tool_call(
                    call_id="call-state-invariant",
                    tool_name="fixture_machine_status",
                    arguments={"machine_id": "M1"},
                )
            )
        ],
        clock=clock,
    )
    bounded = _bounded(fake, ledger, workflow_id, pricing, clock)
    turn = bounded.complete(tool_request, request_id="request-state-invariant")
    assert len(turn.tool_calls) == 1
    assert ledger.snapshot(workflow_id).tool_executions == 0

    from shopfloor_pilot.providers.readiness import execute_fixture_machine_status

    result = json.loads(execute_fixture_machine_status(dict(turn.tool_calls[0].arguments)))
    assert result["authoritative_state"] is False
    assert repository.get_checkpoint(snapshot.run_id) == before_checkpoint
    assert repository.export_lifecycle(snapshot.run_id) == before_lifecycle


def test_request_conversation_correlations(tool_request: ProviderRequest) -> None:
    assistant = ProviderMessage(
        role=MessageRole.ASSISTANT,
        content=None,
        tool_calls=scripted_tool_call(
            call_id="call-1",
            tool_name="fixture_machine_status",
            arguments={"machine_id": "M1"},
        ).message.tool_calls,
    )
    orphan = ProviderRequest(
        messages=(ProviderMessage(role=MessageRole.TOOL, content="{}", tool_call_id="call-x"),),
        max_output_tokens=16,
    )
    with pytest.raises(ProviderError) as exc:
        validate_request(orphan)
    assert exc.value.details["reason"] == "orphan_tool_result"

    unanswered = ProviderRequest(messages=(assistant,), max_output_tokens=16)
    with pytest.raises(ProviderError) as exc:
        validate_request(unanswered)
    assert exc.value.details["reason"] == "unanswered_tool_call"

    resolved = ProviderRequest(
        messages=(
            assistant,
            ProviderMessage(
                role=MessageRole.TOOL,
                content="{}",
                tool_call_id="call-1",
            ),
        ),
        max_output_tokens=16,
    )
    validate_request(resolved)

    duplicate = ProviderRequest(
        messages=(
            assistant,
            ProviderMessage(role=MessageRole.TOOL, content="{}", tool_call_id="call-1"),
            assistant,
            ProviderMessage(role=MessageRole.TOOL, content="{}", tool_call_id="call-1"),
        ),
        max_output_tokens=16,
    )
    with pytest.raises(ProviderError) as exc:
        validate_request(duplicate)
    assert exc.value.details["reason"] == "duplicate_tool_call_id"


def test_request_and_schema_rejections(machine_tool: ToolSchema) -> None:
    with pytest.raises(ValueError):
        ProviderRequest(
            messages=(ProviderMessage(role=MessageRole.USER, content="x"),),
            tools=(machine_tool, machine_tool),
            max_output_tokens=16,
        )
    with pytest.raises(ValueError):
        ToolSchema(
            name="open_schema",
            description="invalid because extras are allowed",
            parameters={"type": "object", "properties": {}},
        )
    with pytest.raises(ValueError):
        ProviderMessage(role=MessageRole.USER)
    with pytest.raises(ValueError):
        ProviderRequest(
            messages=(ProviderMessage(role=MessageRole.USER, content="x"),),
            tools=(machine_tool,),
            tool_choice=ToolChoice.AUTO,
            max_output_tokens=0,
        )


def test_oversized_request_is_rejected() -> None:
    request = ProviderRequest(
        messages=tuple(
            ProviderMessage(role=MessageRole.USER, content="x" * 90_000) for _ in range(3)
        ),
        max_output_tokens=16,
    )
    with pytest.raises(ProviderError) as exc:
        validate_request(request)
    assert exc.value.details["reason"] == "request_too_large"


def test_harmless_fixture_tool_executes_only_valid_arguments() -> None:
    from shopfloor_pilot.providers.readiness import execute_fixture_machine_status

    result = json.loads(execute_fixture_machine_status({"machine_id": "M1"}))
    assert result == {
        "machine_id": "M1",
        "status": "idle",
        "source": "local-synthetic-fixture",
        "authoritative_state": False,
    }
    with pytest.raises(ValueError):
        execute_fixture_machine_status({"machine_id": "M2"})
    with pytest.raises(ValueError):
        execute_fixture_machine_status({"machine_id": "M1", "extra": True})
    with pytest.raises(ValueError):
        execute_fixture_machine_status({})


def test_response_usage_must_be_typed() -> None:
    message = ProviderMessage(role=MessageRole.ASSISTANT, content="ok")
    with pytest.raises(TypeError):
        ProviderResponse(
            message=message,
            model="fake",
            provider="scripted-fake",
            finish_reason="stop",
            usage={"prompt_tokens": 1},
        )  # type: ignore[arg-type]
