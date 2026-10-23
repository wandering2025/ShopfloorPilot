"""Deterministic dashboard actions with no Streamlit dependency.

The controller is intentionally thin: it validates typed presentation input, calls
``ApplicationServices``, and returns immutable domain/service results. It never keeps
an authoritative manufacturing-state copy in browser or Python session memory.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from typing import Any
from uuid import uuid4

from pydantic import ValidationError

from shopfloor_pilot.domain import (
    ApprovalDecision,
    ApprovalRecord,
    AttemptStatus,
    CandidateProposal,
    MachineStatus,
    OperationalSnapshot,
    OperationStatus,
    OrderDelta,
    OrderDeltaKind,
    ScenarioBundle,
    load_scenario,
    materialize_initial_snapshot,
)
from shopfloor_pilot.domain.fixtures import InterventionKind, ScheduledIntervention
from shopfloor_pilot.domain.models import EventType
from shopfloor_pilot.metrics import RunMetrics
from shopfloor_pilot.planning import expand_process_route
from shopfloor_pilot.reporting import ProposalComparisonReport, ReplayResult
from shopfloor_pilot.scheduling import SchedulingResult, SolverConfig
from shopfloor_pilot.services import (
    ApplicationServices,
    ProposalConflictReport,
)
from shopfloor_pilot.simulation import AdvanceResult
from shopfloor_pilot.storage import LifecycleExport, SQLiteStateRepository, WorkflowStatus
from shopfloor_pilot.workflows import RecoveryCoordinator

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_RUNTIME_ROOT = REPOSITORY_ROOT / ".runtime" / "s07-dashboard"
SCENARIO_IDS = (
    "normal",
    "urgent_order",
    "machine_breakdown",
    "no_feasible_recovery",
)


class DashboardInputError(ValueError):
    """Expected operator input failure that is safe to render inline."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        self.code = code
        self.details = dict(details or {})
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class DashboardContext:
    """Persisted active-run pointer; manufacturing state remains in SQLite."""

    scenario_id: str
    run_id: str
    generation: int

    @classmethod
    def from_json(cls, raw: str) -> DashboardContext:
        data = json.loads(raw)
        if not isinstance(data, dict) or data.get("schema_version") != 1:
            raise ValueError("dashboard context must use schema version 1")
        scenario_id = data.get("scenario_id")
        run_id = data.get("run_id")
        generation = data.get("generation")
        if scenario_id not in SCENARIO_IDS:
            raise ValueError("dashboard context names an unsupported scenario")
        if not isinstance(run_id, str) or not run_id:
            raise ValueError("dashboard context run_id is invalid")
        if type(generation) is not int or generation < 1:
            raise ValueError("dashboard context generation is invalid")
        return cls(scenario_id=scenario_id, run_id=run_id, generation=generation)

    def as_json(self) -> str:
        return (
            json.dumps(
                {
                    "schema_version": 1,
                    "scenario_id": self.scenario_id,
                    "run_id": self.run_id,
                    "generation": self.generation,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n"
        )


@dataclass(frozen=True, slots=True)
class PendingProposal:
    candidate: CandidateProposal
    conflict: ProposalConflictReport
    comparison: ProposalComparisonReport | None
    decision: ApprovalRecord | None


@dataclass(frozen=True, slots=True)
class DashboardView:
    context: DashboardContext
    bundle: ScenarioBundle
    snapshot: OperationalSnapshot
    metrics: RunMetrics
    lifecycle: LifecycleExport
    pending: PendingProposal | None


@dataclass(frozen=True, slots=True)
class PlanningAction:
    result: SchedulingResult
    workflow_id: str
    purpose: str

    @property
    def usable(self) -> bool:
        return self.result.candidate is not None


RunIdFactory = Callable[[str], str]


def _default_run_id(scenario_id: str) -> str:
    return f"ui-{scenario_id}-{uuid4().hex[:12]}"


class DashboardController:
    """Shared local UI controller over one durable SQLite repository."""

    def __init__(
        self,
        runtime_root: str | Path = DEFAULT_RUNTIME_ROOT,
        *,
        run_id_factory: RunIdFactory | None = None,
    ) -> None:
        self.runtime_root = Path(runtime_root).expanduser().resolve()
        self.runtime_root.mkdir(parents=True, exist_ok=True)
        self.context_path = self.runtime_root / "active_run.json"
        self.repository = SQLiteStateRepository(self.runtime_root / "state.sqlite3")
        self.repository.initialize()
        self._run_id_factory = run_id_factory or _default_run_id
        self._lock = RLock()

    def ensure_active(self, default_scenario_id: str = "normal") -> DashboardView:
        """Open the persisted run, creating the first run exactly once if absent."""

        with self._lock:
            context = self._read_context()
            if context is None:
                self.start_or_reset(default_scenario_id)
            return self.view()

    def start_or_reset(self, scenario_id: str) -> DashboardView:
        """Create a fresh run identity and atomically select it for all UI sessions."""

        with self._lock:
            bundle = self._load_bundle(scenario_id)
            previous = self._read_context()
            generation = 1 if previous is None else previous.generation + 1
            run_id = self._run_id_factory(scenario_id)
            initial = materialize_initial_snapshot(bundle, run_id=run_id)
            services = ApplicationServices(
                self.repository,
                bundle.catalog,
                seed=bundle.scenario.seed,
            )
            if previous is None:
                services.create_run(initial)
            else:
                services.reset_run(previous.run_id, initial)
            context = DashboardContext(
                scenario_id=scenario_id,
                run_id=run_id,
                generation=generation,
            )
            self._write_context(context)
            return self.view()

    def view(self) -> DashboardView:
        """Read one coherent dashboard projection without mutating the run."""

        context = self._require_context()
        bundle, services = self._services(context)
        snapshot = services.get_snapshot(context.run_id)
        lifecycle = services.export_lifecycle(context.run_id)
        pending = self._pending_proposal(services, lifecycle)
        return DashboardView(
            context=context,
            bundle=bundle,
            snapshot=snapshot,
            metrics=services.calculate_run_metrics(context.run_id),
            lifecycle=lifecycle,
            pending=pending,
        )

    def validate_pending(
        self,
        proposal_id: str,
        expected_proposal_hash: str,
    ) -> ProposalConflictReport:
        """Refresh validation only; this method cannot generate or apply a plan."""

        context = self._require_context()
        _, services = self._services(context)
        return services.inspect_conflict(
            context.run_id,
            proposal_id,
            expected_proposal_hash=expected_proposal_hash,
        )

    def propose_plan(self, *, purpose: str | None = None) -> PlanningAction:
        view = self.view()
        selected_purpose = purpose or ("initial" if view.snapshot.plan_revision == 0 else "replan")
        return self._generate(view, purpose=selected_purpose)

    def propose_order(self, request: Mapping[str, object]) -> PlanningAction:
        """Validate a typed order draft, then include it only in a proposal."""

        view = self.view()
        delta = self.typed_order_delta(view, request)
        return self._generate(view, purpose="urgent", order_delta=delta)

    def typed_order_delta(self, view: DashboardView, request: Mapping[str, object]) -> OrderDelta:
        """Route-validate a typed operator order; the result is still only a draft delta."""

        route = expand_process_route(request, view.bundle.catalog)
        if route.order is None:
            raise DashboardInputError(
                "invalid_order",
                "The order draft is invalid; no backend state changed.",
                details={
                    "issues": [
                        {"code": item.code.value, "field": item.field, "message": item.message}
                        for item in route.failures
                    ]
                },
            )
        if any(item.order_id == route.order.order_id for item in view.snapshot.orders):
            raise DashboardInputError(
                "duplicate_order_id",
                f"Order {route.order.order_id!r} already exists; choose a new stable ID.",
            )
        return OrderDelta(kind=OrderDeltaKind.ADD, order=route.order)

    def services_for(self, context: DashboardContext) -> tuple[ScenarioBundle, ApplicationServices]:
        """Fresh service facade over the shared repository for one run context."""

        return self._services(context)

    def current_context(self) -> DashboardContext:
        return self._require_context()

    def propose_fixture_urgent_order(self) -> PlanningAction:
        """Load the selected scenario's explicit operator order as a draft proposal."""

        view = self.view()
        intervention = next(
            (
                item
                for item in view.bundle.evaluator.interventions
                if item.kind is InterventionKind.ADD_ORDER and item.order_to_add is not None
            ),
            None,
        )
        if intervention is None or intervention.order_to_add is None:
            raise DashboardInputError(
                "fixture_order_unavailable",
                "The selected scenario has no fixture urgent order.",
            )
        return self.propose_order(intervention.order_to_add.model_dump(mode="python"))

    def decide(
        self,
        proposal_id: str,
        expected_proposal_hash: str,
        decision: ApprovalDecision,
        *,
        actor: str = "dashboard-operator",
        reason: str | None = None,
    ) -> ApprovalRecord:
        """Record one immutable human decision without applying it."""

        context = self._require_context()
        _, services = self._services(context)
        return services.record_human_decision(
            context.run_id,
            proposal_id,
            expected_proposal_hash,
            approval_id=self._decision_id(proposal_id),
            decision=decision,
            actor=actor,
            reason=reason or f"Explicit dashboard {decision.value} decision.",
        )

    def apply_approved(
        self,
        proposal_id: str,
        expected_proposal_hash: str,
        approval_id: str,
    ) -> Any:
        """Apply the exact rendered proposal; never refresh or replace it implicitly."""

        context = self._require_context()
        _, services = self._services(context)
        return services.apply_approved_plan(
            context.run_id,
            proposal_id,
            expected_proposal_hash,
            approval_id,
            f"uiapply-{proposal_id}",
        )

    def pause(self) -> OperationalSnapshot:
        """Return authority unchanged; wall-clock waiting never advances simulation."""

        return self.view().snapshot

    def advance_by(self, delta_seconds: int) -> AdvanceResult:
        view = self.view()
        if isinstance(delta_seconds, bool) or not isinstance(delta_seconds, int):
            raise DashboardInputError("invalid_advance", "Advance must be a strict integer.")
        if delta_seconds <= 0:
            raise DashboardInputError("invalid_advance", "Advance must be greater than zero.")
        target = view.snapshot.simulated_time_seconds + delta_seconds
        if target > view.snapshot.hard_horizon_seconds:
            raise DashboardInputError(
                "hard_horizon",
                "Advance would exceed the finite scenario horizon.",
                details={"maximum_delta_seconds": self.remaining_seconds(view.snapshot)},
            )
        return self._coordinator(view.context).advance(
            view.context.run_id,
            target,
            f"uiadvance-v{view.snapshot.state_version}-t{target}",
        )

    def inject_fault(
        self,
        machine_id: str,
        at_seconds: int,
        *,
        unavailable_until_seconds: int | None = None,
    ) -> AdvanceResult:
        view = self.view()
        snapshot = view.snapshot
        machine = next((item for item in snapshot.machines if item.machine_id == machine_id), None)
        if machine is None:
            raise DashboardInputError("unknown_machine", f"Unknown machine {machine_id!r}.")
        if machine.status is MachineStatus.DOWN:
            raise DashboardInputError("machine_already_down", f"{machine_id} is already down.")
        if type(at_seconds) is not int or not (
            snapshot.simulated_time_seconds < at_seconds <= snapshot.hard_horizon_seconds
        ):
            raise DashboardInputError(
                "invalid_fault_time",
                "Fault time must be after the current simulated time and within the horizon.",
            )
        until = unavailable_until_seconds or snapshot.hard_horizon_seconds
        if until <= at_seconds or until > snapshot.hard_horizon_seconds:
            raise DashboardInputError(
                "invalid_fault_window",
                "Unavailable-until must be after the fault and within the hard horizon.",
            )
        intervention = ScheduledIntervention(
            intervention_id=f"uifault-{machine_id}-v{snapshot.state_version}",
            at_seconds=at_seconds,
            kind=InterventionKind.MACHINE_FAULT,
            target_machine_id=machine_id,
            injected_status=MachineStatus.DOWN,
            unavailable_until_seconds=until,
        )
        return self._coordinator(view.context).advance(
            view.context.run_id,
            at_seconds,
            f"uiinject-v{snapshot.state_version}-t{at_seconds}",
            interventions=(intervention,),
        )

    def inject_fixture_disruption(self) -> AdvanceResult:
        """Apply the selected scenario's named fault set only on explicit click."""

        view = self.view()
        snapshot = view.snapshot
        faults = tuple(
            item.model_copy(update={"private_truth_ref": None})
            for item in view.bundle.evaluator.interventions
            if item.kind is InterventionKind.MACHINE_FAULT
        )
        if not faults:
            raise DashboardInputError(
                "fixture_fault_unavailable",
                "The selected scenario has no fixture fault injection.",
            )
        target = max(item.at_seconds for item in faults)
        if target <= snapshot.simulated_time_seconds:
            raise DashboardInputError(
                "fixture_fault_elapsed",
                "The fixture disruption boundary has already elapsed in this run.",
            )
        if any(item.at_seconds != target for item in faults):
            raise DashboardInputError(
                "unsupported_fixture_fault_set",
                "Dashboard fixture injection requires one shared disruption boundary.",
            )
        observations = tuple(
            item
            for item in view.bundle.evaluator.observation_templates
            if item.observed_at_seconds <= target
        )
        return self._coordinator(view.context).advance(
            view.context.run_id,
            target,
            f"uifixture-v{snapshot.state_version}-t{target}",
            interventions=faults,
            observations=observations,
        )

    def restore_machine(
        self,
        machine_id: str,
        *,
        actor: str = "operator:dashboard",
    ) -> AdvanceResult:
        """Explicit operator maintenance confirmation; never inferred from model text."""

        view = self.view()
        snapshot = view.snapshot
        machine = next((item for item in snapshot.machines if item.machine_id == machine_id), None)
        if machine is None:
            raise DashboardInputError("unknown_machine", f"Unknown machine {machine_id!r}.")
        if machine.status not in (MachineStatus.DOWN, MachineStatus.MAINTENANCE):
            raise DashboardInputError("machine_not_down", f"{machine_id} is not down.")
        if snapshot.simulated_time_seconds + 1 > snapshot.hard_horizon_seconds:
            raise DashboardInputError("hard_horizon", "No restore boundary remains in this run.")
        return self._coordinator(view.context).confirm_restoration(
            view.context.run_id,
            machine_id,
            confirmation_id=f"ui-{machine_id}-v{snapshot.state_version}",
            actor=actor,
        )

    def export_replay(self) -> Path:
        view = self.view()
        destination = self.runtime_root / "exports" / view.context.run_id / "events.jsonl"
        _, services = self._services(view.context)
        services.export_replay_bundle(view.context.run_id, destination)
        return destination

    def load_replay(self, path: str | Path) -> ReplayResult:
        """Pure replay in a separate projection; the active repository is untouched."""

        source = Path(path).expanduser().resolve()
        bundle = ApplicationServices.load_replay_bundle(source)
        return ApplicationServices.replay_bundle(bundle)

    @staticmethod
    def remaining_seconds(snapshot: OperationalSnapshot) -> int:
        return snapshot.hard_horizon_seconds - snapshot.simulated_time_seconds

    @staticmethod
    def plan_blockers(snapshot: OperationalSnapshot) -> tuple[str, ...]:
        """Why the applied plan cannot run as it stands (empty when it can).

        Scheduled work on a machine that is down or in maintenance never starts, and an
        interrupted operation has no attempt until a recovery plan schedules a restart.
        Either means a recovery decision is needed before watching the plan run.
        """

        unavailable = {
            item.machine_id
            for item in snapshot.machines
            if item.status in (MachineStatus.DOWN, MachineStatus.MAINTENANCE)
        }
        reasons: list[str] = []
        blocked = sorted(
            {
                item.machine_id
                for item in snapshot.attempts
                if item.status is AttemptStatus.SCHEDULED and item.machine_id in unavailable
            }
        )
        if blocked:
            reasons.append(f"work is still scheduled on unavailable {', '.join(blocked)}")
        scheduled = {
            item.operation_id
            for item in snapshot.attempts
            if item.status is AttemptStatus.SCHEDULED
        }
        waiting = [
            item.operation_id
            for item in snapshot.operations
            if item.status is OperationStatus.INTERRUPTED and item.operation_id not in scheduled
        ]
        if waiting:
            reasons.append(f"{len(waiting)} interrupted operation(s) have no restart scheduled")
        return tuple(reasons)

    def _require_runnable_plan(self, snapshot: OperationalSnapshot) -> None:
        reasons = self.plan_blockers(snapshot)
        if reasons:
            raise DashboardInputError(
                "plan_needs_recovery",
                "The applied plan cannot run as it stands ("
                + "; ".join(reasons)
                + "). Request and approve a recovery plan first.",
                details={"reasons": list(reasons)},
            )

    @staticmethod
    def plan_completion_seconds(snapshot: OperationalSnapshot) -> int | None:
        """Last planned completion of the applied schedule, if work is still pending.

        Read from the recorded attempts (scheduled windows and running attempts'
        start + planned duration); it is the plan's own end, not a forecast.
        """

        ends = [
            item.scheduled_window.end_seconds
            for item in snapshot.attempts
            if item.status is AttemptStatus.SCHEDULED and item.scheduled_window is not None
        ] + [
            item.actual_start_seconds + item.planned_duration_seconds
            for item in snapshot.attempts
            if item.status is AttemptStatus.RUNNING and item.actual_start_seconds is not None
        ]
        end = max(ends, default=None)
        if end is None or end <= snapshot.simulated_time_seconds:
            return None
        return min(end, snapshot.hard_horizon_seconds)

    def degraded_production_target(self) -> int | None:
        """Where "continue production without the failed machine" would stop.

        The span equals the recorded normal segment (first applied plan to the latest
        machine fault), capped at the applied plan's last planned completion. None when
        no machine is down, no fault was recorded or no planned work remains.
        """

        view = self.view()
        snapshot = view.snapshot
        if not any(
            item.status in (MachineStatus.DOWN, MachineStatus.MAINTENANCE)
            for item in snapshot.machines
        ):
            return None
        plan_end = self.plan_completion_seconds(snapshot)
        if plan_end is None:
            return None
        events = [item.event for item in self.repository.list_events(view.context.run_id)]
        plan_start = next(
            (
                item.simulated_time_seconds
                for item in events
                if item.event_type is EventType.PLAN_APPLIED
            ),
            None,
        )
        fault = max(
            (
                item.simulated_time_seconds
                for item in events
                if item.event_type is EventType.MACHINE_DOWN
            ),
            default=None,
        )
        if plan_start is None or fault is None:
            return None
        span = max(1, fault - plan_start)
        return min(snapshot.simulated_time_seconds + span, plan_end, snapshot.hard_horizon_seconds)

    def advance_degraded_production(self) -> AdvanceResult:
        """Advance on the applied plan while the failed machine stays down."""

        snapshot = self.view().snapshot
        self._require_runnable_plan(snapshot)
        target = self.degraded_production_target()
        if target is None or target <= snapshot.simulated_time_seconds:
            raise DashboardInputError(
                "no_degraded_segment",
                "No machine is down with planned work remaining, so there is nothing to continue.",
            )
        return self.advance_by(target - snapshot.simulated_time_seconds)

    def advance_to_plan_completion(self) -> AdvanceResult:
        """Advance exactly to the applied schedule's last planned completion."""

        snapshot = self.view().snapshot
        self._require_runnable_plan(snapshot)
        end = self.plan_completion_seconds(snapshot)
        if end is None:
            raise DashboardInputError(
                "no_pending_plan_work", "The applied plan has no remaining planned work."
            )
        return self.advance_by(end - snapshot.simulated_time_seconds)

    def _generate(
        self,
        view: DashboardView,
        *,
        purpose: str,
        order_delta: OrderDelta | None = None,
    ) -> PlanningAction:
        ordinal = len(view.lifecycle.workflows) + 1
        run_token = view.context.run_id.rsplit("-", maxsplit=1)[-1][:12]
        workflow_id = f"uiwf-{run_token}-{ordinal:03d}-v{view.snapshot.state_version}"
        proposal_id = f"uiprop-{run_token}-{ordinal:03d}-v{view.snapshot.state_version}"
        _, services = self._services(view.context)
        services.ensure_workflow(
            view.context.run_id,
            workflow_id,
            payload={"mode": "dashboard", "purpose": purpose},
        )
        not_before = self._next_application_boundary(services, view.snapshot)
        result = services.generate_proposal(
            view.context.run_id,
            workflow_id,
            proposal_id,
            proposed_order_deltas=() if order_delta is None else (order_delta,),
            not_before_seconds=not_before,
            config=SolverConfig(random_seed=view.bundle.scenario.seed),
        )
        if result.candidate is None:
            services.mark_workflow_blocked(
                view.context.run_id,
                workflow_id,
                reason_code=f"scheduling_{result.outcome.value}",
                details={
                    "purpose": purpose,
                    "solver_outcome": result.outcome.value,
                    "diagnostics": list(result.report.diagnostics),
                },
            )
        return PlanningAction(result=result, workflow_id=workflow_id, purpose=purpose)

    @staticmethod
    def _next_application_boundary(
        services: ApplicationServices,
        snapshot: OperationalSnapshot,
    ) -> int:
        checkpoint = services.get_checkpoint(snapshot.run_id)
        last_key = checkpoint.last_ordering_key
        if last_key is None:
            return snapshot.simulated_time_seconds
        return min(snapshot.simulated_time_seconds + 1, snapshot.hard_horizon_seconds)

    @staticmethod
    def _decision_id(proposal_id: str) -> str:
        return f"uidecision-{proposal_id}"

    def _pending_proposal(
        self,
        services: ApplicationServices,
        lifecycle: LifecycleExport,
    ) -> PendingProposal | None:
        if not lifecycle.workflows:
            return None
        latest = lifecycle.workflows[-1]
        if latest.status is not WorkflowStatus.WAITING_APPROVAL:
            return None
        proposal_id = latest.payload.get("pending_proposal_id")
        proposal_hash = latest.payload.get("pending_proposal_hash")
        if not isinstance(proposal_id, str) or not isinstance(proposal_hash, str):
            return None
        candidate = next(
            (item for item in lifecycle.proposals if item.proposal_id == proposal_id),
            None,
        )
        if candidate is None:
            return None
        conflict = services.inspect_conflict(
            lifecycle.run_id,
            candidate.proposal_id,
            expected_proposal_hash=candidate.proposal_hash,
        )
        comparison = None
        if conflict.can_apply:
            comparison = services.build_proposal_comparison(
                lifecycle.run_id,
                candidate.proposal_id,
                expected_proposal_hash=candidate.proposal_hash,
            )
        decision = next(
            (item for item in lifecycle.approvals if item.proposal_id == candidate.proposal_id),
            None,
        )
        return PendingProposal(
            candidate=candidate,
            conflict=conflict,
            comparison=comparison,
            decision=decision,
        )

    def _services(
        self,
        context: DashboardContext,
    ) -> tuple[ScenarioBundle, ApplicationServices]:
        bundle = self._load_bundle(context.scenario_id)
        return bundle, ApplicationServices(
            self.repository,
            bundle.catalog,
            seed=bundle.scenario.seed,
        )

    def _coordinator(self, context: DashboardContext) -> RecoveryCoordinator:
        """Time moves only through the S11 guard, which refuses while a decision is open."""

        _, services = self._services(context)
        return RecoveryCoordinator(services)

    @staticmethod
    def _load_bundle(scenario_id: str) -> ScenarioBundle:
        if scenario_id not in SCENARIO_IDS:
            raise DashboardInputError(
                "unsupported_scenario",
                f"Unsupported scenario {scenario_id!r}.",
            )
        try:
            return load_scenario(scenario_id)
        except (OSError, ValueError, ValidationError) as exc:
            raise DashboardInputError("invalid_scenario", str(exc)) from exc

    def _read_context(self) -> DashboardContext | None:
        if not self.context_path.is_file():
            return None
        try:
            return DashboardContext.from_json(self.context_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise DashboardInputError(
                "invalid_dashboard_context",
                f"Cannot load active dashboard context: {exc}",
            ) from exc

    def _require_context(self) -> DashboardContext:
        context = self._read_context()
        if context is None:
            raise DashboardInputError(
                "no_active_run",
                "No active run exists. Start a scenario first.",
            )
        return context

    def _write_context(self, context: DashboardContext) -> None:
        temporary = self.context_path.with_suffix(".json.tmp")
        temporary.write_text(context.as_json(), encoding="utf-8")
        temporary.replace(self.context_path)


__all__ = [
    "DEFAULT_RUNTIME_ROOT",
    "SCENARIO_IDS",
    "DashboardContext",
    "DashboardController",
    "DashboardInputError",
    "DashboardView",
    "PendingProposal",
    "PlanningAction",
]
