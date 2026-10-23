"""Shared offline helpers for S11 integration tests and evidence recording.

Templates are built once per process through the S11 coordinator itself: the
initial plan is a deterministic-baseline decision approved by the explicit
offline batch policy and applied by S06, then the scenario harness advances to
the decision point. Each test gets a private SQLite copy.
"""

from __future__ import annotations

import sqlite3
import tempfile
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from shopfloor_pilot import baseline
from shopfloor_pilot.agent.fake import ScriptedCall, ScriptedTurn, TemplateScriptedProvider
from shopfloor_pilot.agent.harness import (
    ABNORMALITY_TASKS,
    OFFLINE_ACCOUNTING_RATES,
    load_request_fixtures,
    scripted_abnormality_turns,
    scripted_order_turns,
)
from shopfloor_pilot.agent.trace import TraceStore
from shopfloor_pilot.agent.workflow import (
    SupervisorBudget,
    SupervisorConfig,
    SupervisorTask,
    SupervisorWorkflow,
)
from shopfloor_pilot.budgets import BudgetLedger, ProviderLimits
from shopfloor_pilot.domain.fixtures import (
    InterventionKind,
    ScenarioBundle,
    load_scenario,
    materialize_initial_snapshot,
)
from shopfloor_pilot.providers.clock import FakeClock
from shopfloor_pilot.services import ApplicationServices
from shopfloor_pilot.storage import SQLiteStateRepository
from shopfloor_pilot.tools import WorkflowKind
from shopfloor_pilot.workflows import (
    DecisionKind,
    DecisionRecord,
    DecisionSource,
    RecoveryCoordinator,
    SupervisorSession,
    operational_identity,
)
from shopfloor_pilot.workflows.scenario_runner import NO_FEASIBLE_TASK

FIXED_START = datetime(2026, 10, 21, 12, 0, tzinfo=UTC)
PINNED = {"run_id": "$run_id", "state_version": "$state_version"}
SNAPSHOT_CALL = ScriptedCall("get_observable_snapshot", {"run_id": "$run_id"})
_TEMPLATE_ROOT = Path(tempfile.mkdtemp(prefix="s11-integration-templates-"))
_TEMPLATES: dict[str, Path] = {}
_TEMPLATE_LOCK = threading.Lock()

# Decision points: (scenario fixture, harness step after the initial plan).
DECISION_POINTS = {
    "initial_normal": "normal",
    "urgent": "urgent_order",
    "fault": "machine_breakdown",
    "no_feasible": "no_feasible_recovery",
}


class SteppedWallClock:
    """Deterministic wall clock; tests jump it to model arbitrary waiting."""

    def __init__(self, start: datetime = FIXED_START, step_ms: int = 5) -> None:
        self._now = start
        self._step = timedelta(milliseconds=step_ms)
        self._lock = threading.Lock()

    def __call__(self) -> str:
        with self._lock:
            self._now += self._step
            return self._now.isoformat(timespec="milliseconds")

    def jump(self, delta: timedelta) -> None:
        with self._lock:
            self._now += delta


def _fixed_datetime() -> datetime:
    return FIXED_START


def _initial_generator(
    services: ApplicationServices, bundle: ScenarioBundle, output: Path
) -> Callable[[DecisionRecord], Any]:
    def generate(decision: DecisionRecord) -> Any:
        if bundle.scenario.scenario_id == "urgent_order":
            return baseline._generate(  # noqa: SLF001 - accepted S06 bootstrap policy
                services,
                decision.run_id,
                "initial",
                seed=0,
                not_before_seconds=1,
                planning_policy="positive_time_origin_floor",
                wall_clock=_fixed_datetime,
                workflow_id=decision.workflow_id,
            )
        return baseline._scarce_capability_first_initial(  # noqa: SLF001
            services,
            bundle.catalog,
            services.get_snapshot(decision.run_id),
            seed=0,
            output=output,
            wall_clock=_fixed_datetime,
            workflow_id=decision.workflow_id,
        )

    return generate


