"""Budget-, retry-, and deadline-enforcing provider wrapper."""

from __future__ import annotations

from collections.abc import Callable

from ..budgets import (
    AttemptOutcome,
    AttemptPermit,
    BudgetLedger,
    ProviderLimits,
    UsageCharge,
    WorkflowStatus,
)
from .base import Provider
from .clock import Clock, SystemClock
from .costs import TokenRates
from .costs import estimate_request_tokens as _estimate_tokens
from .errors import (
    ZERO_CHARGE_CODES,
    BudgetExceededError,
    ProviderError,
    ProviderErrorCode,
    WorkflowStateError,
)
from .models import ProviderRequest, ProviderTurn
from .retry import RetryPolicy
from .validation import validate_request, validate_response_turn


def estimate_request_tokens(request: ProviderRequest) -> int:
    """Conservative estimate from the exact serialized request projection."""

    text = request.estimated_payload_text()
    items = len(request.messages) + len(request.tools)
    return _estimate_tokens(
        utf8_bytes=len(text.encode("utf-8")),
        items=items,
    )


class BoundedProvider:
    """Wrap one non-retrying transport with the shared persistent ledger."""

    def __init__(
        self,
        provider: Provider,
        ledger: BudgetLedger,
        workflow_id: str,
        pricing: TokenRates,
        *,
        retry_policy: RetryPolicy | None = None,
        clock: Clock | None = None,
        estimator: Callable[[ProviderRequest], int] = estimate_request_tokens,
    ) -> None:
        if not isinstance(pricing, TokenRates):
            raise TypeError("pricing must be a TokenRates value")
        self._provider = provider
        self._ledger = ledger
        self._workflow_id = workflow_id
        self._pricing = pricing
        self._clock = clock or SystemClock()
        self._estimator = estimator
        snapshot = ledger.snapshot(workflow_id)
        if snapshot.currency != pricing.currency:
            raise BudgetExceededError(
                "currency",
                "provider tariff currency does not match the run ledger currency",
            )
        self._limits: ProviderLimits = snapshot.limits
        policy = retry_policy or RetryPolicy(
            max_retries_per_request=self._limits.max_retries_per_request
        )
        if policy.max_retries_per_request > self._limits.max_retries_per_request:
            raise BudgetExceededError(
                "retries_per_request",
                "retry policy exceeds the persisted per-request retry limit",
            )
        self._retry_policy = policy

    @property
    def provider_name(self) -> str:
        return self._provider.provider_name

    def complete(self, request: ProviderRequest, *, request_id: str) -> ProviderTurn:
        """Complete one persistently identified logical request.

        Callers must persist and reuse ``request_id`` when resuming after a
        process restart.  This prevents a restart from granting the same
        logical request a fresh retry allowance.
        """

        validate_request(request)
        estimated_prompt = int(self._estimator(request))
        if estimated_prompt < 1:
            raise BudgetExceededError("tokens", "token estimator returned a non-positive value")
        reserved_tokens = estimated_prompt + request.max_output_tokens
        reserved_cost = self._pricing.reserve_micros(estimated_prompt, request.max_output_tokens)
        attempts_allowed = self._retry_policy.max_retries_per_request + 1
        for attempt_index in range(attempts_allowed):
            permit = self._ledger.begin_attempt(
                workflow_id=self._workflow_id,
                request_id=request_id,
                reserved_tokens=reserved_tokens,
                reserved_cost_micros=reserved_cost,
            )
            started_at = self._clock.monotonic()
            try:
                response = self._provider.complete(
                    request,
                    timeout_seconds=permit.timeout_seconds,
                )
            except ProviderError as exc:
                latency = self._elapsed(started_at)
                self._finish_failure(permit, exc, latency)
                if self._active_deadline_reached():
                    raise self._deadline_error(attempt_index + 1) from None
                if not exc.retryable or attempt_index + 1 >= attempts_allowed:
                    raise exc.with_attempts(attempt_index + 1) from None
                self._backoff(attempt_index, exc)
                continue
            except Exception as exc:  # noqa: BLE001 - never leak transport internals
                latency = self._elapsed(started_at)
                failure = ProviderError(
                    ProviderErrorCode.INVALID_RESPONSE,
                    f"unexpected provider failure ({type(exc).__name__})",
                    retryable=False,
                    secrets=(),
                )
                self._finish_failure(permit, failure, latency)
                raise failure.with_attempts(attempt_index + 1) from None

            latency = self._elapsed(started_at)
            if latency >= permit.timeout_seconds - 1e-9:
                timeout = ProviderError(
                    ProviderErrorCode.TIMEOUT,
                    "provider response reached the attempt deadline",
                    retryable=True,
                )
                self._finish_failure(permit, timeout, latency)
                if self._active_deadline_reached():
                    raise self._deadline_error(attempt_index + 1) from None
                if attempt_index + 1 >= attempts_allowed:
                    raise timeout.with_attempts(attempt_index + 1) from None
                self._backoff(attempt_index, timeout)
                continue

            try:
                turn = validate_response_turn(response, request)
            except ProviderError as exc:
                usage = response.usage if hasattr(response, "usage") else None
                charge = UsageCharge.ACTUAL if usage is not None else UsageCharge.RESERVED
                cost = self._pricing.actual_micros(usage) if usage is not None else None
                self._finish_failure(
                    permit,
                    exc,
                    latency,
                    charge=charge,
                    usage_tokens=usage.total_tokens if usage is not None else None,
                    cost_micros=cost,
                )
                raise exc.with_attempts(attempt_index + 1) from None

            usage = response.usage
            if usage is None:
                result = self._ledger.finish_attempt(
                    permit,
                    outcome=AttemptOutcome.SUCCESS,
                    latency_seconds=latency,
                    charge=UsageCharge.RESERVED,
                )
            else:
                result = self._ledger.finish_attempt(
                    permit,
                    outcome=AttemptOutcome.SUCCESS,
                    latency_seconds=latency,
                    charge=UsageCharge.ACTUAL,
                    usage_tokens=usage.total_tokens,
                    cost_micros=self._pricing.actual_micros(usage),
                )
            if result.late:
                raise WorkflowStateError(
                    ProviderErrorCode.CANCELLED,
                    "workflow was cancelled before the provider response was finalized",
                )
            if self._active_deadline_reached():
                raise self._deadline_error(attempt_index + 1)
            if result.over_budget:
                raise BudgetExceededError(
                    "actual_usage",
                    "reconciled provider usage exceeded a configured allowance",
                    details={"attempts": attempt_index + 1},
                )
            return turn
        raise AssertionError("retry loop exhausted without a result")  # pragma: no cover

    def _finish_failure(
        self,
        permit: AttemptPermit,
        error: ProviderError,
        latency: float,
        *,
        charge: UsageCharge | None = None,
        usage_tokens: int | None = None,
        cost_micros: int | None = None,
    ) -> None:
        if charge is None:
            charge = UsageCharge.ZERO if error.code in ZERO_CHARGE_CODES else UsageCharge.RESERVED
        result = self._ledger.finish_attempt(
            permit,
            outcome=AttemptOutcome.FAILURE,
            latency_seconds=latency,
            charge=charge,
            error_code=error.code,
            retryable=error.retryable,
            usage_tokens=usage_tokens,
            cost_micros=cost_micros,
        )
        if result.late:
            raise WorkflowStateError(
                ProviderErrorCode.CANCELLED,
                "workflow was cancelled before the provider failure was finalized",
            )

    def _backoff(self, retry_index: int, error: ProviderError) -> None:
        delay = self._retry_policy.backoff(retry_index, error.retry_after_seconds)
        snapshot = self._ledger.snapshot(self._workflow_id)
        if snapshot.status is not WorkflowStatus.RUNNING:
            raise WorkflowStateError(
                ProviderErrorCode.WORKFLOW_STATE,
                f"cannot retry while workflow is {snapshot.status.value}",
            )
        remaining = self._limits.max_active_seconds - snapshot.active_seconds
        if remaining <= 0:
            raise self._deadline_error(0)
        effective = min(delay, remaining)
        updated = self._ledger.record_backoff(self._workflow_id, effective)
        self._clock.sleep(effective)
        if effective < delay or updated.status is WorkflowStatus.EXHAUSTED:
            raise self._deadline_error(0)

    def _elapsed(self, started_at: float) -> float:
        return max(self._clock.monotonic() - started_at, 0.0)

    def _active_deadline_reached(self) -> bool:
        snapshot = self._ledger.snapshot(self._workflow_id)
        return snapshot.active_seconds >= self._limits.max_active_seconds - 1e-9

    def _deadline_error(self, attempts: int) -> ProviderError:
        return ProviderError(
            ProviderErrorCode.ACTIVE_DEADLINE,
            "workflow active execution deadline reached",
            retryable=False,
            details={"limit_seconds": self._limits.max_active_seconds},
            attempts=attempts,
        )
