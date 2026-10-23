"""Deterministic SimPy execution over the authoritative SQLite event boundary."""

from .errors import (
    BoundaryClosedError,
    CommandConflictError,
    ConcurrentRuntimeError,
    InvalidAdvanceError,
    InvalidPlanError,
    PastInjectionError,
    RuntimeStateError,
    SimulationError,
    StalePlanError,
    UnsupportedPlanDeltaError,
)
from .models import (
    ActivationResult,
    AdvanceResult,
    BlockedAttempt,
    BlockedReason,
    CommandStatus,
    PublicRuntimeEvent,
    RuntimeProjection,
)
from .runtime import SimulationRuntime

__all__ = [
    "ActivationResult",
    "AdvanceResult",
    "BlockedAttempt",
    "BlockedReason",
    "BoundaryClosedError",
    "CommandConflictError",
    "CommandStatus",
    "ConcurrentRuntimeError",
    "InvalidAdvanceError",
    "InvalidPlanError",
    "PastInjectionError",
    "PublicRuntimeEvent",
    "RuntimeProjection",
    "RuntimeStateError",
    "SimulationError",
    "SimulationRuntime",
    "StalePlanError",
    "UnsupportedPlanDeltaError",
]
