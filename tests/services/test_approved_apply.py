"""Atomic approved-plan application, rollback, idempotency, and recovery tests."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from shopfloor_pilot.domain.canonical import canonical_hash
from shopfloor_pilot.domain.models import (
    ApprovalDecision,
    AttemptStatus,
    CandidateProposal,
    EventType,
    Order,
    OrderDelta,
    OrderDeltaKind,
    ScheduleAssignment,
    ScheduleObjective,
    TimeWindow,
)
from shopfloor_pilot.scheduling import SolverConfig
from shopfloor_pilot.services import (
    ApplicationServiceError,
    ApplicationServices,
    PlanApplicationStatus,
    ServiceErrorCode,
)
from shopfloor_pilot.storage import SQLiteStateRepository, WorkflowStatus

FIXED_UTC = "2026-10-20T20:45:00+02:00"


def _urgent_order() -> Order:
    return Order(
        order_id="order-urgent",
        product_id="service-part",
        quantity=1,
        release_time_seconds=0,
        due_time_seconds=8,
        priority_weight=10,
    )


def _proposal(
    services: ApplicationServices,
    *,
    suffix: str,
    urgent: bool = False,
    not_before_seconds: int | None = None,
) -> Any:
    workflow_id = f"workflow-{suffix}"
    services.ensure_workflow(
        services.repository.get_checkpoint("run-s06-services").snapshot.run_id,
        workflow_id,
        payload={"case": suffix},
        updated_at_utc=FIXED_UTC,
    )
    deltas = (OrderDelta(kind=OrderDeltaKind.ADD, order=_urgent_order()),) if urgent else ()
    result = services.generate_proposal(
        "run-s06-services",
        workflow_id,
        f"proposal-{suffix}",
        proposed_order_deltas=deltas,
        not_before_seconds=not_before_seconds,
        config=SolverConfig(max_time_seconds=1.0),
        created_at_utc=FIXED_UTC,
    )
    assert result.candidate is not None, result.report.diagnostics
    return result.candidate


def _approve(
    services: ApplicationServices,
    candidate: Any,
    *,
    suffix: str,
    decision: ApprovalDecision = ApprovalDecision.APPROVED,
) -> Any:
    return services.record_deterministic_policy_decision(
        candidate.run_id,
        candidate.proposal_id,
        candidate.proposal_hash,
        approval_id=f"approval-{suffix}",
        policy_name="deterministic-test-v1",
        decision=decision,
        reason="Focused service acceptance.",
        decided_at_utc=FIXED_UTC,
    )


def test_urgent_order_and_plan_commit_in_one_approved_event_group(
    service_factory: Callable[[str], tuple[ApplicationServices, Any, Path]],
) -> None:
    services, case, _ = service_factory()
    candidate = _proposal(services, suffix="urgent", urgent=True)
    approval = _approve(services, candidate, suffix="urgent")

    result = services.apply_approved_plan(
        case.snapshot.run_id,
        candidate.proposal_id,
        candidate.proposal_hash,
        approval.approval_id,
        "apply-urgent",
    )

    assert result.status is PlanApplicationStatus.APPLIED
    assert result.previous_state_version == 0
    assert result.resulting_state_version == 1
    assert result.snapshot.plan_revision == 1
    assert {item.order_id for item in result.snapshot.orders} == {
        "order-base",
        "order-urgent",
    }
    assert [item.event.event_type for item in result.mutation.events] == [
        EventType.ORDER_REGISTERED,
        EventType.PLAN_APPLIED,
    ]
    assert {item.mutation_id for item in result.mutation.events} == {"apply-urgent"}
    assert all(item.resulting_state_version == 1 for item in result.mutation.events)
    assert result.snapshot.operations == result.validation.projected_operations
    assert result.snapshot.attempts == result.validation.projected_attempts
    lifecycle = services.repository.export_lifecycle(case.snapshot.run_id)
    assert len(lifecycle.approval_consumptions) == 1
    assert lifecycle.approval_consumptions[0].proposal_id == candidate.proposal_id
    application_audits = [
        item for item in lifecycle.audits if item.action == "approved_plan_applied"
    ]
    assert len(application_audits) == 1
    assert application_audits[0].details["result_snapshot_hash"] == canonical_hash(result.snapshot)
    workflow = next(
        item for item in lifecycle.workflows if item.workflow_id == candidate.workflow_id
    )
    assert workflow.status is WorkflowStatus.COMPLETED
    assert workflow.payload["outcome"] == "applied"
    assert workflow.payload["applied_proposal_id"] == candidate.proposal_id
    assert services.repository.replay_run(case.snapshot.run_id) == result.mutation.checkpoint


def test_urgent_order_database_failure_rolls_back_every_atomic_participant_and_retries(
    service_factory: Callable[[str], tuple[ApplicationServices, Any, Path]],
) -> None:
    services, case, _ = service_factory()
    candidate = _proposal(services, suffix="rollback", urgent=True)
    approval = _approve(services, candidate, suffix="rollback")
    before = services.repository.get_checkpoint(case.snapshot.run_id)
    lifecycle_before = services.repository.export_lifecycle(case.snapshot.run_id)

    def fail(stage: str) -> None:
        if stage == "before_commit":
            raise sqlite3.DatabaseError("injected database failure before commit")

    with pytest.raises(sqlite3.DatabaseError, match="injected database failure"):
        services.apply_approved_plan(
            case.snapshot.run_id,
            candidate.proposal_id,
            candidate.proposal_hash,
            approval.approval_id,
            "apply-rollback",
            _failure_injector=fail,
        )

    assert services.repository.get_checkpoint(case.snapshot.run_id) == before
    assert services.repository.list_events(case.snapshot.run_id) == ()
    lifecycle_after = services.repository.export_lifecycle(case.snapshot.run_id)
    assert lifecycle_after.approval_consumptions == ()
    assert lifecycle_after.audits == lifecycle_before.audits
    assert lifecycle_after.workflows == lifecycle_before.workflows
    assert "order-urgent" not in {
        item.order_id for item in services.repository.get_snapshot(case.snapshot.run_id).orders
    }

    retry = services.apply_approved_plan(
        case.snapshot.run_id,
        candidate.proposal_id,
        candidate.proposal_hash,
        approval.approval_id,
        "apply-rollback",
    )
    assert retry.status is PlanApplicationStatus.APPLIED
    assert len(services.repository.list_events(case.snapshot.run_id)) == 2
    assert (
        len(services.repository.export_lifecycle(case.snapshot.run_id).approval_consumptions) == 1
    )


def test_old_approval_cannot_authorize_another_same_version_proposal(
    service_factory: Callable[[str], tuple[ApplicationServices, Any, Path]],
) -> None:
    services, case, _ = service_factory()
    first = _proposal(services, suffix="old-a")
    second = _proposal(services, suffix="new-b")
    old_approval = _approve(services, first, suffix="old-a")
    before = services.repository.get_checkpoint(case.snapshot.run_id)

    with pytest.raises(ApplicationServiceError) as raised:
        services.apply_approved_plan(
            case.snapshot.run_id,
            second.proposal_id,
            second.proposal_hash,
            old_approval.approval_id,
            "apply-wrong-proposal",
        )

    assert raised.value.code is ServiceErrorCode.APPROVAL_BINDING_MISMATCH
    assert services.repository.get_checkpoint(case.snapshot.run_id) == before
    assert services.repository.list_events(case.snapshot.run_id) == ()
    assert services.repository.export_lifecycle(case.snapshot.run_id).approval_consumptions == ()


def test_recomputed_changed_candidate_hash_still_requires_its_own_approval(
    service_factory: Callable[[str], tuple[ApplicationServices, Any, Path]],
) -> None:
    services, case, _ = service_factory()
    original = _proposal(services, suffix="tamper-original")
    old_approval = _approve(services, original, suffix="tamper-original")
    services.ensure_workflow(
        case.snapshot.run_id,
        "workflow-tampered",
        updated_at_utc=FIXED_UTC,
    )
    shifted = tuple(
        ScheduleAssignment(
            operation_id=item.operation_id,
            attempt_id=f"tampered-{index}",
            machine_id=item.machine_id,
            window=TimeWindow(
                start_seconds=item.window.start_seconds + 1,
                end_seconds=item.window.end_seconds + 1,
            ),
        )
        for index, item in enumerate(original.assignments, start=1)
    )
    changed = CandidateProposal.create(
        schema_version=1,
        proposal_id="proposal-tampered",
        run_id=original.run_id,
        workflow_id="workflow-tampered",
        base_state_version=original.base_state_version,
        base_snapshot_hash=original.base_snapshot_hash,
        proposed_plan_revision=original.proposed_plan_revision,
        assignments=shifted,
        objective=ScheduleObjective(
            weighted_total_tardiness_seconds=0,
            makespan_seconds=original.objective.makespan_seconds + 1,
        ),
    )
    services.save_validated_proposal(changed, created_at_utc=FIXED_UTC)
    assert changed.proposal_hash != original.proposal_hash

    before = services.repository.get_checkpoint(case.snapshot.run_id)
    with pytest.raises(ApplicationServiceError) as raised:
        services.apply_approved_plan(
            case.snapshot.run_id,
            changed.proposal_id,
            changed.proposal_hash,
            old_approval.approval_id,
            "apply-tampered-with-old-approval",
        )
    assert raised.value.code is ServiceErrorCode.APPROVAL_BINDING_MISMATCH
    assert services.repository.get_checkpoint(case.snapshot.run_id) == before
    assert services.repository.list_events(case.snapshot.run_id) == ()


def test_exact_double_apply_is_idempotent_and_changed_content_conflicts(
    service_factory: Callable[[str], tuple[ApplicationServices, Any, Path]],
) -> None:
    services, case, _ = service_factory()
    candidate = _proposal(services, suffix="double", not_before_seconds=1)
    approval = _approve(services, candidate, suffix="double")
    args = (
        case.snapshot.run_id,
        candidate.proposal_id,
        candidate.proposal_hash,
        approval.approval_id,
        "apply-double",
    )

    first = services.apply_approved_plan(*args)
    second = services.apply_approved_plan(*args)

    assert first.status is PlanApplicationStatus.APPLIED
    assert second.status is PlanApplicationStatus.IDEMPOTENT
    assert second.mutation.replayed_idempotent
    assert second.mutation.model_dump(exclude={"replayed_idempotent"}) == first.mutation.model_dump(
        exclude={"replayed_idempotent"}
    )
    assert (
        len(services.repository.export_lifecycle(case.snapshot.run_id).approval_consumptions) == 1
    )
    assert (
        len(
            [
                item
                for item in services.repository.export_lifecycle(case.snapshot.run_id).audits
                if item.action == "approved_plan_applied"
            ]
        )
        == 1
    )

    with pytest.raises(ApplicationServiceError) as changed:
        services.apply_approved_plan(*args, effective_time_seconds=1)
    assert changed.value.code is ServiceErrorCode.IDEMPOTENCY_CONFLICT


def test_exact_retry_is_stable_across_distinct_wall_clock_timestamps(
    service_factory: Callable[[str], tuple[ApplicationServices, Any, Path]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    services, case, _ = service_factory()
    candidate = _proposal(services, suffix="delayed-retry")
    approval = _approve(services, candidate, suffix="delayed-retry")
    timestamps = iter(
        (
            "2026-10-20T20:45:01+00:00",
            "2026-10-20T20:55:01+00:00",
        )
    )
    monkeypatch.setattr(
        "shopfloor_pilot.services.application._utc_now",
        lambda: next(timestamps),
    )
    arguments = (
        case.snapshot.run_id,
        candidate.proposal_id,
        candidate.proposal_hash,
        approval.approval_id,
        "apply-delayed-retry",
    )

    first = services.apply_approved_plan(*arguments)
    retry = services.apply_approved_plan(*arguments)

    assert first.status is PlanApplicationStatus.APPLIED
    assert retry.status is PlanApplicationStatus.IDEMPOTENT
    assert retry.mutation.command_hash == first.mutation.command_hash
    lifecycle = services.export_lifecycle(case.snapshot.run_id)
    audit = next(item for item in lifecycle.audits if item.action == "approved_plan_applied")
    assert audit.recorded_at_utc == "2026-10-20T20:45:01+00:00"
    assert len(lifecycle.approval_consumptions) == 1


def test_two_same_key_writers_return_one_commit_and_one_idempotent_result(
    service_factory: Callable[[str], tuple[ApplicationServices, Any, Path]],
) -> None:
    services, case, path = service_factory()
    candidate = _proposal(services, suffix="race")
    approval = _approve(services, candidate, suffix="race")

    def apply() -> Any:
        independent = ApplicationServices(
            SQLiteStateRepository(path, busy_timeout_seconds=2.0),
            case.catalog,
            seed=0,
        )
        return independent.apply_approved_plan(
            case.snapshot.run_id,
            candidate.proposal_id,
            candidate.proposal_hash,
            approval.approval_id,
            "apply-race",
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = [
            future.result(timeout=10) for future in (executor.submit(apply), executor.submit(apply))
        ]
    assert {item.status for item in results} == {
        PlanApplicationStatus.APPLIED,
        PlanApplicationStatus.IDEMPOTENT,
    }
    assert services.repository.get_snapshot(case.snapshot.run_id).state_version == 1
    assert (
        len(services.repository.export_lifecycle(case.snapshot.run_id).approval_consumptions) == 1
    )


def test_two_distinct_approved_proposals_from_same_base_race_with_one_winner(
    service_factory: Callable[[str], tuple[ApplicationServices, Any, Path]],
) -> None:
    services, case, path = service_factory()
    first = _proposal(services, suffix="conflict-a")
    second = _proposal(services, suffix="conflict-b", not_before_seconds=1)
    first_approval = _approve(services, first, suffix="conflict-a")
    second_approval = _approve(services, second, suffix="conflict-b")

    def apply(candidate: Any, approval: Any, key: str) -> Any:
        independent = ApplicationServices(
            SQLiteStateRepository(path, busy_timeout_seconds=2.0),
            case.catalog,
            seed=0,
        )
        try:
            return independent.apply_approved_plan(
                case.snapshot.run_id,
                candidate.proposal_id,
                candidate.proposal_hash,
                approval.approval_id,
                key,
            )
        except ApplicationServiceError as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = (
            executor.submit(apply, first, first_approval, "apply-conflict-a"),
            executor.submit(apply, second, second_approval, "apply-conflict-b"),
        )
        outcomes = [future.result(timeout=10) for future in futures]

    winners = [item for item in outcomes if not isinstance(item, ApplicationServiceError)]
    losers = [item for item in outcomes if isinstance(item, ApplicationServiceError)]
    assert len(winners) == len(losers) == 1
    assert winners[0].status is PlanApplicationStatus.APPLIED
    assert losers[0].code is ServiceErrorCode.STALE_STATE
    lifecycle = services.repository.export_lifecycle(case.snapshot.run_id)
    assert len(lifecycle.approval_consumptions) == 1
    consumed_approval_id = lifecycle.approval_consumptions[0].approval_id
    assert consumed_approval_id in {first_approval.approval_id, second_approval.approval_id}
    losing_approval_id = (
        second_approval.approval_id
        if consumed_approval_id == first_approval.approval_id
        else first_approval.approval_id
    )
    assert losing_approval_id not in {item.approval_id for item in lifecycle.approval_consumptions}
    workflow_statuses = {item.workflow_id: item.status for item in lifecycle.workflows}
    winning_workflow_id = next(
        proposal.workflow_id
        for proposal in (first, second)
        if proposal.proposal_id == lifecycle.approval_consumptions[0].proposal_id
    )
    losing_workflow_id = (
        second.workflow_id if winning_workflow_id == first.workflow_id else first.workflow_id
    )
    assert workflow_statuses[winning_workflow_id] is WorkflowStatus.COMPLETED
    assert workflow_statuses[losing_workflow_id] is WorkflowStatus.WAITING_APPROVAL
    assert services.repository.get_snapshot(case.snapshot.run_id).state_version == 1
    assert (
        sum(
            item.event.event_type is EventType.PLAN_APPLIED
            for item in services.repository.list_events(case.snapshot.run_id)
        )
        == 1
    )


@pytest.mark.parametrize(
    "failure_stage",
    ["after_event_insert", "after_snapshot_insert", "before_commit"],
)
def test_replan_failure_preserves_previous_active_plan_and_history_at_every_failpoint(
    failure_stage: str,
    service_factory: Callable[[str], tuple[ApplicationServices, Any, Path]],
) -> None:
    services, case, _ = service_factory()
    initial = _proposal(services, suffix=f"initial-{failure_stage}")
    initial_approval = _approve(services, initial, suffix=f"initial-{failure_stage}")
    services.apply_approved_plan(
        case.snapshot.run_id,
        initial.proposal_id,
        initial.proposal_hash,
        initial_approval.approval_id,
        f"apply-initial-{failure_stage}",
    )
    services.advance_simulation(
        case.snapshot.run_id,
        3,
        f"advance-initial-{failure_stage}",
    )
    urgent = _proposal(
        services,
        suffix=f"urgent-{failure_stage}",
        urgent=True,
        not_before_seconds=4,
    )
    urgent_approval = _approve(services, urgent, suffix=f"urgent-{failure_stage}")
    before = services.repository.get_checkpoint(case.snapshot.run_id)
    events_before = services.repository.list_events(case.snapshot.run_id)
    lifecycle_before = services.repository.export_lifecycle(case.snapshot.run_id)
    assert before.snapshot.plan_revision == 1
    assert before.snapshot.operations
    assert before.snapshot.attempts

    def fail(stage: str) -> None:
        if stage == failure_stage:
            raise sqlite3.DatabaseError(f"injected {failure_stage}")

    with pytest.raises(sqlite3.DatabaseError, match=failure_stage):
        services.apply_approved_plan(
            case.snapshot.run_id,
            urgent.proposal_id,
            urgent.proposal_hash,
            urgent_approval.approval_id,
            f"apply-urgent-{failure_stage}",
            effective_time_seconds=4,
            _failure_injector=fail,
        )

    reopened = SQLiteStateRepository(services.repository.database_path)
    after = reopened.get_checkpoint(case.snapshot.run_id)
    assert after == before
    assert after.snapshot.plan_revision == before.snapshot.plan_revision
    assert after.snapshot.operations == before.snapshot.operations
    assert after.snapshot.attempts == before.snapshot.attempts
    assert after.snapshot.orders == before.snapshot.orders
    assert reopened.list_events(case.snapshot.run_id) == events_before
    lifecycle_after = reopened.export_lifecycle(case.snapshot.run_id)
    assert lifecycle_after.approval_consumptions == lifecycle_before.approval_consumptions
    assert lifecycle_after.audits == lifecycle_before.audits
    assert lifecycle_after.workflows == lifecycle_before.workflows

    retry_services = ApplicationServices(reopened, case.catalog, seed=0)
    retry = retry_services.apply_approved_plan(
        case.snapshot.run_id,
        urgent.proposal_id,
        urgent.proposal_hash,
        urgent_approval.approval_id,
        f"apply-urgent-{failure_stage}",
        effective_time_seconds=4,
    )
    assert retry.status is PlanApplicationStatus.APPLIED
    assert retry.snapshot.plan_revision == 2
    assert "order-urgent" in {item.order_id for item in retry.snapshot.orders}


def test_committed_plan_rebuilds_and_executes_once_without_runtime_activation(
    service_factory: Callable[[str], tuple[ApplicationServices, Any, Path]],
) -> None:
    services, case, path = service_factory()
    candidate = _proposal(services, suffix="crash")
    approval = _approve(services, candidate, suffix="crash")
    services.apply_approved_plan(
        case.snapshot.run_id,
        candidate.proposal_id,
        candidate.proposal_hash,
        approval.approval_id,
        "apply-crash",
    )

    # Simulate losing all in-memory state after commit and before any callback runs.
    reopened = SQLiteStateRepository(path)
    reopened_services = ApplicationServices(reopened, case.catalog, seed=0)
    final = reopened_services.advance_simulation(
        case.snapshot.run_id,
        30,
        "advance-after-crash",
    ).snapshot
    assert all(item.status is AttemptStatus.COMPLETED for item in final.attempts)
    event_types = [item.event.event_type for item in reopened.list_events(case.snapshot.run_id)]
    starts_before = event_types.count(EventType.OPERATION_STARTED)
    completions_before = event_types.count(EventType.OPERATION_COMPLETED)

    rebuilt_again = ApplicationServices(
        SQLiteStateRepository(path),
        case.catalog,
        seed=0,
    )
    duplicate = rebuilt_again.advance_simulation(
        case.snapshot.run_id,
        30,
        "advance-after-crash",
    )
    assert duplicate.idempotent
    repeated_types = [item.event.event_type for item in reopened.list_events(case.snapshot.run_id)]
    assert repeated_types.count(EventType.OPERATION_STARTED) == starts_before
    assert repeated_types.count(EventType.OPERATION_COMPLETED) == completions_before
