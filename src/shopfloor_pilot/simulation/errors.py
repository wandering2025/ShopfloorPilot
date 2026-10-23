"""Typed failures raised by the deterministic simulation boundary."""

from __future__ import annotations

from collections.abc import Sequence

from shopfloor_pilot.validation import ScheduleViolation, ValidationReport


class SimulationError(RuntimeError):
    """Base class for simulator command and runtime failures."""


class InvalidPlanError(SimulationError):
    """A candidate failed the independent validator without changing state."""

    def __init__(self, report: ValidationReport) -> None:
        self.report = report
        codes = ", ".join(item.code.value for item in report.violations) or "unknown"
        super().__init__(f"candidate is not executable: {codes}")

    @property
    def violations(self) -> tuple[ScheduleViolation, ...]:
        return self.report.violations


class StalePlanError(InvalidPlanError):
    """A candidate is bound to an older or different authoritative snapshot."""


class UnsupportedPlanDeltaError(SimulationError):
    """The low-level S05 activation boundary received proposal deltas."""

    def __init__(self) -> None:
        super().__init__("S05 activation accepts only candidates with no order or resource deltas")


class InvalidAdvanceError(SimulationError):
    """An advance target or supplied boundary input is invalid."""


class BoundaryClosedError(InvalidAdvanceError):
    """A same-second event group has already closed the requested boundary."""


class PastInjectionError(InvalidAdvanceError):
    """A new or conflicting intervention/observation targets elapsed time."""


class CommandConflictError(SimulationError):
    """A public command identity was reused for different public content."""


class RuntimeStateError(SimulationError):
    """Persisted state is internally incompatible with executable callbacks."""


class ConcurrentRuntimeError(SimulationError):
    """Another writer changed the authoritative run during a command.

    The runtime is rebuilt from SQLite before this exception is returned, so a caller
    may safely retry with a fresh command and snapshot.
    """


def has_any_violation(
    report: ValidationReport,
    codes: Sequence[str],
) -> bool:
    """Return whether a validation report contains one of the named stable codes."""

    accepted = frozenset(codes)
    return any(item.code.value in accepted for item in report.violations)
