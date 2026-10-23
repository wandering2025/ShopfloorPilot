"""Agent workbench: supervisor requests, evidence, approval, and API state for the dashboard.

The workbench is Streamlit-independent backend glue over the S07 controller and the
S11 :class:`RecoveryCoordinator`. It holds no authoritative state:

* every decision (initial plan, urgent order, recovery, fallback) is an S11 decision;
  approval and application go through ``coordinator.approve``/``reject``/``apply``;
* a supervisor runs in a background worker thread registered under its decision
  ``workflow_id``, which is also the S09 ledger key. A second request while a decision
  is open returns that decision, so reruns, refreshes and repeated clicks cannot start
  a second paid workflow;
* read projections (trace, evidence, usage, KPIs) come from SQLite, the trace store,
  the provider ledger and the S08 corpus, and never include credentials or hidden
  reasoning.
"""

from __future__ import annotations

import glob
import json
import os
import sqlite3
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from hashlib import sha256
from pathlib import Path
from typing import Any

from shopfloor_pilot import baseline
from shopfloor_pilot.agent.fake import ScriptedCall, ScriptedTurn
from shopfloor_pilot.agent.harness import (
    ABNORMALITY_TASKS,
    RequestFixture,
    load_request_fixtures,
    scripted_abnormality_turns,
    scripted_order_turns,
)
from shopfloor_pilot.agent.trace import TraceRecord, TraceStore, project_trace, sanitize
from shopfloor_pilot.agent.workflow import SupervisorTask
from shopfloor_pilot.budgets import BudgetLedger, LedgerError, ProviderLimits
from shopfloor_pilot.domain import CandidateProposal, MachineStatus, OperationalSnapshot
from shopfloor_pilot.domain.canonical import canonical_hash
from shopfloor_pilot.domain.models import EventType, OrderDelta
from shopfloor_pilot.metrics import forecast_order_outcomes, forecast_recoveries
from shopfloor_pilot.providers.config import load_paid_run_settings, merged_environment
from shopfloor_pilot.providers.errors import ConfigurationError, ProviderError, ProviderErrorCode
from shopfloor_pilot.reporting import ProposalComparisonReport
from shopfloor_pilot.retrieval import load_corpus
from shopfloor_pilot.scheduling import SchedulingResult, SolverConfig
from shopfloor_pilot.services import ApplicationServiceError, ApplicationServices
from shopfloor_pilot.storage import AuditRecord
from shopfloor_pilot.tools import WorkflowKind
from shopfloor_pilot.workflows import (
    ActionResult,
    ActionStatus,
    CoordinatorError,
    CoordinatorErrorCode,
    DecisionKind,
    DecisionRecord,
    DecisionSource,
    DecisionStatus,
    RecoveryCoordinator,
    SupervisorStep,
    approval_request,
)
from shopfloor_pilot.workflows.scenario_runner import NO_FEASIBLE_TASK
from shopfloor_pilot.workflows.sessions import supervisor_session_factory

from .controller import (
    REPOSITORY_ROOT,
    DashboardContext,
    DashboardController,
    DashboardInputError,
    DashboardView,
)

DEFAULT_ACTOR = "operator:dashboard"
READINESS_REPORT_VARIABLE = "SHOPFLOOR_PILOT_READINESS_REPORT"
MAX_REQUEST_CHARACTERS = 500
# The accepted S06/S11 initial-planning policies of each scenario.
INITIAL_POLICIES = {
    "urgent_order": "positive_time_origin_floor",
    "machine_breakdown": "scarce_capability_first",
    "no_feasible_recovery": "scarce_capability_first",
}


class PlannerSource(StrEnum):
    """Where a candidate comes from; shown on every decision and never switched silently."""

    BASELINE = "deterministic_baseline"
    SCRIPTED_FAKE = "scripted-fake"
    DEEPSEEK = "deepseek"


SOURCE_LABELS = {
    PlannerSource.BASELINE: "Deterministic planner (no LLM)",
    PlannerSource.SCRIPTED_FAKE: "Recorded agent session (offline replay)",
    PlannerSource.DEEPSEEK: "LIVE DeepSeek (paid, bounded)",
}


class RequestType(StrEnum):
    ORDER = "order_request"
    ABNORMALITY = "abnormality"
    REPLAN = "replan"


class FakeFixture(StrEnum):
    """Labeled offline provider behaviours for failure-state review."""

    STANDARD = "standard"
    SLOW = "slow_response"
    MALFORMED = "malformed_response"
    OUTAGE = "provider_outage"
    BUDGET = "budget_exhaustion"
    REPAIR_CLAIM = "repair_claim"


FAKE_FIXTURE_LABELS = {
    FakeFixture.STANDARD: "Standard scripted run",
    FakeFixture.SLOW: "Slow first response (cancel/reset/refresh test)",
    FakeFixture.MALFORMED: "Malformed tool-call arguments ×3",
    FakeFixture.OUTAGE: "Provider outage (connection error, bounded retries)",
    FakeFixture.BUDGET: "Budget exhaustion (tool-execution cap 2)",
    FakeFixture.REPAIR_CLAIM: "Agent claims 'repair complete' (rejected claim)",
}


@dataclass(frozen=True, slots=True)
class LiveStatus:
    """Non-secret live DeepSeek readiness; the key is never part of this object."""

    configured: bool
    missing: tuple[str, ...]
    reason: str | None
    settings: Mapping[str, Any]
    readiness: Mapping[str, Any] | None

    @property
    def label(self) -> str:
        if not self.configured:
            return "Not configured — running in offline demo mode (paid-run settings not set)"
        report = self.readiness or {}
        if report.get("status") == "PASS" and report.get("live_readiness") is True:
            return "READY — settings loaded; last live readiness probe PASS"
        return "CONFIGURED — no passing live readiness report found"


