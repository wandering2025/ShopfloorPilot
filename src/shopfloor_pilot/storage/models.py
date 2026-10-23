"""Immutable persistence and lifecycle records shared by SQLite consumers."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal, Self

from pydantic import Field, JsonValue, model_validator

from shopfloor_pilot.domain.base import (
    Identifier,
    Seconds,
    Sha256,
    StrictModel,
    WallClockTimestamp,
)
from shopfloor_pilot.domain.models import ApprovalRecord, CandidateProposal
from shopfloor_pilot.events.models import EventEnvelope, SnapshotCheckpoint


class WorkflowStatus(StrEnum):
    """Durable lifecycle state for one supervisor workflow."""

    CREATED = "created"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class MutationResult(StrictModel):
    """Stored outcome of one atomic operational mutation.

    An idempotent retry returns the same stored versions and event envelopes with only
    ``replayed_idempotent`` changed to describe how this result was obtained.
    """

    schema_version: Literal[1] = 1
    run_id: Identifier
    idempotency_key: Identifier
    mutation_id: Identifier
    command_hash: Sha256
    previous_state_version: int = Field(strict=True, ge=0)
    resulting_state_version: int = Field(strict=True, ge=1)
    events: tuple[EventEnvelope, ...] = Field(min_length=1)
    checkpoint: SnapshotCheckpoint
    replayed_idempotent: bool = False

    @model_validator(mode="after")
    def validate_mutation(self) -> Self:
        if self.resulting_state_version != self.previous_state_version + 1:
            raise ValueError("resulting_state_version must increment exactly once")
        expected_indexes = list(range(len(self.events)))
        if [item.event_index for item in self.events] != expected_indexes:
            raise ValueError("event envelopes must be ordered by contiguous event_index")
        sequences = [item.event.sequence for item in self.events]
        if sequences != list(range(sequences[0], sequences[0] + len(sequences))):
            raise ValueError("mutation event sequences must be contiguous")
        for envelope in self.events:
            if envelope.event.run_id != self.run_id:
                raise ValueError("mutation event run_id must match the result run_id")
            if envelope.mutation_id != self.mutation_id:
                raise ValueError("event envelope mutation_id must match the result")
            if envelope.base_state_version != self.previous_state_version:
                raise ValueError("event envelope base_state_version must match the result")
            if envelope.resulting_state_version != self.resulting_state_version:
                raise ValueError("event envelope resulting_state_version must match the result")
            if envelope.event_count != len(self.events):
                raise ValueError("event envelope event_count must match the result event count")
        if self.checkpoint.snapshot.run_id != self.run_id:
            raise ValueError("mutation checkpoint run_id must match the result run_id")
        if self.checkpoint.snapshot.state_version != self.resulting_state_version:
            raise ValueError("mutation checkpoint must contain the resulting state version")
        if self.checkpoint.last_sequence != self.events[-1].event.sequence:
            raise ValueError("mutation checkpoint must end at the final event sequence")
        return self

    def as_idempotent_replay(self) -> MutationResult:
        """Return this immutable outcome marked as served from idempotency storage."""

        if self.replayed_idempotent:
            return self
        # Only this descriptive flag changes; all validated stored outcome data is retained.
        return self.model_copy(update={"replayed_idempotent": True})


class WorkflowRecord(StrictModel):
    """Durable, non-operational state for one supervisor invocation."""

    schema_version: Literal[1] = 1
    run_id: Identifier
    workflow_id: Identifier
    status: WorkflowStatus
    payload: dict[str, JsonValue] = Field(default_factory=dict)
    updated_at_utc: WallClockTimestamp | None = None


class AuditRecord(StrictModel):
    """Durable bookkeeping that does not change operational state version."""

    schema_version: Literal[1] = 1
    audit_id: Identifier
    run_id: Identifier
    category: str = Field(min_length=1, max_length=80)
    action: str = Field(min_length=1, max_length=120)
    actor: str = Field(min_length=1, max_length=100)
    details: dict[str, JsonValue] = Field(default_factory=dict)
    simulated_time_seconds: Seconds | None = None
    recorded_at_utc: WallClockTimestamp


class ApprovalConsumptionRecord(StrictModel):
    """Historical evidence that an approval was consumed by a committed mutation.

    Reading or replaying this record never grants authorization for a live action.
    """

    schema_version: Literal[1] = 1
    run_id: Identifier
    approval_id: Identifier
    proposal_id: Identifier
    mutation_id: Identifier
    resulting_state_version: int = Field(strict=True, ge=1)
    consumed_at_utc: WallClockTimestamp


class LifecycleExport(StrictModel):
    """Run-scoped non-operational records for restart restoration or display."""

    schema_version: Literal[1] = 1
    run_id: Identifier
    proposals: tuple[CandidateProposal, ...] = ()
    approvals: tuple[ApprovalRecord, ...] = ()
    workflows: tuple[WorkflowRecord, ...] = ()
    audits: tuple[AuditRecord, ...] = ()
    approval_consumptions: tuple[ApprovalConsumptionRecord, ...] = ()

    @model_validator(mode="after")
    def validate_run_scope_and_references(self) -> Self:
        collections = (
            self.proposals,
            self.approvals,
            self.workflows,
            self.audits,
            self.approval_consumptions,
        )
        if any(item.run_id != self.run_id for records in collections for item in records):
            raise ValueError("all lifecycle records must belong to the exported run")

        def require_unique(values: list[str], label: str) -> None:
            if len(values) != len(set(values)):
                raise ValueError(f"{label} values must be unique")

        proposals = {item.proposal_id: item for item in self.proposals}
        approvals = {item.approval_id: item for item in self.approvals}
        workflow_ids = {item.workflow_id for item in self.workflows}
        require_unique([item.proposal_id for item in self.proposals], "proposal_id")
        require_unique([item.approval_id for item in self.approvals], "approval_id")
        require_unique([item.workflow_id for item in self.workflows], "workflow_id")
        require_unique([item.audit_id for item in self.audits], "audit_id")
        require_unique(
            [item.approval_id for item in self.approval_consumptions],
            "consumed approval_id",
        )

        for proposal in self.proposals:
            if proposal.workflow_id not in workflow_ids:
                raise ValueError("proposal references a workflow absent from lifecycle export")
        for approval in self.approvals:
            proposal = proposals.get(approval.proposal_id)
            if proposal is None:
                raise ValueError("approval references a proposal absent from lifecycle export")
            if approval.proposal_hash != proposal.proposal_hash:
                raise ValueError("approval proposal_hash does not match its proposal")
            if approval.workflow_id != proposal.workflow_id:
                raise ValueError("approval workflow_id does not match its proposal")
            if approval.base_state_version != proposal.base_state_version:
                raise ValueError("approval base_state_version does not match its proposal")
        for consumption in self.approval_consumptions:
            approval = approvals.get(consumption.approval_id)
            if approval is None:
                raise ValueError("approval consumption references an absent approval")
            if consumption.proposal_id != approval.proposal_id:
                raise ValueError("approval consumption proposal_id does not match approval")
        return self


class RunRecord(StrictModel):
    """Identity and initial immutable checkpoint for one manufacturing run."""

    schema_version: Literal[1] = 1
    run_id: Identifier
    reset_from_run_id: Identifier | None = None
    created_at_utc: WallClockTimestamp
    initial_checkpoint: SnapshotCheckpoint

    @model_validator(mode="after")
    def validate_initial_checkpoint(self) -> Self:
        if self.reset_from_run_id == self.run_id:
            raise ValueError("a reset run must have a new run_id")
        checkpoint = self.initial_checkpoint
        if checkpoint.snapshot.run_id != self.run_id:
            raise ValueError("initial checkpoint run_id must match the run record")
        if checkpoint.snapshot.state_version != 0 or checkpoint.last_sequence != 0:
            raise ValueError("initial checkpoint must be at state version and sequence zero")
        return self


class RestoredRunState(StrictModel):
    """A checkpoint, operational suffix, and historical lifecycle records.

    Approval records in ``lifecycle`` are display/recovery history only. They cannot
    authorize a new mutation after restoration or replay.
    """

    schema_version: Literal[1] = 1
    checkpoint: SnapshotCheckpoint
    events: tuple[EventEnvelope, ...] = ()
    lifecycle: LifecycleExport

    @model_validator(mode="after")
    def validate_restored_run(self) -> Self:
        run_id = self.checkpoint.snapshot.run_id
        if self.lifecycle.run_id != run_id:
            raise ValueError("checkpoint and lifecycle export must belong to the same run")
        if any(item.event.run_id != run_id for item in self.events):
            raise ValueError("all restored event envelopes must belong to the checkpoint run")
        sequences = [item.event.sequence for item in self.events]
        expected = list(
            range(
                self.checkpoint.last_sequence + 1,
                self.checkpoint.last_sequence + 1 + len(sequences),
            )
        )
        if sequences != expected:
            raise ValueError("restored events must be a contiguous suffix after the checkpoint")
        if self.events and (
            self.events[0].base_state_version != self.checkpoint.snapshot.state_version
        ):
            raise ValueError("restored event suffix must start from the checkpoint state version")
        return self
