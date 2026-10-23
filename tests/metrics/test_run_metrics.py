"""Hand-calculated S06 KPI acceptance tests."""

from __future__ import annotations

from typing import Any

import pytest

from shopfloor_pilot.metrics import (
    MetricBasis,
    MetricsInputError,
    calculate_decision_latency,
    calculate_run_metrics,
)


def test_unfinished_run_separates_actual_censoring_from_plan_prediction(
    metric_timeline: Any,
) -> None:
    metrics = calculate_run_metrics(
        metric_timeline.initial,
        metric_timeline.events_at_twenty,
    )

    assert metrics.run_id == "run-s06-metrics"
    assert metrics.as_of_seconds == 20
    assert metrics.actual_window.start_seconds == 0
    assert metrics.actual_window.end_seconds == 20
    assert metrics.actual_window.interval_convention == "[start,end)"
    assert metrics.actual_window.end_boundary_events_included is True
    assert metrics.prediction_horizon_seconds == 30

    assert metrics.actual_outcome_basis is MetricBasis.CENSORED
    assert metrics.actual_makespan_seconds is None
    assert metrics.actual_makespan_lower_bound_seconds == 20
    assert metrics.actual_weighted_tardiness_seconds is None
    assert metrics.completed_order_weighted_tardiness_seconds == 4
    assert metrics.weighted_tardiness_lower_bound_seconds == 10

    assert metrics.prediction_basis is MetricBasis.PREDICTED
    assert metrics.predicted_makespan_seconds == 23
    assert metrics.predicted_weighted_tardiness_seconds == 19
    orders = {item.order_id: item for item in metrics.orders}
    assert orders["order-a"].actual_basis is MetricBasis.ACTUAL
    assert orders["order-a"].actual_completion_seconds == 10
    assert orders["order-a"].actual_weighted_tardiness_seconds == 4
    assert orders["order-a"].prediction_basis is MetricBasis.ACTUAL
    assert orders["order-b"].actual_basis is MetricBasis.CENSORED
    assert orders["order-b"].censored_at_seconds == 20
    assert orders["order-b"].actual_completion_seconds is None
    assert orders["order-b"].weighted_tardiness_lower_bound_seconds == 6
    assert orders["order-b"].prediction_basis is MetricBasis.PREDICTED
    assert orders["order-b"].predicted_completion_seconds == 23
    assert orders["order-b"].predicted_weighted_tardiness_seconds == 15

    assert metrics.completed_order_count == 1
    assert metrics.unfinished_order_count == 1
    assert metrics.throughput_completed_orders == 1
    assert metrics.throughput_window_seconds == 20
    assert metrics.throughput_orders_per_hour == 180.0
    assert metrics.wip_order_count == 1
    assert metrics.wip_unit_count == 1


def test_machine_accounting_exposes_denominator_idle_downtime_and_waste(
    metric_timeline: Any,
) -> None:
    metrics = calculate_run_metrics(
        metric_timeline.initial,
        metric_timeline.events_at_twenty,
    )
    machine = metrics.machines[0]

    assert machine.machine_id == "mill"
    assert machine.window_duration_seconds == 20
    assert machine.scheduled_capacity_seconds == 20
    assert machine.off_calendar_seconds == 0
    assert machine.busy_processing_seconds == 15
    assert machine.productive_processing_seconds == 12
    assert machine.wasted_processing_seconds == 3
    assert machine.downtime_seconds == 3
    assert machine.capacity_lost_to_downtime_seconds == 3
    assert machine.idle_seconds == 2
    assert machine.utilization_numerator_seconds == 15
    assert machine.utilization_denominator_seconds == 20
    assert machine.utilization_ratio == 0.75
    assert machine.productive_utilization_ratio == 0.6
    assert metrics.wasted_processing_seconds == 3
    assert (
        machine.busy_processing_seconds
        + machine.capacity_lost_to_downtime_seconds
        + machine.idle_seconds
        == machine.scheduled_capacity_seconds
    )