@dataclass(slots=True)
class _TaskRecord:
    task_id: str
    run_id: str
    source: PlannerSource
    fake_fixture: FakeFixture | None
    started_at_utc: str
    deadline: threading.Event = field(default_factory=threading.Event)
    release: threading.Event = field(default_factory=threading.Event)
    finished: threading.Event = field(default_factory=threading.Event)
    thread: threading.Thread | None = None
    step: SupervisorStep | None = None
    error: str | None = None
    finished_at_utc: str | None = None


@dataclass(frozen=True, slots=True)
class TaskView:
    task_id: str
    run_id: str
    source: PlannerSource
    fake_fixture: FakeFixture | None
    running: bool
    started_at_utc: str
    finished_at_utc: str | None
    error: str | None
    timed_out: bool
    discarded: bool


@dataclass(frozen=True, slots=True)
class RequestResult:
    status: str  # started | already_open | proposal_ready | blocked
    decision: DecisionRecord
    message: str


@dataclass(frozen=True, slots=True)
class ApprovalOutcome:
    approval: ActionResult
    application: ActionResult | None

    @property
    def applied(self) -> bool:
        if self.approval.status is ActionStatus.ALREADY_APPLIED:
            return True
        return self.application is not None and self.application.status in (
            ActionStatus.APPLIED,
            ActionStatus.ALREADY_APPLIED,
        )


@dataclass(frozen=True, slots=True)
class EvidenceSource:
    """The authoritative content one cited evidence ID resolves to."""

    evidence_id: str
    kind: str
    authority: str
    resolved: bool
    content: Mapping[str, Any]
    matches_trace: bool | None = None


@dataclass(frozen=True, slots=True)
class ProposalReview:
    candidate: CandidateProposal
    request: Mapping[str, Any]
    comparison: ProposalComparisonReport | None
    projected: OperationalSnapshot | None
    current: OperationalSnapshot


@dataclass(frozen=True, slots=True)
class DecisionDetail:
    decision: DecisionRecord
    trace: Mapping[str, Any]
    tool_results: Mapping[str, Mapping[str, Any]]
    terminal: Mapping[str, Any] | None
    supervisor_audit: Mapping[str, Any] | None
    audits: tuple[AuditRecord, ...]
    usage: Mapping[str, Any] | None
    review: ProposalReview | None
    request: Mapping[str, Any]


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


