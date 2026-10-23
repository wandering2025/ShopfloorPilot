"""Narrow, typed, run-scoped supervisor tools and their registry."""

from .context import (
    ObservedState,
    TerminalInfo,
    ToolContext,
    TransitionError,
    WorkflowKind,
    WorkflowPhase,
    WorkflowState,
    decision_boundary_seconds,
)
from .evidence import EvidenceItem, EvidenceKind, EvidenceRegistry
from .registry import (
    PROHIBITED_TOOL_NAMES,
    RejectionReason,
    ToolCallStatus,
    ToolClassification,
    ToolOutcome,
    ToolRegistry,
    ToolSpec,
)
from .supervisor_tools import build_supervisor_registry, validate_claims

__all__ = [
    "PROHIBITED_TOOL_NAMES",
    "EvidenceItem",
    "EvidenceKind",
    "EvidenceRegistry",
    "ObservedState",
    "RejectionReason",
    "TerminalInfo",
    "ToolCallStatus",
    "ToolClassification",
    "ToolContext",
    "ToolOutcome",
    "ToolRegistry",
    "ToolSpec",
    "TransitionError",
    "WorkflowKind",
    "WorkflowPhase",
    "WorkflowState",
    "build_supervisor_registry",
    "decision_boundary_seconds",
    "validate_claims",
]
