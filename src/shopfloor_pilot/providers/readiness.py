"""Opt-in provider readiness probe and offline contract check."""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from ..budgets import BudgetLedger
from .bounded import BoundedProvider
from .clock import SystemClock
from .config import (
    PaidRunSettings,
    load_paid_run_settings,
    merged_environment,
)
from .costs import micros_to_string
from .deepseek import DeepSeekProvider
from .errors import ConfigurationError, ProviderError, ProviderErrorCode
from .fake import (
    FAKE_PROVIDER_LABEL,
    FakeReply,
    ScriptedFakeProvider,
    scripted_text,
    scripted_tool_call,
)
from .models import (
    MessageRole,
    ProviderMessage,
    ProviderRequest,
    ProviderResponse,
    TokenUsage,
    ToolChoice,
    ToolSchema,
)
from .validation import validate_request, validate_response_turn

FIXTURE_TOOL = ToolSchema(
    name="fixture_machine_status",
    description=(
        "Return the local, synthetic status for one fixture machine. "
        "This tool never reads or changes authoritative operational state."
    ),
    parameters={
        "type": "object",
        "properties": {
            "machine_id": {
                "type": "string",
                "enum": ["M1"],
                "description": "Synthetic fixture machine identifier.",
            }
        },
        "required": ["machine_id"],
        "additionalProperties": False,
    },
)


@dataclass(frozen=True, slots=True)
class ReadinessOutcome:
    status: str
    exit_code: int
    report: dict[str, Any]


def run_provider_check(
    *,
    live: bool,
    output: str | Path,
    env_file: str | Path | None = None,
) -> ReadinessOutcome:
    """Run the labeled offline check or the opt-in live readiness probe."""

    output_path = Path(output)
    output_path.mkdir(parents=True, exist_ok=True)
    if not live:
        outcome = _offline_check(output_path)
        _write_report(output_path, outcome.report)
        return outcome
    selected_env_file = env_file
    if selected_env_file is None and Path(".env").is_file():
        selected_env_file = Path(".env")
    environment = merged_environment(os.environ, env_file=selected_env_file)
    try:
        settings = load_paid_run_settings(environment)
    except ConfigurationError as exc:
        report = {
            "status": "SKIPPED",
            "evidence_kind": "live_readiness_skipped",
            "live_readiness": False,
            "checked_at_utc": _utc_now(),
            "reason": exc.message,
            "missing": exc.details.get("missing", []),
            "message": (
                "Live DeepSeek readiness was not attempted because required paid-run "
                "configuration is absent."
            ),
        }
        _write_report(output_path, report)
        return ReadinessOutcome("SKIPPED", 2, report)
    outcome = _live_check(settings, output_path)
    _write_report(output_path, outcome.report)
    return outcome


def _offline_check(output_path: Path) -> ReadinessOutcome:
    usage_one = TokenUsage(120, 15, 135)
    usage_two = TokenUsage(150, 12, 162)
    script = [
        FakeReply(
            scripted_tool_call(
                call_id="call-offline-1",
                tool_name=FIXTURE_TOOL.name,
                arguments={"machine_id": "M1"},
                usage=usage_one,
            )
        ),
        FakeReply(
            scripted_text(
                content="Offline contract fixture: M1 is idle.",
                usage=usage_two,
            )
        ),
    ]
    fake = ScriptedFakeProvider(script)
    request = ProviderRequest(
        messages=(
            ProviderMessage(role=MessageRole.SYSTEM, content=_SYSTEM_PROMPT),
            ProviderMessage(role=MessageRole.USER, content=_USER_PROMPT),
        ),
        tools=(FIXTURE_TOOL,),
        max_output_tokens=128,
        tool_choice=ToolChoice.REQUIRED,
    )
    checks: dict[str, Any] = {}
    try:
        validate_request(request)
        checks["request_valid"] = True
        first = fake.complete(request, timeout_seconds=30.0)
        first_turn = validate_response_turn(first, request)
        checks["tool_call_returned"] = len(first_turn.tool_calls) == 1
        call = first_turn.tool_calls[0]
        checks["tool_name_matches"] = call.name == FIXTURE_TOOL.name
        checks["arguments_valid"] = dict(call.arguments) == {"machine_id": "M1"}
        checks["call_id_present"] = bool(call.call_id)
        result = execute_fixture_machine_status(dict(call.arguments))
        checks["tool_executed"] = True
        second_request = ProviderRequest(
            messages=(
                *request.messages,
                first.message,
                ProviderMessage(
                    role=MessageRole.TOOL,
                    content=result,
                    tool_call_id=call.call_id,
                    name=call.name,
                ),
            ),
            tools=(FIXTURE_TOOL,),
            max_output_tokens=128,
            tool_choice=ToolChoice.AUTO,
        )
        validate_request(second_request)
        second = fake.complete(second_request, timeout_seconds=30.0)
        second_turn = validate_response_turn(second, second_request)
        checks["final_response_received"] = bool(second_turn.response.message.content)
        checks["tool_result_submitted"] = True
        checks["no_second_tool_call"] = not second_turn.tool_calls
        checks["script_complete"] = fake.remaining_steps == 0
    except Exception as exc:  # noqa: BLE001 - offline report must stay useful
        report = {
            "status": "FAILED",
            "evidence_kind": "offline_contract_evidence",
            "live_readiness": False,
            "provider": fake.provider_name,
            "label": FAKE_PROVIDER_LABEL,
            "checked_at_utc": _utc_now(),
            "checks": checks,
            "error": {"type": type(exc).__name__, "message": str(exc)[:300]},
        }
        _write_report(output_path, report)
        return ReadinessOutcome("FAILED", 1, report)
    total_prompt = usage_one.prompt_tokens + usage_two.prompt_tokens
    total_completion = usage_one.completion_tokens + usage_two.completion_tokens
    report = {
        "status": "OFFLINE_ONLY",
        "evidence_kind": "offline_contract_evidence",
        "live_readiness": False,
        "provider": fake.provider_name,
        "label": FAKE_PROVIDER_LABEL,
        "checked_at_utc": _utc_now(),
        "attempts": fake.calls,
        "tool_executions": 1,
        "usage": {
            "prompt_tokens": total_prompt,
            "completion_tokens": total_completion,
            "total_tokens": total_prompt + total_completion,
            "source": "scripted-fixture",
        },
        "checks": checks,
        "note": (
            "Every check above is offline contract evidence. "
            "No DeepSeek endpoint was contacted and no live readiness is claimed."
        ),
    }
    return ReadinessOutcome("OFFLINE_ONLY", 0, report)


