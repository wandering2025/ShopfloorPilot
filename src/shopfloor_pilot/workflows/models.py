"""Typed records for S11 decision boundaries, operator actions, and run outcomes.

A *decision* is one S06 workflow record opened by the recovery coordinator at a
paused simulated boundary. Its immutable decision context lives in the workflow
payload that the coordinator writes on creation; status is always derived from
the authoritative SQLite workflow, proposal, approval, and consumption records,
so a restarted process reconstructs exactly the same view.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import Field, JsonValue

from shopfloor_pilot.domain.base import Identifier, Sha256, StrictModel

DECISION_MODE = "s11_recovery"


class DecisionKind(StrEnum):
    INITIAL_PLANNING = "initial_planning"
    URGENT_ORDER = "urgent_order"
    FAULT_RECOVERY = "fault_recovery"
    REPLAN = "replan"


class DecisionSource(StrEnum):
    """Where a candidate comes from; never switched silently."""

    SUPERVISOR = "supervisor"
    DETERMINISTIC_BASELINE = "deterministic_baseline"
    DETERMINISTIC_FALLBACK = "deterministic_fallback"


class DecisionStatus(StrEnum):
    OPEN = "open"  # running, no proposal yet
    AWAITING_APPROVAL = "awaiting_approval"
    APPLIED = "applied"
    PAUSED = "paused"  # safe blocked outcome; operational state preserved
    CANCELLED = "cancelled"  # cancelled, timed out, stale, rejected, superseded, reset

    @property
    def is_open(self) -> bool:
        return self in (DecisionStatus.OPEN, DecisionStatus.AWAITING_APPROVAL)


class ActionStatus(StrEnum):
    """Consistent caller-visible results of approve/reject/apply/cancel."""

    APPROVED = "approved"
    ALREADY_APPROVED = "already_approved"
    REJECTED = "rejected"
    APPLIED = "applied"
    ALREADY_APPLIED = "already_applied"
    CANCELLED = "cancelled"
    STALE = "stale"
    CONFLICT = "conflict"
    FAILED = "failed"


class DecisionRecord(StrictModel):
    """Derived, read-only view of one decision; it grants no authority."""

    schema_version: int = 1
    run_id: Identifier
    workflow_id: Identifier
    kind: DecisionKind
    source: DecisionSource
    provider: str | None = None
    status: DecisionStatus
    workflow_status: str
    decision_time_seconds: int = Field(ge=0)
    decision_boundary_seconds: int = Field(ge=0)
    base_state_version: int = Field(ge=0)
    base_plan_revision: int = Field(ge=0)
    base_snapshot_hash: Sha256
    focus_machine_ids: tuple[Identifier, ...] = ()
    fallback_for_workflow_id: Identifier | None = None
    pending_proposal_id: Identifier | None = None
    pending_proposal_hash: Sha256 | None = None
    applied_proposal_id: Identifier | None = None
    approval_ids: tuple[Identifier, ...] = ()
    consumed_approval_id: Identifier | None = None
    outcome: str | None = None
    reason_code: str | None = None
    details: dict[str, JsonValue] = Field(default_factory=dict)

    def public_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


class ActionResult(StrictModel):
    """Machine-readable result of one operator action on a decision."""

    schema_version: int = 1
    action: str
    status: ActionStatus
    run_id: Identifier
    workflow_id: Identifier
    proposal_id: Identifier | None = None
    proposal_hash: Sha256 | None = None
    approval_id: Identifier | None = None
    actor: str | None = None
    reason_code: str | None = None
    previous_state_version: int | None = None
    resulting_state_version: int | None = None
    effective_time_seconds: int | None = None
    details: dict[str, JsonValue] = Field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status in (
            ActionStatus.APPROVED,
            ActionStatus.ALREADY_APPROVED,
            ActionStatus.REJECTED,
            ActionStatus.APPLIED,
            ActionStatus.ALREADY_APPLIED,
            ActionStatus.CANCELLED,
        )

    def public_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


class InvariantResult(StrictModel):
    code: str
    passed: bool
    details: dict[str, JsonValue] = Field(default_factory=dict)


class CoordinatorErrorCode(StrEnum):
    DECISION_PENDING = "decision_pending"
    DECISION_BOUNDARY_BLOCKED = "decision_boundary_blocked"
    DECISION_NOT_FOUND = "decision_not_found"
    DECISION_CLOSED = "decision_closed"
    RUN_SUPERSEDED = "run_superseded"
    INVALID_ACTOR = "invalid_actor"
    INVALID_RESTORATION = "invalid_restoration"
    FALLBACK_NOT_ALLOWED = "fallback_not_allowed"
    SUPERVISOR_MISMATCH = "supervisor_mismatch"


class CoordinatorError(RuntimeError):
    """A refused coordinator command; no operational state was changed."""

    def __init__(
        self,
        code: CoordinatorErrorCode,
        message: str,
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = dict(details or {})

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code.value, "message": self.message, "details": self.details}


__all__ = [
    "DECISION_MODE",
    "ActionResult",
    "ActionStatus",
    "CoordinatorError",
    "CoordinatorErrorCode",
    "DecisionKind",
    "DecisionRecord",
    "DecisionSource",
    "DecisionStatus",
    "InvariantResult",
]