class AgentWorkbench:
    """Process-wide agent glue for every browser session of one dashboard runtime."""

    def __init__(
        self,
        dashboard: DashboardController,
        *,
        environ: Mapping[str, str] | None = None,
        env_file: str | Path | None = None,
        readiness_report: str | Path | None = None,
        supervisor_timeout_seconds: float = 150.0,
        slow_fixture_seconds: float = 8.0,
        wall_clock: Callable[[], str] = _utc_now,
    ) -> None:
        self.dashboard = dashboard
        self.runtime_root = dashboard.runtime_root
        self.trace_path = self.runtime_root / "agent_trace.sqlite3"
        self._environ = dict(os.environ if environ is None else environ)
        self._env_file = Path(env_file) if env_file is not None else None
        self._readiness_report = (
            Path(readiness_report)
            if readiness_report is not None
            else (
                Path(self._environ[READINESS_REPORT_VARIABLE])
                if self._environ.get(READINESS_REPORT_VARIABLE)
                else None
            )
        )
        self._timeout = supervisor_timeout_seconds
        self._slow_seconds = slow_fixture_seconds
        self._wall = wall_clock
        self._lock = threading.RLock()
        self._coordinators: dict[str, RecoveryCoordinator] = {}
        self._tasks: dict[str, _TaskRecord] = {}
        self._corpus = None

    # ------------------------------------------------------------ provider state

    def ledger_path(self, provider: str) -> Path:
        return self.runtime_root / f"provider_ledger-{provider}.sqlite3"

    def live_status(self) -> LiveStatus:
        readiness = self._latest_readiness()
        try:
            settings = self._load_settings()
        except ConfigurationError as exc:
            return LiveStatus(
                configured=False,
                missing=tuple(exc.details.get("missing", ())),
                reason=exc.message,
                settings={},
                readiness=readiness,
            )
        return LiveStatus(
            configured=True,
            missing=(),
            reason=None,
            settings=_public_settings(settings),
            readiness=readiness,
        )

    def _load_settings(self) -> Any:
        env_file = self._env_file
        if env_file is None and (REPOSITORY_ROOT / ".env").is_file():
            env_file = REPOSITORY_ROOT / ".env"
        return load_paid_run_settings(merged_environment(self._environ, env_file=env_file))

    def _latest_readiness(self) -> dict[str, Any] | None:
        candidates: list[Path]
        if self._readiness_report is not None:
            candidates = [self._readiness_report]
        else:
            candidates = [
                Path(item)
                for item in glob.glob(str(REPOSITORY_ROOT / ".runtime" / "*" / "readiness.json"))
            ]
        reports: list[dict[str, Any]] = []
        for path in candidates:
            try:
                report = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(report, dict) and report.get("evidence_kind") in (
                "live_provider_readiness",
                "live_readiness_skipped",
            ):
                reports.append(
                    {
                        "status": report.get("status"),
                        "live_readiness": report.get("live_readiness") is True,
                        "checked_at_utc": report.get("checked_at_utc"),
                        "model": report.get("model"),
                        "evidence_kind": report.get("evidence_kind"),
                        "path": str(path),
                    }
                )
        if not reports:
            return None
        return max(reports, key=lambda item: str(item.get("checked_at_utc") or ""))

    # ------------------------------------------------------------ coordinators

    def coordinator(self, run_id: str | None = None) -> RecoveryCoordinator:
        """One coordinator per run and process, so in-flight cancellation finds its worker."""

        with self._lock:
            context = self.dashboard.current_context()
            if run_id is None:
                run_id = context.run_id
            existing = self._coordinators.get(run_id)
            if existing is not None:
                return existing
            if run_id != context.run_id:
                raise DashboardInputError(
                    "run_not_active", "Only the active run can be coordinated."
                )
            _, services = self.dashboard.services_for(context)
            coordinator = RecoveryCoordinator(services, wall_clock=self._wall)
            self._coordinators[run_id] = coordinator
            return coordinator

    def _services(self) -> tuple[DashboardContext, ApplicationServices]:
        context = self.dashboard.current_context()
        _, services = self.dashboard.services_for(context)
        return context, services

    # ---------------------------------------------------------------- requests

    @staticmethod
    def request_fixtures() -> tuple[RequestFixture, ...]:
        return load_request_fixtures().fixtures

    def request_decision(
        self,
        *,
        source: PlannerSource,
        request_type: RequestType,
        request_text: str | None = None,
        fixture_id: str | None = None,
        typed_order: Mapping[str, object] | None = None,
        fake_fixture: FakeFixture = FakeFixture.STANDARD,
        actor: str = DEFAULT_ACTOR,
    ) -> RequestResult:
        """Open exactly one decision and fill it, or return the decision already open."""

        with self._lock:
            view = self.dashboard.view()
            run_id = view.context.run_id
            coordinator = self.coordinator(run_id)
            pending = coordinator.open_decisions(run_id)
            if pending:
                return RequestResult(
                    "already_open",
                    pending[-1],
                    "A decision is already open on this run; no new request was started.",
                )
            settings = None
            if source is PlannerSource.DEEPSEEK:
                try:
                    settings = self._load_settings()
                except ConfigurationError as exc:
                    raise DashboardInputError(
                        "live_not_ready",
                        (
                            "Live DeepSeek is not configured, so no request was sent. Use the "
                            "recorded agent session or the deterministic planner."
                        ),
                        details={"missing": list(exc.details.get("missing", ()))},
                    ) from exc
            order_delta: OrderDelta | None = None
            task: SupervisorTask | None = None
            turns: list[ScriptedTurn] = []
            details: dict[str, Any] = {
                "requested_by": actor,
                "request_type": request_type.value,
                "planner_source": source.value,
            }
            down = tuple(
                item.machine_id
                for item in view.snapshot.machines
                if item.status in (MachineStatus.DOWN, MachineStatus.MAINTENANCE)
            )
            if request_type is RequestType.ORDER:
                if source is PlannerSource.BASELINE:
                    if typed_order is None:
                        raise DashboardInputError(
                            "typed_order_required", "The baseline needs a typed order."
                        )
                    order_delta = self.dashboard.typed_order_delta(view, typed_order)
                    details["typed_order"] = {
                        str(key): value
                        for key, value in typed_order.items()
                        if isinstance(value, (str, int))
                    }
                else:
                    text, fixture = self._order_text(source, request_text, fixture_id)
                    task = SupervisorTask(WorkflowKind.ORDER_REQUEST, text)
                    details["request_text"] = text
                    details["request_sha256"] = sha256(text.encode("utf-8")).hexdigest()
                    if fixture is not None:
                        details["request_fixture_id"] = fixture.fixture_id
                        turns = scripted_order_turns(fixture.scripted_draft)
            elif request_type is RequestType.ABNORMALITY:
                if source is PlannerSource.BASELINE:
                    raise DashboardInputError(
                        "baseline_has_no_diagnosis",
                        "The deterministic baseline cannot diagnose; use Replan instead.",
                    )
                if not down:
                    raise DashboardInputError(
                        "no_abnormality",
                        "No machine is down or in maintenance; there is nothing to diagnose.",
                    )
                template, query = _abnormality_template(down)
                text = (request_text or "").strip() or template
                if len(text) > MAX_REQUEST_CHARACTERS:
                    raise DashboardInputError("request_too_long", "Keep reports to 500 characters.")
                task = SupervisorTask(WorkflowKind.ABNORMALITY, text, down)
                details["request_text"] = text
                details["request_sha256"] = sha256(text.encode("utf-8")).hexdigest()
                turns = scripted_abnormality_turns(query)
            elif source is not PlannerSource.BASELINE:
                raise DashboardInputError(
                    "replan_is_baseline_only",
                    "A plain replan uses the deterministic baseline; choose a supervisor "
                    "request type for agent work.",
                )
            if source is PlannerSource.SCRIPTED_FAKE:
                details["fake_fixture"] = fake_fixture.value
            kind = _decision_kind(view, request_type, bool(down))
            policy = "original_objective"
            if kind is DecisionKind.INITIAL_PLANNING and order_delta is None:
                policy = INITIAL_POLICIES.get(view.context.scenario_id, policy)
            if source is PlannerSource.BASELINE:
                details["planning_policy"] = policy
            ordinal = len(view.lifecycle.workflows) + 1
            run_token = run_id.rsplit("-", maxsplit=1)[-1][:12]
            workflow_id = f"s12wf-{run_token}-{ordinal:03d}-v{view.snapshot.state_version}"
            supervisor = source is not PlannerSource.BASELINE
            with_delta = order_delta is not None or request_type is RequestType.ORDER
            try:
                decision = coordinator.open_decision(
                    run_id,
                    workflow_id,
                    kind=kind,
                    source=DecisionSource.SUPERVISOR
                    if supervisor
                    else DecisionSource.DETERMINISTIC_BASELINE,
                    provider=source.value if supervisor else None,
                    focus_machine_ids=down if request_type is RequestType.ABNORMALITY else (),
                    with_order_delta=with_delta,
                    details=details,
                )
            except CoordinatorError as exc:
                raise _input_error(exc) from exc
            if not supervisor:
                return self._baseline(coordinator, decision, view, order_delta, policy)
            assert task is not None
            record = _TaskRecord(
                task_id=workflow_id,
                run_id=run_id,
                source=source,
                fake_fixture=fake_fixture if settings is None else None,
                started_at_utc=self._wall(),
            )
            if settings is None:
                turns = self._fixture_turns(turns, fake_fixture, record)
            factory = supervisor_session_factory(
                services=coordinator.services,
                bundle=view.bundle,
                seed=view.bundle.scenario.seed,
                decision=decision,
                task=task,
                ledger_path=self.ledger_path(source.value),
                trace_path=self.trace_path,
                wall_clock=self._wall,
                settings=settings,
                turns=turns,
                offline_limits=ProviderLimits(max_tool_executions=2)
                if settings is None and fake_fixture is FakeFixture.BUDGET
                else None,
            )
            thread = threading.Thread(
                target=self._run_task,
                args=(coordinator, decision, factory, record),
                name=f"s12-{workflow_id}",
                daemon=True,
            )
            record.thread = thread
            self._tasks[workflow_id] = record
            thread.start()
            return RequestResult(
                "started",
                decision,
                f"Supervisor task {workflow_id} started; the simulation stays paused.",
            )

    def _order_text(
        self, source: PlannerSource, request_text: str | None, fixture_id: str | None
    ) -> tuple[str, RequestFixture | None]:
        if source is PlannerSource.SCRIPTED_FAKE:
            fixture = next(
                (item for item in self.request_fixtures() if item.fixture_id == fixture_id), None
            )
            if fixture is None:
                raise DashboardInputError(
                    "fixture_required",
                    "The offline scripted fake replays a fixed request fixture; choose one.",
                )
            return fixture.request_text, fixture
        text = (request_text or "").strip()
        if not text:
            raise DashboardInputError("request_required", "Type an order request first.")
        if len(text) > MAX_REQUEST_CHARACTERS:
            raise DashboardInputError("request_too_long", "Keep requests to 500 characters.")
        return text, None

    def _fixture_turns(
        self, turns: list[ScriptedTurn], fixture: FakeFixture, record: _TaskRecord
    ) -> list[ScriptedTurn]:
        if fixture is FakeFixture.MALFORMED:
            return [
                ScriptedTurn(
                    calls=(ScriptedCall("get_observable_snapshot", raw_arguments="{not json"),)
                )
            ] * 3
        if fixture is FakeFixture.OUTAGE:
            error = ProviderError(ProviderErrorCode.CONNECTION, "synthetic offline outage")
            return [ScriptedTurn(failure=error)] * 3
        if fixture is FakeFixture.SLOW and turns:
            seconds = self._slow_seconds
            release = record.release
            return [replace(turns[0], before=lambda: release.wait(seconds)), *turns[1:]]
        if fixture is FakeFixture.REPAIR_CLAIM and turns:
            final = turns[-1].calls[0]
            claim = {
                "kind": "observation",
                "text": "Maintenance is complete: the machine has been repaired and is "
                "restored to service.",
                "evidence_ids": ["$evidence_first:snapshot"],
            }
            false_claim = ScriptedTurn(
                calls=(ScriptedCall(final.tool, {**final.arguments, "claims": [claim]}),)
            )
            return [*turns[:-1], false_claim, turns[-1]]
        return turns

    def _run_task(
        self,
        coordinator: RecoveryCoordinator,
        decision: DecisionRecord,
        factory: Any,
        record: _TaskRecord,
    ) -> None:
        try:
            record.step = coordinator.run_supervisor(
                decision, factory, timeout_seconds=self._timeout, deadline=record.deadline
            )
        except BaseException as exc:  # worker boundary: never leave the run stuck open
            record.error = f"{type(exc).__name__}: {sanitize(str(exc))}"[:300]
            try:
                current = coordinator.decision(decision.run_id, decision.workflow_id)
                if current.status is DecisionStatus.OPEN:
                    coordinator.services.mark_workflow_blocked(
                        decision.run_id,
                        decision.workflow_id,
                        reason_code="supervisor_error",
                        details={
                            "error_type": type(exc).__name__,
                            "operational_state_preserved": True,
                        },
                        updated_at_utc=self._wall(),
                    )
            except Exception:  # noqa: BLE001 - the original error is what matters
                pass
        finally:
            record.finished_at_utc = self._wall()
            record.finished.set()

    def _baseline(
        self,
        coordinator: RecoveryCoordinator,
        decision: DecisionRecord,
        view: DashboardView,
        order_delta: OrderDelta | None,
        policy: str,
    ) -> RequestResult:
        try:
            settled, result = coordinator.propose_deterministic(
                decision, self._generator(coordinator, view, order_delta, policy)
            )
        except Exception as exc:
            if coordinator.decision(decision.run_id, decision.workflow_id).status.is_open:
                coordinator.services.mark_workflow_blocked(
                    decision.run_id,
                    decision.workflow_id,
                    reason_code="baseline_error",
                    details={
                        "error_type": type(exc).__name__,
                        "operational_state_preserved": True,
                    },
                    updated_at_utc=self._wall(),
                )
            raise
        if result.candidate is None:
            return RequestResult(
                "blocked",
                settled,
                f"No usable baseline schedule: {result.outcome.value}. Operational state is "
                "unchanged.",
            )
        return RequestResult(
            "proposal_ready", settled, "Baseline proposal generated; it awaits approval."
        )

    def _generator(
        self,
        coordinator: RecoveryCoordinator,
        view: DashboardView,
        order_delta: OrderDelta | None,
        policy: str = "original_objective",
    ) -> Callable[[DecisionRecord], SchedulingResult]:
        def generate(current: DecisionRecord) -> SchedulingResult:
            if policy == "scarce_capability_first":
                # Accepted S06/S11 fault-scenario bootstrap policy (CP-SAT, validated).
                output = self.runtime_root / "exports" / current.run_id
                output.mkdir(parents=True, exist_ok=True)
                return baseline._scarce_capability_first_initial(  # noqa: SLF001
                    coordinator.services,
                    view.bundle.catalog,
                    view.snapshot,
                    seed=view.bundle.scenario.seed,
                    output=output,
                    wall_clock=lambda: datetime.now(UTC),
                    workflow_id=current.workflow_id,
                )
            if policy == "positive_time_origin_floor":
                not_before: int | None = 1
            else:
                not_before = (
                    current.decision_boundary_seconds
                    if current.decision_boundary_seconds > current.decision_time_seconds
                    else None
                )
            return coordinator.services.generate_proposal(
                current.run_id,
                current.workflow_id,
                f"proposal-{current.workflow_id}",
                proposed_order_deltas=() if order_delta is None else (order_delta,),
                not_before_seconds=not_before,
                config=SolverConfig(random_seed=view.bundle.scenario.seed),
                created_at_utc=self._wall(),
            )

        return generate

    def run_fallback(self, failed_workflow_id: str, *, actor: str) -> RequestResult:
        """Explicitly chosen deterministic fallback; labeled and needs its own approval."""

        with self._lock:
            view = self.dashboard.view()
            coordinator = self.coordinator(view.context.run_id)
            ordinal = len(view.lifecycle.workflows) + 1
            run_token = view.context.run_id.rsplit("-", maxsplit=1)[-1][:12]
            workflow_id = f"s12wf-{run_token}-{ordinal:03d}-v{view.snapshot.state_version}-fallback"
            try:
                decision, result = coordinator.run_deterministic_fallback(
                    view.context.run_id,
                    failed_workflow_id,
                    workflow_id=workflow_id,
                    actor=actor,
                    seed=view.bundle.scenario.seed,
                )
            except CoordinatorError as exc:
                raise _input_error(exc) from exc
            if result.candidate is None:
                return RequestResult(
                    "blocked",
                    decision,
                    f"Deterministic fallback found no schedule: {result.outcome.value}.",
                )
            return RequestResult(
                "proposal_ready",
                decision,
                "Deterministic fallback proposal generated; it is not LLM success and "
                "awaits approval.",
            )

    def regenerate(self, workflow_id: str, *, actor: str = DEFAULT_ACTOR) -> RequestResult:
        """Repeat a closed decision's recorded request as a new decision (new approval)."""

        context, services = self._services()
        decision = self.coordinator(context.run_id).decision(context.run_id, workflow_id)
        if decision.status.is_open:
            raise DashboardInputError(
                "decision_still_open", "Only a closed decision can be regenerated."
            )
        details = _decision_payload(services, decision)
        source = PlannerSource(details.get("planner_source", PlannerSource.BASELINE.value))
        request_type = RequestType(details.get("request_type", RequestType.REPLAN.value))
        typed = details.get("typed_order")
        return self.request_decision(
            source=source,
            request_type=request_type,
            request_text=details.get("request_text"),
            fixture_id=details.get("request_fixture_id"),
            typed_order=typed if isinstance(typed, dict) else None,
            fake_fixture=FakeFixture(details.get("fake_fixture", FakeFixture.STANDARD.value)),
            actor=actor,
        )

    # ---------------------------------------------------------- human decisions

    def approve(
        self,
        workflow_id: str,
        *,
        proposal_id: str,
        proposal_hash: str,
        actor: str,
    ) -> ApprovalOutcome:
        """Approve the exact rendered proposal hash, then apply it through S06."""

        with self._lock:
            run_id = self.dashboard.current_context().run_id
            coordinator = self.coordinator(run_id)
            try:
                approval = coordinator.approve(
                    run_id,
                    workflow_id,
                    proposal_id=proposal_id,
                    proposal_hash=proposal_hash,
                    actor=actor,
                    reason="Explicit dashboard approval of the reviewed proposal hash.",
                )
            except CoordinatorError as exc:
                raise _input_error(exc) from exc
            application = None
            if approval.status in (ActionStatus.APPROVED, ActionStatus.ALREADY_APPROVED):
                assert approval.approval_id is not None
                application = coordinator.apply(
                    run_id,
                    workflow_id,
                    proposal_id=proposal_id,
                    proposal_hash=proposal_hash,
                    approval_id=approval.approval_id,
                )
            return ApprovalOutcome(approval=approval, application=application)

    def reject(
        self, workflow_id: str, *, proposal_id: str, proposal_hash: str, actor: str
    ) -> ActionResult:
        with self._lock:
            run_id = self.dashboard.current_context().run_id
            try:
                return self.coordinator(run_id).reject(
                    run_id,
                    workflow_id,
                    proposal_id=proposal_id,
                    proposal_hash=proposal_hash,
                    actor=actor,
                    reason="Explicit dashboard rejection; operational state stays unchanged.",
                )
            except CoordinatorError as exc:
                raise _input_error(exc) from exc

    def cancel(self, workflow_id: str, *, actor: str = DEFAULT_ACTOR) -> ActionResult:
        """Close an open decision; a running supervisor is cancelled cooperatively."""

        with self._lock:
            run_id = self.dashboard.current_context().run_id
            result = self.coordinator(run_id).cancel(
                run_id,
                workflow_id,
                outcome="cancelled",
                reason_code="operator_cancelled",
                actor=actor,
            )
            record = self._tasks.get(workflow_id)
            if record is not None:
                record.deadline.set()
            elif result.status is ActionStatus.CANCELLED:
                self._close_orphan_ledger(run_id, workflow_id)
            return result

    def _close_orphan_ledger(self, run_id: str, workflow_id: str) -> None:
        """A worker lost with its process left its ledger row running; close it."""

        decision = self.coordinator(run_id).decision(run_id, workflow_id)
        path = self.ledger_path(decision.provider or PlannerSource.SCRIPTED_FAKE.value)
        if decision.source is not DecisionSource.SUPERVISOR or not path.is_file():
            return
        ledger = BudgetLedger(path)
        try:
            ledger.cancel(workflow_id)
        except (LedgerError, ProviderError):
            pass
        finally:
            ledger.close()

    def reset(self, scenario_id: str, *, actor: str = DEFAULT_ACTOR) -> DashboardView:
        """Close the old run's open decisions, then select a fresh run identity."""

        with self._lock:
            old = self.dashboard.current_context()
            coordinator = self.coordinator(old.run_id)
            for item in coordinator.open_decisions(old.run_id):
                coordinator.cancel(
                    old.run_id,
                    item.workflow_id,
                    outcome="run_reset",
                    reason_code="run_reset",
                    actor=actor,
                )
            for record in self._tasks.values():
                if record.run_id == old.run_id:
                    record.deadline.set()
            return self.dashboard.start_or_reset(scenario_id)

    def confirm_maintenance(self, machine_id: str, *, actor: str) -> Any:
        """Operator maintenance confirmation; separate from any proposal approval."""

        with self._lock:
            try:
                return self.dashboard.restore_machine(machine_id, actor=actor)
            except CoordinatorError as exc:
                raise _input_error(exc) from exc

    # ------------------------------------------------------------------ tasks

    def task(self, task_id: str) -> TaskView | None:
        record = self._tasks.get(task_id)
        return None if record is None else _task_view(record)

    def active_tasks(self, run_id: str | None = None) -> tuple[TaskView, ...]:
        selected = run_id or self.dashboard.current_context().run_id
        return tuple(
            _task_view(item)
            for item in self._tasks.values()
            if item.run_id == selected and not item.finished.is_set()
        )

    def wait_for_task(self, task_id: str, timeout_seconds: float = 30.0) -> TaskView:
        record = self._tasks[task_id]
        record.finished.wait(timeout_seconds)
        return _task_view(record)

    def release_slow_response(self, task_id: str) -> None:
        record = self._tasks.get(task_id)
        if record is not None:
            record.release.set()

    def wait_for_late_result(self, run_id: str, task_id: str, timeout: float = 30.0) -> Any:
        coordinator = self._coordinators.get(run_id)
        if coordinator is None:
            return None
        return coordinator.wait_for_late_result(run_id, task_id, timeout)

    # ------------------------------------------------------------------ reads

    def decisions(self) -> tuple[DecisionRecord, ...]:
        """This run's decisions in creation order (the last one is the current decision).

        Dashboard workflow IDs embed the decision ordinal (``s12wf-<run>-<NNN>-...``);
        the coordinator itself orders by base version and workflow ID.
        """

        run_id = self.dashboard.current_context().run_id
        records = self.coordinator(run_id).decisions(run_id)
        return tuple(sorted(records, key=_ordinal))

    def run_status(self) -> dict[str, Any]:
        coordinator = self.coordinator()
        run_id = self.dashboard.current_context().run_id
        status = coordinator.run_status(run_id)
        restore = coordinator.restore(run_id)
        live = {item.task_id for item in self.active_tasks(run_id)}
        status["orphaned_open"] = [item for item in restore["orphaned_open"] if item not in live]
        status["active_tasks"] = sorted(live)
        return status

    def decision_detail(self, workflow_id: str) -> DecisionDetail:
        context, services = self._services()
        decision = self.coordinator(context.run_id).decision(context.run_id, workflow_id)
        records = self._trace_records(workflow_id)
        trace = project_trace(records)
        tool_results = _tool_results(records)
        terminal = trace.get("terminal") if records else None
        audits = _workflow_audits(services, context.run_id, workflow_id)
        supervisor_audit = next(
            (
                item.details
                for item in reversed(audits)
                if item.action == "supervisor_outcome_recorded"
            ),
            None,
        )
        usage = self.usage(decision) if decision.source is DecisionSource.SUPERVISOR else None
        review = None
        if decision.status is DecisionStatus.AWAITING_APPROVAL:
            review = self._review(services, decision)
        payload_details = _decision_payload(services, decision)
        return DecisionDetail(
            decision=decision,
            trace=trace,
            tool_results=tool_results,
            terminal=terminal,
            supervisor_audit=supervisor_audit,
            audits=audits,
            usage=usage,
            review=review,
            request=payload_details,
        )

    def _review(self, services: ApplicationServices, decision: DecisionRecord) -> ProposalReview:
        assert decision.pending_proposal_id is not None
        candidate = next(
            item
            for item in services.export_lifecycle(decision.run_id).proposals
            if item.proposal_id == decision.pending_proposal_id
        )
        request = approval_request(services, decision)
        comparison = None
        if request["currently_applicable"]:
            comparison = services.build_proposal_comparison(
                decision.run_id,
                candidate.proposal_id,
                expected_proposal_hash=candidate.proposal_hash,
            )
        try:
            projected, _ = services.preview_application(
                decision.run_id, candidate.proposal_id, candidate.proposal_hash
            )
        except ApplicationServiceError:
            projected = None
        return ProposalReview(
            candidate=candidate,
            request=request,
            comparison=comparison,
            projected=projected,
            current=services.get_snapshot(decision.run_id),
        )

    def _trace_records(self, workflow_id: str) -> tuple[TraceRecord, ...]:
        if not self.trace_path.is_file():
            return ()
        store = TraceStore(self.trace_path)
        try:
            return store.records(workflow_id)
        finally:
            store.close()

    def usage(self, decision: DecisionRecord) -> dict[str, Any] | None:
        """Ledger counters and per-attempt rows; amounts in micro-units of the currency."""

        provider = decision.provider or PlannerSource.SCRIPTED_FAKE.value
        path = self.ledger_path(provider)
        if not path.is_file():
            return None
        ledger = BudgetLedger(path)
        try:
            snapshot = ledger.snapshot(decision.workflow_id).public_dict()
        except LedgerError:
            return None
        finally:
            ledger.close()
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        try:
            attempts = [
                {
                    "request_id": row["request_id"],
                    "attempt": row["attempt_number"],
                    "state": row["state"],
                    "used_tokens": row["used_tokens"],
                    "reserved_tokens": row["reserved_tokens"],
                    "actual_cost_micros": row["actual_cost_micros"],
                    "reserved_cost_micros": row["reserved_cost_micros"],
                    "latency_seconds": round(row["latency_micros"] / 1_000_000, 3),
                    "error_code": row["error_code"],
                }
                for row in connection.execute(
                    "SELECT * FROM provider_attempts WHERE workflow_id = ? "
                    "ORDER BY started_at_utc, request_id, attempt_number",
                    (decision.workflow_id,),
                )
            ]
            tools = [
                {"tool": row["tool_name"], "state": row["state"]}
                for row in connection.execute(
                    "SELECT * FROM provider_tool_executions WHERE workflow_id = ? "
                    "ORDER BY started_at_utc, execution_id",
                    (decision.workflow_id,),
                )
            ]
        finally:
            connection.close()
        return {
            "provider": provider,
            "billing_basis": "live provider-reported usage at the configured dated tariff"
            if provider == PlannerSource.DEEPSEEK.value
            else "offline accounting of synthetic scripted-fake tokens; nothing is billed",
            "ledger": snapshot,
            "attempts": attempts,
            "tools": tools,
        }

    def telemetry(self, machine_ids: Sequence[str] = ()) -> list[dict[str, Any]]:
        """Observable telemetry recorded in the run's operational event log."""

        context = self.dashboard.current_context()
        rows: list[dict[str, Any]] = []
        for envelope in self.dashboard.repository.list_events(context.run_id):
            event = envelope.event
            if event.event_type is not EventType.OBSERVATION_RECORDED:
                continue
            payload = _observation(event.payload)
            machine_id = str(payload.get("machine_id"))
            if machine_ids and machine_id not in machine_ids:
                continue
            for signal in payload.get("signals", []) or []:
                if not isinstance(signal, dict):
                    continue
                rows.append(
                    {
                        "evidence_id": f"observation:{event.event_id}",
                        "machine_id": machine_id,
                        "observed_at_seconds": payload.get("observed_at_seconds"),
                        "signal": signal.get("name"),
                        "value": signal.get("value"),
                        "unit": signal.get("unit"),
                    }
                )
        return rows

    def resolve_evidence(self, workflow_id: str, evidence_id: str) -> EvidenceSource:
        """Open the authoritative source behind one cited evidence ID."""

        context, services = self._services()
        kind, _, rest = evidence_id.partition(":")
        records = self._trace_records(workflow_id)
        reported = _tool_results(records).get(evidence_id)
        if kind == "observation":
            event = next(
                (
                    item.event
                    for item in services.list_events(context.run_id)
                    if item.event.event_id == rest
                    and item.event.event_type is EventType.OBSERVATION_RECORDED
                ),
                None,
            )
            if event is None:
                return _unresolved(evidence_id, kind, "operational event log")
            observation = _observation(event.payload)
            content = {
                "event_id": event.event_id,
                "simulated_time_seconds": event.simulated_time_seconds,
                **observation,
            }
            matches = None
            if reported is not None:
                matches = _signals(reported.get("signals")) == _signals(observation.get("signals"))
            return EvidenceSource(
                evidence_id, kind, "operational event log (SQLite)", True, content, matches
            )
        if kind == "guidance":
            chunk = next(
                (item for item in self._load_corpus().chunks if item.chunk_id == rest), None
            )
            if chunk is None:
                return _unresolved(evidence_id, kind, "S08 knowledge corpus")
            content = chunk.model_dump(mode="json")
            matches = None
            if reported is not None and reported.get("chunk_hash"):
                matches = reported["chunk_hash"] == chunk.chunk_hash
            return EvidenceSource(
                evidence_id, kind, "S08 synthetic knowledge corpus", True, content, matches
            )
        if kind == "snapshot":
            version_text, _, hash_prefix = rest.partition(":")
            try:
                version = int(version_text.lstrip("v"))
                checkpoint = services.get_checkpoint(context.run_id, state_version=version)
            except (ValueError, ApplicationServiceError):
                return _unresolved(evidence_id, kind, "snapshot checkpoints")
            snapshot = checkpoint.snapshot
            return EvidenceSource(
                evidence_id,
                kind,
                "snapshot checkpoint (SQLite)",
                checkpoint.snapshot_hash.startswith(hash_prefix),
                {
                    "state_version": snapshot.state_version,
                    "plan_revision": snapshot.plan_revision,
                    "simulated_time_seconds": snapshot.simulated_time_seconds,
                    "snapshot_hash": checkpoint.snapshot_hash,
                    "machines": {item.machine_id: item.status.value for item in snapshot.machines},
                    "orders": {item.order_id: item.status.value for item in snapshot.orders},
                },
                checkpoint.snapshot_hash.startswith(hash_prefix),
            )
        if reported is not None:
            return EvidenceSource(
                evidence_id,
                kind,
                f"supervisor tool result ({reported.get('_tool')})",
                True,
                {key: value for key, value in reported.items() if key != "_tool"},
            )
        return _unresolved(evidence_id, kind, "trace")

    def _load_corpus(self) -> Any:
        if self._corpus is None:
            self._corpus = load_corpus()
        return self._corpus


