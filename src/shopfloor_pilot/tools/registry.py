"""Allowlisted typed tool registry with execution-time boundary checks.

The provider layer validates arguments against the schemas advertised in a
request. The registry repeats that check and adds what a schema cannot express:
prohibited capabilities, phase gating, run scope, pinned state version, live
staleness, machine/order references, and result size. A rejected call never
reaches its handler.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from jsonschema import Draft202012Validator

from shopfloor_pilot.providers.models import ToolSchema

from .context import ToolContext, WorkflowPhase

# Names that must never be registered or executed, whatever a model asks for.
PROHIBITED_TOOL_NAMES = frozenset(
    {
        "apply_plan",
        "apply_approved_plan",
        "approve",
        "approve_plan",
        "approve_proposal",
        "record_human_decision",
        "record_deterministic_policy_decision",
        "advance_simulation",
        "restore_machine",
        "mark_machine_restored",
        "mark_workflow_blocked",
        "create_run",
        "reset_run",
        "shell",
        "bash",
        "exec",
        "run_shell",
        "sql",
        "execute_sql",
        "query_database",
        "python",
        "run_python",
        "eval",
        "read_file",
        "write_file",
        "filesystem",
        "http_request",
        "fetch_url",
        "web_search",
    }
)
PROHIBITED_NAME_FRAGMENTS = (
    "approve",
    "apply",
    "restore",
    "advance",
    "shell",
    "sql",
    "exec",
    "python",
    "file",
    "http",
)
IDENTIFIER_JSON_PATTERN = r"^[A-Za-z][A-Za-z0-9_.:-]{0,63}$"


class ToolClassification(StrEnum):
    READ_ONLY = "read_only"
    DRAFT = "draft"
    PROPOSAL_WRITE = "proposal_write"


class ToolCallStatus(StrEnum):
    OK = "ok"
    REJECTED = "rejected"
    FAILED = "failed"


class RejectionReason(StrEnum):
    PROHIBITED_TOOL = "prohibited_tool"
    UNKNOWN_TOOL = "unknown_tool"
    PHASE_NOT_ALLOWED = "phase_not_allowed"
    INVALID_ARGUMENTS = "invalid_arguments"
    WRONG_RUN = "wrong_run"
    SNAPSHOT_NOT_PINNED = "snapshot_not_pinned"
    STATE_VERSION_MISMATCH = "state_version_mismatch"
    STALE_STATE = "stale_state"
    UNKNOWN_REFERENCE = "unknown_reference"
    RESULT_TOO_LARGE = "result_too_large"
    PRECONDITION_FAILED = "precondition_failed"
    UNSUPPORTED_CLAIM = "unsupported_claim"
    TOOL_ERROR = "tool_error"


@dataclass(frozen=True, slots=True)
class ToolOutcome:
    """A tool result as sent back to the model, plus its terminal effect."""

    status: ToolCallStatus
    payload: Mapping[str, Any]
    reason: str | None = None
    # Applied by the registry only after every post-handler check passes.
    effect: Callable[[], None] | None = field(default=None, compare=False, repr=False)

    @property
    def ok(self) -> bool:
        return self.status is ToolCallStatus.OK

    def content(self) -> str:
        return json.dumps(dict(self.payload), ensure_ascii=False, sort_keys=True)


Handler = Callable[[Mapping[str, Any], ToolContext], ToolOutcome]


@dataclass(frozen=True, slots=True)
class ToolSpec:
    name: str
    description: str
    parameters: Mapping[str, Any]
    classification: ToolClassification
    phases: frozenset[WorkflowPhase]
    handler: Handler
    requires_state_version: bool = False
    max_result_bytes: int = 8_000
    machine_reference_fields: tuple[str, ...] = ()
    kinds: frozenset[str] = field(default_factory=lambda: frozenset({"*"}))

    def __post_init__(self) -> None:
        if self.name in PROHIBITED_TOOL_NAMES or any(
            fragment in self.name for fragment in PROHIBITED_NAME_FRAGMENTS
        ):
            raise ValueError(f"tool name {self.name!r} names a prohibited capability")
        properties = self.parameters.get("properties", {})
        if "run_id" not in properties or "run_id" not in self.parameters.get("required", ()):
            raise ValueError("every supervisor tool must require an explicit run_id")
        if self.requires_state_version and "state_version" not in self.parameters.get(
            "required", ()
        ):
            raise ValueError("state-version tools must require state_version")
        # ToolSchema enforces additionalProperties=false and JSON Schema validity.
        ToolSchema(self.name, self.description, self.parameters)

    def schema(self) -> ToolSchema:
        return ToolSchema(self.name, self.description, self.parameters)

    def available_for(self, kind: str) -> bool:
        return "*" in self.kinds or kind in self.kinds

    def public_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "classification": self.classification.value,
            "phases": sorted(item.value for item in self.phases),
            "requires_state_version": self.requires_state_version,
            "run_scoped": True,
            "max_result_bytes": self.max_result_bytes,
            "workflow_kinds": sorted(self.kinds),
            "parameters": dict(self.parameters),
        }


def rejection(reason: RejectionReason | str, message: str, **details: Any) -> ToolOutcome:
    payload: dict[str, Any] = {
        "status": ToolCallStatus.REJECTED.value,
        "reason": str(reason),
        "message": message,
    }
    if details:
        payload["details"] = details
    return ToolOutcome(ToolCallStatus.REJECTED, payload, reason=str(reason))


class ToolRegistry:
    """Execute only allowlisted specs after every boundary check passes."""

    def __init__(self, specs: tuple[ToolSpec, ...]) -> None:
        names = [item.name for item in specs]
        if len(names) != len(set(names)):
            raise ValueError("tool names must be unique")
        self._specs = {item.name: item for item in specs}
        self._validators = {
            item.name: Draft202012Validator(dict(item.parameters)) for item in specs
        }

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(self._specs)

    def spec(self, name: str) -> ToolSpec | None:
        return self._specs.get(name)

    def specs(self) -> tuple[ToolSpec, ...]:
        return tuple(self._specs.values())

    def schemas_for(self, phase: WorkflowPhase, kind: str) -> tuple[ToolSchema, ...]:
        return tuple(
            item.schema()
            for item in self._specs.values()
            if phase in item.phases and item.available_for(kind)
        )

    def execute(
        self,
        name: str,
        arguments: Mapping[str, Any],
        context: ToolContext,
    ) -> ToolOutcome:
        if name in PROHIBITED_TOOL_NAMES:
            return rejection(
                RejectionReason.PROHIBITED_TOOL,
                "this capability is outside the supervisor tool registry",
                tool_name=name,
            )
        spec = self._specs.get(name)
        if spec is None:
            return rejection(
                RejectionReason.UNKNOWN_TOOL, "tool is not allowlisted", tool_name=name
            )
        state = context.state
        if state.phase not in spec.phases or not spec.available_for(state.kind.value):
            return rejection(
                RejectionReason.PHASE_NOT_ALLOWED,
                f"{name} is not available in phase {state.phase.value}",
                phase=state.phase.value,
            )
        if not isinstance(arguments, Mapping):
            return rejection(RejectionReason.INVALID_ARGUMENTS, "arguments must be an object")
        errors = sorted(
            self._validators[name].iter_errors(dict(arguments)),
            key=lambda item: list(item.absolute_path),
        )
        if errors:
            first = errors[0]
            return rejection(
                RejectionReason.INVALID_ARGUMENTS,
                "arguments do not match the tool schema",
                validator=first.validator,
                path=[str(item) for item in first.absolute_path],
            )
        if arguments.get("run_id") != context.run_id:
            return rejection(
                RejectionReason.WRONG_RUN,
                "tool calls are scoped to the workflow's run_id only",
            )
        if spec.requires_state_version:
            observed = state.observed
            if observed is None:
                return rejection(
                    RejectionReason.SNAPSHOT_NOT_PINNED,
                    "call get_observable_snapshot before state-versioned tools",
                )
            if arguments.get("state_version") != observed.state_version:
                return rejection(
                    RejectionReason.STATE_VERSION_MISMATCH,
                    "state_version must equal the pinned observed state_version",
                    pinned_state_version=observed.state_version,
                )
            live = context.facade.get_checkpoint(context.run_id).snapshot
            if (
                live.state_version != observed.state_version
                or live.plan_revision != observed.plan_revision
            ):
                details = {
                    "pinned_state_version": observed.state_version,
                    "live_state_version": live.state_version,
                    "pinned_plan_revision": observed.plan_revision,
                    "live_plan_revision": live.plan_revision,
                }
                return rejection(
                    RejectionReason.STALE_STATE,
                    "operational state changed after observation; a fresh workflow is required",
                    **details,
                )
        for field_name in spec.machine_reference_fields:
            values = arguments.get(field_name) or ()
            values = (values,) if isinstance(values, str) else tuple(values)
            known = state.observed.machine_ids if state.observed is not None else frozenset()
            unknown = sorted(item for item in values if item not in known)
            if unknown:
                return rejection(
                    RejectionReason.UNKNOWN_REFERENCE,
                    "machine references must name machines in the pinned snapshot",
                    unknown=unknown[:4],
                )
        try:
            outcome = spec.handler(arguments, context)
        except Exception as exc:  # noqa: BLE001 - tool failures are typed, never raw
            return ToolOutcome(
                ToolCallStatus.FAILED,
                {
                    "status": ToolCallStatus.FAILED.value,
                    "reason": RejectionReason.TOOL_ERROR.value,
                    "message": f"tool failed safely ({type(exc).__name__})",
                },
                reason=RejectionReason.TOOL_ERROR.value,
            )
        size = len(outcome.content().encode("utf-8"))
        if size > spec.max_result_bytes:
            return rejection(
                RejectionReason.RESULT_TOO_LARGE,
                "tool result exceeded its configured size limit",
                size_bytes=size,
                limit_bytes=spec.max_result_bytes,
            )
        if outcome.effect is not None:
            outcome.effect()
        return outcome
