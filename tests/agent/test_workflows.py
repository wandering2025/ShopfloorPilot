"""S10-A03/A08: offline normal, urgent, and fault workflows stop before approval."""

from __future__ import annotations

from pathlib import Path

import pytest
from agent_support import (
    FIXED_UTC,
    fixtures_by_id,
    fresh_run,
    identity,
    run_supervisor,
    tool_results,
)

from shopfloor_pilot.agent.harness import (
    ABNORMALITY_TASKS,
    scripted_abnormality_turns,
    scripted_order_turns,
)
from shopfloor_pilot.agent.workflow import SupervisorTask
from shopfloor_pilot.domain.canonical import canonical_hash
from shopfloor_pilot.domain.models import ApprovalDecision, EventType
from shopfloor_pilot.services import ApplicationServiceError, ServiceErrorCode
from shopfloor_pilot.storage import WorkflowStatus
from shopfloor_pilot.tools import WorkflowKind, WorkflowPhase, build_supervisor_registry
from shopfloor_pilot.tools.registry import ToolClassification


def _order_run(tmp_path: Path, fixture_id: str):
    fixture = fixtures_by_id()[fixture_id]
    handle = fresh_run(tmp_path, fixture.scenario)
    before = identity(handle.services, handle.run_id)
    run = run_supervisor(
        handle,
        SupervisorTask(WorkflowKind.ORDER_REQUEST, fixture.request_text),
        scripted_order_turns(fixture.scripted_draft),
    )
    return handle, before, run


@pytest.mark.parametrize("fixture_id", ["normal-en-plate-today", "urgent-zh-plate-90min"])
def test_a03_order_workflow_produces_validated_immutable_proposal(
    tmp_path: Path, fixture_id: str
) -> None:
    handle, before, run = _order_run(tmp_path, fixture_id)
    outcome = run.outcome
    assert outcome.terminal is WorkflowPhase.AWAITING_APPROVAL, outcome.public_dict()
    assert outcome.evidence_label.startswith("OFFLINE CONTRACT EVIDENCE")
    proposal = outcome.proposal
    assert proposal is not None

    # The persisted proposal is independently valid against current authority.
    conflict = handle.services.inspect_conflict(
        handle.run_id, proposal["proposal_id"], proposal["proposal_hash"]
    )
    assert conflict.can_apply and conflict.validation.is_valid
    stored = handle.services.repository.get_proposal(handle.run_id, proposal["proposal_id"])
    assert stored.proposal_hash == proposal["proposal_hash"]
    assert stored.base_state_version == before["state_version"]

    # Solver and validator saw exactly the same declared order delta.
    declared = [item.model_dump(mode="json") for item in stored.proposed_order_deltas]
    assert len(declared) == 1
    delta_hash = canonical_hash(declared)
    solved = tool_results(run, "solve_schedule")[0]
    validated = tool_results(run, "validate_candidate")[0]
    assert solved["projected_order_delta_sha256"] == delta_hash
    assert validated["projected_order_delta_sha256"] == delta_hash
    assert validated["is_valid"] is True
    assert solved["candidate_content_hash"] == proposal["proposal_hash"]

    # Committed operational state is unchanged: the drafted order is not registered.
    after = identity(handle.services, handle.run_id)
    assert after == before
    drafted = declared[0]["order"]["order_id"]
    assert drafted not in {item["order_id"] for item in after["orders"]}

    # Approval stays outside: S06 workflow waits, no approval exists.
    workflow = handle.services.repository.get_workflow(handle.run_id, outcome.workflow_id)
    assert workflow.status is WorkflowStatus.WAITING_APPROVAL
    assert workflow.payload["pending_proposal_hash"] == proposal["proposal_hash"]
    approvals = handle.services.export_lifecycle(handle.run_id).approvals
    assert all(item.proposal_id != proposal["proposal_id"] for item in approvals)
    assert all(item.actor == "policy:deterministic" for item in approvals)  # bootstrap only
    assert [[tool.name for tool in request.tools] for request in run.provider.requests] == [
        ["submit_order_interpretation"],
        ["get_observable_snapshot"],
        ["solve_schedule"],
        ["validate_candidate"],
        ["persist_proposal"],
    ]


