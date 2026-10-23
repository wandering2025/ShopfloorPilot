"""S09-A05/A06: deadlines, pause/restart, token and currency boundaries."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from shopfloor_pilot.budgets import (
    AttemptOutcome,
    BudgetLedger,
    LedgerError,
    ProviderLimits,
    ToolReceipt,
    UsageCharge,
    WorkflowStatus,
)
from shopfloor_pilot.providers.bounded import BoundedProvider, estimate_request_tokens
from shopfloor_pilot.providers.clock import FakeClock
from shopfloor_pilot.providers.costs import TokenRates
from shopfloor_pilot.providers.errors import (
    BudgetExceededError,
    ProviderError,
    ProviderErrorCode,
    WorkflowStateError,
)
from shopfloor_pilot.providers.fake import FakeReply, ScriptedFakeProvider, scripted_text
from shopfloor_pilot.providers.models import ProviderRequest
from shopfloor_pilot.providers.retry import RetryPolicy

TARIFF_URL = "https://api-docs.deepseek.com/quick_start/pricing"


def _rates(
    *,
    hit: str = "0.006",
    miss: str = "0.30",
    output: str = "1.20",
    currency: str = "USD",
) -> TokenRates:
    return TokenRates(
        currency=currency,
        input_cache_hit_per_million=Decimal(hit),
        input_cache_miss_per_million=Decimal(miss),
        output_per_million=Decimal(output),
        tariff_date=date(2026, 10, 21),
        source_url=TARIFF_URL,
        model="deepseek-flash",
    )


def _open(
    tmp_path: Path,
    *,
    limits: ProviderLimits,
    workflow_cap_micros: int = 25_000,
    run_cap_micros: int = 50_000,
    workflow_id: str = "workflow-budget",
    run_id: str = "run-budget",
) -> BudgetLedger:
    ledger = BudgetLedger(tmp_path / f"{workflow_id}.sqlite3")
    ledger.open_run(run_id=run_id, currency="USD", cap_micros=run_cap_micros)
    ledger.open_workflow(
        workflow_id=workflow_id,
        run_id=run_id,
        limits=limits,
        workflow_cap_micros=workflow_cap_micros,
    )
    return ledger


def _bounded(
    ledger: BudgetLedger,
    fake: ScriptedFakeProvider,
    workflow_id: str,
    pricing: TokenRates,
    clock: FakeClock,
    *,
    estimator: int = 100,
    max_retries: int = 2,
) -> BoundedProvider:
    return BoundedProvider(
        fake,
        ledger,
        workflow_id,
        pricing,
        clock=clock,
        estimator=lambda _request: estimator,
        retry_policy=RetryPolicy(max_retries_per_request=max_retries),
    )


def test_attempt_deadline_is_enforced(tmp_path: Path, clock: FakeClock) -> None:
    limits = ProviderLimits(
        attempt_timeout_seconds=30.0,
        max_active_seconds=120.0,
        max_retries_per_request=0,
    )
    with _open(tmp_path, limits=limits) as ledger:
        fake = ScriptedFakeProvider(
            [FakeReply(scripted_text(content="late"), advance_seconds=30.001)],
            clock=clock,
        )
        bounded = _bounded(
            ledger,
            fake,
            "workflow-budget",
            _rates(),
            clock,
            max_retries=0,
        )
        request = _text_request()
        with pytest.raises(ProviderError) as exc:
            bounded.complete(request, request_id="request-attempt-deadline")
        assert exc.value.code is ProviderErrorCode.TIMEOUT
        assert exc.value.attempts == 1
        snapshot = ledger.snapshot("workflow-budget")
        assert snapshot.provider_attempts == 1
        assert snapshot.active_seconds == pytest.approx(30.001)
        assert snapshot.status is WorkflowStatus.RUNNING


def test_response_at_exact_attempt_deadline_is_rejected(tmp_path: Path, clock: FakeClock) -> None:
    limits = ProviderLimits(
        attempt_timeout_seconds=30.0,
        max_active_seconds=120.0,
        max_retries_per_request=0,
    )
    with _open(tmp_path, limits=limits) as ledger:
        fake = ScriptedFakeProvider(
            [FakeReply(scripted_text(content="at-boundary"), advance_seconds=30.0)],
            clock=clock,
        )
        bounded = _bounded(
            ledger,
            fake,
            "workflow-budget",
            _rates(),
            clock,
            max_retries=0,
        )
        with pytest.raises(ProviderError) as exc:
            bounded.complete(_text_request(), request_id="request-at-deadline")
        assert exc.value.code is ProviderErrorCode.TIMEOUT
        snapshot = ledger.snapshot("workflow-budget")
        assert snapshot.active_seconds == pytest.approx(30.0)
        assert snapshot.provider_attempts == 1


def test_response_just_below_attempt_deadline_is_accepted(tmp_path: Path, clock: FakeClock) -> None:
    limits = ProviderLimits(
        attempt_timeout_seconds=30.0,
        max_active_seconds=120.0,
        max_retries_per_request=0,
    )
    with _open(tmp_path, limits=limits) as ledger:
        fake = ScriptedFakeProvider(
            [FakeReply(scripted_text(content="before-boundary"), advance_seconds=29.999)],
            clock=clock,
        )
        bounded = _bounded(
            ledger,
            fake,
            "workflow-budget",
            _rates(),
            clock,
            max_retries=0,
        )
        turn = bounded.complete(_text_request(), request_id="request-before-deadline")
        assert turn.response.message.content == "before-boundary"
        assert ledger.snapshot("workflow-budget").active_seconds == pytest.approx(29.999)


def test_active_deadline_is_enforced_at_120_seconds(tmp_path: Path, clock: FakeClock) -> None:
    limits = ProviderLimits(
        attempt_timeout_seconds=30.0,
        max_active_seconds=120.0,
        max_retries_per_request=0,
    )
    with _open(tmp_path, limits=limits) as ledger:
        ledger.record_backoff("workflow-budget", 119.5)
        fake = ScriptedFakeProvider(
            [FakeReply(scripted_text(content="last"), advance_seconds=0.5)],
            clock=clock,
        )
        bounded = _bounded(
            ledger,
            fake,
            "workflow-budget",
            _rates(),
            clock,
            max_retries=0,
        )
        with pytest.raises(ProviderError) as exc:
            bounded.complete(_text_request(), request_id="request-active-deadline")
        assert exc.value.code is ProviderErrorCode.ACTIVE_DEADLINE
        snapshot = ledger.snapshot("workflow-budget")
        assert snapshot.active_seconds == pytest.approx(120.0)
        assert snapshot.status is WorkflowStatus.EXHAUSTED
        with pytest.raises(BudgetExceededError):
            bounded.complete(_text_request(), request_id="request-after-exhaustion")


def test_human_pause_excludes_waiting_time(tmp_path: Path, clock: FakeClock) -> None:
    with _open(tmp_path, limits=ProviderLimits()) as ledger:
        fake = ScriptedFakeProvider(
            [FakeReply(scripted_text(content="after approval"))],
            clock=clock,
        )
        bounded = _bounded(ledger, fake, "workflow-budget", _rates(), clock)
        ledger.pause_for_human("workflow-budget", "awaiting operator approval")
        clock.advance(5_000)
        with pytest.raises(WorkflowStateError):
            bounded.complete(_text_request(), request_id="request-while-paused")
        snapshot = ledger.snapshot("workflow-budget")
        assert snapshot.active_seconds == 0.0
        assert snapshot.status is WorkflowStatus.PAUSED_HUMAN
        ledger.resume("workflow-budget")
        bounded.complete(_text_request(), request_id="request-after-resume")
        assert ledger.snapshot("workflow-budget").active_seconds == 0.0
        assert fake.calls == 1


def test_restart_charges_orphaned_attempt_without_fresh_allowance(
    tmp_path: Path,
) -> None:
    path = tmp_path / "restart.sqlite3"
    limits = ProviderLimits()
    first = BudgetLedger(path)
    first.open_run(run_id="run-restart", currency="USD", cap_micros=50_000)
    first.open_workflow(
        workflow_id="workflow-restart",
        run_id="run-restart",
        limits=limits,
        workflow_cap_micros=25_000,
    )
    first.begin_attempt(
        workflow_id="workflow-restart",
        request_id="request-before-crash",
        reserved_tokens=164,
        reserved_cost_micros=107,
    )
    first.close()

    second = BudgetLedger(path)
    try:
        second.open_run(run_id="run-restart", currency="USD", cap_micros=50_000)
        snapshot = second.open_workflow(
            workflow_id="workflow-restart",
            run_id="run-restart",
            limits=limits,
            workflow_cap_micros=25_000,
        )
        assert snapshot.provider_attempts == 1
        assert snapshot.used_tokens == 164
        assert snapshot.reserved_tokens == 0
        assert snapshot.actual_cost_micros == 107
        assert snapshot.active_seconds == pytest.approx(30.0)
        assert snapshot.status is WorkflowStatus.RUNNING
        second.begin_attempt(
            workflow_id="workflow-restart",
            request_id="request-after-restart",
            reserved_tokens=100,
            reserved_cost_micros=10,
        )
        assert second.snapshot("workflow-restart").provider_attempts == 2
    finally:
        second.close()


def test_restart_exhausts_remaining_active_time_for_orphaned_tool(tmp_path: Path) -> None:
    path = tmp_path / "orphan-tool.sqlite3"
    limits = ProviderLimits(max_active_seconds=5.0)
    first = BudgetLedger(path)
    first.open_run(run_id="run-orphan-tool", currency="USD", cap_micros=50_000)
    first.open_workflow(
        workflow_id="workflow-orphan-tool",
        run_id="run-orphan-tool",
        limits=limits,
        workflow_cap_micros=25_000,
    )
    first.record_backoff("workflow-orphan-tool", 1.25)
    receipt = first.record_tool_execution("workflow-orphan-tool", "fixture_machine_status")
    first.close()

    second = BudgetLedger(path)
    try:
        second.open_run(run_id="run-orphan-tool", currency="USD", cap_micros=50_000)
        snapshot = second.open_workflow(
            workflow_id="workflow-orphan-tool",
            run_id="run-orphan-tool",
            limits=limits,
            workflow_cap_micros=25_000,
        )
        assert snapshot.active_seconds == pytest.approx(5.0)
        assert snapshot.status is WorkflowStatus.EXHAUSTED
        assert snapshot.last_error_code == ProviderErrorCode.ACTIVE_DEADLINE.value
        with pytest.raises(LedgerError):
            second.finish_tool_execution(receipt, active_seconds=0.1)
        with pytest.raises(WorkflowStateError):
            second.record_tool_execution("workflow-orphan-tool", "fixture_machine_status")
    finally:
        second.close()


def test_restart_with_changed_limits_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "limits.sqlite3"
    limits = ProviderLimits()
    first = BudgetLedger(path)
    first.open_run(run_id="run-limits", currency="USD", cap_micros=50_000)
    first.open_workflow(
        workflow_id="workflow-limits",
        run_id="run-limits",
        limits=limits,
        workflow_cap_micros=25_000,
    )
    first.close()
    second = BudgetLedger(path)
    try:
        second.open_run(run_id="run-limits", currency="USD", cap_micros=50_000)
        with pytest.raises(LedgerError):
            second.open_workflow(
                workflow_id="workflow-limits",
                run_id="run-limits",
                limits=ProviderLimits(max_provider_attempts=9),
                workflow_cap_micros=25_000,
            )
    finally:
        second.close()


def test_late_response_after_cancel_cannot_revive_work(tmp_path: Path) -> None:
    with _open(tmp_path, limits=ProviderLimits()) as ledger:
        permit = ledger.begin_attempt(
            workflow_id="workflow-budget",
            request_id="request-cancel",
            reserved_tokens=164,
            reserved_cost_micros=107,
        )
        ledger.cancel("workflow-budget")
        result = ledger.finish_attempt(
            permit,
            outcome=AttemptOutcome.SUCCESS,
            latency_seconds=1.0,
            charge=UsageCharge.ACTUAL,
            usage_tokens=10,
            cost_micros=1,
        )
        assert result.late is True
        assert result.state.value == "late_after_cancel"
        snapshot = ledger.snapshot("workflow-budget")
        assert snapshot.status is WorkflowStatus.CANCELLED
        assert snapshot.used_tokens == 164
        assert snapshot.actual_cost_micros == 107
        with pytest.raises(WorkflowStateError) as exc:
            ledger.begin_attempt(
                workflow_id="workflow-budget",
                request_id="request-after-cancel",
                reserved_tokens=10,
                reserved_cost_micros=1,
            )
        assert exc.value.code is ProviderErrorCode.CANCELLED


def test_token_boundaries_32000(tmp_path: Path, clock: FakeClock) -> None:
    limits = ProviderLimits(max_tokens=32_000, max_retries_per_request=0)
    with _open(
        tmp_path,
        limits=limits,
        workflow_cap_micros=100_000,
        run_cap_micros=200_000,
    ) as ledger:
        fake = ScriptedFakeProvider([FakeReply(scripted_text(content="ok"))], clock=clock)
        bounded = _bounded(
            ledger,
            fake,
            "workflow-budget",
            _rates(),
            clock,
            estimator=1_000,
            max_retries=0,
        )
        request = ProviderRequest(
            messages=(_user_message("x"),),
            max_output_tokens=31_000,
        )
        bounded.complete(request, request_id="request-token-boundary")
        assert fake.calls == 1
        second = ProviderRequest(messages=(_user_message("y"),), max_output_tokens=1)
        with pytest.raises(BudgetExceededError) as exc:
            bounded.complete(second, request_id="request-after-token-boundary")
        assert exc.value.limit == "tokens"
        assert fake.calls == 1
        assert ledger.snapshot("workflow-budget").status is WorkflowStatus.EXHAUSTED


def test_token_request_below_boundary_31999_is_sent(tmp_path: Path, clock: FakeClock) -> None:
    limits = ProviderLimits(max_tokens=32_000, max_retries_per_request=0)
    with _open(
        tmp_path,
        limits=limits,
        workflow_cap_micros=100_000,
        run_cap_micros=200_000,
    ) as ledger:
        fake = ScriptedFakeProvider([FakeReply(scripted_text(content="ok"))], clock=clock)
        bounded = _bounded(
            ledger,
            fake,
            "workflow-budget",
            _rates(),
            clock,
            estimator=1_000,
            max_retries=0,
        )
        request = ProviderRequest(
            messages=(_user_message("x"),),
            max_output_tokens=30_999,
        )
        bounded.complete(request, request_id="request-below-token-boundary")
        assert fake.calls == 1
        assert ledger.snapshot("workflow-budget").used_tokens == 31_999


@pytest.mark.parametrize(
    "content",
    (
        "ASCII-control-like-{}[]:/?&=" * 20,
        "机床故障温度振动" * 40,
        "9F2a+/=_-~!@#$%^&*()[]{}" * 25,
    ),
)
def test_production_token_estimator_bounds_serialized_utf8_bytes(content: str) -> None:
    request = ProviderRequest(messages=(_user_message(content),), max_output_tokens=16)
    payload_bytes = len(request.estimated_payload_text().encode("utf-8"))
    assert estimate_request_tokens(request) >= payload_bytes + 256


def test_production_estimator_enforces_its_real_boundary(
    tmp_path: Path,
    clock: FakeClock,
) -> None:
    request = ProviderRequest(messages=(_user_message("机床-A7f/" * 40),), max_output_tokens=17)
    estimated_total = estimate_request_tokens(request) + request.max_output_tokens
    limits = ProviderLimits(max_tokens=estimated_total, max_retries_per_request=0)
    with _open(tmp_path, limits=limits, workflow_id="workflow-estimator-at") as ledger:
        fake = ScriptedFakeProvider([FakeReply(scripted_text(content="ok"))], clock=clock)
        bounded = BoundedProvider(
            fake,
            ledger,
            "workflow-estimator-at",
            _rates(),
            clock=clock,
            retry_policy=RetryPolicy(max_retries_per_request=0),
        )
        bounded.complete(request, request_id="request-real-estimator-at")
        assert ledger.snapshot("workflow-estimator-at").used_tokens == estimated_total

    below_limits = ProviderLimits(max_tokens=estimated_total - 1, max_retries_per_request=0)
    with _open(
        tmp_path,
        limits=below_limits,
        workflow_id="workflow-estimator-above",
    ) as ledger:
        fake = ScriptedFakeProvider([], clock=clock)
        bounded = BoundedProvider(
            fake,
            ledger,
            "workflow-estimator-above",
            _rates(),
            clock=clock,
            retry_policy=RetryPolicy(max_retries_per_request=0),
        )
        with pytest.raises(BudgetExceededError) as exc:
            bounded.complete(request, request_id="request-real-estimator-above")
        assert exc.value.limit == "tokens"
        assert fake.calls == 0


def test_token_request_above_boundary_is_never_sent(tmp_path: Path, clock: FakeClock) -> None:
    limits = ProviderLimits(max_tokens=32_000, max_retries_per_request=0)
    with _open(tmp_path, limits=limits) as ledger:
        fake = ScriptedFakeProvider([], clock=clock)
        bounded = _bounded(
            ledger,
            fake,
            "workflow-budget",
            _rates(),
            clock,
            estimator=1_000,
            max_retries=0,
        )
        request = ProviderRequest(
            messages=(_user_message("x"),),
            max_output_tokens=31_001,
        )
        with pytest.raises(BudgetExceededError) as exc:
            bounded.complete(request, request_id="request-above-token-boundary")
        assert exc.value.limit == "tokens"
        assert fake.calls == 0
        assert ledger.snapshot("workflow-budget").status is WorkflowStatus.EXHAUSTED


def test_workflow_and_run_currency_caps(tmp_path: Path) -> None:
    with _open(
        tmp_path,
        limits=ProviderLimits(),
        workflow_cap_micros=5_000,
        run_cap_micros=10_000,
    ) as ledger:
        ledger.begin_attempt(
            workflow_id="workflow-budget",
            request_id="request-a",
            reserved_tokens=100,
            reserved_cost_micros=4_000,
        )
        ledger.open_workflow(
            workflow_id="workflow-b",
            run_id="run-budget",
            limits=ProviderLimits(),
            workflow_cap_micros=5_000,
        )
        ledger.begin_attempt(
            workflow_id="workflow-b",
            request_id="request-b",
            reserved_tokens=100,
            reserved_cost_micros=4_000,
        )
        ledger.open_workflow(
            workflow_id="workflow-c",
            run_id="run-budget",
            limits=ProviderLimits(),
            workflow_cap_micros=5_000,
        )
        with pytest.raises(BudgetExceededError) as exc:
            ledger.begin_attempt(
                workflow_id="workflow-c",
                request_id="request-c",
                reserved_tokens=100,
                reserved_cost_micros=3_000,
            )
        assert exc.value.limit == "run_cost"
        assert ledger.snapshot("workflow-c").status is WorkflowStatus.EXHAUSTED

        ledger.open_workflow(
            workflow_id="workflow-d",
            run_id="run-budget",
            limits=ProviderLimits(),
            workflow_cap_micros=1_000,
        )
        with pytest.raises(BudgetExceededError) as exc:
            ledger.begin_attempt(
                workflow_id="workflow-d",
                request_id="request-d",
                reserved_tokens=100,
                reserved_cost_micros=1_001,
            )
        assert exc.value.limit == "workflow_cost"


def test_actual_usage_reconciles_once(tmp_path: Path) -> None:
    with _open(tmp_path, limits=ProviderLimits()) as ledger:
        permit = ledger.begin_attempt(
            workflow_id="workflow-budget",
            request_id="request-usage",
            reserved_tokens=164,
            reserved_cost_micros=107,
        )
        result = ledger.finish_attempt(
            permit,
            outcome=AttemptOutcome.SUCCESS,
            latency_seconds=0.5,
            charge=UsageCharge.ACTUAL,
            usage_tokens=120,
            cost_micros=42,
        )
        assert result.used_tokens == 120
        snapshot = ledger.snapshot("workflow-budget")
        assert snapshot.used_tokens == 120
        assert snapshot.reserved_tokens == 0
        assert snapshot.actual_cost_micros == 42
        with pytest.raises(LedgerError):
            ledger.finish_attempt(
                permit,
                outcome=AttemptOutcome.SUCCESS,
                latency_seconds=0.5,
                charge=UsageCharge.ACTUAL,
                usage_tokens=120,
                cost_micros=42,
            )


def test_missing_usage_retains_conservative_reservation(tmp_path: Path, clock: FakeClock) -> None:
    with _open(tmp_path, limits=ProviderLimits()) as ledger:
        fake = ScriptedFakeProvider([FakeReply(scripted_text(content="no usage"))], clock=clock)
        bounded = _bounded(ledger, fake, "workflow-budget", _rates(), clock)
        bounded.complete(_text_request(), request_id="request-missing-usage")
        snapshot = ledger.snapshot("workflow-budget")
        assert snapshot.used_tokens == 164
        assert snapshot.reserved_tokens == 0
        assert snapshot.actual_cost_micros == 107
        assert snapshot.reserved_cost_micros == 0


def test_tool_execution_cap(tmp_path: Path) -> None:
    limits = ProviderLimits(max_tool_executions=16)
    with _open(tmp_path, limits=limits) as ledger:
        receipts = [
            ledger.record_tool_execution("workflow-budget", "fixture_machine_status")
            for _ in range(16)
        ]
        for receipt in receipts:
            ledger.finish_tool_execution(receipt, active_seconds=0.01)
        with pytest.raises(BudgetExceededError) as exc:
            ledger.record_tool_execution("workflow-budget", "fixture_machine_status")
        assert exc.value.limit == "tool_executions"
        assert ledger.snapshot("workflow-budget").status is WorkflowStatus.EXHAUSTED


def test_tool_receipt_cannot_cross_workflow_or_tool_identity(tmp_path: Path) -> None:
    with _open(tmp_path, limits=ProviderLimits()) as ledger:
        ledger.open_workflow(
            workflow_id="workflow-other",
            run_id="run-budget",
            limits=ProviderLimits(),
            workflow_cap_micros=25_000,
        )
        receipt = ledger.record_tool_execution("workflow-budget", "fixture_machine_status")
        forged_workflow = ToolReceipt(
            execution_id=receipt.execution_id,
            workflow_id="workflow-other",
            tool_name=receipt.tool_name,
        )
        forged_tool = ToolReceipt(
            execution_id=receipt.execution_id,
            workflow_id=receipt.workflow_id,
            tool_name="different_tool",
        )
        with pytest.raises(LedgerError, match="does not match"):
            ledger.finish_tool_execution(forged_workflow, active_seconds=3.0)
        with pytest.raises(LedgerError, match="does not match"):
            ledger.finish_tool_execution(forged_tool, active_seconds=3.0)
        assert ledger.snapshot("workflow-budget").active_seconds == 0.0
        assert ledger.snapshot("workflow-other").active_seconds == 0.0
        snapshot = ledger.finish_tool_execution(receipt, active_seconds=0.5)
        assert snapshot.active_seconds == pytest.approx(0.5)


def _text_request() -> ProviderRequest:
    return ProviderRequest(messages=(_user_message("hello"),), max_output_tokens=64)


def _user_message(content: str):
    from shopfloor_pilot.providers.models import MessageRole, ProviderMessage

    return ProviderMessage(role=MessageRole.USER, content=content)
