"""S09-A04: classified retries, exact attempt counts, and hidden-retry control."""

from __future__ import annotations

import pytest

from shopfloor_pilot.budgets import (
    AttemptOutcome,
    BudgetLedger,
    ProviderLimits,
    UsageCharge,
)
from shopfloor_pilot.providers.bounded import BoundedProvider
from shopfloor_pilot.providers.clock import FakeClock
from shopfloor_pilot.providers.config import DeepSeekSettings
from shopfloor_pilot.providers.costs import TokenRates
from shopfloor_pilot.providers.errors import (
    BudgetExceededError,
    ProviderError,
    ProviderErrorCode,
)
from shopfloor_pilot.providers.fake import (
    FakeFailure,
    FakeReply,
    ScriptedFakeProvider,
    scripted_text,
)
from shopfloor_pilot.providers.models import ProviderRequest
from shopfloor_pilot.providers.retry import RetryPolicy


def _error(code: ProviderErrorCode) -> ProviderError:
    return ProviderError(
        code,
        "fixture failure",
        retryable=code
        in {
            ProviderErrorCode.RATE_LIMITED,
            ProviderErrorCode.SERVER_ERROR,
            ProviderErrorCode.TIMEOUT,
            ProviderErrorCode.CONNECTION,
        },
    )


def _bounded(
    fake: ScriptedFakeProvider,
    ledger: BudgetLedger,
    workflow_id: str,
    pricing: TokenRates,
    clock: FakeClock,
    *,
    retries: int = 2,
) -> BoundedProvider:
    return BoundedProvider(
        fake,
        ledger,
        workflow_id,
        pricing,
        clock=clock,
        estimator=lambda _request: 100,
        retry_policy=RetryPolicy(max_retries_per_request=retries),
    )


@pytest.mark.parametrize(
    "code",
    [
        ProviderErrorCode.AUTHENTICATION,
        ProviderErrorCode.INSUFFICIENT_BALANCE,
        ProviderErrorCode.INVALID_REQUEST,
    ],
)
def test_permanent_failures_are_not_retried(
    ledger: BudgetLedger,
    workflow_id: str,
    pricing: TokenRates,
    clock: FakeClock,
    text_request: ProviderRequest,
    code: ProviderErrorCode,
) -> None:
    fake = ScriptedFakeProvider([FakeFailure(_error(code))], clock=clock)
    bounded = _bounded(fake, ledger, workflow_id, pricing, clock)
    with pytest.raises(ProviderError) as exc:
        bounded.complete(text_request, request_id="request-permanent-failure")
    assert exc.value.code is code
    assert fake.calls == 1
    snapshot = ledger.snapshot(workflow_id)
    assert snapshot.provider_attempts == 1
    assert snapshot.used_tokens == 0
    assert snapshot.actual_cost_micros == 0
    assert snapshot.status.value == "running"


@pytest.mark.parametrize(
    "code",
    [
        ProviderErrorCode.RATE_LIMITED,
        ProviderErrorCode.SERVER_ERROR,
        ProviderErrorCode.CONNECTION,
        ProviderErrorCode.TIMEOUT,
    ],
)
def test_transient_failures_retry_exactly_twice(
    ledger: BudgetLedger,
    workflow_id: str,
    pricing: TokenRates,
    clock: FakeClock,
    text_request: ProviderRequest,
    code: ProviderErrorCode,
) -> None:
    fake = ScriptedFakeProvider(
        [FakeFailure(_error(code)), FakeFailure(_error(code)), FakeFailure(_error(code))],
        clock=clock,
    )
    bounded = _bounded(fake, ledger, workflow_id, pricing, clock)
    with pytest.raises(ProviderError) as exc:
        bounded.complete(text_request, request_id="request-transient-failure")
    assert exc.value.code is code
    assert exc.value.attempts == 3
    assert fake.calls == 3
    snapshot = ledger.snapshot(workflow_id)
    assert snapshot.provider_attempts == 3
    assert snapshot.used_tokens == 3 * (100 + 64)
    assert snapshot.active_seconds == pytest.approx(3.0)
    assert clock.sleep_calls == [1.0, 2.0]