def _build_template(point: str) -> Path:
    scenario_id = DECISION_POINTS[point]
    root = _TEMPLATE_ROOT / point
    root.mkdir(parents=True)
    bundle = load_scenario(scenario_id)
    repository = SQLiteStateRepository(root / "state.sqlite3")
    repository.initialize()
    services = ApplicationServices(repository, bundle.catalog, seed=0)
    services.create_run(materialize_initial_snapshot(bundle))
    if point == "initial_normal":
        return root / "state.sqlite3"
    run_id = bundle.scenario.run_id
    coordinator = RecoveryCoordinator(
        services, wall_clock=SteppedWallClock(), batch_policy="deterministic"
    )
    decision = coordinator.open_decision(
        run_id,
        "workflow-s11-template-initial",
        kind=DecisionKind.INITIAL_PLANNING,
        source=DecisionSource.DETERMINISTIC_BASELINE,
    )
    decision, _result = coordinator.propose_deterministic(
        decision, _initial_generator(services, bundle, root)
    )
    assert decision.pending_proposal_id and decision.pending_proposal_hash
    approval = coordinator.approve_by_policy(
        run_id,
        decision.workflow_id,
        proposal_id=decision.pending_proposal_id,
        proposal_hash=decision.pending_proposal_hash,
    )
    assert approval.approval_id is not None
    applied = coordinator.apply(
        run_id,
        decision.workflow_id,
        proposal_id=decision.pending_proposal_id,
        proposal_hash=decision.pending_proposal_hash,
        approval_id=approval.approval_id,
    )
    assert applied.status.value == "applied", applied
    if point == "urgent":
        arrival = next(
            item.at_seconds
            for item in bundle.evaluator.interventions
            if item.kind is InterventionKind.ADD_ORDER
        )
        coordinator.advance(run_id, arrival - 1, "advance-template-before-urgent")
    else:
        coordinator.advance(
            run_id,
            bundle.evaluator.interventions[0].at_seconds,
            "advance-template-disruption",
            interventions=bundle.evaluator.interventions,
            observations=bundle.evaluator.observation_templates,
        )
    return root / "state.sqlite3"


def _template(point: str) -> Path:
    with _TEMPLATE_LOCK:
        if point not in _TEMPLATES:
            _TEMPLATES[point] = _build_template(point)
        return _TEMPLATES[point]


@dataclass
class Harness:
    """A private run copy at one decision point plus an operator coordinator."""

    root: Path
    bundle: ScenarioBundle
    services: ApplicationServices
    coordinator: RecoveryCoordinator
    wall: SteppedWallClock
    run_id: str
    sessions: list[SupervisorSession] = field(default_factory=list)

    def reopen(self, *, batch_policy: str | None = None) -> Harness:
        """A brand-new services/coordinator pair on the same file (process restart)."""

        services = ApplicationServices(
            SQLiteStateRepository(self.root / "state.sqlite3"), self.bundle.catalog, seed=0
        )
        return Harness(
            root=self.root,
            bundle=self.bundle,
            services=services,
            coordinator=RecoveryCoordinator(
                services, wall_clock=self.wall, batch_policy=batch_policy
            ),
            wall=self.wall,
            run_id=self.run_id,
        )

    def identity(self, run_id: str | None = None) -> dict[str, Any]:
        return operational_identity(self.services, run_id or self.run_id)

    # ------------------------------------------------------------ decisions

    def open_fault(self, workflow_id: str = "workflow-s11-test-recovery") -> DecisionRecord:
        machines = (
            ("CNC02", "CNC03")
            if self.bundle.scenario.scenario_id == "no_feasible_recovery"
            else ("CNC02",)
        )
        return self.coordinator.open_decision(
            self.run_id,
            workflow_id,
            kind=DecisionKind.FAULT_RECOVERY,
            source=DecisionSource.SUPERVISOR,
            provider="scripted-fake",
            focus_machine_ids=machines,
        )

    def open_order(
        self, workflow_id: str = "workflow-s11-test-urgent", *, kind: DecisionKind | None = None
    ) -> DecisionRecord:
        return self.coordinator.open_decision(
            self.run_id,
            workflow_id,
            kind=kind or DecisionKind.URGENT_ORDER,
            source=DecisionSource.SUPERVISOR,
            provider="scripted-fake",
            with_order_delta=True,
        )

    def fault_task(self) -> SupervisorTask:
        if self.bundle.scenario.scenario_id == "no_feasible_recovery":
            text, machines, _query = NO_FEASIBLE_TASK
        else:
            text, machines, _query = ABNORMALITY_TASKS["machine_breakdown"]
        return SupervisorTask(WorkflowKind.ABNORMALITY, text, machines)

    def fault_turns(self) -> list[ScriptedTurn]:
        if self.bundle.scenario.scenario_id == "no_feasible_recovery":
            return scripted_abnormality_turns(NO_FEASIBLE_TASK[2])
        return scripted_abnormality_turns(ABNORMALITY_TASKS["machine_breakdown"][2])

    def order_task_and_turns(
        self, fixture_id: str = "urgent-zh-plate-90min"
    ) -> tuple[SupervisorTask, list[ScriptedTurn]]:
        fixture = {item.fixture_id: item for item in load_request_fixtures().fixtures}[fixture_id]
        return (
            SupervisorTask(WorkflowKind.ORDER_REQUEST, fixture.request_text),
            scripted_order_turns(fixture.scripted_draft),
        )

    def factory(
        self,
        decision: DecisionRecord,
        task: SupervisorTask,
        turns: Sequence[ScriptedTurn],
        *,
        limits: ProviderLimits | None = None,
        config: SupervisorConfig | None = None,
        wall: Callable[[], str] | None = None,
    ) -> Callable[[], SupervisorSession]:
        ledger_path = self.root / "provider_ledger.sqlite3"

        def build() -> SupervisorSession:
            provider = TemplateScriptedProvider(list(turns))
            ledger = BudgetLedger(ledger_path)
            trace = TraceStore(self.root / f"trace-{decision.workflow_id}.sqlite3")
            workflow = SupervisorWorkflow(
                facade=self.services.for_model(),
                catalog=self.bundle.catalog,
                scenario_clock=self.bundle.scenario.clock,
                seed=0,
                provider=provider,
                ledger=ledger,
                pricing=OFFLINE_ACCOUNTING_RATES,
                budget=SupervisorBudget(
                    currency="USD",
                    run_cap_micros=50_000,
                    workflow_cap_micros=50_000,
                    limits=limits or ProviderLimits(),
                ),
                trace=trace,
                run_id=decision.run_id,
                workflow_id=decision.workflow_id,
                config=config,
                clock=FakeClock(),
                wall_clock=wall or self.wall,
            )

            def close() -> None:
                trace.close()
                ledger.close()

            session = SupervisorSession(
                workflow=workflow, task=task, close=close, ledger_path=ledger_path
            )
            self.sessions.append(session)
            return session

        return build

    def run_fault_supervisor(
        self, decision: DecisionRecord, turns: Sequence[ScriptedTurn] | None = None, **kwargs: Any
    ) -> Any:
        return self.coordinator.run_supervisor(
            decision,
            self.factory(decision, self.fault_task(), turns or self.fault_turns(), **kwargs),
        )

    def approve_apply(self, decision: DecisionRecord, actor: str = "human:alice") -> Any:
        current = self.coordinator.decision(decision.run_id, decision.workflow_id)
        assert current.pending_proposal_id and current.pending_proposal_hash
        approval = self.coordinator.approve(
            decision.run_id,
            decision.workflow_id,
            proposal_id=current.pending_proposal_id,
            proposal_hash=current.pending_proposal_hash,
            actor=actor,
        )
        assert approval.ok, approval
        assert approval.approval_id is not None
        return self.coordinator.apply(
            decision.run_id,
            decision.workflow_id,
            proposal_id=current.pending_proposal_id,
            proposal_hash=current.pending_proposal_hash,
            approval_id=approval.approval_id,
        )