def _live_check(settings: PaidRunSettings, output_path: Path) -> ReadinessOutcome:
    connection = replace(settings.connection, thinking=False, reasoning_effort=None)
    run_id = f"run-provider-readiness-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}"
    workflow_id = f"workflow-provider-readiness-{uuid4().hex[:12]}"
    ledger = BudgetLedger(output_path / "provider_ledger.sqlite3")
    provider: DeepSeekProvider | None = None
    checks: dict[str, Any] = {}
    usage_parts: list[TokenUsage] = []
    try:
        ledger.open_run(
            run_id=run_id,
            currency=settings.currency,
            cap_micros=settings.run_cap_micros,
        )
        ledger.open_workflow(
            workflow_id=workflow_id,
            run_id=run_id,
            limits=connection.limits,
            workflow_cap_micros=settings.workflow_cap_micros,
        )
        provider = DeepSeekProvider(connection)
        bounded = BoundedProvider(
            provider,
            ledger,
            workflow_id,
            settings.pricing,
            retry_policy=settings.retry_policy,
            clock=SystemClock(),
        )
        started = time.perf_counter()
        request = ProviderRequest(
            messages=(
                ProviderMessage(role=MessageRole.SYSTEM, content=_SYSTEM_PROMPT),
                ProviderMessage(role=MessageRole.USER, content=_USER_PROMPT),
            ),
            tools=(FIXTURE_TOOL,),
            max_output_tokens=min(settings.max_output_tokens, 512),
            tool_choice=ToolChoice.REQUIRED,
        )
        first_turn = bounded.complete(request, request_id=f"{workflow_id}-request-1")
        _validate_live_identity(
            first_turn.response,
            provider_name=provider.provider_name,
            expected_model=connection.model,
        )
        checks["first_response_identity_valid"] = True
        if first_turn.response.usage is None:
            raise ProviderError(
                ProviderErrorCode.INVALID_RESPONSE,
                "readiness probe first response omitted provider usage",
                retryable=False,
            )
        usage_parts.append(first_turn.response.usage)
        checks["first_usage_reported"] = True
        checks["tool_call_returned"] = len(first_turn.tool_calls) == 1
        if len(first_turn.tool_calls) != 1:
            raise ProviderError(
                ProviderErrorCode.INVALID_RESPONSE,
                "readiness probe expected exactly one tool call",
                retryable=False,
            )
        call = first_turn.tool_calls[0]
        checks["tool_name_matches"] = call.name == FIXTURE_TOOL.name
        checks["arguments_valid"] = dict(call.arguments) == {"machine_id": "M1"}
        checks["call_id_present"] = bool(call.call_id)
        if call.name != FIXTURE_TOOL.name or dict(call.arguments) != {"machine_id": "M1"}:
            raise ProviderError(
                ProviderErrorCode.INVALID_TOOL_CALL,
                "readiness probe tool call did not match the fixture schema",
                retryable=False,
            )
        receipt = ledger.record_tool_execution(workflow_id, call.name)
        result = execute_fixture_machine_status(dict(call.arguments))
        ledger.finish_tool_execution(receipt, active_seconds=0.0)
        checks["tool_executed"] = True
        second_request = ProviderRequest(
            messages=(
                *request.messages,
                first_turn.response.message,
                ProviderMessage(
                    role=MessageRole.TOOL,
                    content=result,
                    tool_call_id=call.call_id,
                    name=call.name,
                ),
            ),
            tools=(FIXTURE_TOOL,),
            max_output_tokens=min(settings.max_output_tokens, 512),
            tool_choice=ToolChoice.AUTO,
        )
        validate_request(second_request)
        second_turn = bounded.complete(
            second_request,
            request_id=f"{workflow_id}-request-2",
        )
        _validate_live_identity(
            second_turn.response,
            provider_name=provider.provider_name,
            expected_model=connection.model,
        )
        checks["second_response_identity_valid"] = True
        if second_turn.response.usage is None:
            raise ProviderError(
                ProviderErrorCode.INVALID_RESPONSE,
                "readiness probe second response omitted provider usage",
                retryable=False,
            )
        usage_parts.append(second_turn.response.usage)
        checks["second_usage_reported"] = True
        checks["final_response_received"] = bool(second_turn.response.message.content)
        checks["no_second_tool_call"] = not second_turn.tool_calls
        checks["correlated_call_id"] = second_request.messages[-1].tool_call_id == call.call_id
        if not second_turn.response.message.content or second_turn.tool_calls:
            raise ProviderError(
                ProviderErrorCode.INVALID_RESPONSE,
                "readiness probe did not receive a final text response",
                retryable=False,
            )
        snapshot = ledger.snapshot(workflow_id)
        elapsed = time.perf_counter() - started
        checks["attempts_within_limit"] = (
            snapshot.provider_attempts <= snapshot.limits.max_provider_attempts
        )
        checks["tools_within_limit"] = (
            snapshot.tool_executions <= snapshot.limits.max_tool_executions
        )
        checks["tokens_within_limit"] = (
            snapshot.used_tokens + snapshot.reserved_tokens <= snapshot.limits.max_tokens
        )
        checks["time_within_limit"] = snapshot.active_seconds <= snapshot.limits.max_active_seconds
        report = {
            "status": "PASS",
            "evidence_kind": "live_provider_readiness",
            "live_readiness": True,
            "provider": first_turn.response.provider,
            "model": first_turn.response.model,
            "base_url_origin": _origin(connection.base_url),
            "checked_at_utc": _utc_now(),
            "elapsed_seconds": round(elapsed, 3),
            "attempts": snapshot.provider_attempts,
            "tool_executions": snapshot.tool_executions,
            "tool_name": call.name,
            "tool_call_id": call.call_id,
            "usage": _combined_usage(usage_parts),
            "usage_known": True,
            "cost": _cost_projection(snapshot, settings.currency),
            "limits": snapshot.limits.public_dict(),
            "workflow_id": workflow_id,
            "run_id": run_id,
            "checks": checks,
            "note": (
                "Live DeepSeek readiness evidence. Prompts, completions, reasoning text, "
                "authorization headers, and credentials are deliberately omitted."
            ),
        }
        return ReadinessOutcome("PASS", 0, report)
    except ProviderError as exc:
        snapshot = _safe_snapshot(ledger, workflow_id)
        report = {
            "status": "FAILED",
            "evidence_kind": "live_provider_readiness",
            "live_readiness": False,
            "provider": "deepseek",
            "model": connection.model,
            "base_url_origin": _origin(connection.base_url),
            "checked_at_utc": _utc_now(),
            "attempts": None if snapshot is None else snapshot.provider_attempts,
            "tool_executions": None if snapshot is None else snapshot.tool_executions,
            "usage": _combined_usage(usage_parts) if usage_parts else None,
            "cost": None if snapshot is None else _cost_projection(snapshot, settings.currency),
            "checks": checks,
            "error": exc.as_dict(),
            "workflow_id": workflow_id,
            "run_id": run_id,
        }
        return ReadinessOutcome("FAILED", 1, report)
    except Exception as exc:  # noqa: BLE001 - keep a redacted failure record
        report = {
            "status": "FAILED",
            "evidence_kind": "live_provider_readiness",
            "live_readiness": False,
            "provider": "deepseek",
            "model": connection.model,
            "checked_at_utc": _utc_now(),
            "checks": checks,
            "error": {
                "code": "unexpected_failure",
                "message": f"unexpected readiness failure ({type(exc).__name__})",
            },
            "workflow_id": workflow_id,
            "run_id": run_id,
        }
        return ReadinessOutcome("FAILED", 1, report)
    finally:
        if provider is not None:
            provider.close()
        ledger.close()


