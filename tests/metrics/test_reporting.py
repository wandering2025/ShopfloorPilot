"""S06 proposal-report and portable event-replay acceptance tests."""

from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Any

import pytest

import shopfloor_pilot.scheduling as scheduling
from shopfloor_pilot.domain.models import CandidateProposal, ScheduleObjective
from shopfloor_pilot.reporting import (
    ReplayBundleError,
    ReportConflictError,
    build_proposal_comparison,
    export_proposal_reports,
    export_replay_bundle,
    load_replay_bundle,
    replay_bundle,
)
from shopfloor_pilot.validation import ValidationReport


def _current_candidate(metric_timeline: Any) -> CandidateProposal:
    checkpoint = metric_timeline.at_twenty
    return CandidateProposal.create(
        schema_version=1,
        proposal_id="proposal-s06-report",
        run_id=checkpoint.snapshot.run_id,
        workflow_id="workflow-s06-report",
        base_state_version=checkpoint.snapshot.state_version,
        base_snapshot_hash=checkpoint.snapshot_hash,
        proposed_plan_revision=checkpoint.snapshot.plan_revision + 1,
        assignments=(),
        objective=ScheduleObjective(
            weighted_total_tardiness_seconds=19,
            makespan_seconds=23,
        ),
        created_at_utc="2026-10-20T12:00:00+00:00",
    )


def _valid_projection(metric_timeline: Any) -> ValidationReport:
    snapshot = metric_timeline.at_twenty.snapshot
    return ValidationReport(
        is_valid=True,
        projected_operations=snapshot.operations,
        projected_attempts=snapshot.attempts,
        total_tardiness_seconds=19,
        makespan_seconds=23,
    )


def test_proposal_report_labels_forecast_and_never_claims_realized_improvement(
    metric_timeline: Any,
    tmp_path: Path,
) -> None:
    candidate = _current_candidate(metric_timeline)
    report = build_proposal_comparison(
        metric_timeline.initial,
        metric_timeline.at_twenty,
        metric_timeline.events_at_twenty,
        candidate,
        _valid_projection(metric_timeline),
    )

    assert report.run_id == metric_timeline.at_twenty.snapshot.run_id
    assert report.actual_at_snapshot.actual_makespan_seconds is None
    assert report.actual_at_snapshot.predicted_makespan_seconds == 23
    assert report.before.report_side == "before"
    assert report.after.report_side == "after"
    assert report.before.metric_basis == "predicted"
    assert report.after.metric_basis == "predicted"
    assert report.before.snapshot_state_version == report.after.snapshot_state_version
    assert report.before.snapshot_hash == report.after.snapshot_hash
    assert report.before.snapshot_simulated_time_seconds == 20
    assert report.before.plan_revision == 2
    assert report.after.plan_revision == 3
    assert report.before.order_ids == ("order-a", "order-b")
    assert report.after.order_ids == ("order-a", "order-b")
    assert report.before.objective is not None
    assert report.after.objective is not None
    assert report.before.objective.weighted_total_tardiness_seconds == 19
    assert report.after.objective.weighted_total_tardiness_seconds == 19
    assert report.predicted_change is not None
    assert report.predicted_change.basis == "predicted_change"
    assert report.predicted_change.weighted_tardiness_seconds == 0
    assert report.predicted_change.makespan_seconds == 0
    assert report.realization_status == "not_observed"
    assert report.realized_improvement is None
    assert report.before.prediction_window == report.after.prediction_window
    assert report.before.prediction_window.start_seconds == 20
    assert report.before.prediction_window.end_seconds == 30

    paths = export_proposal_reports(tmp_path / "reports", report)
    assert paths.before.name == "proposal_before.json"
    assert paths.after.name == "proposal_after.json"
    assert paths.comparison.name == "proposal_comparison.json"
    before = json.loads(paths.before.read_text(encoding="utf-8"))
    after = json.loads(paths.after.read_text(encoding="utf-8"))
    comparison = json.loads(paths.comparison.read_text(encoding="utf-8"))
    assert before["metric_basis"] == "predicted"
    assert after["metric_basis"] == "predicted"
    assert comparison["predicted_change"]["basis"] == "predicted_change"
    assert comparison["realization_status"] == "not_observed"
    assert comparison["realized_improvement"] is None


def test_proposal_report_rejects_stale_candidate(metric_timeline: Any) -> None:
    candidate = _current_candidate(metric_timeline)
    stale = CandidateProposal.create(
        schema_version=1,
        proposal_id=candidate.proposal_id,
        run_id=candidate.run_id,
        workflow_id=candidate.workflow_id,
        base_state_version=candidate.base_state_version - 1,
        base_snapshot_hash=candidate.base_snapshot_hash,
        proposed_plan_revision=candidate.proposed_plan_revision,
        assignments=candidate.assignments,
        objective=candidate.objective,
    )

    with pytest.raises(ReportConflictError, match="state version is stale"):
        build_proposal_comparison(
            metric_timeline.initial,
            metric_timeline.at_twenty,
            metric_timeline.events_at_twenty,
            stale,
            _valid_projection(metric_timeline),
        )


def test_portable_jsonl_embeds_anchor_envelopes_and_replays_without_solver_or_rng(
    metric_timeline: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "events.jsonl"
    header = export_replay_bundle(
        path,
        metric_timeline.initial,
        metric_timeline.events_at_twenty,
    )
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    assert records[0]["record_type"] == "replay_header"
    assert records[0]["initial_checkpoint"]["snapshot"]["state_version"] == 0
    assert records[0]["initial_checkpoint"]["last_sequence"] == 0
    assert records[0]["event_count"] == len(metric_timeline.events_at_twenty)
    assert records[0]["operational_stream_hash"] == header.operational_stream_hash
    assert all(record["record_type"] == "event" for record in records[1:])
    assert [record["envelope"] for record in records[1:]] == [
        event.model_dump(mode="json") for event in metric_timeline.events_at_twenty
    ]

    bundle = load_replay_bundle(path)
    assert bundle.header == header
    assert bundle.events == metric_timeline.events_at_twenty

    def forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("pure replay must not solve or sample")

    monkeypatch.setattr(scheduling, "solve_schedule", forbidden)
    monkeypatch.setattr(random, "Random", forbidden)
    first = replay_bundle(bundle)
    second = replay_bundle(path)
    assert first == second
    assert first.checkpoint == metric_timeline.at_twenty
    assert first.actual_metrics == header.actual_metrics
    assert first.operational_stream_hash == header.operational_stream_hash
    assert first.final_snapshot_hash == header.final_snapshot_hash
    assert first.actual_metrics_hash == header.actual_metrics_hash


@pytest.mark.parametrize("tamper", ["stream", "truncate", "metrics"])
def test_portable_jsonl_rejects_tampering_or_truncation(
    metric_timeline: Any,
    tmp_path: Path,
    tamper: str,
) -> None:
    path = tmp_path / "events.jsonl"
    export_replay_bundle(path, metric_timeline.initial, metric_timeline.events_at_twenty)
    lines = path.read_text(encoding="utf-8").splitlines()

    if tamper == "stream":
        record = json.loads(lines[1])
        record["envelope"]["mutation_id"] = "mutation-tampered"
        lines[1] = json.dumps(record, separators=(",", ":"))
    elif tamper == "truncate":
        lines.pop()
    else:
        header = json.loads(lines[0])
        header["actual_metrics"]["wip_unit_count"] += 1
        lines[0] = json.dumps(header, separators=(",", ":"))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    with pytest.raises(ReplayBundleError):
        load_replay_bundle(path)