def harness(tmp: Path, point: str) -> Harness:
    tmp.mkdir(parents=True, exist_ok=True)
    target = tmp / "state.sqlite3"
    source = sqlite3.connect(_template(point))
    destination = sqlite3.connect(target)
    with destination:
        source.backup(destination)
    source.close()
    destination.close()
    bundle = load_scenario(DECISION_POINTS[point])
    services = ApplicationServices(SQLiteStateRepository(target), bundle.catalog, seed=0)
    wall = SteppedWallClock()
    return Harness(
        root=tmp,
        bundle=bundle,
        services=services,
        coordinator=RecoveryCoordinator(services, wall_clock=wall),
        wall=wall,
        run_id=bundle.scenario.run_id,
    )


class Gate:
    """Deterministic barrier inside a scripted provider turn (no timing sleeps)."""

    def __init__(self, action: Callable[[], None] | None = None) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        self.action = action

    def __call__(self) -> None:
        if self.action is not None:
            self.action()
        self.entered.set()
        if not self.release.wait(30):
            raise AssertionError("test gate was never released")


def with_gate(
    turns: Sequence[ScriptedTurn], index: int, gate: Callable[[], None]
) -> list[ScriptedTurn]:
    """Attach a barrier to the provider call that returns turn ``index``."""

    copied = list(turns)
    turn = copied[index]
    copied[index] = ScriptedTurn(
        calls=turn.calls,
        text=turn.text,
        failure=turn.failure,
        before=gate,
        reasoning=turn.reasoning,
    )
    return copied


def run_in_thread(target: Callable[[], Any]) -> tuple[threading.Thread, dict[str, Any]]:
    box: dict[str, Any] = {}

    def wrapper() -> None:
        try:
            box["result"] = target()
        except BaseException as exc:  # surfaced by the test
            box["error"] = exc

    thread = threading.Thread(target=wrapper, daemon=True)
    thread.start()
    return thread, box


def lifecycle_counts(services: ApplicationServices, run_id: str) -> dict[str, int]:
    lifecycle = services.export_lifecycle(run_id)
    events = services.list_events(run_id)
    return {
        "proposals": len(lifecycle.proposals),
        "approvals": len(lifecycle.approvals),
        "consumptions": len(lifecycle.approval_consumptions),
        "plan_applied_events": sum(
            1 for item in events if item.event.event_type.value == "plan_applied"
        ),
        "events": len(events),
    }


def record_evidence(name: str, payload: dict[str, Any]) -> None:
    """Write sanitized observed facts when ``S11_EVIDENCE_DIR`` is set (no-op otherwise)."""

    import json
    import os

    directory = os.environ.get("S11_EVIDENCE_DIR")
    if not directory:
        return
    path = Path(directory) / f"{name}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def event_type_counts(services: ApplicationServices, run_id: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in services.list_events(run_id):
        key = item.event.event_type.value
        counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items()))
