"""Approval actors, stale conflicts, validation, and capability separation."""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from typing import Any

import pytest

from shopfloor_pilot.domain.fixtures import InterventionKind, ScheduledIntervention
from shopfloor_pilot.domain.models import (
    ApprovalDecision,
    CandidateProposal,
    EventType,
    MachineStatus,
    Order,
    OrderDelta,
    OrderDeltaKind,
    ScheduleAssignment,
)
from shopfloor_pilot.events import EventDraft, TimeAdvancedPayload
from shopfloor_pilot.scheduling import SolverConfig, solve_schedule
from shopfloor_pilot.services import (
    ApplicationServiceError,
    ApplicationServices,
    ServiceErrorCode,
)
from shopfloor_pilot.storage import WorkflowRecord, WorkflowStatus

FIXED_UTC = "2026-10-20T20:45:00+02:00"


def _candidate(services: ApplicationServices, suffix: str = "decision") -> Any:
    services.ensure_workflow(
        "run-s06-services",
        f"workflow-{suffix}",
        updated_at_utc=FIXED_UTC,
    )
    result = services.generate_proposal(
        "run-s06-services",
        f"workflow-{suffix}",
        f"proposal-{suffix}",
        config=SolverConfig(max_time_seconds=1.0),
        created_at_utc=FIXED_UTC,
    )
    assert result.candidate is not None
    return result.candidate


def test_human_and_policy_actors_are_distinct_and_rejection_cannot_apply(
    service_factory: Callable[[str], tuple[ApplicationServices, Any, Path]],
) -> None:
    human_services, _, _ = service_factory()
    human_candidate = _candidate(human_services, "human")
    human = human_services.record_human_decision(
        human_candidate.run_id,
        human_candidate.proposal_id,
        human_candidate.proposal_hash,
        approval_id="approval-human",
        decision=ApprovalDecision.REJECTED,
        actor="operator-7",
        decided_at_utc=FIXED_UTC,
    )
    assert human.actor == "human:operator-7"
    with pytest.raises(ApplicationServiceError) as rejected:
        human_services.apply_approved_plan(
            human_candidate.run_id,
            human_candidate.proposal_id,
            human_candidate.proposal_hash,
            human.approval_id,
            "apply-rejected",
        )
    assert rejected.value.code is ServiceErrorCode.APPROVAL_REJECTED
    assert human_services.repository.get_snapshot(human_candidate.run_id).state_version == 0

    policy_services, _, _ = service_factory()
    policy_candidate = _candidate(policy_services, "policy")
    policy = policy_services.record_deterministic_policy_decision(
        policy_candidate.run_id,
        policy_candidate.proposal_id,
        policy_candidate.proposal_hash,
        approval_id="approval-policy",
        policy_name="deterministic-v1",
        decided_at_utc=FIXED_UTC,
    )
    assert policy.actor == "policy:deterministic-v1"
    assert policy.actor != human.actor


def test_no_decision_and_rejected_replan_leave_operational_state_unchanged(
    service_factory: Callable[[str], tuple[ApplicationServices, Any, Path]],
) -> None:
    services, case, _ = service_factory()
    initial_before = services.repository.get_checkpoint(case.snapshot.run_id)
    initial = _candidate(services, "initial-no-decision")

    # Merely displaying/persisting a candidate cannot activate it.
    assert services.repository.get_checkpoint(case.snapshot.run_id) == initial_before
    assert services.repository.list_events(case.snapshot.run_id) == ()

    initial_approval = services.record_human_decision(
        initial.run_id,
        initial.proposal_id,
        initial.proposal_hash,
        approval_id="approval-initial-human",
        decision=ApprovalDecision.APPROVED,
        actor="operator-7",
        decided_at_utc=FIXED_UTC,
    )
    services.apply_approved_plan(
        initial.run_id,
        initial.proposal_id,
        initial.proposal_hash,
        initial_approval.approval_id,
        "apply-initial-human",
    )

    services.ensure_workflow(
        initial.run_id,
        "workflow-replan-rejected",
        updated_at_utc=FIXED_UTC,
    )
    proposed_order = Order(
        order_id="order-draft-rejected",
        product_id="service-part",
        quantity=1,
        release_time_seconds=0,
        due_time_seconds=24,
        priority_weight=5,
    )
    result = services.generate_proposal(
        initial.run_id,
        "workflow-replan-rejected",
        "proposal-replan-rejected",
        proposed_order_deltas=(OrderDelta(kind=OrderDeltaKind.ADD, order=proposed_order),),
        config=SolverConfig(max_time_seconds=1.0),
        created_at_utc=FIXED_UTC,
    )
    assert result.candidate is not None
    rejected = services.record_human_decision(
        initial.run_id,
        result.candidate.proposal_id,
        result.candidate.proposal_hash,
        approval_id="approval-replan-rejected",
        decision=ApprovalDecision.REJECTED,
        actor="operator-7",
        decided_at_utc=FIXED_UTC,
    )
    applied_before = services.repository.get_checkpoint(initial.run_id)
    events_before = services.repository.list_events(initial.run_id)

    with pytest.raises(ApplicationServiceError) as refusal:
        services.apply_approved_plan(
            initial.run_id,
            result.candidate.proposal_id,
            result.candidate.proposal_hash,
            rejected.approval_id,
            "apply-rejected-replan",
        )

    assert refusal.value.code is ServiceErrorCode.APPROVAL_REJECTED
    assert services.repository.get_checkpoint(initial.run_id) == applied_before
    assert services.repository.list_events(initial.run_id) == events_before
    assert "order-draft-rejected" not in {item.order_id for item in applied_before.snapshot.orders}