def test_transient_failure_then_success(
    ledger: BudgetLedger,
    workflow_id: str,
    pricing: TokenRates,
    clock: FakeClock,
    text_request: ProviderRequest,
) -> None:
    fake = ScriptedFakeProvider(
        [
            FakeFailure(_error(ProviderErrorCode.RATE_LIMITED), advance_seconds=0.25),
            FakeReply(scripted_text(content="recovered")),
        ],
        clock=clock,
    )
    bounded = _bounded(fake, ledger, workflow_id, pricing, clock)
    turn = bounded.complete(text_request, request_id="request-transient-success")
    assert turn.response.message.content == "recovered"
    assert fake.calls == 2
    snapshot = ledger.snapshot(workflow_id)
    assert snapshot.provider_attempts == 2
    assert snapshot.active_seconds == pytest.approx(1.25)
    assert snapshot.status.value == "running"


def test_total_attempt_cap_is_eight(
    tmp_path,
    pricing: TokenRates,
    clock: FakeClock,
    text_request: ProviderRequest,
) -> None:
    limits = ProviderLimits(max_provider_attempts=8, max_retries_per_request=2)
    ledger = BudgetLedger(tmp_path / "attempt-cap.sqlite3")
    try:
        ledger.open_run(run_id="run-attempt-cap", currency="USD", cap_micros=50_000)
        ledger.open_workflow(
            workflow_id="workflow-attempt-cap",
            run_id="run-attempt-cap",
            limits=limits,
            workflow_cap_micros=25_000,
        )
        fake = ScriptedFakeProvider(
            [FakeFailure(_error(ProviderErrorCode.SERVER_ERROR)) for _ in range(10)],
            clock=clock,
        )
        bounded = _bounded(
            fake,
            ledger,
            "workflow-attempt-cap",
            pricing,
            clock,
        )
        with pytest.raises(ProviderError):
            bounded.complete(text_request, request_id="request-total-cap-1")
        with pytest.raises(ProviderError):
            bounded.complete(text_request, request_id="request-total-cap-2")
        with pytest.raises(BudgetExceededError) as exc:
            bounded.complete(text_request, request_id="request-total-cap-3")
        assert exc.value.limit == "provider_attempts"
        snapshot = ledger.snapshot("workflow-attempt-cap")
        assert snapshot.provider_attempts == 8
        assert snapshot.status.value == "exhausted"
        assert fake.calls == 8
    finally:
        ledger.close()


def test_retry_limit_is_persisted_per_request(
    ledger: BudgetLedger,
    workflow_id: str,
) -> None:
    for index in range(3):
        permit = ledger.begin_attempt(
            workflow_id=workflow_id,
            request_id="request-retry-limit",
            reserved_tokens=100,
            reserved_cost_micros=10,
        )
        ledger.finish_attempt(
            permit,
            outcome=AttemptOutcome.FAILURE,
            latency_seconds=0.0,
            charge=UsageCharge.ZERO,
            error_code=ProviderErrorCode.INVALID_REQUEST,
        )
        assert permit.attempt_number == index + 1
    with pytest.raises(BudgetExceededError) as exc:
        ledger.begin_attempt(
            workflow_id=workflow_id,
            request_id="request-retry-limit",
            reserved_tokens=100,
            reserved_cost_micros=10,
        )
    assert exc.value.limit == "retries_per_request"
    snapshot = ledger.snapshot(workflow_id)
    assert snapshot.provider_attempts == 3


