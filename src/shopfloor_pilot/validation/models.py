"""Typed results for the solver-independent schedule validator."""

from __future__ import annotations

from enum import StrEnum
from typing import Self

from pydantic import Field, model_validator

from shopfloor_pilot.domain.base import Seconds, StrictModel
from shopfloor_pilot.domain.models import Operation, OperationAttempt, TimeWindow


class ViolationCode(StrEnum):
    """Stable failure vocabulary for schedule and proposal validation."""

    PROPOSAL_HASH_MISMATCH = "proposal_hash_mismatch"
    RUN_ID_MISMATCH = "run_id_mismatch"
    BASE_STATE_VERSION_MISMATCH = "base_state_version_mismatch"
    BASE_SNAPSHOT_HASH_MISMATCH = "base_snapshot_hash_mismatch"
    PLAN_REVISION_MISMATCH = "plan_revision_mismatch"
    ORDER_DELTA_MISMATCH = "order_delta_mismatch"
    INVALID_ORDER_DELTA = "invalid_order_delta"
    RESOURCE_DELTA_MISMATCH = "resource_delta_mismatch"
    RESOURCE_RESTORE_FORBIDDEN = "resource_restore_forbidden"
    INVALID_RESOURCE_DELTA = "invalid_resource_delta"
    MISSING_OPERATION = "missing_operation"
    DUPLICATE_OPERATION = "duplicate_operation"
    UNKNOWN_OPERATION = "unknown_operation"
    EXTRA_OPERATION = "extra_operation"
    DUPLICATE_ATTEMPT = "duplicate_attempt"
    ATTEMPT_ID_CONFLICT = "attempt_id_conflict"
    UNKNOWN_MACHINE = "unknown_machine"
    CAPABILITY_MISMATCH = "capability_mismatch"
    DURATION_MISMATCH = "duration_mismatch"
    MACHINE_OVERLAP = "machine_overlap"
    PRECEDENCE_VIOLATION = "precedence_violation"
    RELEASE_TIME_VIOLATION = "release_time_violation"
    START_BEFORE_SNAPSHOT = "start_before_snapshot"
    CALENDAR_VIOLATION = "calendar_violation"
    HARD_HORIZON_VIOLATION = "hard_horizon_violation"
    COMPLETED_OPERATION_RESCHEDULED = "completed_operation_rescheduled"
    RUNNING_OPERATION_RESCHEDULED = "running_operation_rescheduled"
    FIXED_HISTORY_MISMATCH = "fixed_history_mismatch"
    RESTART_DURATION_MISMATCH = "restart_duration_mismatch"
    OBJECTIVE_MISMATCH = "objective_mismatch"


class ScheduleViolation(StrictModel):
    """One deterministic hard-constraint or provenance violation."""

    code: ViolationCode
    message: str = Field(min_length=1, max_length=500)
    operation_ids: tuple[str, ...] = ()
    attempt_ids: tuple[str, ...] = ()
    machine_ids: tuple[str, ...] = ()
    windows: tuple[TimeWindow, ...] = ()
    field: str | None = None
    references: dict[str, str] = Field(default_factory=dict)


class ValidationReport(StrictModel):
    """Pure validation result and the complete plan projection for valid input."""

    is_valid: bool
    violations: tuple[ScheduleViolation, ...] = ()
    projected_operations: tuple[Operation, ...] = ()
    projected_attempts: tuple[OperationAttempt, ...] = ()
    total_tardiness_seconds: Seconds = Field(
        default=0,
        description=(
            "Priority-weighted total tardiness in seconds; this matches "
            "CandidateObjective.weighted_total_tardiness_seconds."
        ),
    )
    makespan_seconds: Seconds = 0

    @model_validator(mode="after")
    def validate_consistency(self) -> Self:
        if self.is_valid == bool(self.violations):
            raise ValueError("is_valid must be true exactly when violations is empty")
        if not self.is_valid and (self.projected_operations or self.projected_attempts):
            raise ValueError("invalid reports cannot expose an apply-ready plan projection")
        return self
