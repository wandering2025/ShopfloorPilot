"""Operator-side recovery coordinator: decision boundaries, approval, and application.

The coordinator is backend orchestration around the accepted S06 service. It owns
no operational state and duplicates no validation or application logic:

* a *decision* is opened at a paused simulated boundary before any snapshot is
  collected, and its integer decision time/version/hash is persisted in the S06
  workflow payload;
* the supervisor (or an explicit deterministic generator) only ever produces a
  proposal through the model-readable facade;
* approval is bound to the exact stored proposal hash and the current state
  version, and application is S06 ``apply_approved_plan`` with a deterministic
  idempotency key and the recorded decision boundary;
* cancellation, timeouts, stale state, reset, and late supervisor results close or
  annotate lifecycle records only; they never touch operational state.

Every status is derived from SQLite, so a new process constructs the same view.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from shopfloor_pilot.agent.workflow import SupervisorOutcome, SupervisorTask, SupervisorWorkflow
from shopfloor_pilot.budgets import BudgetLedger
from shopfloor_pilot.domain.canonical import canonical_hash
from shopfloor_pilot.domain.fixtures import (
    InterventionKind,
    ObservationTemplate,
    ScheduledIntervention,
)
from shopfloor_pilot.domain.models import (
    ApprovalDecision,
    ApprovalRecord,
    AttemptStatus,
    MachineStatus,
    Observation,
    OrderDelta,
)
from shopfloor_pilot.providers.errors import ProviderError
from shopfloor_pilot.scheduling import SchedulingResult, SolverConfig
from shopfloor_pilot.services import (
    ApplicationServiceError,
    ApplicationServices,
    PlanApplicationStatus,
    ServiceErrorCode,
)
from shopfloor_pilot.simulation import AdvanceResult
from shopfloor_pilot.storage import AuditRecord, WorkflowRecord, WorkflowStatus
from shopfloor_pilot.tools import WorkflowPhase
from shopfloor_pilot.tools.context import decision_boundary_seconds

from .models import (
    DECISION_MODE,
    ActionResult,
    ActionStatus,
    CoordinatorError,
    CoordinatorErrorCode,
    DecisionKind,
    DecisionRecord,
    DecisionSource,
    DecisionStatus,
)

WallClock = Callable[[], str]
HUMAN_ACTOR_PREFIXES = ("human:", "operator:")
RESTORATION_ACTOR_PREFIXES = ("human:", "operator:", "harness:")
_STATUS_BY_WORKFLOW = {
    WorkflowStatus.CREATED: DecisionStatus.OPEN,
    WorkflowStatus.RUNNING: DecisionStatus.OPEN,
    WorkflowStatus.WAITING_APPROVAL: DecisionStatus.AWAITING_APPROVAL,
    WorkflowStatus.COMPLETED: DecisionStatus.APPLIED,
    WorkflowStatus.FAILED: DecisionStatus.PAUSED,
    WorkflowStatus.CANCELLED: DecisionStatus.CANCELLED,
}
_CONFLICT_CODES = frozenset(
    {
        ServiceErrorCode.APPROVAL_BINDING_MISMATCH,
        ServiceErrorCode.APPROVAL_REJECTED,
        ServiceErrorCode.APPROVAL_NOT_FOUND,
        ServiceErrorCode.PROPOSAL_HASH_MISMATCH,
        ServiceErrorCode.PROPOSAL_NOT_FOUND,
        ServiceErrorCode.WORKFLOW_CONFLICT,
        ServiceErrorCode.IDEMPOTENCY_CONFLICT,
        ServiceErrorCode.WRONG_RUN,
    }
)


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def operational_identity(services: ApplicationServices, run_id: str) -> dict[str, Any]:
    """Everything a decision, approval failure, or late callback must not change."""

    checkpoint = services.get_checkpoint(run_id)
    snapshot = checkpoint.snapshot
    return {
        "run_id": run_id,
        "state_version": snapshot.state_version,
        "plan_revision": snapshot.plan_revision,
        "simulated_time_seconds": snapshot.simulated_time_seconds,
        "snapshot_hash": checkpoint.snapshot_hash,
        "event_count": checkpoint.last_sequence,
        "orders": [item.model_dump(mode="json") for item in snapshot.orders],
        "machines": [item.model_dump(mode="json") for item in snapshot.machines],
        "operations": [item.model_dump(mode="json") for item in snapshot.operations],
        "attempts": [item.model_dump(mode="json") for item in snapshot.attempts],
    }


@dataclass(slots=True)
class SupervisorSession:
    """A supervisor built inside its worker thread (SQLite handles are thread-bound)."""

    workflow: SupervisorWorkflow
    task: SupervisorTask
    close: Callable[[], None] = lambda: None
    ledger_path: Path | None = None
    trace_records: Callable[[], Any] | None = None


SessionFactory = Callable[[], SupervisorSession]


@dataclass(slots=True)
class _InFlight:
    decision: DecisionRecord
    lock: threading.Lock = field(default_factory=threading.Lock)
    done: threading.Event = field(default_factory=threading.Event)
    session: SupervisorSession | None = None
    outcome: SupervisorOutcome | None = None
    error: BaseException | None = None
    abandoned: bool = False
    thread: threading.Thread | None = None
    late_audit: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class SupervisorStep:
    """Settled result of one supervisor invocation for one decision."""

    decision: DecisionRecord
    outcome: SupervisorOutcome | None
    timed_out: bool
    discarded: bool
    identity_before: dict[str, Any]
    identity_after: dict[str, Any]

    @property
    def operational_state_unchanged(self) -> bool:
        return self.identity_before == self.identity_after

    def public_dict(self) -> dict[str, Any]:
        return {
            "decision": self.decision.public_dict(),
            "supervisor_terminal": None if self.outcome is None else self.outcome.terminal.value,
            "supervisor_reason_code": None if self.outcome is None else self.outcome.reason_code,
            "timed_out": self.timed_out,
            "discarded": self.discarded,
            "operational_state_unchanged": self.operational_state_unchanged,
        }


class RecoveryCoordinator:
    """One backend coordinator per process over one shared SQLite authority."""

    def __init__(
        self,
        services: ApplicationServices,
        *,
        wall_clock: WallClock = _utc_now,
        batch_policy: str | None = None,
    ) -> None:
        if not isinstance(services, ApplicationServices):
            raise TypeError("the coordinator is operator-side and needs ApplicationServices")
        self.services = services
        self._wall_clock = wall_clock
        self._batch_policy = batch_policy
        self._inflight: dict[tuple[str, str], _InFlight] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ reads

    def decisions(self, run_id: str) -> tuple[DecisionRecord, ...]:
        lifecycle = self.services.export_lifecycle(run_id)
        approvals: dict[str, list[ApprovalRecord]] = {}
        for approval in lifecycle.approvals:
            approvals.setdefault(approval.workflow_id, []).append(approval)
        consumed = {item.approval_id: item for item in lifecycle.approval_consumptions}
        records: list[DecisionRecord] = []
        for workflow in lifecycle.workflows:
            if workflow.payload.get("mode") != DECISION_MODE:
                continue
            records.append(self._decision_from(workflow, approvals, consumed))
        return tuple(sorted(records, key=lambda item: (item.base_state_version, item.workflow_id)))

    def decision(self, run_id: str, workflow_id: str) -> DecisionRecord:
        for item in self.decisions(run_id):
            if item.workflow_id == workflow_id:
                return item
        raise CoordinatorError(
            CoordinatorErrorCode.DECISION_NOT_FOUND,
            "no S11 decision with this workflow ID exists in the run",
            details={"run_id": run_id, "workflow_id": workflow_id},
        )

    def open_decisions(self, run_id: str) -> tuple[DecisionRecord, ...]:
        return tuple(item for item in self.decisions(run_id) if item.status.is_open)

    def superseded_by(self, run_id: str) -> tuple[str, ...]:
        return self.services.list_reset_successors(run_id)

    def run_status(self, run_id: str) -> dict[str, Any]:
        checkpoint = self.services.get_checkpoint(run_id)
        decisions = self.decisions(run_id)
        open_items = [item for item in decisions if item.status.is_open]
        paused = [item for item in decisions if item.status is DecisionStatus.PAUSED]
        latest = decisions[-1] if decisions else None
        if open_items:
            state = "paused_at_decision"
            reason = open_items[-1].status.value
        elif latest is not None and latest.status is DecisionStatus.PAUSED:
            state = "paused_blocked"
            reason = latest.reason_code
        else:
            state = "idle"
            reason = None
        return {
            "schema_version": 1,
            "run_id": run_id,
            "state": state,
            "reason_code": reason,
            "superseded_by": list(self.superseded_by(run_id)),
            "simulated_time_seconds": checkpoint.snapshot.simulated_time_seconds,
            "state_version": checkpoint.snapshot.state_version,
            "plan_revision": checkpoint.snapshot.plan_revision,
            "open_decisions": [item.workflow_id for item in open_items],
            "paused_decisions": [item.workflow_id for item in paused],
            "simulation_advances_only_on_explicit_command": True,
        }

    def restore(self, run_id: str) -> dict[str, Any]:
        """Describe durable decisions after a process restart; grants nothing.

        Awaiting approvals remain actionable only after the normal fresh-state
        checks in :meth:`approve`/:meth:`apply`. An OPEN decision whose supervisor
        lived in a previous process is reported as orphaned; it must be cancelled
        explicitly before time can advance.
        """

        decisions = self.decisions(run_id)
        with self._lock:
            live = {key for key, item in self._inflight.items() if not item.done.is_set()}
        return {
            "schema_version": 1,
            "run_id": run_id,
            "status": self.run_status(run_id),
            "awaiting_approval": [
                item.public_dict()
                for item in decisions
                if item.status is DecisionStatus.AWAITING_APPROVAL
            ],
            "applied": [
                item.workflow_id for item in decisions if item.status is DecisionStatus.APPLIED
            ],
            "orphaned_open": [
                item.workflow_id
                for item in decisions
                if item.status is DecisionStatus.OPEN and (run_id, item.workflow_id) not in live
            ],
            "approvals_from_history_are_actionable": False,
        }

    # ------------------------------------------------------- decision boundary

    def open_decision(
        self,
        run_id: str,
        workflow_id: str,
        *,
        kind: DecisionKind,
        source: DecisionSource,
        provider: str | None = None,
        focus_machine_ids: Sequence[str] = (),
        with_order_delta: bool = False,
        fallback_for_workflow_id: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> DecisionRecord:
        """Pause at the current boundary and persist the decision context.

        This happens before the supervisor collects any snapshot. Reopening the
        same open decision after a restart returns it unchanged.
        """

        self._require_live_run(run_id)
        for item in self.open_decisions(run_id):
            if item.workflow_id == workflow_id:
                return item
            raise CoordinatorError(
                CoordinatorErrorCode.DECISION_PENDING,
                "another decision is open on this run; close it before opening a new one",
                details={"run_id": run_id, "open_workflow_id": item.workflow_id},
            )
        if any(item.workflow_id == workflow_id for item in self.decisions(run_id)):
            raise CoordinatorError(
                CoordinatorErrorCode.DECISION_CLOSED,
                "a closed decision cannot be reopened; use a new workflow ID",
                details={"run_id": run_id, "workflow_id": workflow_id},
            )
        checkpoint = self.services.get_checkpoint(run_id)
        snapshot = checkpoint.snapshot
        blocker = _boundary_blocker(checkpoint, with_order_delta=with_order_delta)
        if blocker is not None:
            raise CoordinatorError(
                CoordinatorErrorCode.DECISION_BOUNDARY_BLOCKED,
                "a running operation completes exactly at the next application boundary; "
                "advance through that completion before pausing for a decision",
                details={"run_id": run_id, "advance_to_seconds": blocker},
            )
        payload: dict[str, Any] = {
            "mode": DECISION_MODE,
            "decision_kind": kind.value,
            "source": source.value,
            "provider": provider,
            "decision_time_seconds": snapshot.simulated_time_seconds,
            "decision_boundary_seconds": decision_boundary_seconds(
                checkpoint, with_order_delta=with_order_delta
            ),
            "base_state_version": snapshot.state_version,
            "base_plan_revision": snapshot.plan_revision,
            "base_snapshot_hash": checkpoint.snapshot_hash,
            "focus_machine_ids": list(focus_machine_ids),
            "fallback_for_workflow_id": fallback_for_workflow_id,
            "decision_details": dict(details or {}),
        }
        opened_at = self._wall_clock()
        self.services.ensure_workflow(
            run_id, workflow_id, payload=payload, updated_at_utc=opened_at
        )
        self._audit(
            run_id,
            workflow_id,
            "decision_opened",
            actor="coordinator",
            details={key: value for key, value in payload.items() if key != "mode"},
            simulated_time_seconds=snapshot.simulated_time_seconds,
        )
        rivals = [item for item in self.open_decisions(run_id) if item.workflow_id != workflow_id]
        if rivals:
            # Two concurrent opens both passed the check above. Each side withdraws its
            # own decision, so at most one survives and no snapshot is ever shared.
            self.cancel(
                run_id,
                workflow_id,
                outcome="superseded",
                reason_code="concurrent_decision_open",
                actor="coordinator",
                details={"rival_workflow_ids": [item.workflow_id for item in rivals]},
            )
            raise CoordinatorError(
                CoordinatorErrorCode.DECISION_PENDING,
                "another decision was opened concurrently; this one was withdrawn",
                details={
                    "run_id": run_id,
                    "open_workflow_ids": [item.workflow_id for item in rivals],
                },
            )
        return self.decision(run_id, workflow_id)

    def settle_decision_boundary(
        self,
        run_id: str,
        advance_id_prefix: str,
        *,
        with_order_delta: bool = False,
        max_steps: int = 5,
    ) -> list[int]:
        """Explicitly advance through completions due at the next decision boundary.

        At a closed second ``T`` the earliest application boundary is ``T + 1``. S06
        refuses to pre-stage a plan there if a running operation completes at that
        second, because the plan would skip the completion. This harness/operator
        step makes the time movement explicit (durable advance IDs, returned
        seconds) instead of hiding it inside ``open_decision``.
        """

        advanced: list[int] = []
        for _step in range(max_steps):
            checkpoint = self.services.get_checkpoint(run_id)
            blocker = _boundary_blocker(checkpoint, with_order_delta=with_order_delta)
            if blocker is None:
                return advanced
            self.advance(run_id, blocker, f"{advance_id_prefix}-settle-{blocker}")
            advanced.append(blocker)
        raise CoordinatorError(
            CoordinatorErrorCode.DECISION_BOUNDARY_BLOCKED,
            "the decision boundary is still blocked after the allowed settle steps",
            details={"run_id": run_id, "advanced_to": advanced},
        )

    def run_supervisor(
        self,
        decision: DecisionRecord,
        factory: SessionFactory,
        *,
        timeout_seconds: float | None = None,
        deadline: threading.Event | None = None,
    ) -> SupervisorStep:
        """Run one supervisor for an OPEN decision; never approves or applies.

        The supervisor runs in a worker thread so a wall-clock timeout or an operator
        cancellation can close the decision while a provider call is outstanding.
        Waiting consumes wall time only; simulated time is never advanced here.
        """

        current = self.decision(decision.run_id, decision.workflow_id)
        if current.status is not DecisionStatus.OPEN:
            raise CoordinatorError(
                CoordinatorErrorCode.DECISION_CLOSED,
                "the supervisor may only run for an open decision without a proposal",
                details={"workflow_id": decision.workflow_id, "status": current.status.value},
            )
        before = operational_identity(self.services, decision.run_id)
        inflight = _InFlight(decision=current)
        key = (decision.run_id, decision.workflow_id)
        with self._lock:
            self._inflight[key] = inflight

        def target() -> None:
            try:
                session = factory()
                inflight.session = session
                workflow = session.workflow
                if (workflow.run_id, workflow.workflow_id) != key:
                    raise CoordinatorError(
                        CoordinatorErrorCode.SUPERVISOR_MISMATCH,
                        "the supervisor session is bound to another run or workflow",
                    )
                try:
                    inflight.outcome = workflow.run(session.task)
                finally:
                    session.close()
            except BaseException as exc:  # surfaced to the waiting caller
                inflight.error = exc
            finally:
                with inflight.lock:
                    abandoned = inflight.abandoned
                    inflight.done.set()
                if abandoned:
                    self._record_late_result(inflight)

        thread = threading.Thread(target=target, name=f"supervisor-{decision.workflow_id}")
        inflight.thread = thread
        thread.daemon = True
        thread.start()
        finished = _wait_for(inflight.done, timeout_seconds, deadline)
        if not finished:
            with inflight.lock:
                finished = inflight.done.is_set()
                if not finished:
                    inflight.abandoned = True
        if not finished:
            self.cancel(
                decision.run_id,
                decision.workflow_id,
                outcome="timed_out",
                reason_code="decision_timeout",
                actor="coordinator",
                details={
                    "timeout_seconds": timeout_seconds,
                    "trigger": "deadline_event"
                    if deadline is not None and deadline.is_set()
                    else "timeout_seconds",
                },
            )
            return SupervisorStep(
                decision=self.decision(decision.run_id, decision.workflow_id),
                outcome=None,
                timed_out=True,
                discarded=True,
                identity_before=before,
                identity_after=operational_identity(self.services, decision.run_id),
            )
        if inflight.error is not None:
            raise inflight.error
        assert inflight.outcome is not None
        return self._settle(inflight, before)

    def wait_for_late_result(
        self, run_id: str, workflow_id: str, timeout_seconds: float | None = None
    ) -> dict[str, Any] | None:
        """Join an abandoned supervisor thread (tests/shutdown); return its audit."""

        with self._lock:
            inflight = self._inflight.get((run_id, workflow_id))
        if inflight is None:
            return None
        inflight.done.wait(timeout_seconds)
        if inflight.thread is not None:
            inflight.thread.join(timeout_seconds)
        return inflight.late_audit

    def propose_deterministic(
        self,
        decision: DecisionRecord,
        generate: Callable[[DecisionRecord], SchedulingResult],
    ) -> tuple[DecisionRecord, SchedulingResult]:
        """Let explicit deterministic code (baseline/fallback) fill an OPEN decision."""

        current = self.decision(decision.run_id, decision.workflow_id)
        if current.status is not DecisionStatus.OPEN:
            raise CoordinatorError(
                CoordinatorErrorCode.DECISION_CLOSED,
                "deterministic generation needs an open decision",
                details={"workflow_id": decision.workflow_id, "status": current.status.value},
            )
        if current.source is DecisionSource.SUPERVISOR:
            raise CoordinatorError(
                CoordinatorErrorCode.FALLBACK_NOT_ALLOWED,
                "a supervisor decision is never silently filled by deterministic code",
                details={"workflow_id": decision.workflow_id},
            )
        result = generate(current)
        settled = self.decision(decision.run_id, decision.workflow_id)
        if result.candidate is None and settled.status is DecisionStatus.OPEN:
            self.services.mark_workflow_blocked(
                decision.run_id,
                decision.workflow_id,
                reason_code=f"deterministic_{result.outcome.value}",
                details={
                    "solver_outcome": result.outcome.value,
                    "diagnostics": list(result.report.diagnostics[:5]),
                },
                updated_at_utc=self._wall_clock(),
            )
        elif result.candidate is not None:
            self._require_proposal_on_boundary(settled, result.candidate.base_state_version)
        return self.decision(decision.run_id, decision.workflow_id), result

    def run_deterministic_fallback(
        self,
        run_id: str,
        failed_workflow_id: str,
        *,
        workflow_id: str,
        actor: str,
        order_deltas: Sequence[OrderDelta] = (),
        seed: int = 0,
    ) -> tuple[DecisionRecord, SchedulingResult]:
        """Explicit, user-selected deterministic fallback for a paused decision.

        The new decision is labeled ``deterministic_fallback`` and still needs a
        fresh human approval. It is never counted as provider or LLM success.
        Typed order deltas, if any, are explicit operator inputs.
        """

        self._require_actor(actor, HUMAN_ACTOR_PREFIXES)
        failed = self.decision(run_id, failed_workflow_id)
        if failed.status is not DecisionStatus.PAUSED and not (
            failed.status is DecisionStatus.CANCELLED and failed.outcome == "timed_out"
        ):
            raise CoordinatorError(
                CoordinatorErrorCode.FALLBACK_NOT_ALLOWED,
                "a fallback is offered only for a paused (blocked) or timed-out decision",
                details={"workflow_id": failed_workflow_id, "status": failed.status.value},
            )
        decision = self.open_decision(
            run_id,
            workflow_id,
            kind=failed.kind,
            source=DecisionSource.DETERMINISTIC_FALLBACK,
            focus_machine_ids=failed.focus_machine_ids,
            with_order_delta=bool(order_deltas),
            fallback_for_workflow_id=failed_workflow_id,
            details={
                "selected_by": actor,
                "label": "DETERMINISTIC FALLBACK - not provider or LLM evidence",
            },
        )
        self._audit(
            run_id,
            workflow_id,
            "deterministic_fallback_selected",
            actor=actor,
            details={
                "fallback_for_workflow_id": failed_workflow_id,
                "counts_as_llm_success": False,
            },
            simulated_time_seconds=decision.decision_time_seconds,
        )

        def generate(current: DecisionRecord) -> SchedulingResult:
            not_before = (
                current.decision_boundary_seconds
                if current.decision_boundary_seconds > current.decision_time_seconds
                else None
            )
            return self.services.generate_proposal(
                run_id,
                workflow_id,
                f"proposal-{workflow_id}",
                proposed_order_deltas=tuple(order_deltas),
                not_before_seconds=not_before,
                config=SolverConfig(random_seed=seed),
                created_at_utc=self._wall_clock(),
            )

        return self.propose_deterministic(decision, generate)

    # -------------------------------------------------------- operator actions

    def approve(
        self,
        run_id: str,
        workflow_id: str,
        *,
        proposal_id: str,
        proposal_hash: str,
        actor: str,
        reason: str | None = None,
    ) -> ActionResult:
        """Record one explicit human approval bound to the hash and current version."""

        self._require_actor(actor, HUMAN_ACTOR_PREFIXES)
        return self._decide(
            run_id,
            workflow_id,
            proposal_id=proposal_id,
            proposal_hash=proposal_hash,
            actor=actor,
            decision=ApprovalDecision.APPROVED,
            reason=reason,
            policy_name=None,
        )

    def approve_by_policy(
        self,
        run_id: str,
        workflow_id: str,
        *,
        proposal_id: str,
        proposal_hash: str,
        reason: str | None = None,
    ) -> ActionResult:
        """Explicitly configured offline batch policy; never an interactive default."""

        if self._batch_policy is None:
            raise CoordinatorError(
                CoordinatorErrorCode.INVALID_ACTOR,
                "no explicit offline batch approval policy is configured",
            )
        return self._decide(
            run_id,
            workflow_id,
            proposal_id=proposal_id,
            proposal_hash=proposal_hash,
            actor=f"policy:{self._batch_policy}",
            decision=ApprovalDecision.APPROVED,
            reason=reason or f"Explicit offline batch policy {self._batch_policy}.",
            policy_name=self._batch_policy,
        )

    def reject(
        self,
        run_id: str,
        workflow_id: str,
        *,
        proposal_id: str,
        proposal_hash: str,
        actor: str,
        reason: str | None = None,
    ) -> ActionResult:
        self._require_actor(actor, HUMAN_ACTOR_PREFIXES)
        return self._decide(
            run_id,
            workflow_id,
            proposal_id=proposal_id,
            proposal_hash=proposal_hash,
            actor=actor,
            decision=ApprovalDecision.REJECTED,
            reason=reason,
            policy_name=None,
        )

    def apply(
        self,
        run_id: str,
        workflow_id: str,
        *,
        proposal_id: str,
        proposal_hash: str,
        approval_id: str,
        idempotency_key: str | None = None,
        _failure_injector: Callable[[str], None] | None = None,
    ) -> ActionResult:
        """Apply through S06 with the recorded decision boundary; idempotent."""

        decision = self.decision(run_id, workflow_id)
        key = idempotency_key or f"apply-{approval_id}"
        base = {
            "action": "apply",
            "run_id": run_id,
            "workflow_id": workflow_id,
            "proposal_id": proposal_id,
            "proposal_hash": proposal_hash,
            "approval_id": approval_id,
        }
        if decision.status not in (DecisionStatus.AWAITING_APPROVAL, DecisionStatus.APPLIED):
            return ActionResult(
                **base,
                status=ActionStatus.CONFLICT,
                reason_code=f"decision_{decision.status.value}",
            )
        if decision.status is DecisionStatus.AWAITING_APPROVAL:
            self._require_live_run(run_id)
        try:
            result = self.services.apply_approved_plan(
                run_id,
                proposal_id,
                proposal_hash,
                approval_id,
                key,
                effective_time_seconds=decision.decision_boundary_seconds,
                _failure_injector=_failure_injector,
            )
        except ApplicationServiceError as exc:
            consumed = self._consumption(run_id, proposal_id)
            if consumed is not None:
                return ActionResult(
                    **base,
                    status=ActionStatus.ALREADY_APPLIED,
                    reason_code="proposal_already_applied",
                    resulting_state_version=consumed.resulting_state_version,
                    details={
                        "consumed_approval_id": consumed.approval_id,
                        "attempt_code": exc.code.value,
                    },
                )
            if exc.code is ServiceErrorCode.STALE_STATE:
                self._close_stale(run_id, workflow_id, "stale_at_application", exc.details)
                return ActionResult(
                    **base,
                    status=ActionStatus.STALE,
                    reason_code="stale_state",
                    details=_json_details(exc.details),
                )
            status = ActionStatus.CONFLICT if exc.code in _CONFLICT_CODES else ActionStatus.FAILED
            return ActionResult(
                **base,
                status=status,
                reason_code=exc.code.value,
                details={
                    **_json_details(exc.details),
                    "retryable": exc.code is ServiceErrorCode.DATABASE_BUSY,
                },
            )
        except Exception as exc:  # transaction failure: S06 rolled everything back
            return ActionResult(
                **base,
                status=ActionStatus.FAILED,
                reason_code="transaction_failed",
                details={"error_type": type(exc).__name__, "retryable": True},
            )
        status = (
            ActionStatus.APPLIED
            if result.status is PlanApplicationStatus.APPLIED
            else ActionStatus.ALREADY_APPLIED
        )
        return ActionResult(
            **base,
            status=status,
            previous_state_version=result.previous_state_version,
            resulting_state_version=result.resulting_state_version,
            effective_time_seconds=result.effective_time_seconds,
            details={"idempotency_key": key, "mutation_id": result.mutation.mutation_id},
        )

    def cancel(
        self,
        run_id: str,
        workflow_id: str,
        *,
        outcome: str = "cancelled",
        reason_code: str = "operator_cancelled",
        actor: str = "operator",
        details: dict[str, Any] | None = None,
    ) -> ActionResult:
        """Close an open decision; a live supervisor is cancelled cooperatively."""

        with self._lock:
            inflight = self._inflight.get((run_id, workflow_id))
        if inflight is not None and not inflight.done.is_set() and inflight.session is not None:
            inflight.session.workflow.cancel()
            if inflight.session.ledger_path is not None:
                ledger = BudgetLedger(inflight.session.ledger_path)
                try:
                    ledger.cancel(workflow_id)
                except ProviderError:
                    pass
                finally:
                    ledger.close()
        decision = self.decision(run_id, workflow_id)
        base = {"action": "cancel", "run_id": run_id, "workflow_id": workflow_id, "actor": actor}
        if decision.status is DecisionStatus.APPLIED:
            return ActionResult(**base, status=ActionStatus.CONFLICT, reason_code="already_applied")
        if decision.status is DecisionStatus.PAUSED:
            return ActionResult(**base, status=ActionStatus.CONFLICT, reason_code="decision_paused")
        try:
            self.services.cancel_workflow(
                run_id,
                workflow_id,
                reason_code=reason_code,
                outcome=outcome,
                details=_json_details(details or {}),
                updated_at_utc=self._wall_clock(),
            )
        except ApplicationServiceError as exc:
            settled = self.decision(run_id, workflow_id)
            if settled.status is DecisionStatus.APPLIED:
                return ActionResult(
                    **base, status=ActionStatus.CONFLICT, reason_code="already_applied"
                )
            return ActionResult(**base, status=ActionStatus.CONFLICT, reason_code=exc.code.value)
        self._audit(
            run_id,
            workflow_id,
            f"decision_{outcome}",
            actor=actor,
            details={"reason_code": reason_code, **_json_details(details or {})},
            simulated_time_seconds=decision.decision_time_seconds,
        )
        return ActionResult(**base, status=ActionStatus.CANCELLED, reason_code=reason_code)

    def advance(
        self,
        run_id: str,
        target_seconds: int,
        advance_id: str,
        *,
        interventions: Sequence[ScheduledIntervention] = (),
        observations: Sequence[Observation | ObservationTemplate] = (),
        cancel_open_decisions: bool = False,
    ) -> AdvanceResult:
        """Explicit operator/harness time advance; refused while a decision is open."""

        self._require_live_run(run_id)
        pending = self.open_decisions(run_id)
        if pending and not cancel_open_decisions:
            raise CoordinatorError(
                CoordinatorErrorCode.DECISION_PENDING,
                "simulation is paused at an open decision; close it or pass "
                "cancel_open_decisions=True to supersede it",
                details={"open_workflow_ids": [item.workflow_id for item in pending]},
            )
        for item in pending:
            self.cancel(
                run_id,
                item.workflow_id,
                outcome="superseded",
                reason_code="operator_advanced_time",
                details={"advance_id": advance_id, "target_seconds": target_seconds},
            )
        return self.services.advance_simulation(
            run_id,
            target_seconds,
            advance_id,
            interventions=interventions,
            observations=observations,
        )

    def confirm_restoration(
        self,
        run_id: str,
        machine_id: str,
        *,
        confirmation_id: str,
        actor: str,
        at_seconds: int | None = None,
        cancel_open_decisions: bool = False,
    ) -> AdvanceResult:
        """Only an operator/harness confirmation restores capacity, never model prose."""

        self._require_actor(actor, RESTORATION_ACTOR_PREFIXES)
        checkpoint = self.services.get_checkpoint(run_id)
        snapshot = checkpoint.snapshot
        machine = next((item for item in snapshot.machines if item.machine_id == machine_id), None)
        if machine is None or machine.status not in (MachineStatus.DOWN, MachineStatus.MAINTENANCE):
            raise CoordinatorError(
                CoordinatorErrorCode.INVALID_RESTORATION,
                "only a currently down or maintenance machine can be restored",
                details={
                    "machine_id": machine_id,
                    "status": None if machine is None else machine.status.value,
                },
            )
        # The first second after the clock and after any pre-staged plan boundary;
        # restoration (priority 40) cannot be appended behind a staged priority-60 fact.
        staged = checkpoint.last_event_simulated_time_seconds or 0
        at = max(snapshot.simulated_time_seconds, staged) + 1 if at_seconds is None else at_seconds
        intervention = ScheduledIntervention(
            intervention_id=f"restore-{confirmation_id}",
            at_seconds=at,
            kind=InterventionKind.MACHINE_RESTORED,
            target_machine_id=machine_id,
            injected_status=MachineStatus.AVAILABLE,
        )
        result = self.advance(
            run_id,
            at,
            f"advance-restore-{confirmation_id}",
            interventions=(intervention,),
            cancel_open_decisions=cancel_open_decisions,
        )
        self._audit(
            run_id,
            None,
            f"restoration_confirmed-{confirmation_id}",
            actor=actor,
            details={
                "machine_id": machine_id,
                "confirmation_id": confirmation_id,
                "intervention_id": intervention.intervention_id,
                "at_seconds": at,
                "resulting_state_version": result.snapshot.state_version,
            },
            simulated_time_seconds=at,
            category="restoration",
        )
        return result

    def reset_run(self, source_run_id: str, new_run_id: str) -> dict[str, Any]:
        """Close every open decision of the source run, then create a new run.

        The new run starts from the source run's version-zero snapshot with a new
        identity. The old run becomes read-only for the coordinator.
        """

        cancelled = [
            self.cancel(
                source_run_id,
                item.workflow_id,
                outcome="run_reset",
                reason_code="run_reset",
                details={"new_run_id": new_run_id},
            ).public_dict()
            for item in self.open_decisions(source_run_id)
        ]
        initial = self.services.get_checkpoint(source_run_id, state_version=0).snapshot
        snapshot = type(initial).model_validate(
            {**initial.model_dump(mode="python"), "run_id": new_run_id}
        )
        record = self.services.reset_run(source_run_id, snapshot, created_at_utc=self._wall_clock())
        self._audit(
            source_run_id,
            None,
            "run_superseded_by_reset",
            actor="coordinator",
            details={"new_run_id": record.run_id},
            simulated_time_seconds=None,
            category="run",
        )
        return {"source_run_id": source_run_id, "new_run_id": record.run_id, "cancelled": cancelled}

    # ----------------------------------------------------------------- internals

    def _decide(
        self,
        run_id: str,
        workflow_id: str,
        *,
        proposal_id: str,
        proposal_hash: str,
        actor: str,
        decision: ApprovalDecision,
        reason: str | None,
        policy_name: str | None,
    ) -> ActionResult:
        current = self.decision(run_id, workflow_id)
        action = "approve" if decision is ApprovalDecision.APPROVED else "reject"
        base = {
            "action": action,
            "run_id": run_id,
            "workflow_id": workflow_id,
            "proposal_id": proposal_id,
            "proposal_hash": proposal_hash,
            "actor": actor,
        }
        if current.status is DecisionStatus.APPLIED:
            return ActionResult(
                **base, status=ActionStatus.ALREADY_APPLIED, reason_code="already_applied"
            )
        if current.status is not DecisionStatus.AWAITING_APPROVAL:
            return ActionResult(
                **base, status=ActionStatus.CONFLICT, reason_code=f"decision_{current.status.value}"
            )
        self._require_live_run(run_id)
        if (current.pending_proposal_id, current.pending_proposal_hash) != (
            proposal_id,
            proposal_hash,
        ):
            return ActionResult(
                **base, status=ActionStatus.CONFLICT, reason_code="proposal_not_pending"
            )
        if decision is ApprovalDecision.APPROVED:
            report = self.services.inspect_conflict(run_id, proposal_id, proposal_hash)
            if not report.can_apply:
                reasons = [item.value for item in report.reasons]
                if ServiceErrorCode.STALE_STATE in report.reasons:
                    self._close_stale(
                        run_id,
                        workflow_id,
                        "stale_at_approval",
                        {
                            "base_state_version": report.base_state_version,
                            "current_state_version": report.current_state_version,
                        },
                    )
                    return ActionResult(
                        **base,
                        status=ActionStatus.STALE,
                        reason_code="stale_state",
                        details={
                            "reasons": reasons,
                            "base_state_version": report.base_state_version,
                            "current_state_version": report.current_state_version,
                        },
                    )
                return ActionResult(
                    **base,
                    status=ActionStatus.CONFLICT,
                    reason_code="cannot_apply",
                    details={"reasons": reasons},
                )
        approval_id = f"approval-{proposal_id}"
        try:
            if policy_name is None:
                record = self.services.record_human_decision(
                    run_id,
                    proposal_id,
                    proposal_hash,
                    approval_id=approval_id,
                    decision=decision,
                    actor=actor,
                    reason=reason,
                    decided_at_utc=self._wall_clock(),
                )
            else:
                record = self.services.record_deterministic_policy_decision(
                    run_id,
                    proposal_id,
                    proposal_hash,
                    approval_id=approval_id,
                    policy_name=policy_name,
                    decision=decision,
                    reason=reason,
                    decided_at_utc=self._wall_clock(),
                )
        except ApplicationServiceError as exc:
            existing = self._approval(run_id, approval_id)
            if (
                existing is not None
                and existing.proposal_hash == proposal_hash
                and existing.decision is decision
            ):
                return ActionResult(
                    **{**base, "actor": existing.actor},
                    status=(
                        ActionStatus.ALREADY_APPROVED
                        if decision is ApprovalDecision.APPROVED
                        else ActionStatus.REJECTED
                    ),
                    approval_id=existing.approval_id,
                    reason_code="decision_already_recorded",
                    details={"requested_actor": actor},
                )
            return ActionResult(**base, status=ActionStatus.CONFLICT, reason_code=exc.code.value)
        if decision is ApprovalDecision.REJECTED:
            self.services.cancel_workflow(
                run_id,
                workflow_id,
                reason_code="proposal_rejected",
                outcome="rejected",
                details={"approval_id": record.approval_id, "actor": record.actor},
                updated_at_utc=self._wall_clock(),
            )
            return ActionResult(
                **base, status=ActionStatus.REJECTED, approval_id=record.approval_id
            )
        return ActionResult(
            **{**base, "actor": record.actor},
            status=ActionStatus.APPROVED,
            approval_id=record.approval_id,
            details={"base_state_version": record.base_state_version},
        )

    def _settle(self, inflight: _InFlight, before: dict[str, Any]) -> SupervisorStep:
        decision = inflight.decision
        outcome = inflight.outcome
        assert outcome is not None
        run_id, workflow_id = decision.run_id, decision.workflow_id
        after = operational_identity(self.services, run_id)
        current = self.decision(run_id, workflow_id)
        discarded = False
        self._record_supervisor_audit(decision, outcome, inflight.session)
        if current.status in (DecisionStatus.CANCELLED, DecisionStatus.PAUSED):
            discarded = True
            self._audit(
                run_id,
                workflow_id,
                "supervisor_result_discarded",
                actor="coordinator",
                details=self._outcome_details(outcome, current),
                simulated_time_seconds=decision.decision_time_seconds,
            )
        elif outcome.terminal is WorkflowPhase.AWAITING_APPROVAL:
            assert outcome.proposal is not None
            self._require_proposal_on_boundary(current, outcome.proposal["base_state_version"])
        elif outcome.terminal is WorkflowPhase.STALE_STATE:
            self._close_stale(run_id, workflow_id, "stale_state", outcome.details)
        elif outcome.terminal is WorkflowPhase.CANCELLED:
            self.cancel(
                run_id, workflow_id, reason_code="supervisor_cancelled", actor="coordinator"
            )
        elif current.status is DecisionStatus.OPEN:
            self.services.mark_workflow_blocked(
                run_id,
                workflow_id,
                reason_code=outcome.reason_code,
                details={
                    "supervisor_terminal": outcome.terminal.value,
                    "message": outcome.message,
                    "provider": outcome.provider,
                    "evidence_label": outcome.evidence_label,
                    "operational_state_preserved": True,
                },
                updated_at_utc=self._wall_clock(),
            )
        return SupervisorStep(
            decision=self.decision(run_id, workflow_id),
            outcome=outcome,
            timed_out=False,
            discarded=discarded,
            identity_before=before,
            identity_after=after,
        )

    def _record_late_result(self, inflight: _InFlight) -> None:
        decision = inflight.decision
        current = self.decision(decision.run_id, decision.workflow_id)
        if inflight.outcome is not None:
            self._record_supervisor_audit(decision, inflight.outcome, inflight.session)
            details = self._outcome_details(inflight.outcome, current)
        else:
            details = {
                "supervisor_terminal": None,
                "error_type": type(inflight.error).__name__ if inflight.error else None,
                "decision_status": current.status.value,
            }
        details["late"] = True
        self._audit(
            decision.run_id,
            decision.workflow_id,
            "late_supervisor_result_discarded",
            actor="coordinator",
            details=details,
            simulated_time_seconds=decision.decision_time_seconds,
        )
        inflight.late_audit = details

    def _record_supervisor_audit(
        self,
        decision: DecisionRecord,
        outcome: SupervisorOutcome,
        session: SupervisorSession | None,
    ) -> None:
        hypotheses = [
            item["evidence_id"]
            for item in outcome.evidence
            if str(item.get("evidence_id", "")).startswith("hypothesis:")
        ]
        self._audit(
            decision.run_id,
            decision.workflow_id,
            "supervisor_outcome_recorded",
            actor=f"supervisor:{outcome.provider}",
            details={
                "terminal": outcome.terminal.value,
                "reason_code": outcome.reason_code,
                "provider": outcome.provider,
                "evidence_label": outcome.evidence_label,
                "proposal_id": None
                if outcome.proposal is None
                else outcome.proposal["proposal_id"],
                "proposal_hash": None
                if outcome.proposal is None
                else outcome.proposal["proposal_hash"],
                "claims": [dict(item) for item in outcome.claims],
                "repair_recommendation": {
                    "hypothesis_evidence_ids": hypotheses,
                    "confirmed_restoration": False,
                    "authority": "recommendation only; restoration needs an operator event",
                },
                "budget": _json_details(outcome.budget),
            },
            simulated_time_seconds=decision.decision_time_seconds,
        )

    @staticmethod
    def _outcome_details(outcome: SupervisorOutcome, current: DecisionRecord) -> dict[str, Any]:
        return {
            "supervisor_terminal": outcome.terminal.value,
            "supervisor_reason_code": outcome.reason_code,
            "decision_status": current.status.value,
            "decision_outcome": current.outcome,
            "proposal_id": None if outcome.proposal is None else outcome.proposal["proposal_id"],
        }

    def _require_proposal_on_boundary(
        self, decision: DecisionRecord, base_state_version: int
    ) -> None:
        if base_state_version != decision.base_state_version:
            self._close_stale(
                decision.run_id,
                decision.workflow_id,
                "proposal_not_on_decision_boundary",
                {
                    "decision_state_version": decision.base_state_version,
                    "proposal_base_state_version": base_state_version,
                },
            )

    def _close_stale(self, run_id: str, workflow_id: str, reason_code: str, details: Any) -> None:
        current = self.decision(run_id, workflow_id)
        if current.status.is_open:
            self.cancel(
                run_id,
                workflow_id,
                outcome="stale",
                reason_code=reason_code,
                actor="coordinator",
                details=_json_details(details or {}),
            )

    def _consumption(self, run_id: str, proposal_id: str) -> Any:
        lifecycle = self.services.export_lifecycle(run_id)
        return next(
            (item for item in lifecycle.approval_consumptions if item.proposal_id == proposal_id),
            None,
        )

    def _approval(self, run_id: str, approval_id: str) -> ApprovalRecord | None:
        lifecycle = self.services.export_lifecycle(run_id)
        return next((item for item in lifecycle.approvals if item.approval_id == approval_id), None)

    def _require_live_run(self, run_id: str) -> None:
        successors = self.superseded_by(run_id)
        if successors:
            raise CoordinatorError(
                CoordinatorErrorCode.RUN_SUPERSEDED,
                "the run was reset; its decisions and time are read-only",
                details={"run_id": run_id, "superseded_by": list(successors)},
            )

    @staticmethod
    def _require_actor(actor: str, prefixes: tuple[str, ...]) -> None:
        prefix = next(
            (item for item in prefixes if isinstance(actor, str) and actor.startswith(item)), None
        )
        if prefix is None or not actor[len(prefix) :].strip():
            raise CoordinatorError(
                CoordinatorErrorCode.INVALID_ACTOR,
                "this action needs an explicit operator identity",
                details={"allowed_prefixes": list(prefixes)},
            )

    def _audit(
        self,
        run_id: str,
        workflow_id: str | None,
        action: str,
        *,
        actor: str,
        details: dict[str, Any],
        simulated_time_seconds: int | None,
        category: str = "workflow",
    ) -> None:
        audit_id = (
            "audit-s11-"
            + canonical_hash({"run_id": run_id, "workflow_id": workflow_id, "action": action})[:40]
        )
        record = AuditRecord(
            audit_id=audit_id,
            run_id=run_id,
            category=category,
            action=action[:120],
            actor=actor[:100],
            details=_json_details(details),
            simulated_time_seconds=simulated_time_seconds,
            recorded_at_utc=self._wall_clock(),
        )
        try:
            self.services.append_audit(record, workflow_id=workflow_id)
        except ApplicationServiceError as exc:
            if exc.code is not ServiceErrorCode.STORAGE_CONFLICT:
                raise
            # Same audit action already recorded for this decision (retry/restart).

    @staticmethod
    def _decision_from(
        workflow: WorkflowRecord,
        approvals: dict[str, list[ApprovalRecord]],
        consumed: dict[str, Any],
    ) -> DecisionRecord:
        payload = workflow.payload
        own = approvals.get(workflow.workflow_id, [])
        consumed_id = next((item.approval_id for item in own if item.approval_id in consumed), None)
        terminal_details = payload.get("details")
        return DecisionRecord(
            run_id=workflow.run_id,
            workflow_id=workflow.workflow_id,
            kind=DecisionKind(payload["decision_kind"]),
            source=DecisionSource(payload["source"]),
            provider=payload.get("provider"),
            status=_STATUS_BY_WORKFLOW[workflow.status],
            workflow_status=workflow.status.value,
            decision_time_seconds=payload["decision_time_seconds"],
            decision_boundary_seconds=payload["decision_boundary_seconds"],
            base_state_version=payload["base_state_version"],
            base_plan_revision=payload["base_plan_revision"],
            base_snapshot_hash=payload["base_snapshot_hash"],
            focus_machine_ids=tuple(payload.get("focus_machine_ids") or ()),
            fallback_for_workflow_id=payload.get("fallback_for_workflow_id"),
            pending_proposal_id=payload.get("pending_proposal_id"),
            pending_proposal_hash=payload.get("pending_proposal_hash"),
            applied_proposal_id=payload.get("applied_proposal_id"),
            approval_ids=tuple(sorted(item.approval_id for item in own)),
            consumed_approval_id=consumed_id,
            outcome=payload.get("outcome"),
            reason_code=payload.get("reason_code"),
            details=terminal_details if isinstance(terminal_details, dict) else {},
        )


def _boundary_blocker(checkpoint: Any, *, with_order_delta: bool) -> int | None:
    """Second of a running completion that the next application boundary would skip."""

    snapshot = checkpoint.snapshot
    boundary = decision_boundary_seconds(checkpoint, with_order_delta=with_order_delta)
    if boundary <= snapshot.simulated_time_seconds:
        return None
    due = [
        attempt.actual_start_seconds + attempt.planned_duration_seconds
        for attempt in snapshot.attempts
        if attempt.status is AttemptStatus.RUNNING
        and attempt.actual_start_seconds is not None
        and attempt.actual_start_seconds + attempt.planned_duration_seconds <= boundary
    ]
    return max(due) if due else None


def _wait_for(
    done: threading.Event, timeout_seconds: float | None, deadline: threading.Event | None
) -> bool:
    """Wait for completion until the wall timeout or an explicit deadline signal.

    ``deadline`` lets an operator control, a timer, or a deterministic test barrier
    end the wait at a chosen point instead of after an arbitrary duration.
    """

    if deadline is None:
        return done.wait(timeout_seconds)
    started = time.monotonic()
    while not done.wait(0.01):
        if deadline.is_set():
            return done.is_set()
        if timeout_seconds is not None and time.monotonic() - started >= timeout_seconds:
            return done.is_set()
    return True


def _json_details(value: Any) -> Any:
    """Strict JSON copy (tuples to lists, non-JSON values to strings)."""

    if isinstance(value, dict):
        return {str(key): _json_details(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_details(item) for item in value]
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return str(value)


__all__ = [
    "HUMAN_ACTOR_PREFIXES",
    "RESTORATION_ACTOR_PREFIXES",
    "RecoveryCoordinator",
    "SessionFactory",
    "SupervisorSession",
    "SupervisorStep",
    "operational_identity",
]
