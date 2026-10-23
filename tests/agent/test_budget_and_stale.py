"""S10-A06/A07: common-ledger caps, cycle terminals, and stale-state handling."""

from __future__ import annotations

from pathlib import Path

import pytest
from agent_support import (
    FIXED_UTC,
    RunHandle,
    fixtures_by_id,
    fresh_run,
    identity,
    run_supervisor,
)

from shopfloor_pilot.agent.fake import ScriptedCall, ScriptedTurn
from shopfloor_pilot.agent.harness import ABNORMALITY_TASKS, scripted_order_turns
from shopfloor_pilot.agent.workflow import SupervisorConfig, SupervisorTask, SupervisorWorkflow
from shopfloor_pilot.budgets import ProviderLimits
from shopfloor_pilot.domain.models import ApprovalDecision
from shopfloor_pilot.providers.errors import ProviderError, ProviderErrorCode
from shopfloor_pilot.scheduling import SolverConfig
from shopfloor_pilot.services import ApplicationServiceError, ServiceErrorCode
from shopfloor_pilot.tools import WorkflowKind, WorkflowPhase

SNAPSHOT = ScriptedCall("get_observable_snapshot", {"run_id": "$run_id"})
PINNED = {"run_id": "$run_id", "state_version": "$state_version"}


def _fault_task() -> SupervisorTask:
    text, machines, _query = ABNORMALITY_TASKS["machine_breakdown"]
    return SupervisorTask(WorkflowKind.ABNORMALITY, text, machines)


def _assert_preserved(handle: RunHandle, before: dict[str, object]) -> None:
    assert identity(handle.services, handle.run_id) == before


def test_a06_repeated_observation_ends_in_cycle_terminal(tmp_path: Path) -> None:
    handle = fresh_run(tmp_path, "machine_breakdown")
    before = identity(handle.services, handle.run_id)
    run = run_supervisor(
        handle,
        _fault_task(),
        [ScriptedTurn(calls=(SNAPSHOT,))] * 5,
        config=SupervisorConfig(narrow_next_step_tools=False),
    )
    assert run.outcome.terminal is WorkflowPhase.CYCLE_DETECTED
    assert run.outcome.reason_code == "repeated_calls_without_progress"
    assert run.outcome.budget["tool_executions"] == 3
    assert run.outcome.budget["provider_attempts"] == 3
    assert run.provider.remaining_turns == 2
    _assert_preserved(handle, before)


def test_a06_two_tool_cycle_ends_visibly(tmp_path: Path) -> None:
    handle = fresh_run(tmp_path, "machine_breakdown")
    before = identity(handle.services, handle.run_id)
    pair = ScriptedTurn(
        calls=(
            ScriptedCall("summarize_observations", {**PINNED, "machine_ids": ["CNC02"]}),
            ScriptedCall("retrieve_guidance", {**PINNED, "query": "temperature vibration alarm"}),
        )
    )
    run = run_supervisor(
        handle,
        _fault_task(),
        [ScriptedTurn(calls=(SNAPSHOT,)), *[pair] * 4],
        config=SupervisorConfig(narrow_next_step_tools=False),
    )
    assert run.outcome.terminal is WorkflowPhase.CYCLE_DETECTED
    assert run.outcome.details["repeats"] == 2
    assert run.outcome.budget["tool_executions"] == 5
    assert run.outcome.budget["provider_attempts"] == 3
    notices = [item.payload for item in run.records if item.kind == "notice"]
    assert [item["tool"] for item in notices] == ["summarize_observations", "retrieve_guidance"]
    _assert_preserved(handle, before)


def test_a06_repeated_malformed_calls_consume_attempts(tmp_path: Path) -> None:
    handle = fresh_run(tmp_path, "normal")
    before = identity(handle.services, handle.run_id)
    malformed = ScriptedTurn(
        calls=(ScriptedCall("submit_order_interpretation", raw_arguments='{"run_id": '),)
    )
    run = run_supervisor(
        handle,
        SupervisorTask(WorkflowKind.ORDER_REQUEST, "2 steel brackets today"),
        [malformed] * 5,
    )
    assert run.outcome.terminal is WorkflowPhase.CYCLE_DETECTED
    assert run.outcome.reason_code == "repeated_invalid_turns"
    assert run.outcome.budget["provider_attempts"] == 3
    assert run.outcome.budget["tool_executions"] == 0
    assert run.outcome.budget["used_tokens"] > 0  # malformed turns are still charged
    _assert_preserved(handle, before)


