"""Immutable scheduler inputs and outcomes; none grants approval to apply."""

from enum import IntEnum, StrEnum
from typing import Literal, Self

from pydantic import Field, model_validator

from shopfloor_pilot.domain.base import Seconds, StrictModel
from shopfloor_pilot.domain.models import CandidateProposal, ScheduleAssignment
from shopfloor_pilot.validation import ValidationReport


class SolverConfig(StrictModel):
    max_time_seconds: float = Field(default=5.0, gt=0, allow_inf_nan=False)
    random_seed: int = Field(default=0, ge=0, le=2**31 - 1)
    num_search_workers: Literal[1] = 1
    relative_gap_limit: Literal[0.0] = 0.0
    absolute_gap_limit: Literal[0.0] = 0.0


class NativeStatus(IntEnum):
    """CP-SAT's native status numbers, including its model-error state."""

    UNKNOWN = 0
    MODEL_INVALID = 1
    FEASIBLE = 2
    INFEASIBLE = 3
    OPTIMAL = 4


class SolveOutcome(StrEnum):
    OPTIMAL = "optimal"
    FEASIBLE = "feasible"
    INFEASIBLE = "infeasible"
    UNKNOWN = "unknown"
    INVALID_INPUT = "invalid_input"
    MODEL_INVALID = "model_invalid"
    VALIDATION_FAILED = "validation_failed"


class AdapterResult(StrictModel):
    """Internal adapter seam for deterministic status/extraction regression tests."""

    status: NativeStatus
    assignments: tuple[ScheduleAssignment, ...] = ()
    weighted_tardiness_seconds: int | None = None
    makespan_seconds: int | None = None
    objective_value: float | None = None
    best_objective_bound: float | None = None
    wall_time_seconds: float = 0.0
    response_stats: str = ""


class SolverReport(StrictModel):
    config: SolverConfig
    solver_version: str
    not_before_seconds: Seconds | None = None
    native_status: NativeStatus | None = None
    native_status_name: str | None = None
    objective_value: float | None = None
    best_objective_bound: float | None = None
    objective_multiplier: int | None = None
    objective_upper_bound: int | None = None
    solver_wall_time_seconds: float = 0.0
    total_elapsed_seconds: float = 0.0
    response_stats: str = ""
    diagnostics: tuple[str, ...] = ()


class SchedulingResult(StrictModel):
    outcome: SolveOutcome
    report: SolverReport
    candidate: CandidateProposal | None = None
    validation: ValidationReport | None = None

    @model_validator(mode="after")
    def require_validated_candidate(self) -> Self:
        successful = self.outcome in (SolveOutcome.OPTIMAL, SolveOutcome.FEASIBLE)
        if successful != (self.candidate is not None):
            raise ValueError("only optimal/feasible results may expose a candidate")
        if successful and (self.validation is None or not self.validation.is_valid):
            raise ValueError("every exposed candidate requires independent validation")
        if not successful and self.validation is not None and self.validation.is_valid:
            raise ValueError("failed scheduling results cannot expose a valid plan projection")
        return self

    @property
    def is_usable(self) -> bool:
        """Validated planning output, still requiring subsequent approval/apply."""
        return self.candidate is not None