def test_stale_and_wrong_hash_fail_without_additional_operational_mutation(
    service_factory: Callable[[str], tuple[ApplicationServices, Any, Path]],
) -> None:
    services, case, _ = service_factory()
    candidate = _candidate(services, "stale")
    approval = services.record_deterministic_policy_decision(
        candidate.run_id,
        candidate.proposal_id,
        candidate.proposal_hash,
        approval_id="approval-stale",
        policy_name="deterministic-v1",
        decided_at_utc=FIXED_UTC,
    )

    with pytest.raises(ApplicationServiceError) as wrong_hash:
        services.apply_approved_plan(
            candidate.run_id,
            candidate.proposal_id,
            "f" * 64,
            approval.approval_id,
            "apply-wrong-hash",
        )
    assert wrong_hash.value.code is ServiceErrorCode.PROPOSAL_HASH_MISMATCH
    assert services.repository.get_snapshot(candidate.run_id).state_version == 0

    payload = TimeAdvancedPayload(target_time_seconds=1)
    services.repository.apply_mutation(
        run_id=candidate.run_id,
        expected_state_version=0,
        idempotency_key="advance-before-approval",
        command_name="test_advance",
        command_payload=payload.model_dump(mode="json"),
        events=(
            EventDraft(
                simulated_time_seconds=1,
                event_type=EventType.TIME_ADVANCED,
                payload=payload.model_dump(mode="json"),
            ),
        ),
    )
    before = services.repository.get_checkpoint(candidate.run_id)
    event_count = len(services.repository.list_events(candidate.run_id))
    conflict = services.inspect_conflict(
        candidate.run_id,
        candidate.proposal_id,
        expected_proposal_hash=candidate.proposal_hash,
    )
    assert not conflict.can_apply
    assert ServiceErrorCode.STALE_STATE in conflict.reasons
    with pytest.raises(ApplicationServiceError) as stale:
        services.apply_approved_plan(
            candidate.run_id,
            candidate.proposal_id,
            candidate.proposal_hash,
            approval.approval_id,
            "apply-stale",
        )
    assert stale.value.code is ServiceErrorCode.STALE_STATE
    assert services.repository.get_checkpoint(candidate.run_id) == before
    assert len(services.repository.list_events(candidate.run_id)) == event_count
    assert services.repository.export_lifecycle(candidate.run_id).approval_consumptions == ()