# ---------------------------------------------------------------------- helpers


def _public_settings(settings: Any) -> dict[str, Any]:
    connection = settings.connection
    return {
        "model": connection.model,
        "base_url_origin": str(connection.base_url).split("/v1")[0],
        "thinking": bool(connection.thinking),
        "currency": settings.currency,
        "workflow_cap_micros": settings.workflow_cap_micros,
        "run_cap_micros": settings.run_cap_micros,
        "tariff_date": settings.pricing.tariff_date.isoformat(),
        "tariff_source": settings.pricing.source_url,
        "max_output_tokens": settings.max_output_tokens,
        "limits": connection.limits.public_dict(),
    }


def _abnormality_template(down: Sequence[str]) -> tuple[str, str]:
    if set(down) >= {"CNC02", "CNC03"}:
        text, _machines, query = NO_FEASIBLE_TASK
        return text, query
    text, _machines, query = ABNORMALITY_TASKS["machine_breakdown"]
    if tuple(down) != ("CNC02",):
        text = (
            f"Operator report: {', '.join(down)} stopped. Diagnose from observable evidence "
            "and propose a recovery schedule."
        )
    return text, query


def _decision_kind(view: DashboardView, request_type: RequestType, any_down: bool) -> DecisionKind:
    if view.snapshot.plan_revision == 0:
        return DecisionKind.INITIAL_PLANNING
    if request_type is RequestType.ORDER:
        return DecisionKind.URGENT_ORDER
    if request_type is RequestType.ABNORMALITY or any_down:
        return DecisionKind.FAULT_RECOVERY
    return DecisionKind.REPLAN


