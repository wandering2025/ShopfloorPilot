"""Shared temporary-database fixtures for S02 storage acceptance tests."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from shopfloor_pilot.domain.fixtures import load_scenario
from shopfloor_pilot.domain.models import (
    ApprovalDecision,
    ApprovalRecord,
    CandidateProposal,
    EventType,
    OperationalSnapshot,
    ScheduleObjective,
)
from shopfloor_pilot.events import EventDraft, TimeAdvancedPayload
from shopfloor_pilot.storage import (
    AuditRecord,
    MutationResult,
    SQLiteStateRepository,
    WorkflowRecord,
    WorkflowStatus,
)

FIXED_UTC = "2026-10-20T18:30:00+02:00"


@dataclass(frozen=True)
class LifecycleRecords:
    workflow: WorkflowRecord
    proposal: CandidateProposal
    approval: ApprovalRecord
    audit: AuditRecord


@pytest.fixture
def database_path(tmp_path: Path) -> Path:
    return tmp_path / "authoritative-state.db"


@pytest.fixture
def snapshot_factory() -> Callable[[str], OperationalSnapshot]:
    bundle = load_scenario("normal")

    def build(run_id: str) -> OperationalSnapshot:
        return OperationalSnapshot(
            schema_version=1,
            run_id=run_id,
            state_version=0,
            plan_revision=0,
            simulated_time_seconds=bundle.scenario.clock.start_seconds,
            hard_horizon_seconds=bundle.scenario.hard_horizon_seconds,
            machines=bundle.catalog.machines,
            orders=bundle.scenario.orders,
            operations=(),
            attempts=(),
        )

    return build


@pytest.fixture
def initial_snapshot(
    snapshot_factory: Callable[[str], OperationalSnapshot],
) -> OperationalSnapshot:
    return snapshot_factory("run-storage-normal")


@pytest.fixture
def initialized_repository(database_path: Path) -> SQLiteStateRepository:
    repository = SQLiteStateRepository(database_path)
    repository.initialize(applied_at_utc=FIXED_UTC)
    return repository


@pytest.fixture
def repository(
    initialized_repository: SQLiteStateRepository,
    initial_snapshot: OperationalSnapshot,
) -> SQLiteStateRepository:
    initialized_repository.create_run(initial_snapshot, created_at_utc=FIXED_UTC)
    return initialized_repository


@pytest.fixture
def apply_time() -> Callable[..., MutationResult]:
    def apply(
        repository: SQLiteStateRepository,
        *,
        run_id: str,
        expected_state_version: int,
        target_time_seconds: int,
        idempotency_key: str,
        participant: Any = None,
        failure_injector: Any = None,
    ) -> MutationResult:
        payload = TimeAdvancedPayload(target_time_seconds=target_time_seconds)
        event = EventDraft(
            simulated_time_seconds=target_time_seconds,
            event_type=EventType.TIME_ADVANCED,
            payload=payload.model_dump(mode="json"),
            recorded_at_utc=FIXED_UTC,
        )
        return repository.apply_mutation(
            run_id=run_id,
            expected_state_version=expected_state_version,
            idempotency_key=idempotency_key,
            command_name="advance_time",
            command_payload=payload.model_dump(mode="json"),
            events=(event,),
            participant=participant,
            _failure_injector=failure_injector,
            created_at_utc=FIXED_UTC,
        )

    return apply


@pytest.fixture
def persist_lifecycle() -> Callable[..., LifecycleRecords]:
    def persist(
        repository: SQLiteStateRepository,
        run_id: str,
        *,
        suffix: str,
        audit_details: dict[str, object] | None = None,
    ) -> LifecycleRecords:
        checkpoint = repository.get_checkpoint(run_id)
        workflow = WorkflowRecord(
            run_id=run_id,
            workflow_id=f"workflow-{suffix}",
            status=WorkflowStatus.WAITING_APPROVAL,
            payload={"request": "candidate review", "suffix": suffix},
            updated_at_utc=FIXED_UTC,
        )
        proposal = CandidateProposal.create(
            schema_version=1,
            proposal_id=f"proposal-{suffix}",
            run_id=run_id,
            workflow_id=workflow.workflow_id,
            base_state_version=checkpoint.snapshot.state_version,
            base_snapshot_hash=checkpoint.snapshot_hash,
            proposed_plan_revision=checkpoint.snapshot.plan_revision + 1,
            assignments=(),
            objective=ScheduleObjective(
                weighted_total_tardiness_seconds=0,
                makespan_seconds=checkpoint.snapshot.simulated_time_seconds,
            ),
            created_at_utc=FIXED_UTC,
        )
        approval = ApprovalRecord(
            schema_version=1,
            approval_id=f"approval-{suffix}",
            proposal_id=proposal.proposal_id,
            proposal_hash=proposal.proposal_hash,
            run_id=run_id,
            workflow_id=workflow.workflow_id,
            base_state_version=proposal.base_state_version,
            decision=ApprovalDecision.APPROVED,
            actor="storage-test-operator",
            decided_at_utc=FIXED_UTC,
            reason="Acceptance fixture approval; storage alone grants no live authority.",
        )
        audit = AuditRecord(
            audit_id=f"audit-{suffix}",
            run_id=run_id,
            category="storage_acceptance",
            action="lifecycle_saved",
            actor="storage-test",
            details=audit_details or {"suffix": suffix},
            simulated_time_seconds=checkpoint.snapshot.simulated_time_seconds,
            recorded_at_utc=FIXED_UTC,
        )
        repository.save_workflow(workflow)
        repository.save_proposal(proposal)
        repository.save_approval(approval)
        repository.append_audit(audit, workflow_id=workflow.workflow_id)
        return LifecycleRecords(
            workflow=workflow,
            proposal=proposal,
            approval=approval,
            audit=audit,
        )

    return persist