def test_a06_tool_cap_is_enforced_by_the_common_ledger(tmp_path: Path) -> None:
    handle = fresh_run(tmp_path, "machine_breakdown")
    before = identity(handle.services, handle.run_id)
    log = [
        ScriptedTurn(calls=(ScriptedCall("summarize_event_log", {**PINNED, "limit": n}),))
        for n in (1, 2, 3, 4)
    ]
    run = run_supervisor(
        handle,
        _fault_task(),
        [ScriptedTurn(calls=(SNAPSHOT,)), *log],
        limits=ProviderLimits(max_tool_executions=3),
        config=SupervisorConfig(narrow_next_step_tools=False),
    )
    assert run.outcome.terminal is WorkflowPhase.BUDGET_EXHAUSTED
    assert run.outcome.details["limit"] == "tool_executions"
    assert run.outcome.budget["tool_executions"] == 3
    assert run.outcome.budget["status"] == "exhausted"
    _assert_preserved(handle, before)


def test_a06_attempt_retry_and_token_caps(tmp_path: Path) -> None:
    handle = fresh_run(tmp_path, "machine_breakdown")
    before = identity(handle.services, handle.run_id)
    transient = ProviderError(ProviderErrorCode.SERVER_ERROR, "scripted 503", retryable=True)
    run = run_supervisor(
        handle,
        _fault_task(),
        [
            ScriptedTurn(failure=transient),
            ScriptedTurn(failure=transient),
            ScriptedTurn(calls=(SNAPSHOT,)),
            ScriptedTurn(calls=(ScriptedCall("summarize_event_log", {**PINNED, "limit": 1}),)),
            ScriptedTurn(calls=(ScriptedCall("summarize_event_log", {**PINNED, "limit": 2}),)),
        ],
        limits=ProviderLimits(max_provider_attempts=4),
    )
    # Two retries of request 1, then request 2 succeeds; request 3 has no attempt left.
    assert run.outcome.terminal is WorkflowPhase.BUDGET_EXHAUSTED
    assert run.outcome.budget["provider_attempts"] == 4
    assert run.outcome.budget["active_seconds"] >= 3.0  # committed retry backoff
    _assert_preserved(handle, before)

    # Resumption under the same workflow_id never grants a fresh allowance.
    resumed = run_supervisor(
        handle,
        _fault_task(),
        [ScriptedTurn(calls=(SNAPSHOT,))],
        limits=ProviderLimits(max_provider_attempts=4),
        ledger_path=run.ledger_path,
        workflow_id="workflow-s10-test",
    )
    assert resumed.outcome.terminal is WorkflowPhase.BUDGET_EXHAUSTED
    assert resumed.provider.calls == 0
    assert resumed.outcome.budget["provider_attempts"] == 4

    tiny = fresh_run(tmp_path / "tokens", "machine_breakdown")
    capped = run_supervisor(
        tiny,
        _fault_task(),
        [ScriptedTurn(calls=(SNAPSHOT,))],
        limits=ProviderLimits(max_tokens=2_000),
    )
    assert capped.outcome.terminal is WorkflowPhase.BUDGET_EXHAUSTED
    assert capped.outcome.details["limit"] == "tokens"
    assert capped.provider.calls == 0


def test_a06_cancellation_stops_before_further_work(tmp_path: Path) -> None:
    handle = fresh_run(tmp_path, "machine_breakdown")
    before = identity(handle.services, handle.run_id)
    bound: list[SupervisorWorkflow] = []
    turns = [
        ScriptedTurn(calls=(SNAPSHOT,)),
        ScriptedTurn(calls=(SNAPSHOT,), before=lambda: bound[0].cancel()),
        ScriptedTurn(calls=(SNAPSHOT,)),
    ]
    run = run_supervisor(handle, _fault_task(), turns, workflow_hook=bound.append)
    assert run.outcome.terminal is WorkflowPhase.CANCELLED
    assert run.outcome.budget["status"] == "cancelled"
    # The in-flight response arrived after cancellation; its call never executed.
    assert run.outcome.budget["tool_executions"] == 1
    assert run.provider.remaining_turns == 1
    _assert_preserved(handle, before)


def _advance(handle: RunHandle, advance_id: str) -> None:
    now = handle.services.get_snapshot(handle.run_id).simulated_time_seconds
    handle.services.advance_simulation(handle.run_id, now + 1, advance_id)


def test_a07_state_change_after_observation_is_stale(tmp_path: Path) -> None:
    handle = fresh_run(tmp_path, "urgent_order")
    fixture = fixtures_by_id()["urgent-zh-plate-90min"]
    turns = scripted_order_turns(fixture.scripted_draft)
    turns[2] = ScriptedTurn(calls=turns[2].calls, before=lambda: _advance(handle, "advance-a07"))
    run = run_supervisor(
        handle, SupervisorTask(WorkflowKind.ORDER_REQUEST, fixture.request_text), turns
    )
    assert run.outcome.terminal is WorkflowPhase.STALE_STATE
    assert (
        run.outcome.details["live_state_version"] == run.outcome.details["pinned_state_version"] + 1
    )
    assert run.outcome.proposal is None
    lifecycle = handle.services.export_lifecycle(handle.run_id)
    assert not [item for item in lifecycle.proposals if item.workflow_id == run.outcome.workflow_id]

    # A fresh workflow observes the new version and needs its own approval.
    fresh = run_supervisor(
        handle,
        SupervisorTask(WorkflowKind.ORDER_REQUEST, fixture.request_text),
        scripted_order_turns(fixture.scripted_draft),
        workflow_id="workflow-s10-fresh",
    )
    assert fresh.outcome.terminal is WorkflowPhase.AWAITING_APPROVAL
    assert fresh.outcome.proposal["base_state_version"] == run.outcome.details["live_state_version"]