def test_backoff_is_persisted_before_process_can_crash(
    ledger: BudgetLedger,
    workflow_id: str,
    pricing: TokenRates,
    text_request: ProviderRequest,
) -> None:
    class CrashOnSleep(FakeClock):
        def sleep(self, seconds: float) -> None:
            del seconds
            raise SystemExit("simulated crash during backoff")

    clock = CrashOnSleep()
    fake = ScriptedFakeProvider(
        [FakeFailure(_error(ProviderErrorCode.SERVER_ERROR))],
        clock=clock,
    )
    bounded = _bounded(fake, ledger, workflow_id, pricing, clock)
    with pytest.raises(SystemExit, match="simulated crash"):
        bounded.complete(text_request, request_id="request-crash-in-backoff")
    snapshot = ledger.snapshot(workflow_id)
    assert snapshot.provider_attempts == 1
    assert snapshot.active_seconds == pytest.approx(1.0)


def test_same_logical_request_keeps_retry_identity_across_restart(
    tmp_path,
    pricing: TokenRates,
    text_request: ProviderRequest,
) -> None:
    path = tmp_path / "request-restart.sqlite3"
    limits = ProviderLimits(max_retries_per_request=2)
    first = BudgetLedger(path)
    first.open_run(run_id="run-request-restart", currency="USD", cap_micros=50_000)
    first.open_workflow(
        workflow_id="workflow-request-restart",
        run_id="run-request-restart",
        limits=limits,
        workflow_cap_micros=25_000,
    )
    first.begin_attempt(
        workflow_id="workflow-request-restart",
        request_id="request-persistent",
        reserved_tokens=164,
        reserved_cost_micros=107,
    )
    first.close()

    second = BudgetLedger(path)
    try:
        second.open_run(run_id="run-request-restart", currency="USD", cap_micros=50_000)
        second.open_workflow(
            workflow_id="workflow-request-restart",
            run_id="run-request-restart",
            limits=limits,
            workflow_cap_micros=25_000,
        )
        clock = FakeClock()
        fake = ScriptedFakeProvider(
            [
                FakeFailure(_error(ProviderErrorCode.SERVER_ERROR)),
                FakeFailure(_error(ProviderErrorCode.SERVER_ERROR)),
                FakeReply(scripted_text(content="must not be reached")),
            ],
            clock=clock,
        )
        bounded = _bounded(
            fake,
            second,
            "workflow-request-restart",
            pricing,
            clock,
        )
        with pytest.raises(BudgetExceededError) as exc:
            bounded.complete(text_request, request_id="request-persistent")
        assert exc.value.limit == "retries_per_request"
        assert fake.calls == 2
        assert second.snapshot("workflow-request-restart").provider_attempts == 3
    finally:
        second.close()


def test_retry_policy_cannot_exceed_persisted_limit(
    ledger: BudgetLedger,
    workflow_id: str,
    pricing: TokenRates,
    clock: FakeClock,
) -> None:
    fake = ScriptedFakeProvider([], clock=clock)
    with pytest.raises(BudgetExceededError):
        BoundedProvider(
            fake,
            ledger,
            workflow_id,
            pricing,
            clock=clock,
            retry_policy=RetryPolicy(max_retries_per_request=3),
        )


def test_sdk_hidden_retries_are_disabled(
    monkeypatch: pytest.MonkeyPatch,
    limits: ProviderLimits,
) -> None:
    captured: dict[str, object] = {}

    class StubOpenAI:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

    import shopfloor_pilot.providers.deepseek as deepseek_module

    monkeypatch.setattr(deepseek_module, "OpenAI", StubOpenAI)
    settings = DeepSeekSettings(
        api_key="dummy-s09-test-key",
        base_url="https://api.deepseek.com",
        model="deepseek-flash",
        thinking=False,
        reasoning_effort=None,
        limits=limits,
    )
    deepseek_module.DeepSeekProvider(settings)
    assert captured["max_retries"] == 0
    assert captured["timeout"] == limits.attempt_timeout_seconds
    assert captured["base_url"] == "https://api.deepseek.com"
