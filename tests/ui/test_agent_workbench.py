"""S12 workbench behaviour: requests, evidence, approval binding, tasks, failures.

All tests are offline. The scripted fake proves orchestration only; live DeepSeek
behaviour is covered by the recorded manual run, not by this suite.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import pytest

from shopfloor_pilot.domain.models import EventType, MachineStatus
from shopfloor_pilot.tools.supervisor_tools import forbidden_claim_phrase
from shopfloor_pilot.ui import DashboardController
from shopfloor_pilot.ui.agent_presenters import (
    comparison_gantts,
    hypothesis_rows,
    kpi_comparison_rows,
    schedule_changes,
    telemetry_figures,
)
from shopfloor_pilot.ui.controller import DashboardInputError
from shopfloor_pilot.ui.workbench import (
    AgentWorkbench,
    FakeFixture,
    PlannerSource,
    RequestType,
)
from shopfloor_pilot.workflows import (
    ActionStatus,
    CoordinatorError,
    DecisionSource,
    DecisionStatus,
    check_run_invariants,
    operational_identity,
)

ACTOR = "human:tester"
DUMMY_KEY = "dummy-s12-dashboard-test-key"


@pytest.fixture
def workbench(controller: DashboardController, tmp_path: Path) -> AgentWorkbench:
    return AgentWorkbench(
        controller,
        environ={},
        readiness_report=tmp_path / "no-readiness.json",
        slow_fixture_seconds=30.0,
    )


def _identity(workbench: AgentWorkbench) -> dict:
    services = workbench.coordinator().services
    return operational_identity(services, workbench.dashboard.current_context().run_id)


def _settle(workbench: AgentWorkbench, result):
    if result.status == "started":
        task = workbench.wait_for_task(result.decision.workflow_id, 60)
        assert not task.running and task.error is None
    return workbench.coordinator().decision(result.decision.run_id, result.decision.workflow_id)


def _approve(workbench: AgentWorkbench, decision):
    return workbench.approve(
        decision.workflow_id,
        proposal_id=decision.pending_proposal_id,
        proposal_hash=decision.pending_proposal_hash,
        actor=ACTOR,
    )


def _baseline_initial(workbench: AgentWorkbench):
    result = workbench.request_decision(
        source=PlannerSource.BASELINE, request_type=RequestType.REPLAN, actor=ACTOR
    )
    decision = _settle(workbench, result)
    assert decision.status is DecisionStatus.AWAITING_APPROVAL
    assert _approve(workbench, decision).applied
    return decision


def _breakdown_awaiting(workbench: AgentWorkbench, fixture=FakeFixture.STANDARD):
    workbench.dashboard.start_or_reset("machine_breakdown")
    _baseline_initial(workbench)
    workbench.dashboard.inject_fixture_disruption()
    result = workbench.request_decision(
        source=PlannerSource.SCRIPTED_FAKE,
        request_type=RequestType.ABNORMALITY,
        fake_fixture=fixture,
        actor=ACTOR,
    )
    return result, _settle(workbench, result)


def _ledger_workflows(workbench: AgentWorkbench) -> list[tuple[str, int]]:
    path = workbench.ledger_path(PlannerSource.SCRIPTED_FAKE.value)
    connection = sqlite3.connect(path)
    try:
        return connection.execute(
            "SELECT workflow_id, provider_attempts FROM provider_workflows ORDER BY workflow_id"
        ).fetchall()
    finally:
        connection.close()


def _event_count(workbench: AgentWorkbench, event_type: EventType) -> int:
    run_id = workbench.dashboard.current_context().run_id
    return sum(
        item.event.event_type is event_type
        for item in workbench.dashboard.repository.list_events(run_id)
    )


def test_a01_initial_plan_needs_explicit_approval(workbench: AgentWorkbench) -> None:
    workbench.dashboard.ensure_active()
    before = _identity(workbench)
    result = workbench.request_decision(
        source=PlannerSource.SCRIPTED_FAKE,
        request_type=RequestType.ORDER,
        fixture_id="normal-en-plate-today",
        actor=ACTOR,
    )
    assert result.status == "started"
    decision = _settle(workbench, result)
    assert decision.status is DecisionStatus.AWAITING_APPROVAL
    assert decision.provider == "scripted-fake"
    assert _identity(workbench) == before  # nothing runs before approval
    status = workbench.run_status()
    assert status["state"] == "paused_at_decision"
    with pytest.raises(CoordinatorError):
        workbench.dashboard.advance_by(60)
    outcome = _approve(workbench, decision)
    assert outcome.applied
    snapshot = workbench.dashboard.view().snapshot
    assert snapshot.plan_revision == 1
    assert any(item.order_id.startswith("order-req-") for item in snapshot.orders)


def test_a02_evidence_references_resolve_to_observable_sources(
    workbench: AgentWorkbench,
) -> None:
    _result, decision = _breakdown_awaiting(workbench)
    assert decision.status is DecisionStatus.AWAITING_APPROVAL
    detail = workbench.decision_detail(decision.workflow_id)
    claims = detail.supervisor_audit["claims"]
    cited = {item for claim in claims for item in claim["evidence_ids"]}
    assert cited
    for evidence_id in cited | set(detail.tool_results):
        source = workbench.resolve_evidence(decision.workflow_id, evidence_id)
        assert source.resolved, evidence_id
        assert source.matches_trace is not False, evidence_id
        assert "private_truth" not in json.dumps(source.content)
    # Chart values and units equal the recorded observation events exactly.
    rows = workbench.telemetry(("CNC02",))
    assert rows and {item["unit"] for item in rows} == {"mdegC", "um_per_s"}
    for row in rows:
        event = workbench.resolve_evidence(decision.workflow_id, row["evidence_id"])
        recorded = {item["name"]: item for item in event.content["signals"]}
        assert recorded[row["signal"]]["value"] == row["value"]
        assert recorded[row["signal"]]["unit"] == row["unit"]
    figures = dict(telemetry_figures(rows, 28800, 2700))
    temperature = figures["temperature_millicelsius"].data[0]
    raw = sorted(
        (item["observed_at_seconds"], item["value"])
        for item in rows
        if item["signal"] == "temperature_millicelsius"
    )
    assert list(temperature.x) == [item[0] for item in raw]
    assert list(temperature.y) == [item[1] / 1000 for item in raw]
    # Hypotheses and unknowns are labeled; nothing claims a confirmed cause or repair.
    diagnosis = next(v for k, v in detail.tool_results.items() if k.startswith("diagnosis:"))
    assert diagnosis["confirmed_root_cause"] is False
    assert diagnosis["missing_evidence"]
    assert all("not a confirmed root cause" in row["Label"] for row in hypothesis_rows(diagnosis))
    assert all(forbidden_claim_phrase(claim["text"]) is None for claim in claims)
    assert detail.review.request["repair_recommendation"]["confirmed_restoration"] is False


def test_a03_schedule_comparison_shares_axes_and_fixes_history(
    workbench: AgentWorkbench,
) -> None:
    _result, decision = _breakdown_awaiting(workbench)
    review = workbench.decision_detail(decision.workflow_id).review
    before, after = comparison_gantts(review.current, review.candidate)
    assert before.layout.xaxis.range == after.layout.xaxis.range
    end = before.layout.xaxis.range[1]
    assert review.current.simulated_time_seconds < end <= review.current.hard_horizon_seconds
    for figure in (before, after):
        for trace in figure.data:
            assert all(base + width <= end for base, width in zip(trace.base, trace.x, strict=True))
    assert before.layout.yaxis.categoryarray == after.layout.yaxis.categoryarray

    def bars(figure, name):
        return {
            (lane, base, width)
            for trace in figure.data
            if trace.name == name
            for lane, base, width in zip(trace.y, trace.base, trace.x, strict=True)
        }

    for fixed in ("Fixed: completed", "Fixed: running", "Interrupted (wasted)"):
        assert bars(before, fixed) == bars(after, fixed)
    assert bars(before, "Fixed: completed")
    changes = schedule_changes(review.current, review.candidate)
    assert "Changed" in changes.values() or "New" in changes.values()
    started = {
        item.operation_id
        for item in review.current.attempts
        if item.status.value in ("running", "completed")
    }
    assert not started & set(changes)
    rows = {row["KPI"]: row for row in kpi_comparison_rows(review)}
    assert (
        rows["WIP at comparison point"]["Before (existing plan)"]
        == (rows["WIP at comparison point"]["After (proposal)"])
    )
    assert review.comparison.before.prediction_window == review.comparison.after.prediction_window
    assert review.comparison.before.snapshot_state_version == review.current.state_version


def test_a04_approval_binds_hash_reject_is_inert_and_stale_is_refused(
    workbench: AgentWorkbench,
) -> None:
    workbench.dashboard.ensure_active()
    first = _settle(
        workbench,
        workbench.request_decision(
            source=PlannerSource.BASELINE, request_type=RequestType.REPLAN, actor=ACTOR
        ),
    )
    before = _identity(workbench)
    wrong = workbench.approve(
        first.workflow_id,
        proposal_id=first.pending_proposal_id,
        proposal_hash="0" * 64,
        actor=ACTOR,
    )
    assert wrong.approval.status is ActionStatus.CONFLICT and wrong.application is None
    assert _identity(workbench) == before
    rejected = workbench.reject(
        first.workflow_id,
        proposal_id=first.pending_proposal_id,
        proposal_hash=first.pending_proposal_hash,
        actor=ACTOR,
    )
    assert rejected.status is ActionStatus.REJECTED
    assert _identity(workbench) == before
    late = _approve(workbench, first)
    assert late.approval.status is ActionStatus.CONFLICT and not late.applied

    second = _settle(
        workbench,
        workbench.request_decision(
            source=PlannerSource.BASELINE, request_type=RequestType.REPLAN, actor=ACTOR
        ),
    )
    _approve(workbench, second)
    third = _settle(
        workbench,
        workbench.request_decision(
            source=PlannerSource.BASELINE, request_type=RequestType.REPLAN, actor=ACTOR
        ),
    )
    # An external writer that bypasses the UI guard makes the proposal stale.
    services = workbench.coordinator().services
    run_id = third.run_id
    services.advance_simulation(run_id, 60, "external-writer-advance")
    after_external = _identity(workbench)
    stale = _approve(workbench, third)
    assert stale.approval.status is ActionStatus.STALE and stale.application is None
    assert _identity(workbench) == after_external
    closed = workbench.coordinator().decision(run_id, third.workflow_id)
    assert closed.status is DecisionStatus.CANCELLED and closed.outcome == "stale"
    regenerated = _settle(workbench, workbench.regenerate(third.workflow_id, actor=ACTOR))
    assert regenerated.workflow_id != third.workflow_id
    assert regenerated.status is DecisionStatus.AWAITING_APPROVAL
    assert regenerated.base_state_version == after_external["state_version"]


def test_a05_repeated_clicks_create_one_task_and_one_application(
    workbench: AgentWorkbench,
) -> None:
    workbench.dashboard.ensure_active()
    requests = [
        workbench.request_decision(
            source=PlannerSource.SCRIPTED_FAKE,
            request_type=RequestType.ORDER,
            fixture_id="normal-en-plate-today",
            fake_fixture=FakeFixture.SLOW,
            actor=ACTOR,
        )
        for _ in range(3)
    ]
    assert [item.status for item in requests] == ["started", "already_open", "already_open"]
    assert len({item.decision.workflow_id for item in requests}) == 1
    assert len(workbench.active_tasks()) == 1
    # A "refresh" while pending is only a read.
    view_before = workbench.dashboard.view().snapshot
    assert workbench.run_status()["active_tasks"] == [requests[0].decision.workflow_id]
    assert workbench.dashboard.view().snapshot == view_before
    workbench.release_slow_response(requests[0].decision.workflow_id)
    decision = _settle(workbench, requests[0])
    ledger = _ledger_workflows(workbench)
    assert [item[0] for item in ledger] == [decision.workflow_id]
    attempts = ledger[0][1]
    outcomes = [_approve(workbench, decision) for _ in range(3)]
    assert outcomes[0].application.status is ActionStatus.APPLIED
    assert all(item.applied for item in outcomes[1:])
    assert all(item.approval.status is ActionStatus.ALREADY_APPLIED for item in outcomes[1:])
    assert _event_count(workbench, EventType.PLAN_APPLIED) == 1
    run_id = decision.run_id
    lifecycle = workbench.coordinator().services.export_lifecycle(run_id)
    assert len(lifecycle.approval_consumptions) == 1
    assert _ledger_workflows(workbench) == [(decision.workflow_id, attempts)]
    assert all(
        item.passed for item in check_run_invariants(workbench.coordinator().services, run_id)
    )


def test_a06_cancel_and_reset_keep_late_results_non_actionable(
    workbench: AgentWorkbench,
) -> None:
    workbench.dashboard.ensure_active()
    started = workbench.request_decision(
        source=PlannerSource.SCRIPTED_FAKE,
        request_type=RequestType.ORDER,
        fixture_id="normal-en-plate-today",
        fake_fixture=FakeFixture.SLOW,
        actor=ACTOR,
    )
    old_run = started.decision.run_id
    task_id = started.decision.workflow_id
    cancelled = workbench.cancel(task_id, actor=ACTOR)
    assert cancelled.status is ActionStatus.CANCELLED
    workbench.reset("normal", actor=ACTOR)
    new_run = workbench.dashboard.current_context().run_id
    assert new_run != old_run
    workbench.release_slow_response(task_id)
    workbench.wait_for_task(task_id, 60)
    workbench.wait_for_late_result(old_run, task_id, 60)
    old = workbench.coordinator(old_run).decision(old_run, task_id)
    assert old.status is DecisionStatus.CANCELLED and old.outcome == "cancelled"
    assert old.pending_proposal_id is None
    assert workbench.decisions() == ()
    assert workbench.active_tasks() == ()
    assert workbench.run_status()["state"] == "idle"
    # Reset while a task is still running closes it as run_reset.
    second = workbench.request_decision(
        source=PlannerSource.SCRIPTED_FAKE,
        request_type=RequestType.ORDER,
        fixture_id="normal-en-plate-today",
        fake_fixture=FakeFixture.SLOW,
        actor=ACTOR,
    )
    workbench.reset("normal", actor=ACTOR)
    workbench.release_slow_response(second.decision.workflow_id)
    workbench.wait_for_task(second.decision.workflow_id, 60)
    workbench.wait_for_late_result(second.decision.run_id, second.decision.workflow_id, 60)
    closed = workbench.coordinator(second.decision.run_id).decision(
        second.decision.run_id, second.decision.workflow_id
    )
    assert closed.status is DecisionStatus.CANCELLED and closed.outcome == "run_reset"
    assert workbench.decisions() == ()
    refused = workbench.coordinator(second.decision.run_id).approve(
        second.decision.run_id,
        second.decision.workflow_id,
        proposal_id="proposal-x",
        proposal_hash="0" * 64,
        actor=ACTOR,
    )
    assert refused.status is ActionStatus.CONFLICT


@pytest.mark.parametrize(
    ("fixture", "reason"),
    [
        (FakeFixture.MALFORMED, "repeated_invalid_turns"),
        (FakeFixture.OUTAGE, "provider_failure"),
        (FakeFixture.BUDGET, "budget_exhausted"),
    ],
)
def test_a07_failure_fixtures_pause_without_mutation(
    workbench: AgentWorkbench, fixture: FakeFixture, reason: str
) -> None:
    workbench.dashboard.start_or_reset("machine_breakdown")
    _baseline_initial(workbench)
    workbench.dashboard.inject_fixture_disruption()
    before = _identity(workbench)
    result = workbench.request_decision(
        source=PlannerSource.SCRIPTED_FAKE,
        request_type=RequestType.ABNORMALITY,
        fake_fixture=fixture,
        actor=ACTOR,
    )
    decision = _settle(workbench, result)
    assert decision.status is DecisionStatus.PAUSED and decision.reason_code == reason
    assert _identity(workbench) == before
    assert workbench.run_status()["state"] == "paused_blocked"
    detail = workbench.decision_detail(decision.workflow_id)
    assert detail.terminal["reason_code"] == reason
    assert detail.usage is not None
    fallback = workbench.run_fallback(decision.workflow_id, actor=ACTOR)
    fallback_decision = workbench.coordinator().decision(
        decision.run_id, fallback.decision.workflow_id
    )
    assert fallback_decision.source is DecisionSource.DETERMINISTIC_FALLBACK
    assert fallback_decision.status is DecisionStatus.AWAITING_APPROVAL
    assert _identity(workbench) == before  # a fallback still needs approval


def test_a07_infeasible_recovery_is_paused_and_never_applied(
    workbench: AgentWorkbench,
) -> None:
    workbench.dashboard.start_or_reset("no_feasible_recovery")
    _baseline_initial(workbench)
    workbench.dashboard.inject_fixture_disruption()
    before = _identity(workbench)
    decision = _settle(
        workbench,
        workbench.request_decision(
            source=PlannerSource.SCRIPTED_FAKE,
            request_type=RequestType.ABNORMALITY,
            actor=ACTOR,
        ),
    )
    assert decision.status is DecisionStatus.PAUSED
    assert decision.reason_code == "no_feasible_schedule"
    assert decision.consumed_approval_id is None
    assert _identity(workbench) == before


def test_a08_sources_live_gate_and_no_credential_rendering(
    controller: DashboardController, tmp_path: Path
) -> None:
    controller.ensure_active()
    offline = AgentWorkbench(controller, environ={}, readiness_report=tmp_path / "none.json")
    status = offline.live_status()
    assert not status.configured and status.label.startswith("Not configured")
    assert "DEEPSEEK_BASE_URL" in status.missing
    with pytest.raises(DashboardInputError) as refused:
        offline.request_decision(
            source=PlannerSource.DEEPSEEK,
            request_type=RequestType.ORDER,
            request_text="Urgent: 1 aluminum mounting plate within 2 hours.",
            actor=ACTOR,
        )
    assert refused.value.code == "live_not_ready"
    assert offline.decisions() == ()
    assert not offline.ledger_path("deepseek").exists()

    key_file = tmp_path / "key.txt"
    key_file.write_text(DUMMY_KEY)
    readiness = tmp_path / "readiness.json"
    readiness.write_text(
        json.dumps(
            {
                "status": "PASS",
                "live_readiness": True,
                "evidence_kind": "live_provider_readiness",
                "checked_at_utc": "2026-10-21T09:08:36+00:00",
                "model": "deepseek-flash",
            }
        )
    )
    environ = {
        "DEEPSEEK_API_KEY_FILE": str(key_file),
        "DEEPSEEK_BASE_URL": "https://api.deepseek.com",
        "DEEPSEEK_MODEL": "deepseek-flash",
        "SHOPFLOOR_PROVIDER_CURRENCY": "USD",
        "SHOPFLOOR_PROVIDER_WORKFLOW_BUDGET": "0.02",
        "SHOPFLOOR_PROVIDER_RUN_BUDGET": "0.05",
        "SHOPFLOOR_PROVIDER_INPUT_CACHE_HIT_RATE": "0.006",
        "SHOPFLOOR_PROVIDER_INPUT_CACHE_MISS_RATE": "0.30",
        "SHOPFLOOR_PROVIDER_OUTPUT_RATE": "1.20",
        "SHOPFLOOR_PROVIDER_TARIFF_DATE": "2026-10-21",
        "SHOPFLOOR_PROVIDER_TARIFF_SOURCE": "https://api-docs.deepseek.com/quick_start/pricing",
    }
    live = AgentWorkbench(controller, environ=environ, readiness_report=readiness)
    configured = live.live_status()
    assert configured.configured and configured.label.startswith("READY")
    assert DUMMY_KEY not in repr(configured)
    assert DUMMY_KEY not in json.dumps(dict(configured.settings))

    offline_result = _settle(
        offline,
        offline.request_decision(
            source=PlannerSource.SCRIPTED_FAKE,
            request_type=RequestType.ORDER,
            fixture_id="normal-en-plate-today",
            actor=ACTOR,
        ),
    )
    usage = offline.usage(offline_result)
    assert "nothing is billed" in usage["billing_basis"]
    assert usage["ledger"]["provider_attempts"] == len(usage["attempts"]) > 0
    trace = offline.decision_detail(offline_result.workflow_id).trace
    assert "OFFLINE" in trace["evidence_label"]
    assert "reasoning" not in json.dumps(trace).replace("reasoning_tokens", "")


def test_a09_agent_repair_text_cannot_restore_a_machine(workbench: AgentWorkbench) -> None:
    _result, decision = _breakdown_awaiting(workbench, FakeFixture.REPAIR_CLAIM)
    assert decision.status is DecisionStatus.AWAITING_APPROVAL
    trace = workbench.decision_detail(decision.workflow_id).trace
    rejected = [
        step
        for step in trace["steps"]
        if step["kind"] == "tool_result" and step["tool"] == "persist_proposal"
    ]
    assert rejected[0]["status"] != "ok"
    machines = {m.machine_id: m.status for m in workbench.dashboard.view().snapshot.machines}
    assert machines["CNC02"] is MachineStatus.DOWN
    assert _approve(workbench, decision).applied
    machines = {m.machine_id: m.status for m in workbench.dashboard.view().snapshot.machines}
    assert machines["CNC02"] is MachineStatus.DOWN  # approval never restores
    with pytest.raises(DashboardInputError):
        workbench.confirm_maintenance("CNC02", actor="model:deepseek")
    restored = workbench.confirm_maintenance("CNC02", actor="operator:alice")
    machines = {m.machine_id: m.status for m in restored.snapshot.machines}
    assert machines["CNC02"] is MachineStatus.AVAILABLE
    assert _event_count(workbench, EventType.MACHINE_RESTORED) == 1
    run_id = decision.run_id
    audits = workbench.coordinator().services.export_lifecycle(run_id).audits
    confirmation = [item for item in audits if item.action.startswith("restoration_confirmed")]
    assert len(confirmation) == 1 and confirmation[0].actor == "operator:alice"


def test_a11_offline_walkthrough_normal_urgent_breakdown_recovery(
    workbench: AgentWorkbench,
) -> None:
    # Normal → urgent (urgent_order scenario).
    workbench.dashboard.start_or_reset("urgent_order")
    _baseline_initial(workbench)
    workbench.dashboard.advance_by(3599)
    urgent = _settle(
        workbench,
        workbench.request_decision(
            source=PlannerSource.SCRIPTED_FAKE,
            request_type=RequestType.ORDER,
            fixture_id="urgent-zh-plate-90min",
            actor=ACTOR,
        ),
    )
    assert urgent.kind.value == "urgent_order"
    assert _approve(workbench, urgent).applied
    workbench.dashboard.advance_by(
        workbench.dashboard.remaining_seconds(workbench.dashboard.view().snapshot)
    )
    finished = workbench.dashboard.view().metrics
    assert finished.actual_outcome_basis.value == "actual"
    run_id = workbench.dashboard.current_context().run_id
    services = workbench.coordinator().services
    assert all(item.passed for item in check_run_invariants(services, run_id))

    # Breakdown → recovery (machine_breakdown scenario).
    _result, recovery = _breakdown_awaiting(workbench)
    assert recovery.kind.value == "fault_recovery"
    assert _approve(workbench, recovery).applied
    workbench.confirm_maintenance("CNC02", actor="operator:alice")
    workbench.dashboard.advance_by(
        workbench.dashboard.remaining_seconds(workbench.dashboard.view().snapshot)
    )
    metrics = workbench.dashboard.view().metrics
    assert metrics.actual_outcome_basis.value == "actual"
    assert metrics.recoveries and metrics.recoveries[0].actual_basis.value == "actual"
    run_id = workbench.dashboard.current_context().run_id
    services = workbench.coordinator().services
    assert all(item.passed for item in check_run_invariants(services, run_id))
    sources = [item.source.value for item in workbench.decisions()]
    assert sources == ["deterministic_baseline", "supervisor"]


def test_a05_restart_reports_orphan_and_cancel_closes_its_ledger(
    controller: DashboardController, tmp_path: Path
) -> None:
    controller.ensure_active()
    before = AgentWorkbench(controller, environ={}, slow_fixture_seconds=30.0)
    started = before.request_decision(
        source=PlannerSource.SCRIPTED_FAKE,
        request_type=RequestType.ORDER,
        fixture_id="normal-en-plate-today",
        fake_fixture=FakeFixture.SLOW,
        actor=ACTOR,
    )
    task_id = started.decision.workflow_id
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            if _ledger_workflows(before):
                break
        except sqlite3.OperationalError:
            pass  # the worker has not created the ledger schema yet
        time.sleep(0.05)
    assert [item[0] for item in _ledger_workflows(before)] == [task_id]
    # A new process: fresh workbench and coordinator over the same SQLite authority.
    restarted = AgentWorkbench(
        DashboardController(controller.runtime_root), environ={}, slow_fixture_seconds=30.0
    )
    status = restarted.run_status()
    assert status["orphaned_open"] == [task_id]
    again = restarted.request_decision(
        source=PlannerSource.SCRIPTED_FAKE,
        request_type=RequestType.ORDER,
        fixture_id="normal-en-plate-today",
        actor=ACTOR,
    )
    assert again.status == "already_open" and again.decision.workflow_id == task_id
    assert restarted.cancel(task_id, actor=ACTOR).status is ActionStatus.CANCELLED
    ledger = sqlite3.connect(restarted.ledger_path("scripted-fake"))
    try:
        row = ledger.execute(
            "SELECT status FROM provider_workflows WHERE workflow_id = ?", (task_id,)
        ).fetchone()
    finally:
        ledger.close()
    assert row == ("cancelled",)
    before.release_slow_response(task_id)
    before.wait_for_task(task_id, 60)
    late = restarted.coordinator().decision(started.decision.run_id, task_id)
    assert late.status is DecisionStatus.CANCELLED and late.pending_proposal_id is None
    assert restarted.run_status()["state"] == "idle"