def test_a07_plan_revision_change_before_validation_is_stale(tmp_path: Path) -> None:
    handle = fresh_run(tmp_path, "urgent_order")
    fixture = fixtures_by_id()["urgent-en-plate-2h"]

    def operator_replan() -> None:
        services = handle.services
        services.ensure_workflow(handle.run_id, "workflow-operator", updated_at_utc=FIXED_UTC)
        result = services.generate_proposal(
            handle.run_id,
            "workflow-operator",
            "proposal-operator",
            not_before_seconds=3600,
            config=SolverConfig(random_seed=0),
            created_at_utc=FIXED_UTC,
        )
        assert result.candidate is not None
        approval = services.record_human_decision(
            handle.run_id,
            "proposal-operator",
            result.candidate.proposal_hash,
            approval_id="approval-operator",
            decision=ApprovalDecision.APPROVED,
            actor="operator",
            decided_at_utc=FIXED_UTC,
        )
        services.apply_approved_plan(
            handle.run_id,
            "proposal-operator",
            result.candidate.proposal_hash,
            approval.approval_id,
            "apply-operator",
        )

    turns = scripted_order_turns(fixture.scripted_draft)
    turns[3] = ScriptedTurn(calls=turns[3].calls, before=operator_replan)
    run = run_supervisor(
        handle, SupervisorTask(WorkflowKind.ORDER_REQUEST, fixture.request_text), turns
    )
    assert run.outcome.terminal is WorkflowPhase.STALE_STATE
    details = run.outcome.details
    assert details["live_plan_revision"] == details["pinned_plan_revision"] + 1
    assert run.outcome.proposal is None


def test_a07_persisted_proposal_goes_stale_and_cannot_be_applied(tmp_path: Path) -> None:
    handle = fresh_run(tmp_path, "urgent_order")
    fixture = fixtures_by_id()["urgent-en-bracket-units"]
    run = run_supervisor(
        handle,
        SupervisorTask(WorkflowKind.ORDER_REQUEST, fixture.request_text),
        scripted_order_turns(fixture.scripted_draft),
    )
    proposal = run.outcome.proposal
    assert proposal is not None
    _advance(handle, "advance-after-proposal")
    after_advance = identity(handle.services, handle.run_id)
    conflict = handle.services.inspect_conflict(
        handle.run_id, proposal["proposal_id"], proposal["proposal_hash"]
    )
    assert not conflict.can_apply
    assert ServiceErrorCode.STALE_STATE in conflict.reasons
    approval = handle.services.record_human_decision(
        handle.run_id,
        proposal["proposal_id"],
        proposal["proposal_hash"],
        approval_id="approval-stale",
        decision=ApprovalDecision.APPROVED,
        actor="operator",
        decided_at_utc=FIXED_UTC,
    )
    with pytest.raises(ApplicationServiceError) as stale:
        handle.services.apply_approved_plan(
            handle.run_id,
            proposal["proposal_id"],
            proposal["proposal_hash"],
            approval.approval_id,
            "apply-stale",
        )
    assert stale.value.code is ServiceErrorCode.STALE_STATE
    assert identity(handle.services, handle.run_id) == after_advance

    fresh = run_supervisor(
        handle,
        SupervisorTask(WorkflowKind.ORDER_REQUEST, fixture.request_text),
        scripted_order_turns(fixture.scripted_draft),
        workflow_id="workflow-s10-replan",
    )
    new = fresh.outcome.proposal
    assert new is not None and new["proposal_hash"] != proposal["proposal_hash"]
    with pytest.raises(ApplicationServiceError) as reused:
        handle.services.apply_approved_plan(
            handle.run_id,
            new["proposal_id"],
            new["proposal_hash"],
            approval.approval_id,
            "apply-reuse",
        )
    assert reused.value.code is ServiceErrorCode.APPROVAL_BINDING_MISMATCH
    fresh_approval = handle.services.record_human_decision(
        handle.run_id,
        new["proposal_id"],
        new["proposal_hash"],
        approval_id="approval-fresh",
        decision=ApprovalDecision.APPROVED,
        actor="operator",
        decided_at_utc=FIXED_UTC,
    )
    applied = handle.services.apply_approved_plan(
        handle.run_id,
        new["proposal_id"],
        new["proposal_hash"],
        fresh_approval.approval_id,
        "apply-fresh",
    )
    assert applied.resulting_state_version == after_advance["state_version"] + 1
