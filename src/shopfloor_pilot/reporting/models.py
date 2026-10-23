"""Typed proposal-report and portable event-bundle contracts."""

from __future__ import annotations

from pathlib import Path
from typing import Literal, Self

from pydantic import Field, model_validator

from shopfloor_pilot.domain.base import Identifier, Sha256, StrictModel
from shopfloor_pilot.domain.models import ScheduleObjective
from shopfloor_pilot.events import EventEnvelope, SnapshotCheckpoint
from shopfloor_pilot.metrics import DecisionLatency, MetricBasis, MetricWindow, RunMetrics


class PlanProjectionReport(StrictModel):
    """One side of a proposal comparison, always labeled as a projection."""

    schema_version: Literal[1] = 1
    report_side: Literal["before", "after"]
    metric_basis: Literal["predicted"] = "predicted"
    run_id: Identifier
    snapshot_state_version: int = Field(strict=True, ge=0)
    snapshot_hash: Sha256
    snapshot_simulated_time_seconds: int = Field(strict=True, ge=0)
    plan_revision: int = Field(strict=True, ge=0)
    order_ids: tuple[Identifier, ...]
    objective_basis: Literal[
        MetricBasis.ACTUAL,
        MetricBasis.PREDICTED,
        MetricBasis.UNAVAILABLE,
    ]
    objective: ScheduleObjective | None = None
    prediction_window: MetricWindow
    proposal_id: Identifier | None = None
    proposal_hash: Sha256 | None = None

    @model_validator(mode="after")
    def validate_projection(self) -> Self:
        if self.prediction_window.start_seconds != self.snapshot_simulated_time_seconds:
            raise ValueError("prediction window must start at the compared snapshot")
        if self.objective_basis is MetricBasis.UNAVAILABLE:
            if self.objective is not None:
                raise ValueError("unavailable objective cannot contain values")
        elif self.objective is None:
            raise ValueError("available objective basis requires objective values")
        proposal_fields = (self.proposal_id, self.proposal_hash)
        if self.report_side == "before":
            if any(value is not None for value in proposal_fields):
                raise ValueError("before projection cannot claim candidate identity")
        elif None in proposal_fields:
            raise ValueError("after projection requires exact proposal identity/hash")
        return self


class PredictedObjectiveChange(StrictModel):
    """After-minus-before objective deltas; negative values are forecast reductions."""

    basis: Literal["predicted_change"] = "predicted_change"
    weighted_tardiness_seconds: int = Field(strict=True)
    makespan_seconds: int = Field(strict=True)
    interpretation: Literal["after_minus_before_lower_is_better"] = (
        "after_minus_before_lower_is_better"
    )


class ProposalComparisonReport(StrictModel):
    """A prediction-only before/after report with actual state held fixed."""

    schema_version: Literal[1] = 1
    run_id: Identifier
    proposal_id: Identifier
    proposal_hash: Sha256
    actual_at_snapshot: RunMetrics
    before: PlanProjectionReport
    after: PlanProjectionReport
    predicted_change: PredictedObjectiveChange | None = None
    realization_status: Literal["not_observed"] = "not_observed"
    realized_improvement: None = None
    decision_latency: DecisionLatency | None = None

    @model_validator(mode="after")
    def validate_comparison(self) -> Self:
        if not (
            self.run_id == self.actual_at_snapshot.run_id == self.before.run_id == self.after.run_id
        ):
            raise ValueError("proposal comparison must stay within one run")
        if (
            self.proposal_id != self.after.proposal_id
            or self.proposal_hash != self.after.proposal_hash
        ):
            raise ValueError("comparison proposal binding must match its after projection")
        comparable = self.before.objective is not None and self.after.objective is not None
        if comparable != (self.predicted_change is not None):
            raise ValueError("predicted change exists exactly when both objectives exist")
        if self.before.prediction_window != self.after.prediction_window:
            raise ValueError("before and after must use one common prediction window")
        return self


class ProposalReportPaths(StrictModel):
    before: Path
    after: Path
    comparison: Path


class ReplayBundleHeader(StrictModel):
    """First JSONL record; embeds everything required to anchor pure replay."""

    schema_version: Literal[1] = 1
    record_type: Literal["replay_header"] = "replay_header"
    run_id: Identifier
    initial_checkpoint: SnapshotCheckpoint
    event_count: int = Field(strict=True, ge=0)
    operational_stream_hash: Sha256
    final_snapshot_hash: Sha256
    actual_metrics: RunMetrics
    actual_metrics_hash: Sha256

    @model_validator(mode="after")
    def validate_header(self) -> Self:
        checkpoint = self.initial_checkpoint
        if checkpoint.snapshot.run_id != self.run_id:
            raise ValueError("replay header run does not match initial checkpoint")
        if checkpoint.snapshot.state_version != 0 or checkpoint.last_sequence != 0:
            raise ValueError("portable replay must embed the version-zero checkpoint")
        if self.actual_metrics.run_id != self.run_id:
            raise ValueError("embedded actual metrics belong to another run")
        return self


class ReplayEventRecord(StrictModel):
    schema_version: Literal[1] = 1
    record_type: Literal["event"] = "event"
    envelope: EventEnvelope


class ReplayBundle(StrictModel):
    header: ReplayBundleHeader
    events: tuple[EventEnvelope, ...]

    @model_validator(mode="after")
    def validate_records(self) -> Self:
        if len(self.events) != self.header.event_count:
            raise ValueError("portable replay event count does not match its header")
        if any(item.event.run_id != self.header.run_id for item in self.events):
            raise ValueError("portable replay contains a cross-run event")
        return self


class ReplayResult(StrictModel):
    schema_version: Literal[1] = 1
    run_id: Identifier
    event_count: int = Field(strict=True, ge=0)
    operational_stream_hash: Sha256
    final_snapshot_hash: Sha256
    actual_metrics_hash: Sha256
    checkpoint: SnapshotCheckpoint
    actual_metrics: RunMetrics