def _input_error(exc: CoordinatorError) -> DashboardInputError:
    message = exc.message
    if exc.code is CoordinatorErrorCode.DECISION_BOUNDARY_BLOCKED:
        target = exc.details.get("advance_to_seconds")
        message = (
            f"A running operation completes at {target} s, exactly at the next decision "
            f"boundary. Advance to {target} s first, then submit the request again."
        )
    return DashboardInputError(exc.code.value, message, details=exc.details)


def _ordinal(decision: DecisionRecord) -> tuple[int, int]:
    parts = decision.workflow_id.split("-")
    if len(parts) >= 3 and parts[0] == "s12wf" and parts[2].isdigit():
        return (int(parts[2]), decision.base_state_version)
    return (0, decision.base_state_version)


def _task_view(record: _TaskRecord) -> TaskView:
    step = record.step
    return TaskView(
        task_id=record.task_id,
        run_id=record.run_id,
        source=record.source,
        fake_fixture=record.fake_fixture,
        running=not record.finished.is_set(),
        started_at_utc=record.started_at_utc,
        finished_at_utc=record.finished_at_utc,
        error=record.error,
        timed_out=bool(step and step.timed_out),
        discarded=bool(step and step.discarded),
    )


def _tool_results(records: Sequence[TraceRecord]) -> dict[str, dict[str, Any]]:
    """Index every evidence-bearing entry of the persisted tool results by evidence ID."""

    index: dict[str, dict[str, Any]] = {}

    def visit(value: Any, tool: str) -> None:
        if isinstance(value, dict):
            evidence_id = value.get("evidence_id")
            if isinstance(evidence_id, str) and evidence_id not in index:
                index[evidence_id] = {"_tool": tool, **value}
            for item in value.values():
                visit(item, tool)
        elif isinstance(value, list):
            for item in value:
                visit(item, tool)

    for record in records:
        if record.kind == "tool_result":
            visit(record.payload.get("result"), str(record.payload.get("tool")))
    return index