def _safe_snapshot(ledger: BudgetLedger, workflow_id: str) -> Any:
    try:
        return ledger.snapshot(workflow_id)
    except ProviderError:
        return None


def _validate_live_identity(
    response: ProviderResponse,
    *,
    provider_name: str,
    expected_model: str,
) -> None:
    if response.provider != provider_name or response.model != expected_model:
        raise ProviderError(
            ProviderErrorCode.INVALID_RESPONSE,
            "readiness probe response identity did not match the configured provider and model",
            retryable=False,
            details={
                "expected_provider": provider_name,
                "actual_provider": response.provider,
                "expected_model": expected_model,
                "actual_model": response.model,
            },
        )


def _combined_usage(parts: list[TokenUsage]) -> dict[str, Any] | None:
    if not parts:
        return None
    prompt = sum(part.prompt_tokens for part in parts)
    completion = sum(part.completion_tokens for part in parts)
    hits = [part.cache_hit_tokens for part in parts]
    misses = [part.cache_miss_tokens for part in parts]
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": prompt + completion,
        "cache_hit_tokens": None if any(value is None for value in hits) else sum(hits),
        "cache_miss_tokens": None if any(value is None for value in misses) else sum(misses),
        "source": "provider-reported",
    }


def _cost_projection(snapshot: Any, currency: str) -> dict[str, Any]:
    return {
        "currency": currency,
        "actual_micros": snapshot.actual_cost_micros,
        "actual": micros_to_string(snapshot.actual_cost_micros),
        "reserved_micros": snapshot.reserved_cost_micros,
        "workflow_cap_micros": snapshot.workflow_cap_micros,
        "run_cap_micros": snapshot.run_cap_micros,
        "run_actual_micros": snapshot.run_actual_cost_micros,
    }


