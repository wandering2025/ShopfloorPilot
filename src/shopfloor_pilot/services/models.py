"""Immutable results and capability declarations for application services."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import Field, model_validator

from shopfloor_pilot.domain.base import Identifier, Seconds, Sha256, StrictModel
from shopfloor_pilot.domain.models import OperationalSnapshot
from shopfloor_pilot.storage import MutationResult
from shopfloor_pilot.validation import ValidationReport

from .errors import ServiceErrorCode


class PlanApplicationStatus(StrEnum):
    APPLIED = "applied"
    IDEMPOTENT = "idempotent"


class ServiceCapabilities(StrictModel):
    """Explicit exposure sets; model-readable actions never grant approval."""

    schema_version: Literal[1] = 1
    model_readable_actions: tuple[str, ...]
    operator_actions: tuple[str, ...]
    batch_policy_actions: tuple[str, ...]

    @model_validator(mode="after")
    def validate_separation(self) -> ServiceCapabilities:
        model = set(self.model_readable_actions)
        privileged = set(self.operator_actions) | set(self.batch_policy_actions)
        if model & privileged:
            raise ValueError("model-readable and approval/application actions must be disjoint")
        return self


class ProposalConflictReport(StrictModel):
    """Read-only comparison of one stored proposal with current authority."""

    schema_version: Literal[1] = 1
    run_id: Identifier
    proposal_id: Identifier
    proposal_hash: Sha256
    expected_proposal_hash: Sha256 | None = None
    base_state_version: int = Field(strict=True, ge=0)
    current_state_version: int = Field(strict=True, ge=0)
    base_snapshot_hash: Sha256
    current_snapshot_hash: Sha256
    can_apply: bool
    reasons: tuple[ServiceErrorCode, ...] = ()
    validation: ValidationReport

    @model_validator(mode="after")
    def validate_report(self) -> ProposalConflictReport:
        if self.can_apply != (not self.reasons and self.validation.is_valid):
            raise ValueError("can_apply must match reasons and candidate validation")
        return self


class PlanApplicationResult(StrictModel):
    """Committed result of applying one exact, approved, stored proposal."""

    schema_version: Literal[1] = 1
    status: PlanApplicationStatus
    run_id: Identifier
    workflow_id: Identifier
    proposal_id: Identifier
    proposal_hash: Sha256
    approval_id: Identifier
    effective_time_seconds: Seconds
    validation: ValidationReport
    mutation: MutationResult

    @model_validator(mode="after")
    def validate_result(self) -> PlanApplicationResult:
        expected = (
            PlanApplicationStatus.IDEMPOTENT
            if self.mutation.replayed_idempotent
            else PlanApplicationStatus.APPLIED
        )
        if self.status is not expected:
            raise ValueError("application status must match mutation idempotency state")
        if self.run_id != self.mutation.run_id:
            raise ValueError("application and mutation run_id values must match")
        if not self.validation.is_valid:
            raise ValueError("a committed application requires a valid proposal")
        return self

    @property
    def snapshot(self) -> OperationalSnapshot:
        return self.mutation.checkpoint.snapshot

    @property
    def previous_state_version(self) -> int:
        return self.mutation.previous_state_version

    @property
    def resulting_state_version(self) -> int:
        return self.mutation.resulting_state_version