_DECISION_AUDIT_ACTIONS = (
    "decision_opened",
    "supervisor_outcome_recorded",
    "supervisor_result_discarded",
    "late_supervisor_result_discarded",
    "deterministic_fallback_selected",
    "decision_cancelled",
    "decision_timed_out",
    "decision_stale",
    "decision_superseded",
    "decision_run_reset",
)


def _workflow_audits(
    services: ApplicationServices, run_id: str, workflow_id: str
) -> tuple[AuditRecord, ...]:
    """S11 decision audits; their IDs are derived from (run, workflow, action)."""

    wanted = {
        "audit-s11-"
        + canonical_hash({"run_id": run_id, "workflow_id": workflow_id, "action": action})[:40]
        for action in _DECISION_AUDIT_ACTIONS
    }
    return tuple(
        item for item in services.export_lifecycle(run_id).audits if item.audit_id in wanted
    )


def _observation(payload: Mapping[str, Any]) -> dict[str, Any]:
    nested = payload.get("observation")
    return dict(nested) if isinstance(nested, dict) else dict(payload)


def _signals(value: Any) -> list[tuple[Any, Any, Any]]:
    if not isinstance(value, list):
        return []
    return sorted(
        (item.get("name"), item.get("unit"), item.get("value"))
        for item in value
        if isinstance(item, dict)
    )


