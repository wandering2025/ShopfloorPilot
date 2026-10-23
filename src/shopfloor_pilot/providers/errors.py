"""Typed provider failures and retry classification."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from enum import StrEnum
from typing import Any

from .redaction import SecretRedactor, redact_text


class ProviderErrorCode(StrEnum):
    """Stable reason codes emitted at the provider boundary."""

    CONFIGURATION = "configuration"
    AUTHENTICATION = "authentication"
    INSUFFICIENT_BALANCE = "insufficient_balance"
    INVALID_REQUEST = "invalid_request"
    INVALID_TOOL_CALL = "invalid_tool_call"
    INVALID_RESPONSE = "invalid_response"
    RATE_LIMITED = "rate_limited"
    SERVER_ERROR = "server_error"
    TIMEOUT = "timeout"
    CONNECTION = "connection"
    CANCELLED = "cancelled"
    ACTIVE_DEADLINE = "active_deadline_exceeded"
    BUDGET = "budget_exceeded"
    WORKFLOW_STATE = "workflow_state"
    LEDGER = "ledger_error"


RETRYABLE_CODES = frozenset(
    {
        ProviderErrorCode.RATE_LIMITED,
        ProviderErrorCode.SERVER_ERROR,
        ProviderErrorCode.TIMEOUT,
        ProviderErrorCode.CONNECTION,
    }
)

# Failures that can be assumed to have produced no billable generation.
ZERO_CHARGE_CODES = frozenset(
    {
        ProviderErrorCode.AUTHENTICATION,
        ProviderErrorCode.INSUFFICIENT_BALANCE,
        ProviderErrorCode.INVALID_REQUEST,
        ProviderErrorCode.CONFIGURATION,
        ProviderErrorCode.CANCELLED,
        ProviderErrorCode.ACTIVE_DEADLINE,
        ProviderErrorCode.BUDGET,
        ProviderErrorCode.WORKFLOW_STATE,
    }
)


class ProviderError(RuntimeError):
    """A normalized, redacted provider failure with explicit retry semantics."""

    def __init__(
        self,
        code: ProviderErrorCode,
        message: str,
        *,
        status_code: int | None = None,
        retryable: bool | None = None,
        retry_after_seconds: float | None = None,
        details: Mapping[str, Any] | None = None,
        secrets: Iterable[str] = (),
        attempts: int = 0,
    ) -> None:
        redactor = SecretRedactor(secrets)
        self.code = code
        self.status_code = status_code
        self.retryable = code in RETRYABLE_CODES if retryable is None else retryable
        self.retry_after_seconds = retry_after_seconds
        self.details = redactor.details(dict(details or {}))
        self.attempts = attempts
        self.message = redactor.text(message)
        super().__init__(self.message)

    @property
    def permanent(self) -> bool:
        return not self.retryable

    def with_attempts(self, attempts: int) -> ProviderError:
        """Annotate the same typed failure with the observed attempt count."""

        self.attempts = attempts
        return self

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "code": self.code.value,
            "message": self.message,
            "retryable": self.retryable,
            "attempts": self.attempts,
        }
        if self.status_code is not None:
            payload["status_code"] = self.status_code
        if self.retry_after_seconds is not None:
            payload["retry_after_seconds"] = self.retry_after_seconds
        if self.details:
            payload["details"] = self.details
        return payload


class ConfigurationError(ProviderError):
    """A paid run was requested without complete, explicit configuration."""

    def __init__(self, message: str, *, details: Mapping[str, Any] | None = None) -> None:
        super().__init__(
            ProviderErrorCode.CONFIGURATION,
            message,
            retryable=False,
            details=details,
        )


class ToolCallValidationError(ProviderError):
    """A model tool call failed local schema, name, or correlation validation."""

    def __init__(
        self,
        reason: str,
        message: str,
        *,
        tool_name: str | None = None,
        call_id: str | None = None,
    ) -> None:
        details: dict[str, Any] = {"reason": reason}
        if tool_name is not None:
            details["tool_name"] = tool_name
        if call_id is not None:
            details["call_id"] = call_id
        self.reason = reason
        super().__init__(
            ProviderErrorCode.INVALID_TOOL_CALL,
            message,
            retryable=False,
            details=details,
        )


class ProviderResponseValidationError(ProviderError):
    """The provider returned a shape the adapter cannot safely consume."""

    def __init__(self, reason: str, message: str) -> None:
        self.reason = reason
        super().__init__(
            ProviderErrorCode.INVALID_RESPONSE,
            message,
            retryable=False,
            details={"reason": reason},
        )


class BudgetExceededError(ProviderError):
    """A configured workflow or run ceiling blocks further provider work."""

    def __init__(
        self,
        limit: str,
        message: str,
        *,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        payload = {"limit": limit}
        payload.update(details or {})
        self.limit = limit
        super().__init__(
            ProviderErrorCode.BUDGET,
            message,
            retryable=False,
            details=payload,
        )


class LedgerError(ProviderError):
    """The persistent budget ledger rejected an inconsistent operation."""

    def __init__(self, message: str, *, details: Mapping[str, Any] | None = None) -> None:
        super().__init__(
            ProviderErrorCode.LEDGER,
            message,
            retryable=False,
            details=details,
        )


class WorkflowStateError(ProviderError):
    """Work is not running, so no new provider or tool attempt may start."""

    def __init__(self, code: ProviderErrorCode, message: str) -> None:
        super().__init__(code, message, retryable=False)


def redact(value: object, secrets: Iterable[str] = ()) -> str:
    """Convenience wrapper used by adapters that do not own a redactor."""

    return redact_text(value, secrets)
