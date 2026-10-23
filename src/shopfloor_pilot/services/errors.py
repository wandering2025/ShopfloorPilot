"""Stable application-service failures for operator, CLI, and future tool adapters."""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from typing import Any

from shopfloor_pilot.validation import ValidationReport


class ServiceErrorCode(StrEnum):
    """Machine-readable reasons emitted at the deterministic service boundary."""

    RUN_NOT_FOUND = "run_not_found"
    WORKFLOW_CONFLICT = "workflow_conflict"
    PROPOSAL_NOT_FOUND = "proposal_not_found"
    PROPOSAL_HASH_MISMATCH = "proposal_hash_mismatch"
    APPROVAL_NOT_FOUND = "approval_not_found"
    APPROVAL_REJECTED = "approval_rejected"
    APPROVAL_BINDING_MISMATCH = "approval_binding_mismatch"
    STALE_STATE = "stale_state"
    WRONG_RUN = "wrong_run"
    INVALID_CANDIDATE = "invalid_candidate"
    INVALID_EFFECTIVE_TIME = "invalid_effective_time"
    BOUNDARY_CLOSED = "boundary_closed"
    IDEMPOTENCY_CONFLICT = "idempotency_conflict"
    DATABASE_BUSY = "database_busy"
    STORAGE_CONFLICT = "storage_conflict"


class ApplicationServiceError(RuntimeError):
    """Expected, actionable rejection that never implies an operational commit."""

    def __init__(
        self,
        code: ServiceErrorCode,
        message: str,
        *,
        details: Mapping[str, Any] | None = None,
        validation: ValidationReport | None = None,
    ) -> None:
        self.code = code
        self.details = dict(details or {})
        self.validation = validation
        super().__init__(f"{code.value}: {message}")