def _fixture_result(machine_id: str) -> str:
    return json.dumps(
        {
            "machine_id": machine_id,
            "status": "idle",
            "source": "local-synthetic-fixture",
            "authoritative_state": False,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def execute_fixture_machine_status(arguments: dict[str, Any]) -> str:
    """Execute the harmless local fixture tool after local schema validation."""

    machine_id = arguments.get("machine_id")
    if machine_id != "M1" or set(arguments) != {"machine_id"}:
        raise ValueError("fixture_machine_status accepts exactly machine_id='M1'")
    return _fixture_result(machine_id)


def _origin(base_url: str) -> str:
    from urllib.parse import urlsplit

    parsed = urlsplit(base_url)
    return f"{parsed.scheme}://{parsed.netloc}"


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _write_report(output_path: Path, report: dict[str, Any]) -> None:
    output_path.mkdir(parents=True, exist_ok=True)
    (output_path / "readiness.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    lines = [
        f"# Provider readiness: {report.get('status', 'UNKNOWN')}",
        "",
        f"- Evidence kind: `{report.get('evidence_kind', 'unknown')}`",
        f"- Live readiness claimed: `{report.get('live_readiness', False)}`",
    ]
    if report.get("provider"):
        lines.append(f"- Provider: `{report['provider']}`")
    if report.get("model"):
        lines.append(f"- Model: `{report['model']}`")
    if report.get("label"):
        lines.extend(["", f"> {report['label']}"])
    if report.get("reason"):
        lines.extend(["", f"Reason: {report['reason']}"])
    if report.get("usage"):
        usage = report["usage"]
        lines.append(
            f"- Tokens: prompt={usage.get('prompt_tokens')} "
            f"completion={usage.get('completion_tokens')} total={usage.get('total_tokens')}"
        )
    if report.get("cost"):
        cost = report["cost"]
        lines.append(
            f"- Cost: {cost.get('actual')} {cost.get('currency')} actual "
            f"(cap {cost.get('workflow_cap_micros')} micro-units per workflow)"
        )
    if report.get("error"):
        error = report["error"]
        lines.append(f"- Error: `{error.get('code', error.get('type', 'unknown'))}`")
    lines.append("")
    lines.append(
        "This summary is redacted: it contains no credential, prompt, completion, "
        "reasoning text, or authorization header."
    )
    (output_path / "SUMMARY.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


_SYSTEM_PROMPT = (
    "You are a bounded provider-readiness probe for a synthetic manufacturing demo. "
    "Call the fixture_machine_status tool exactly once with machine_id M1, then "
    "summarize only the status returned by that local fixture. Never invent a status."
)
_USER_PROMPT = "Check fixture machine M1 and report its status."
