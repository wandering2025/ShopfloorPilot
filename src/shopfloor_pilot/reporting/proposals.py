"""Pure before/after proposal reports with explicit prediction semantics."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from shopfloor_pilot.domain.models import (
    CandidateProposal,
    OrderDeltaKind,
    ScheduleObjective,
)
from shopfloor_pilot.events import EventEnvelope, SnapshotCheckpoint, replay_events
from shopfloor_pilot.metrics import (
    DecisionLatency,
    MetricBasis,
    MetricWindow,
    calculate_run_metrics,
)
from shopfloor_pilot.validation import ValidationReport

from .errors import ReportConflictError
from .models import (
    PlanProjectionReport,
    PredictedObjectiveChange,
    ProposalComparisonReport,
    ProposalReportPaths,
)


def _projected_order_ids(
    candidate: CandidateProposal, current_ids: tuple[str, ...]
) -> tuple[str, ...]:
    orders = list(current_ids)
    for delta in candidate.proposed_order_deltas:
        order_id = delta.order.order_id
        if delta.kind is OrderDeltaKind.ADD:
            if order_id in orders:
                raise ReportConflictError(f"proposal adds existing order {order_id}")
            orders.append(order_id)
        elif delta.kind is OrderDeltaKind.REPLACE:
            if order_id not in orders:
                raise ReportConflictError(f"proposal replaces absent order {order_id}")
        else:  # pragma: no cover - enum exhaustiveness guard
            raise ReportConflictError(f"unsupported order delta {delta.kind}")
    return tuple(sorted(orders))


def build_proposal_comparison(
    initial_checkpoint: SnapshotCheckpoint,
    current_checkpoint: SnapshotCheckpoint,
    events: Sequence[EventEnvelope],
    candidate: CandidateProposal,
    validation: ValidationReport,
    *,
    decision_latency: DecisionLatency | None = None,
) -> ProposalComparisonReport:
    """Compare the current applied projection with one validated immutable proposal.

    No field calls the predicted delta a realized improvement. ``actual_at_snapshot``
    is common to both sides and remains the replay-derived operational measurement.
    """

    event_tuple = tuple(events)
    try:
        replayed = replay_events(initial_checkpoint, event_tuple)
    except Exception as exc:
        raise ReportConflictError(f"cannot replay proposal report input: {exc}") from exc
    if replayed != current_checkpoint:
        raise ReportConflictError("current checkpoint does not equal the supplied event stream")
    snapshot = current_checkpoint.snapshot
    if candidate.run_id != snapshot.run_id:
        raise ReportConflictError("candidate belongs to another run")
    if candidate.base_state_version != snapshot.state_version:
        raise ReportConflictError("candidate base state version is stale")
    if candidate.base_snapshot_hash != current_checkpoint.snapshot_hash:
        raise ReportConflictError("candidate base snapshot hash is stale")
    if candidate.proposed_plan_revision != snapshot.plan_revision + 1:
        raise ReportConflictError("candidate plan revision is not the next revision")
    if not validation.is_valid:
        raise ReportConflictError("proposal comparison requires a valid independent report")
    if (
        validation.total_tardiness_seconds != candidate.objective.weighted_total_tardiness_seconds
        or validation.makespan_seconds != candidate.objective.makespan_seconds
    ):
        raise ReportConflictError("candidate objective disagrees with independent validation")

    actual = calculate_run_metrics(initial_checkpoint, event_tuple)
    prediction_window = MetricWindow(
        start_seconds=snapshot.simulated_time_seconds,
        end_seconds=snapshot.hard_horizon_seconds,
    )
    if (
        actual.predicted_makespan_seconds is None
        or actual.predicted_weighted_tardiness_seconds is None
    ):
        before_objective = None
        before_basis = MetricBasis.UNAVAILABLE
    else:
        before_objective = ScheduleObjective(
            weighted_total_tardiness_seconds=actual.predicted_weighted_tardiness_seconds,
            makespan_seconds=actual.predicted_makespan_seconds,
        )
        before_basis = actual.prediction_basis
    before = PlanProjectionReport(
        report_side="before",
        run_id=snapshot.run_id,
        snapshot_state_version=snapshot.state_version,
        snapshot_hash=current_checkpoint.snapshot_hash,
        snapshot_simulated_time_seconds=snapshot.simulated_time_seconds,
        plan_revision=snapshot.plan_revision,
        order_ids=tuple(sorted(item.order_id for item in snapshot.orders)),
        objective_basis=before_basis,
        objective=before_objective,
        prediction_window=prediction_window,
    )
    after = PlanProjectionReport(
        report_side="after",
        run_id=snapshot.run_id,
        snapshot_state_version=snapshot.state_version,
        snapshot_hash=current_checkpoint.snapshot_hash,
        snapshot_simulated_time_seconds=snapshot.simulated_time_seconds,
        plan_revision=candidate.proposed_plan_revision,
        order_ids=_projected_order_ids(
            candidate,
            tuple(item.order_id for item in snapshot.orders),
        ),
        objective_basis=MetricBasis.PREDICTED,
        objective=candidate.objective,
        prediction_window=prediction_window,
        proposal_id=candidate.proposal_id,
        proposal_hash=candidate.proposal_hash,
    )
    change = (
        None
        if before.objective is None
        else PredictedObjectiveChange(
            weighted_tardiness_seconds=(
                after.objective.weighted_total_tardiness_seconds
                - before.objective.weighted_total_tardiness_seconds
            ),
            makespan_seconds=(after.objective.makespan_seconds - before.objective.makespan_seconds),
        )
    )
    return ProposalComparisonReport(
        run_id=snapshot.run_id,
        proposal_id=candidate.proposal_id,
        proposal_hash=candidate.proposal_hash,
        actual_at_snapshot=actual,
        before=before,
        after=after,
        predicted_change=change,
        decision_latency=decision_latency,
    )


def export_proposal_reports(
    output_directory: str | Path,
    report: ProposalComparisonReport,
) -> ProposalReportPaths:
    """Write stable, separate before/after views plus their comparison wrapper."""

    root = Path(output_directory)
    root.mkdir(parents=True, exist_ok=True)
    paths = ProposalReportPaths(
        before=root / "proposal_before.json",
        after=root / "proposal_after.json",
        comparison=root / "proposal_comparison.json",
    )
    paths.before.write_text(report.before.model_dump_json(indent=2) + "\n", encoding="utf-8")
    paths.after.write_text(report.after.model_dump_json(indent=2) + "\n", encoding="utf-8")
    paths.comparison.write_text(report.model_dump_json(indent=2) + "\n", encoding="utf-8")
    return paths