def test_a03_urgency_cannot_bypass_approval_and_applies_atomically(tmp_path: Path) -> None:
    handle, before, run = _order_run(tmp_path, "urgent-en-plate-2h")
    proposal = run.outcome.proposal
    assert proposal is not None
    assert proposal["order_deltas"][0]["order"]["priority_weight"] == 10
    with pytest.raises(ApplicationServiceError) as missing:
        handle.services.apply_approved_plan(
            handle.run_id,
            proposal["proposal_id"],
            proposal["proposal_hash"],
            "approval-that-does-not-exist",
            "apply-without-approval",
        )
    assert missing.value.code is ServiceErrorCode.APPROVAL_NOT_FOUND
    assert identity(handle.services, handle.run_id) == before

    # A separate human decision is required; then S06 applies order + plan together.
    approval = handle.services.record_human_decision(
        handle.run_id,
        proposal["proposal_id"],
        proposal["proposal_hash"],
        approval_id="approval-human-urgent",
        decision=ApprovalDecision.APPROVED,
        actor="operator-test",
        decided_at_utc=FIXED_UTC,
    )
    assert approval.actor == "human:operator-test"
    result = handle.services.apply_approved_plan(
        handle.run_id,
        proposal["proposal_id"],
        proposal["proposal_hash"],
        approval.approval_id,
        "apply-human-urgent",
    )
    events = handle.services.list_events(handle.run_id, after_sequence=before["event_count"])
    group = [item for item in events if item.mutation_id == result.mutation.mutation_id]
    kinds = [item.event.event_type for item in group]
    assert kinds == [EventType.ORDER_REGISTERED, EventType.PLAN_APPLIED]
    assert {item.resulting_state_version for item in group} == {before["state_version"] + 1}


def test_a03_fault_workflow_cites_hypotheses_and_waits(tmp_path: Path) -> None:
    handle = fresh_run(tmp_path, "machine_breakdown")
    before = identity(handle.services, handle.run_id)
    text, machines, query = ABNORMALITY_TASKS["machine_breakdown"]
    run = run_supervisor(
        handle,
        SupervisorTask(WorkflowKind.ABNORMALITY, text, machines),
        scripted_abnormality_turns(query),
    )
    outcome = run.outcome
    assert outcome.terminal is WorkflowPhase.AWAITING_APPROVAL, outcome.public_dict()
    assert outcome.proposal is not None and outcome.proposal["order_deltas"] == []
    diagnosis = tool_results(run, "diagnose_machine")[0]
    assert diagnosis["confirmed_root_cause"] is False
    assert diagnosis["maintenance_completed"] is False
    assert diagnosis["restoration_authority"] == "operator_or_scenario_harness_only"
    assert len(diagnosis["hypotheses"]) >= 2  # ambiguity is preserved, not collapsed
    kinds = {claim["kind"] for claim in outcome.claims}
    assert kinds == {"observation", "hypothesis", "plan"}
    assert [[tool.name for tool in request.tools] for request in run.provider.requests] == [
        ["get_observable_snapshot"],
        ["summarize_observations", "retrieve_guidance"],
        ["diagnose_machine"],
        ["solve_schedule"],
        ["validate_candidate"],
        ["persist_proposal"],
    ]
    assert identity(handle.services, handle.run_id) == before
    snapshot = handle.services.get_snapshot(handle.run_id)
    assert {item.machine_id: item.status.value for item in snapshot.machines}["CNC02"] == "down"


def test_a08_valid_proposal_leaves_simulation_unchanged_and_no_self_approval(
    tmp_path: Path,
) -> None:
    handle, before, run = _order_run(tmp_path, "urgent-en-bracket-units")
    assert run.outcome.terminal is WorkflowPhase.AWAITING_APPROVAL
    after = identity(handle.services, handle.run_id)
    for key in (
        "state_version",
        "plan_revision",
        "simulated_time_seconds",
        "orders",
        "machines",
        "attempts",
        "event_count",
        "snapshot_hash",
    ):
        assert after[key] == before[key], key
    lifecycle = handle.services.export_lifecycle(handle.run_id)
    assert run.outcome.proposal["proposal_id"] in {item.proposal_id for item in lifecycle.proposals}
    supervisor_id = run.outcome.proposal["proposal_id"]
    assert all(item.proposal_id != supervisor_id for item in lifecycle.approvals)
    assert all(item.proposal_id != supervisor_id for item in lifecycle.approval_consumptions)
    # The registry offers no decision or application capability at all.
    registry = build_supervisor_registry()
    writes = [
        item.name
        for item in registry.specs()
        if item.classification is not ToolClassification.READ_ONLY
    ]
    assert sorted(writes) == ["persist_proposal", "submit_order_interpretation"]
    capabilities = handle.services.capabilities
    privileged = set(capabilities.operator_actions) | set(capabilities.batch_policy_actions)
    assert not privileged & set(registry.names)
    assert not any(hasattr(handle.services.for_model(), name) for name in privileged)
    # The ledger is paused for the human; no further provider work may start.
    from shopfloor_pilot.budgets import BudgetLedger
    from shopfloor_pilot.budgets import WorkflowStatus as LedgerStatus

    with BudgetLedger(run.ledger_path) as ledger:
        assert ledger.snapshot(run.outcome.workflow_id).status is LedgerStatus.PAUSED_HUMAN