@pytest.mark.parametrize("change_kind", ["machine_fault", "order_registration"])
def test_each_explicit_operational_change_makes_an_old_approval_stale(
    change_kind: str,
    service_factory: Callable[[str], tuple[ApplicationServices, Any, Path]],
) -> None:
    services, case, _ = service_factory()
    candidate = _candidate(services, f"stale-{change_kind}")
    approval = services.record_deterministic_policy_decision(
        candidate.run_id,
        candidate.proposal_id,
        candidate.proposal_hash,
        approval_id=f"approval-stale-{change_kind}",
        policy_name="deterministic-v1",
        decided_at_utc=FIXED_UTC,
    )
    if change_kind == "machine_fault":
        intervention = ScheduledIntervention(
            intervention_id="fault-before-apply",
            at_seconds=1,
            kind=InterventionKind.MACHINE_FAULT,
            target_machine_id="mill",
            injected_status=MachineStatus.DOWN,
        )
    else:
        intervention = ScheduledIntervention(
            intervention_id="order-before-apply",
            at_seconds=1,
            kind=InterventionKind.ADD_ORDER,
            order_to_add=Order(
                order_id="order-explicit",
                product_id="service-part",
                quantity=1,
                release_time_seconds=1,
                due_time_seconds=14,
                priority_weight=2,
            ),
        )
    services.advance_simulation(
        case.snapshot.run_id,
        1,
        f"advance-{change_kind}",
        interventions=(intervention,),
    )
    before = services.repository.get_checkpoint(case.snapshot.run_id)
    events_before = services.repository.list_events(case.snapshot.run_id)
    if change_kind == "machine_fault":
        assert (
            next(
                machine for machine in before.snapshot.machines if machine.machine_id == "mill"
            ).status
            is MachineStatus.DOWN
        )
    else:
        assert "order-explicit" in {order.order_id for order in before.snapshot.orders}

    with pytest.raises(ApplicationServiceError) as stale:
        services.apply_approved_plan(
            candidate.run_id,
            candidate.proposal_id,
            candidate.proposal_hash,
            approval.approval_id,
            f"apply-stale-{change_kind}",
        )

    assert stale.value.code is ServiceErrorCode.STALE_STATE
    assert services.repository.get_checkpoint(case.snapshot.run_id) == before
    assert services.repository.list_events(case.snapshot.run_id) == events_before
    assert services.repository.export_lifecycle(candidate.run_id).approval_consumptions == ()


def test_reset_run_rejects_old_run_proposal_and_approval_without_mutation(
    service_factory: Callable[[str], tuple[ApplicationServices, Any, Path]],
) -> None:
    services, case, _ = service_factory()
    candidate = _candidate(services, "before-reset")
    approval = services.record_deterministic_policy_decision(
        candidate.run_id,
        candidate.proposal_id,
        candidate.proposal_hash,
        approval_id="approval-before-reset",
        policy_name="deterministic-v1",
        decided_at_utc=FIXED_UTC,
    )
    reset_run_id = "run-s06-services-after-reset"
    reset_snapshot = case.snapshot.model_copy(update={"run_id": reset_run_id})
    services.repository.reset_run(
        case.snapshot.run_id,
        reset_snapshot,
        created_at_utc=FIXED_UTC,
    )
    source_before = services.repository.get_checkpoint(case.snapshot.run_id)
    reset_before = services.repository.get_checkpoint(reset_run_id)

    with pytest.raises(ApplicationServiceError) as wrong_run:
        services.apply_approved_plan(
            reset_run_id,
            candidate.proposal_id,
            candidate.proposal_hash,
            approval.approval_id,
            "apply-old-approval-after-reset",
        )

    assert wrong_run.value.code is ServiceErrorCode.WRONG_RUN
    assert services.repository.get_checkpoint(case.snapshot.run_id) == source_before
    assert services.repository.get_checkpoint(reset_run_id) == reset_before
    assert services.repository.list_events(case.snapshot.run_id) == ()
    assert services.repository.list_events(reset_run_id) == ()
    assert services.repository.export_lifecycle(candidate.run_id).approval_consumptions == ()


def test_save_validated_proposal_rejects_invalid_external_candidate_without_persisting(
    service_factory: Callable[[str], tuple[ApplicationServices, Any, Path]],
) -> None:
    services, _, _ = service_factory()
    valid = _candidate(services, "valid-source")
    services.ensure_workflow("run-s06-services", "workflow-invalid", updated_at_utc=FIXED_UTC)
    assignments = list(valid.assignments)
    first = assignments[0]
    assignments[0] = ScheduleAssignment(
        operation_id=first.operation_id,
        attempt_id="attempt-invalid-capability",
        machine_id="qc",
        window=first.window,
    )
    invalid = CandidateProposal.create(
        schema_version=1,
        proposal_id="proposal-invalid",
        run_id=valid.run_id,
        workflow_id="workflow-invalid",
        base_state_version=valid.base_state_version,
        base_snapshot_hash=valid.base_snapshot_hash,
        proposed_plan_revision=valid.proposed_plan_revision,
        assignments=tuple(assignments),
        objective=valid.objective,
    )

    with pytest.raises(ApplicationServiceError) as raised:
        services.save_validated_proposal(invalid, created_at_utc=FIXED_UTC)
    assert raised.value.code is ServiceErrorCode.INVALID_CANDIDATE
    with pytest.raises(ApplicationServiceError) as absent:
        services.inspect_conflict(invalid.run_id, invalid.proposal_id)
    assert absent.value.code is ServiceErrorCode.PROPOSAL_NOT_FOUND