def test_recovery_is_simulated_time_and_wall_clock_latency_is_separate(
    metric_timeline: Any,
) -> None:
    metrics = calculate_run_metrics(
        metric_timeline.initial,
        metric_timeline.events_at_twenty,
    )

    assert len(metrics.recoveries) == 1
    recovery = metrics.recoveries[0]
    assert recovery.interruption_attempt_id == "attempt-b-1"
    assert recovery.resumed_attempt_id == "attempt-b-2"
    assert recovery.disruption_event_id is not None
    assert recovery.disruption_seconds == 15
    assert recovery.actual_basis is MetricBasis.ACTUAL
    assert recovery.actual_resumption_seconds == 18
    assert recovery.actual_recovery_duration_seconds == 3
    assert recovery.predicted_recovery_duration_seconds is None

    latency = calculate_decision_latency(
        start_simulated_seconds=15,
        end_simulated_seconds=15,
        started_at_utc="2026-10-20T12:00:00+00:00",
        completed_at_utc="2026-10-20T12:00:02.500000+00:00",
    )
    assert latency.simulated_elapsed_seconds == 0
    assert latency.wall_clock_elapsed_ms == 2500


def test_fault_boundary_is_censored_and_has_no_invented_forecast(
    metric_timeline: Any,
) -> None:
    metrics = calculate_run_metrics(
        metric_timeline.initial,
        metric_timeline.events_at_fault,
    )

    assert metrics.as_of_seconds == 15
    assert metrics.actual_outcome_basis is MetricBasis.CENSORED
    assert metrics.prediction_basis is MetricBasis.UNAVAILABLE
    assert metrics.predicted_makespan_seconds is None
    assert metrics.predicted_weighted_tardiness_seconds is None
    assert len(metrics.recoveries) == 1
    recovery = metrics.recoveries[0]
    assert recovery.actual_basis is MetricBasis.CENSORED
    assert recovery.censored_at_seconds == 15
    assert recovery.censored_lower_bound_seconds == 0
    assert recovery.predicted_resumption_seconds is None
    assert recovery.predicted_recovery_duration_seconds is None


def test_completed_run_exposes_exact_actual_outcome(metric_timeline: Any) -> None:
    metrics = calculate_run_metrics(
        metric_timeline.initial,
        metric_timeline.events_complete,
    )

    assert metrics.as_of_seconds == 23
    assert metrics.actual_outcome_basis is MetricBasis.ACTUAL
    assert metrics.actual_makespan_seconds == 23
    assert metrics.actual_makespan_lower_bound_seconds == 23
    assert metrics.actual_weighted_tardiness_seconds == 19
    assert metrics.completed_order_weighted_tardiness_seconds == 19
    assert metrics.weighted_tardiness_lower_bound_seconds == 19
    assert metrics.prediction_basis is MetricBasis.ACTUAL
    assert metrics.predicted_makespan_seconds == 23
    assert metrics.predicted_weighted_tardiness_seconds == 19
    assert metrics.completed_order_count == 2
    assert metrics.unfinished_order_count == 0
    assert metrics.wip_order_count == 0
    assert metrics.wip_unit_count == 0
    assert metrics.throughput_completed_orders == 2
    assert metrics.throughput_window_seconds == 23
    assert metrics.throughput_orders_per_hour == 2 * 3600 / 23

    machine = metrics.machines[0]
    assert machine.scheduled_capacity_seconds == 23
    assert machine.productive_processing_seconds == 15
    assert machine.wasted_processing_seconds == 3
    assert machine.busy_processing_seconds == 18
    assert machine.capacity_lost_to_downtime_seconds == 3
    assert machine.idle_seconds == 2


@pytest.mark.parametrize(
    ("started", "completed"),
    [
        ("2026-10-20T12:00:00", "2026-10-20T12:00:01+00:00"),
        ("2026-10-20T12:00:02+00:00", "2026-10-20T12:00:01+00:00"),
        (
            "2026-10-20T12:00:01.000500+00:00",
            "2026-10-20T12:00:01+00:00",
        ),
    ],
)
def test_decision_latency_rejects_ambiguous_or_reversed_wall_clock(
    started: str,
    completed: str,
) -> None:
    with pytest.raises(MetricsInputError):
        calculate_decision_latency(
            start_simulated_seconds=15,
            end_simulated_seconds=15,
            started_at_utc=started,
            completed_at_utc=completed,
        )