def _unresolved(evidence_id: str, kind: str, authority: str) -> EvidenceSource:
    return EvidenceSource(evidence_id, kind, authority, False, {}, None)


def _decision_payload(services: ApplicationServices, decision: DecisionRecord) -> dict[str, Any]:
    workflow = next(
        (
            item
            for item in services.export_lifecycle(decision.run_id).workflows
            if item.workflow_id == decision.workflow_id
        ),
        None,
    )
    if workflow is None:
        return {}
    details = workflow.payload.get("decision_details")
    return dict(details) if isinstance(details, dict) else {}


def predicted_recovery_seconds(snapshot: OperationalSnapshot | None) -> int | None:
    """Longest predicted interruption-to-restart duration (S06 definition), if any."""

    if snapshot is None:
        return None
    values = [
        item.actual_recovery_duration_seconds
        if item.actual_recovery_duration_seconds is not None
        else item.predicted_recovery_duration_seconds
        for item in forecast_recoveries(snapshot)
    ]
    known = [item for item in values if item is not None]
    return max(known) if known else None


def predicted_late_orders(snapshot: OperationalSnapshot | None) -> int | None:
    if snapshot is None:
        return None
    late = 0
    for order in forecast_order_outcomes(snapshot):
        completion = (
            order.actual_completion_seconds
            if order.actual_completion_seconds is not None
            else order.predicted_completion_seconds
        )
        if completion is not None and completion > order.due_time_seconds:
            late += 1
    return late


__all__ = [
    "DEFAULT_ACTOR",
    "FAKE_FIXTURE_LABELS",
    "SOURCE_LABELS",
    "AgentWorkbench",
    "ApprovalOutcome",
    "DecisionDetail",
    "EvidenceSource",
    "FakeFixture",
    "LiveStatus",
    "PlannerSource",
    "ProposalReview",
    "RequestResult",
    "RequestType",
    "TaskView",
    "predicted_late_orders",
    "predicted_recovery_seconds",
]