def test_model_facade_exposes_no_decision_or_apply_capability(
    service_factory: Callable[[str], tuple[ApplicationServices, Any, Path]],
) -> None:
    services, _, _ = service_factory()
    model_view = services.for_model()
    assert set(services.capabilities.model_readable_actions) == {
        "ensure_workflow",
        "generate_proposal",
        "save_validated_proposal",
        "inspect_conflict",
        "get_snapshot",
        "get_checkpoint",
        "list_events",
        "export_lifecycle",
        "calculate_run_metrics",
        "calculate_decision_latency",
        "build_proposal_comparison",
    }
    assert not hasattr(model_view, "record_human_decision")
    assert not hasattr(model_view, "record_deterministic_policy_decision")
    assert not hasattr(model_view, "apply_approved_plan")
    assert not hasattr(model_view, "advance_simulation")
    assert not hasattr(model_view, "mark_workflow_blocked")
    assert not hasattr(model_view, "export_proposal_reports")
    assert not hasattr(model_view, "export_replay_bundle")
    assert "apply_approved_plan" in services.capabilities.operator_actions
    assert "advance_simulation" in services.capabilities.operator_actions
    assert "mark_workflow_blocked" in services.capabilities.operator_actions


def test_model_workflow_ensure_only_creates_running_and_preserves_existing_state(
    service_factory: Callable[[str], tuple[ApplicationServices, Any, Path]],
) -> None:
    services, case, _ = service_factory()
    model_view = services.for_model()

    created = model_view.ensure_workflow(
        case.snapshot.run_id,
        "workflow-model-running",
        updated_at_utc=FIXED_UTC,
    )
    repeated = model_view.ensure_workflow(
        case.snapshot.run_id,
        "workflow-model-running",
        updated_at_utc="2026-10-20T20:46:00+02:00",
    )
    assert repeated == created
    assert created.status is WorkflowStatus.RUNNING
    assert created.payload == {}
    assert (
        services.ensure_workflow(
            case.snapshot.run_id,
            "workflow-model-running",
            payload={"outcome": "blocked"},
        )
        == created
    )

    barrier = Barrier(2)

    def concurrent_ensure() -> Any:
        barrier.wait()
        return model_view.ensure_workflow(
            case.snapshot.run_id,
            "workflow-model-concurrent",
            updated_at_utc=FIXED_UTC,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        concurrent_results = tuple(executor.map(lambda _: concurrent_ensure(), range(2)))
    assert concurrent_results[0] == concurrent_results[1]
    assert concurrent_results[0].status is WorkflowStatus.RUNNING

    with pytest.raises(TypeError):
        model_view.ensure_workflow(
            case.snapshot.run_id,
            "workflow-model-forged-status",
            status=WorkflowStatus.FAILED,  # type: ignore[call-arg]
        )
    with pytest.raises(TypeError):
        model_view.ensure_workflow(
            case.snapshot.run_id,
            "workflow-model-forged-payload",
            payload={"outcome": "blocked"},  # type: ignore[call-arg]
        )
    with pytest.raises(TypeError):
        services.ensure_workflow(
            case.snapshot.run_id,
            "workflow-service-forged-status",
            status=WorkflowStatus.COMPLETED,  # type: ignore[call-arg]
        )

    for status in (WorkflowStatus.FAILED, WorkflowStatus.COMPLETED):
        workflow_id = f"workflow-model-terminal-{status.value}"
        terminal = services.repository.save_workflow(
            WorkflowRecord(
                run_id=case.snapshot.run_id,
                workflow_id=workflow_id,
                status=status,
                payload={"outcome": "blocked", "reason_code": "operator-owned"},
                updated_at_utc=FIXED_UTC,
            )
        )
        with pytest.raises(ApplicationServiceError) as raised:
            model_view.ensure_workflow(case.snapshot.run_id, workflow_id)
        assert raised.value.code is ServiceErrorCode.WORKFLOW_CONFLICT
        assert services.repository.get_workflow(case.snapshot.run_id, workflow_id) == terminal


def test_model_proposal_paths_cannot_revive_terminal_or_replace_waiting_workflow(
    service_factory: Callable[[str], tuple[ApplicationServices, Any, Path]],
) -> None:
    services, case, _ = service_factory()
    model_view = services.for_model()
    candidate = _candidate(services, "model-lifecycle")

    # An exact persistence retry is safe, but a waiting workflow cannot be rebound.
    assert (
        model_view.save_validated_proposal(
            candidate,
            created_at_utc="2026-10-20T20:47:00+02:00",
        )
        == candidate
    )
    replacement = CandidateProposal.create(
        schema_version=1,
        proposal_id="proposal-model-replacement",
        run_id=candidate.run_id,
        workflow_id=candidate.workflow_id,
        base_state_version=candidate.base_state_version,
        base_snapshot_hash=candidate.base_snapshot_hash,
        proposed_plan_revision=candidate.proposed_plan_revision,
        assignments=candidate.assignments,
        objective=candidate.objective,
        proposed_order_deltas=candidate.proposed_order_deltas,
        proposed_resource_deltas=candidate.proposed_resource_deltas,
        created_at_utc=FIXED_UTC,
    )
    before_waiting = services.export_lifecycle(case.snapshot.run_id)
    with pytest.raises(ApplicationServiceError) as waiting_conflict:
        model_view.save_validated_proposal(replacement, created_at_utc=FIXED_UTC)
    assert waiting_conflict.value.code is ServiceErrorCode.WORKFLOW_CONFLICT
    assert services.export_lifecycle(case.snapshot.run_id) == before_waiting

    terminal = services.repository.save_workflow(
        WorkflowRecord(
            run_id=case.snapshot.run_id,
            workflow_id=candidate.workflow_id,
            status=WorkflowStatus.COMPLETED,
            payload={"outcome": "applied", "operator_owned": True},
            updated_at_utc=FIXED_UTC,
        )
    )
    before_terminal = services.export_lifecycle(case.snapshot.run_id)

    with pytest.raises(ApplicationServiceError) as generated_conflict:
        model_view.generate_proposal(
            case.snapshot.run_id,
            candidate.workflow_id,
            "proposal-model-terminal-generated",
            config=SolverConfig(max_time_seconds=1.0),
            created_at_utc=FIXED_UTC,
        )
    assert generated_conflict.value.code is ServiceErrorCode.WORKFLOW_CONFLICT
    with pytest.raises(ApplicationServiceError) as saved_conflict:
        model_view.save_validated_proposal(replacement, created_at_utc=FIXED_UTC)
    assert saved_conflict.value.code is ServiceErrorCode.WORKFLOW_CONFLICT

    assert services.repository.get_workflow(case.snapshot.run_id, candidate.workflow_id) == terminal
    assert services.export_lifecycle(case.snapshot.run_id) == before_terminal


def test_proposal_persistence_rolls_back_if_workflow_closes_during_generation(
    service_factory: Callable[[str], tuple[ApplicationServices, Any, Path]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    services, case, _ = service_factory()
    workflow_id = "workflow-close-during-solve"
    services.for_model().ensure_workflow(
        case.snapshot.run_id,
        workflow_id,
        updated_at_utc=FIXED_UTC,
    )

    def close_workflow_then_solve(*args: Any, **kwargs: Any) -> Any:
        services.mark_workflow_blocked(
            case.snapshot.run_id,
            workflow_id,
            reason_code="closed_during_solve",
            updated_at_utc=FIXED_UTC,
        )
        return solve_schedule(*args, **kwargs)

    monkeypatch.setattr(
        "shopfloor_pilot.services.application.solve_schedule",
        close_workflow_then_solve,
    )
    with pytest.raises(ApplicationServiceError) as raised:
        services.for_model().generate_proposal(
            case.snapshot.run_id,
            workflow_id,
            "proposal-close-during-solve",
            config=SolverConfig(max_time_seconds=1.0),
            created_at_utc=FIXED_UTC,
        )

    assert raised.value.code is ServiceErrorCode.WORKFLOW_CONFLICT
    lifecycle = services.export_lifecycle(case.snapshot.run_id)
    assert lifecycle.proposals == ()
    assert len(lifecycle.workflows) == 1
    assert lifecycle.workflows[0].status is WorkflowStatus.FAILED
    assert lifecycle.workflows[0].payload["outcome"] == "blocked"
