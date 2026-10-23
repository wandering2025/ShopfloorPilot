"""The single DeepSeek-backed supervisory workflow.

One supervisor drives an explicit phase machine through the fixed tool
registry. Every provider attempt and every tool call is charged to the S09
persistent ledger. The workflow ends in exactly one typed terminal and never
approves, applies, restores, or advances anything.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from shopfloor_pilot.budgets import BudgetLedger, ProviderLimits
from shopfloor_pilot.diagnostics import DiagnosticService
from shopfloor_pilot.domain.canonical import canonical_hash
from shopfloor_pilot.domain.fixtures import ScenarioClock
from shopfloor_pilot.domain.models import DomainCatalog
from shopfloor_pilot.providers.base import Provider
from shopfloor_pilot.providers.bounded import BoundedProvider
from shopfloor_pilot.providers.clock import Clock, SystemClock
from shopfloor_pilot.providers.costs import TokenRates
from shopfloor_pilot.providers.errors import (
    BudgetExceededError,
    ProviderError,
    ProviderErrorCode,
    ProviderResponseValidationError,
    ToolCallValidationError,
    WorkflowStateError,
)
from shopfloor_pilot.providers.fake import FAKE_PROVIDER_LABEL
from shopfloor_pilot.providers.models import (
    MessageRole,
    ProviderMessage,
    ProviderRequest,
    ToolChoice,
    ValidatedToolCall,
)
from shopfloor_pilot.retrieval import LexicalRetriever, load_corpus
from shopfloor_pilot.services import ApplicationServiceError, ModelReadableApplicationServices
from shopfloor_pilot.tools import (
    EvidenceKind,
    ToolContext,
    ToolOutcome,
    ToolRegistry,
    WorkflowKind,
    WorkflowPhase,
    WorkflowState,
    build_supervisor_registry,
)
from shopfloor_pilot.tools.registry import RejectionReason

from .interpretation import MAX_REQUEST_CHARACTERS
from .prompts import SYSTEM_PROMPT, build_digest, digest_message
from .trace import TraceStore

OFFLINE_EVIDENCE_LABEL = FAKE_PROVIDER_LABEL
LIVE_EVIDENCE_LABEL = "LIVE DeepSeek provider evidence"


@dataclass(frozen=True, slots=True)
class SupervisorTask:
    """Operator-supplied task; ``request_text`` is untrusted natural language."""

    kind: WorkflowKind
    request_text: str
    focus_machine_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.kind, WorkflowKind):
            raise TypeError("kind must be a WorkflowKind")
        if not isinstance(self.request_text, str) or not self.request_text.strip():
            raise ValueError("request_text must be non-empty")
        if len(self.request_text) > MAX_REQUEST_CHARACTERS:
            raise ValueError(f"request_text exceeds {MAX_REQUEST_CHARACTERS} characters")
        if self.kind is WorkflowKind.ABNORMALITY and not self.focus_machine_ids:
            raise ValueError("an abnormality task names at least one focus machine")


@dataclass(frozen=True, slots=True)
class SupervisorBudget:
    """Explicit monetary caps and ledger limits; nothing is inferred from a key."""

    currency: str
    run_cap_micros: int
    workflow_cap_micros: int
    limits: ProviderLimits = field(default_factory=ProviderLimits)


@dataclass(frozen=True, slots=True)
class SupervisorConfig:
    max_output_tokens: int = 512
    force_tool_choice: bool = True
    max_no_progress_repeats: int = 2
    max_consecutive_invalid_turns: int = 3
    max_turns: int = 32
    solver_time_limit_seconds: float = 5.0
    narrow_next_step_tools: bool = True


@dataclass(frozen=True, slots=True)
class SupervisorOutcome:
    terminal: WorkflowPhase
    reason_code: str
    message: str
    details: dict[str, Any]
    run_id: str
    workflow_id: str
    kind: WorkflowKind
    provider: str
    evidence_label: str
    observed: dict[str, Any] | None
    interpretation: dict[str, Any] | None
    proposal: dict[str, Any] | None
    claims: tuple[dict[str, Any], ...]
    evidence: tuple[dict[str, Any], ...]
    budget: dict[str, Any]

    def public_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "terminal": self.terminal.value,
            "reason_code": self.reason_code,
            "message": self.message,
            "details": self.details,
            "run_id": self.run_id,
            "workflow_id": self.workflow_id,
            "workflow_kind": self.kind.value,
            "provider": self.provider,
            "evidence_label": self.evidence_label,
            "observed": self.observed,
            "interpretation": self.interpretation,
            "proposal": self.proposal,
            "claims": list(self.claims),
            "evidence": list(self.evidence),
            "budget": self.budget,
            "approval_required": self.terminal is WorkflowPhase.AWAITING_APPROVAL,
        }


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


class SupervisorWorkflow:
    """Run one supervisor invocation (``workflow_id``) on one manufacturing run."""

    def __init__(
        self,
        *,
        facade: ModelReadableApplicationServices,
        catalog: DomainCatalog,
        scenario_clock: ScenarioClock | None,
        seed: int,
        provider: Provider,
        ledger: BudgetLedger,
        pricing: TokenRates,
        budget: SupervisorBudget,
        trace: TraceStore,
        run_id: str,
        workflow_id: str,
        config: SupervisorConfig | None = None,
        clock: Clock | None = None,
        wall_clock: Callable[[], str] = _utc_now,
        retriever: LexicalRetriever | None = None,
        registry: ToolRegistry | None = None,
    ) -> None:
        if not isinstance(facade, ModelReadableApplicationServices):
            raise TypeError("the supervisor receives only the model-readable service facade")
        self._facade = facade
        self._catalog = catalog
        self._scenario_clock = scenario_clock
        self._seed = seed
        self._provider = provider
        self._ledger = ledger
        self._pricing = pricing
        self._budget = budget
        self._trace = trace
        self._run_id = run_id
        self._workflow_id = workflow_id
        self._config = config or SupervisorConfig()
        self._clock = clock or SystemClock()
        self._wall_clock = wall_clock
        self._retriever = retriever or LexicalRetriever(load_corpus())
        self._registry = registry or build_supervisor_registry()
        self._cancel_requested = False
        self._state: WorkflowState | None = None
        self._notices: list[str] = []
        self._fingerprints: set[str] = set()
        self._no_progress_repeats = 0
        self._invalid_streak = 0
        self._recorded_transitions = 0

    @property
    def run_id(self) -> str:
        return self._run_id

    @property
    def workflow_id(self) -> str:
        return self._workflow_id

    @property
    def state(self) -> WorkflowState | None:
        return self._state

    @property
    def evidence_label(self) -> str:
        if self._provider.provider_name == "deepseek":
            return LIVE_EVIDENCE_LABEL
        label = getattr(self._provider, "label", None)
        if isinstance(label, str) and label:
            return label
        return OFFLINE_EVIDENCE_LABEL

    def cancel(self) -> None:
        """Cancel cooperatively; the ledger also refuses any further attempt."""

        self._cancel_requested = True
        try:
            self._ledger.cancel(self._workflow_id)
        except (ProviderError, sqlite3.ProgrammingError):
            # A caller on another thread cannot use this thread-bound ledger handle.
            # The flag is still checked before every attempt and after every
            # response; such callers cancel the ledger file through their own handle.
            pass

    # ------------------------------------------------------------------ helpers

    def _record(self, kind: str, payload: dict[str, Any]) -> None:
        assert self._state is not None
        self._trace.append(
            workflow_id=self._workflow_id,
            run_id=self._run_id,
            kind=kind,
            phase=self._state.phase.value,
            payload=payload,
        )

    def _record_transitions(self) -> None:
        assert self._state is not None
        for source, target, reason in self._state.transitions[self._recorded_transitions :]:
            self._trace.append(
                workflow_id=self._workflow_id,
                run_id=self._run_id,
                kind="transition",
                phase=target.value,
                payload={"from": source.value, "to": target.value, "reason": reason},
            )
        self._recorded_transitions = len(self._state.transitions)

    def _finish(self, phase: WorkflowPhase, reason: str, message: str, **details: Any) -> None:
        assert self._state is not None
        if not self._state.phase.is_terminal:
            self._state.finish(phase, reason, message, details)
            self._record_transitions()

    def _context(self) -> ToolContext:
        assert self._state is not None
        diagnostics = DiagnosticService(
            run_id=self._run_id,
            get_checkpoint=self._facade.get_checkpoint,
            list_events=self._facade.list_events,
            retriever=self._retriever,
        )
        return ToolContext(
            run_id=self._run_id,
            workflow_id=self._workflow_id,
            facade=self._facade,
            catalog=self._catalog,
            diagnostics=diagnostics,
            scenario_clock=self._scenario_clock,
            seed=self._seed,
            state=self._state,
            wall_clock=self._wall_clock,
            solver_time_limit_seconds=self._config.solver_time_limit_seconds,
        )

    def _open_ledger(self) -> None:
        self._ledger.open_run(
            run_id=self._run_id,
            currency=self._budget.currency,
            cap_micros=self._budget.run_cap_micros,
        )
        self._ledger.open_workflow(
            workflow_id=self._workflow_id,
            run_id=self._run_id,
            limits=self._budget.limits,
            workflow_cap_micros=self._budget.workflow_cap_micros,
        )

    def _request(self, previous: tuple[ProviderMessage, ...]) -> ProviderRequest:
        assert self._state is not None
        tools = self._registry.schemas_for(self._state.phase, self._state.kind.value)
        wanted: set[str] | None = None
        if self._config.narrow_next_step_tools and self._state.phase is (
            WorkflowPhase.GATHER_EVIDENCE
        ):
            if self._state.kind is WorkflowKind.ORDER_REQUEST:
                # Interpretation, route and snapshot are already registered. Offering
                # evidence tools here caused live order workflows to reread in loops.
                wanted = {"solve_schedule"}
            elif self._state.evidence.of_kind(EvidenceKind.DIAGNOSIS):
                # A diagnosis is the final evidence product; move to planning rather
                # than advertising tools that can only grow the digest.
                wanted = {"solve_schedule"}
            else:
                has_observations = bool(self._state.evidence.of_kind(EvidenceKind.OBSERVATION))
                has_guidance = bool(self._state.evidence.of_kind(EvidenceKind.GUIDANCE))
                if not has_observations and not has_guidance:
                    wanted = {"summarize_observations", "retrieve_guidance"}
                elif not has_observations:
                    wanted = {"summarize_observations"}
                elif not has_guidance:
                    wanted = {"retrieve_guidance"}
                else:
                    wanted = {"diagnose_machine"}
        elif self._config.narrow_next_step_tools and self._state.phase is WorkflowPhase.VALIDATE:
            wanted = (
                {"validate_candidate"}
                if self._state.validated_candidate_hash is None
                else {"persist_proposal"}
            )
        if wanted is not None:
            tools = tuple(item for item in tools if item.name in wanted)
        digest = build_digest(
            self._state,
            allowed_tools=[item.name for item in tools],
            notices=self._notices,
        )
        choice = ToolChoice.REQUIRED if self._config.force_tool_choice else ToolChoice.AUTO
        return ProviderRequest(
            messages=(
                ProviderMessage(role=MessageRole.SYSTEM, content=SYSTEM_PROMPT),
                ProviderMessage(role=MessageRole.USER, content=digest_message(digest)),
                *previous,
            ),
            tools=tools,
            max_output_tokens=self._config.max_output_tokens,
            tool_choice=choice if tools else None,
        )

    def _budget_snapshot(self) -> dict[str, Any]:
        try:
            return self._ledger.snapshot(self._workflow_id).public_dict()
        except ProviderError:
            return {}

    # --------------------------------------------------------------- tool turns

    def _execute_tool(self, call: ValidatedToolCall, context: ToolContext) -> ToolOutcome | None:
        """Charge, execute, trace, and cycle-check one tool call; None ends the turn."""

        assert self._state is not None
        state = self._state
        arguments = dict(call.arguments)
        self._record(
            "tool_call",
            {"call_id": call.call_id, "tool": call.name, "arguments": arguments},
        )
        try:
            receipt = self._ledger.record_tool_execution(self._workflow_id, call.name)
        except BudgetExceededError as exc:
            self._finish(
                WorkflowPhase.BUDGET_EXHAUSTED,
                "budget_exhausted",
                "the workflow tool allowance is exhausted",
                limit=exc.details.get("limit"),
            )
            return None
        except WorkflowStateError as exc:
            self._finish_from_state_error(exc)
            return None
        before = state.progress_marker()
        evidence_before = {item.evidence_id for item in state.evidence.items()}
        started = self._clock.monotonic()
        outcome = self._registry.execute(call.name, arguments, context)
        elapsed = max(self._clock.monotonic() - started, 0.0)
        try:
            self._ledger.finish_tool_execution(
                receipt, active_seconds=elapsed, succeeded=outcome.ok
            )
        except ProviderError:
            pass
        new_evidence = sorted(
            item.evidence_id
            for item in state.evidence.items()
            if item.evidence_id not in evidence_before
        )
        self._record(
            "tool_result",
            {
                "call_id": call.call_id,
                "tool": call.name,
                "result": dict(outcome.payload),
                "evidence_ids": new_evidence,
                "active_seconds": round(elapsed, 6),
            },
        )
        self._record_transitions()
        if outcome.reason == RejectionReason.STALE_STATE.value:
            self._finish(
                WorkflowPhase.STALE_STATE,
                "stale_state",
                "operational state changed after observation; a fresh snapshot, replan, "
                "and new human approval are required",
                **dict(outcome.payload.get("details", {})),
            )
        if not outcome.ok:
            self._notices.append(f"{call.name} rejected: {outcome.reason}")
        fingerprint = canonical_hash({"tool": call.name, "arguments": arguments})
        progressed = state.progress_marker() != before
        if fingerprint in self._fingerprints and not progressed:
            self._no_progress_repeats += 1
            self._record(
                "notice",
                {
                    "notice": "repeated_call_without_progress",
                    "tool": call.name,
                    "repeats": self._no_progress_repeats,
                },
            )
            # Live S11 probe: this notice was only traced, so the model kept repeating.
            self._notices.append(
                f"{call.name} repeated with identical arguments and no progress; its result "
                "is already in the evidence list - follow next_step instead"
            )
            if self._no_progress_repeats >= self._config.max_no_progress_repeats:
                self._finish(
                    WorkflowPhase.CYCLE_DETECTED,
                    "repeated_calls_without_progress",
                    "identical tool calls repeated without workflow progress",
                    repeats=self._no_progress_repeats,
                )
        self._fingerprints.add(fingerprint)
        return outcome

    def _finish_from_state_error(self, exc: ProviderError) -> None:
        if exc.code is ProviderErrorCode.CANCELLED or self._cancel_requested:
            self._finish(WorkflowPhase.CANCELLED, "cancelled", "the workflow was cancelled")
        else:
            self._finish(
                WorkflowPhase.BUDGET_EXHAUSTED,
                "budget_exhausted",
                "the provider ledger refused further work",
                code=exc.code.value,
            )

    def _handle_provider_error(self, exc: ProviderError, request_id: str) -> None:
        assert self._state is not None
        self._record("provider_rejection", {"request_id": request_id, "error": exc.as_dict()})
        if isinstance(exc, (ToolCallValidationError, ProviderResponseValidationError)):
            self._invalid_streak += 1
            reason = exc.details.get("reason", exc.code.value)
            tool = exc.details.get("tool_name")
            notice = f"previous turn rejected: {reason}" + (f" ({tool})" if tool else "")
            if reason == "malformed_json":
                # Live S11 probe: long claims were cut off by the output token limit.
                notice += (
                    "; the arguments were probably truncated by the output limit - send "
                    "fewer, shorter claims with fewer evidence ids"
                )
            self._notices.append(notice)
            if self._invalid_streak >= self._config.max_consecutive_invalid_turns:
                self._finish(
                    WorkflowPhase.CYCLE_DETECTED,
                    "repeated_invalid_turns",
                    "the provider repeatedly returned invalid or disallowed tool calls",
                    consecutive_invalid_turns=self._invalid_streak,
                )
            return
        if isinstance(exc, BudgetExceededError) or exc.code is ProviderErrorCode.ACTIVE_DEADLINE:
            self._finish(
                WorkflowPhase.BUDGET_EXHAUSTED,
                "budget_exhausted",
                "a configured provider allowance was exhausted",
                code=exc.code.value,
                limit=exc.details.get("limit"),
            )
            return
        if isinstance(exc, WorkflowStateError):
            self._finish_from_state_error(exc)
            return
        self._finish(
            WorkflowPhase.FAILED,
            "provider_failure",
            "the provider failed; operational state is unchanged",
            code=exc.code.value,
            attempts=exc.attempts,
        )

    # ---------------------------------------------------------------------- run

    def run(self, task: SupervisorTask) -> SupervisorOutcome:
        if self._state is not None:
            raise RuntimeError("a SupervisorWorkflow instance runs exactly once")
        initial = (
            WorkflowPhase.INTERPRET
            if task.kind is WorkflowKind.ORDER_REQUEST
            else WorkflowPhase.OBSERVE
        )
        self._state = WorkflowState(
            run_id=self._run_id,
            workflow_id=self._workflow_id,
            kind=task.kind,
            request_text=task.request_text,
            focus_machine_ids=tuple(task.focus_machine_ids),
            phase=initial,
        )
        self._record(
            "workflow_started",
            {
                "workflow_kind": task.kind.value,
                "request_sha256": hashlib.sha256(task.request_text.encode()).hexdigest(),
                "request_text_untrusted": task.request_text,
                "focus_machine_ids": list(task.focus_machine_ids),
                "provider": self._provider.provider_name,
                "evidence_label": self.evidence_label,
                "tool_registry": [item.public_dict()["name"] for item in self._registry.specs()],
            },
        )
        try:
            self._open_ledger()
            self._facade.ensure_workflow(
                self._run_id, self._workflow_id, updated_at_utc=self._wall_clock()
            )
        except ApplicationServiceError as exc:
            self._finish(
                WorkflowPhase.FAILED,
                "workflow_conflict",
                "the S06 workflow record could not be opened as running",
                code=exc.code.value,
            )
        except ProviderError as exc:
            self._finish(
                WorkflowPhase.FAILED,
                "ledger_rejected",
                "the provider ledger rejected the workflow budget",
                code=exc.code.value,
            )
        else:
            self._loop()
        return self._outcome()

    def _loop(self) -> None:
        assert self._state is not None
        state = self._state
        bounded = BoundedProvider(
            self._provider,
            self._ledger,
            self._workflow_id,
            self._pricing,
            clock=self._clock,
        )
        context = self._context()
        previous: tuple[ProviderMessage, ...] = ()
        for turn_index in range(1, self._config.max_turns + 1):
            if state.phase.is_terminal:
                return
            if self._cancel_requested:
                self._finish(WorkflowPhase.CANCELLED, "cancelled", "the workflow was cancelled")
                return
            request = self._request(previous)
            request_id = f"{self._workflow_id}-req-{turn_index}"
            self._record(
                "provider_request",
                {
                    "request_id": request_id,
                    "tools": [item.name for item in request.tools],
                    "tool_choice": None
                    if request.tool_choice is None
                    else request.tool_choice.value,
                    "messages": [item.public_dict() for item in request.messages],
                    "request_bytes": len(request.estimated_payload_text().encode("utf-8")),
                },
            )
            try:
                turn = bounded.complete(request, request_id=request_id)
            except ProviderError as exc:
                self._handle_provider_error(exc, request_id)
                previous = ()
                continue
            self._record("provider_response", {"request_id": request_id, **turn.public_dict()})
            if self._cancel_requested:
                self._finish(WorkflowPhase.CANCELLED, "cancelled", "the workflow was cancelled")
                return
            if not turn.tool_calls:
                self._invalid_streak += 1
                self._notices.append("previous turn had no tool call; a tool call is required")
                if self._invalid_streak >= self._config.max_consecutive_invalid_turns:
                    self._finish(
                        WorkflowPhase.CYCLE_DETECTED,
                        "repeated_invalid_turns",
                        "the provider repeatedly answered without a tool call",
                        consecutive_invalid_turns=self._invalid_streak,
                    )
                previous = ()
                continue
            self._invalid_streak = 0
            results: list[ProviderMessage] = []
            for call in turn.tool_calls:
                if state.phase.is_terminal:
                    results.append(self._skipped(call))
                    continue
                outcome = self._execute_tool(call, context)
                if outcome is None:
                    results.append(self._skipped(call))
                    continue
                results.append(
                    ProviderMessage(
                        role=MessageRole.TOOL,
                        content=outcome.content(),
                        tool_call_id=call.call_id,
                        name=call.name,
                    )
                )
            previous = (turn.response.message, *results)
        if not state.phase.is_terminal:
            self._finish(
                WorkflowPhase.BUDGET_EXHAUSTED,
                "turn_limit",
                "the supervisor turn safety limit was reached",
                max_turns=self._config.max_turns,
            )

    @staticmethod
    def _skipped(call: ValidatedToolCall) -> ProviderMessage:
        return ProviderMessage(
            role=MessageRole.TOOL,
            content=json.dumps({"status": "skipped", "reason": "workflow_terminal"}),
            tool_call_id=call.call_id,
            name=call.name,
        )

    def _outcome(self) -> SupervisorOutcome:
        assert self._state is not None
        state = self._state
        if state.terminal is None:  # pragma: no cover - every path finishes
            self._finish(WorkflowPhase.FAILED, "internal_error", "workflow ended without terminal")
        assert state.terminal is not None
        if state.terminal.phase is WorkflowPhase.AWAITING_APPROVAL:
            try:
                self._ledger.pause_for_human(self._workflow_id)
            except ProviderError:
                pass
        proposal = None
        if state.proposal is not None:
            proposal = {
                "proposal_id": state.proposal.proposal_id,
                "proposal_hash": state.proposal.proposal_hash,
                "base_state_version": state.proposal.base_state_version,
                "base_snapshot_hash": state.proposal.base_snapshot_hash,
                "proposed_plan_revision": state.proposal.proposed_plan_revision,
                "objective": state.proposal.objective.model_dump(mode="json"),
                "order_deltas": [
                    item.model_dump(mode="json") for item in state.proposal.proposed_order_deltas
                ],
                "resource_deltas": [
                    item.model_dump(mode="json") for item in state.proposal.proposed_resource_deltas
                ],
            }
        budget = self._budget_snapshot()
        outcome = SupervisorOutcome(
            terminal=state.terminal.phase,
            reason_code=state.terminal.reason_code,
            message=state.terminal.message,
            details=dict(state.terminal.details),
            run_id=self._run_id,
            workflow_id=self._workflow_id,
            kind=state.kind,
            provider=self._provider.provider_name,
            evidence_label=self.evidence_label,
            observed=None if state.observed is None else state.observed.public_dict(),
            interpretation=(
                None
                if state.interpretation is None
                else state.interpretation.model_dump(mode="json")
            ),
            proposal=proposal,
            claims=state.claims,
            evidence=tuple(item.public_dict() for item in state.evidence.items()),
            budget=budget,
        )
        self._record(
            "terminal",
            {
                "terminal": outcome.terminal.value,
                "reason_code": outcome.reason_code,
                "details": outcome.details,
                "proposal_id": None if proposal is None else proposal["proposal_id"],
                "proposal_hash": None if proposal is None else proposal["proposal_hash"],
                "claims": list(outcome.claims),
                "budget": budget,
            },
        )
        return outcome
