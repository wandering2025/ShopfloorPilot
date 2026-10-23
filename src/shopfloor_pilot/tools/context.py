"""Workflow phases, mutable workflow state, and the narrow tool execution context."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from shopfloor_pilot.diagnostics import DiagnosticResult, DiagnosticService
from shopfloor_pilot.domain.fixtures import ScenarioClock
from shopfloor_pilot.domain.models import (
    EVENT_TYPE_PRIORITY,
    CandidateProposal,
    DomainCatalog,
    EventType,
    OrderDelta,
)
from shopfloor_pilot.events import SnapshotCheckpoint
from shopfloor_pilot.services import ModelReadableApplicationServices

from .evidence import EvidenceRegistry


class WorkflowKind(StrEnum):
    ORDER_REQUEST = "order_request"
    ABNORMALITY = "abnormality"


class WorkflowPhase(StrEnum):
    INTERPRET = "interpret"
    OBSERVE = "observe"
    GATHER_EVIDENCE = "gather_evidence"
    PLAN = "plan"
    VALIDATE = "validate"
    AWAITING_APPROVAL = "awaiting_approval"
    CLARIFICATION_REQUIRED = "clarification_required"
    REJECTED = "rejected"
    INFEASIBLE = "infeasible"
    STALE_STATE = "stale_state"
    FAILED = "failed"
    CANCELLED = "cancelled"
    BUDGET_EXHAUSTED = "budget_exhausted"
    CYCLE_DETECTED = "cycle_detected"

    @property
    def is_terminal(self) -> bool:
        return self not in ACTIVE_PHASES


ACTIVE_PHASES = frozenset(
    {
        WorkflowPhase.INTERPRET,
        WorkflowPhase.OBSERVE,
        WorkflowPhase.GATHER_EVIDENCE,
        WorkflowPhase.PLAN,
        WorkflowPhase.VALIDATE,
    }
)

# Only these transitions are legal; every other change is a programming error.
ALLOWED_TRANSITIONS: dict[WorkflowPhase, frozenset[WorkflowPhase]] = {
    WorkflowPhase.INTERPRET: frozenset(
        {
            WorkflowPhase.OBSERVE,
            WorkflowPhase.CLARIFICATION_REQUIRED,
            WorkflowPhase.REJECTED,
        }
    ),
    WorkflowPhase.OBSERVE: frozenset({WorkflowPhase.GATHER_EVIDENCE}),
    WorkflowPhase.GATHER_EVIDENCE: frozenset({WorkflowPhase.PLAN}),
    WorkflowPhase.PLAN: frozenset({WorkflowPhase.VALIDATE, WorkflowPhase.INFEASIBLE}),
    WorkflowPhase.VALIDATE: frozenset({WorkflowPhase.AWAITING_APPROVAL}),
}
# Any active phase may additionally end in one of these terminals.
UNIVERSAL_TERMINALS = frozenset(
    {
        WorkflowPhase.STALE_STATE,
        WorkflowPhase.FAILED,
        WorkflowPhase.CANCELLED,
        WorkflowPhase.BUDGET_EXHAUSTED,
        WorkflowPhase.CYCLE_DETECTED,
    }
)


class TransitionError(RuntimeError):
    """An illegal workflow transition was attempted."""


@dataclass(frozen=True, slots=True)
class ObservedState:
    """The snapshot identity pinned by ``get_observable_snapshot``."""

    state_version: int
    plan_revision: int
    snapshot_hash: str
    simulated_time_seconds: int
    hard_horizon_seconds: int
    last_sequence: int
    decision_boundary_seconds: int
    machine_ids: frozenset[str]
    order_ids: frozenset[str]

    def public_dict(self) -> dict[str, Any]:
        return {
            "state_version": self.state_version,
            "plan_revision": self.plan_revision,
            "snapshot_hash": self.snapshot_hash,
            "simulated_time_seconds": self.simulated_time_seconds,
            "hard_horizon_seconds": self.hard_horizon_seconds,
            "last_sequence": self.last_sequence,
            "decision_boundary_seconds": self.decision_boundary_seconds,
        }


@dataclass(frozen=True, slots=True)
class TerminalInfo:
    phase: WorkflowPhase
    reason_code: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class WorkflowState:
    """Mutable, in-memory supervisor state; authority stays in SQLite."""

    run_id: str
    workflow_id: str
    kind: WorkflowKind
    request_text: str
    focus_machine_ids: tuple[str, ...] = ()
    phase: WorkflowPhase = WorkflowPhase.INTERPRET
    observed: ObservedState | None = None
    interpretation: Any = None
    interpretation_base_state_version: int | None = None
    declared_order_deltas: tuple[OrderDelta, ...] = ()
    evidence: EvidenceRegistry = field(default_factory=EvidenceRegistry)
    diagnostics: dict[str, DiagnosticResult] = field(default_factory=dict)
    candidate: CandidateProposal | None = None
    solve_count: int = 0
    validated_candidate_hash: str | None = None
    proposal: CandidateProposal | None = None
    claims: tuple[dict[str, Any], ...] = ()
    terminal: TerminalInfo | None = None
    transitions: list[tuple[WorkflowPhase, WorkflowPhase, str]] = field(default_factory=list)

    def transition(self, target: WorkflowPhase, reason: str) -> None:
        current = self.phase
        if current.is_terminal:
            raise TransitionError(f"workflow is already terminal ({current.value})")
        allowed = ALLOWED_TRANSITIONS.get(current, frozenset()) | UNIVERSAL_TERMINALS
        if target not in allowed:
            raise TransitionError(f"illegal transition {current.value} -> {target.value}")
        self.phase = target
        self.transitions.append((current, target, reason))

    def finish(
        self,
        target: WorkflowPhase,
        reason_code: str,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        if not target.is_terminal:
            raise TransitionError("finish requires a terminal phase")
        self.transition(target, reason_code)
        self.terminal = TerminalInfo(target, reason_code, message, dict(details or {}))

    def progress_marker(self) -> tuple[object, ...]:
        return (
            self.phase,
            len(self.evidence),
            None if self.observed is None else self.observed.state_version,
            None if self.candidate is None else self.candidate.proposal_hash,
            self.validated_candidate_hash,
            None if self.proposal is None else self.proposal.proposal_hash,
        )


@dataclass(frozen=True, slots=True)
class ToolContext:
    """Everything a tool handler may touch; deliberately no privileged service."""

    run_id: str
    workflow_id: str
    facade: ModelReadableApplicationServices
    catalog: DomainCatalog
    diagnostics: DiagnosticService
    scenario_clock: ScenarioClock | None
    seed: int
    state: WorkflowState
    wall_clock: Callable[[], str]
    solver_time_limit_seconds: float = 5.0

    def __post_init__(self) -> None:
        if not isinstance(self.facade, ModelReadableApplicationServices):
            raise TypeError("tools may only receive the model-readable service facade")


def decision_boundary_seconds(checkpoint: SnapshotCheckpoint, *, with_order_delta: bool) -> int:
    """The simulated second at which an approved plan could next be applied.

    Mirrors S06 ``ApplicationServices._effective_time``: the current second when
    its first event priority is still open after the last persisted event,
    otherwise the immediately following second.
    """

    now = checkpoint.snapshot.simulated_time_seconds
    first = EventType.ORDER_REGISTERED if with_order_delta else EventType.PLAN_APPLIED
    last_key = checkpoint.last_ordering_key
    if last_key is None:
        return now
    is_open = (now, EVENT_TYPE_PRIORITY[first], checkpoint.last_sequence + 1) > last_key
    return now if is_open else now + 1
