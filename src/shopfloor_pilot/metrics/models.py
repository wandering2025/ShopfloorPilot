"""Strict, replayable contracts for actual and forecast manufacturing KPIs."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal, Self

from pydantic import Field, model_validator

from shopfloor_pilot.domain.base import (
    Identifier,
    Seconds,
    StrictModel,
    WallClockTimestamp,
)


class MetricBasis(StrEnum):
    """Whether a value is observed, forecast, censored, or unavailable."""

    ACTUAL = "actual"
    PREDICTED = "predicted"
    CENSORED = "censored"
    UNAVAILABLE = "unavailable"


class MetricWindow(StrictModel):
    """A half-open duration window, with state observed after its end boundary."""

    start_seconds: Seconds
    end_seconds: Seconds
    interval_convention: Literal["[start,end)"] = "[start,end)"
    end_boundary_events_included: Literal[True] = True

    @model_validator(mode="after")
    def validate_bounds(self) -> Self:
        if self.end_seconds < self.start_seconds:
            raise ValueError("metric window end cannot precede its start")
        return self

    @property
    def duration_seconds(self) -> int:
        return self.end_seconds - self.start_seconds


class OrderMetrics(StrictModel):
    """Actual outcome and current-plan forecast for one authoritative order."""

    order_id: Identifier
    release_time_seconds: Seconds
    due_time_seconds: Seconds
    priority_weight: int = Field(strict=True, ge=1, le=100)
    quantity: int = Field(strict=True, ge=1)
    actual_basis: Literal[MetricBasis.ACTUAL, MetricBasis.CENSORED]
    actual_completion_seconds: Seconds | None = None
    actual_weighted_tardiness_seconds: Seconds | None = None
    censored_at_seconds: Seconds | None = None
    weighted_tardiness_lower_bound_seconds: Seconds
    prediction_basis: Literal[
        MetricBasis.ACTUAL,
        MetricBasis.PREDICTED,
        MetricBasis.UNAVAILABLE,
    ]
    predicted_completion_seconds: Seconds | None = None
    predicted_weighted_tardiness_seconds: Seconds | None = None

    @model_validator(mode="after")
    def validate_basis_fields(self) -> Self:
        actual_values = (
            self.actual_completion_seconds,
            self.actual_weighted_tardiness_seconds,
        )
        if self.actual_basis is MetricBasis.ACTUAL:
            if None in actual_values or self.censored_at_seconds is not None:
                raise ValueError("actual order outcomes require completion/tardiness only")
        elif any(value is not None for value in actual_values) or self.censored_at_seconds is None:
            raise ValueError("censored order outcomes require only censored_at_seconds")

        predicted_values = (
            self.predicted_completion_seconds,
            self.predicted_weighted_tardiness_seconds,
        )
        if self.prediction_basis is MetricBasis.UNAVAILABLE:
            if any(value is not None for value in predicted_values):
                raise ValueError("unavailable predictions cannot contain predicted values")
        elif None in predicted_values:
            raise ValueError("actual/predicted order forecasts require both predicted values")
        if self.prediction_basis is MetricBasis.ACTUAL and (
            self.predicted_completion_seconds != self.actual_completion_seconds
            or self.predicted_weighted_tardiness_seconds != self.actual_weighted_tardiness_seconds
        ):
            raise ValueError("resolved forecasts must equal their actual order outcome")
        return self


class MachineMetrics(StrictModel):
    """Actual machine-time accounting over the report's common window.

    The utilization denominator is scheduled calendar capacity. Downtime stays in
    that denominator so a failure visibly reduces gross utilization. Interrupted
    execution counts as busy time and is also identified as its wasted subset.
    """

    machine_id: Identifier
    window_duration_seconds: Seconds
    scheduled_capacity_seconds: Seconds
    off_calendar_seconds: Seconds
    busy_processing_seconds: Seconds
    productive_processing_seconds: Seconds
    wasted_processing_seconds: Seconds
    downtime_seconds: Seconds
    capacity_lost_to_downtime_seconds: Seconds
    idle_seconds: Seconds
    utilization_numerator_seconds: Seconds
    utilization_denominator_seconds: Seconds
    utilization_ratio: float | None = Field(default=None, ge=0.0, le=1.0, allow_inf_nan=False)
    productive_utilization_ratio: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        allow_inf_nan=False,
    )

    @model_validator(mode="after")
    def validate_accounting(self) -> Self:
        if self.off_calendar_seconds + self.scheduled_capacity_seconds != (
            self.window_duration_seconds
        ):
            raise ValueError("calendar and off-calendar seconds must cover the metric window")
        if self.productive_processing_seconds + self.wasted_processing_seconds != (
            self.busy_processing_seconds
        ):
            raise ValueError("productive and wasted processing must equal busy processing")
        if (
            self.busy_processing_seconds
            + self.capacity_lost_to_downtime_seconds
            + self.idle_seconds
            != self.scheduled_capacity_seconds
        ):
            raise ValueError("busy, downtime loss, and idle must equal scheduled capacity")
        if self.capacity_lost_to_downtime_seconds > self.downtime_seconds:
            raise ValueError("capacity downtime cannot exceed elapsed downtime")
        if self.utilization_numerator_seconds != self.busy_processing_seconds:
            raise ValueError("utilization numerator must be actual busy processing")
        if self.utilization_denominator_seconds != self.scheduled_capacity_seconds:
            raise ValueError("utilization denominator must be scheduled calendar capacity")
        if self.utilization_denominator_seconds == 0:
            if self.utilization_ratio is not None or self.productive_utilization_ratio is not None:
                raise ValueError("zero-capacity utilization ratios must be null")
        else:
            expected = self.busy_processing_seconds / self.utilization_denominator_seconds
            productive = self.productive_processing_seconds / self.utilization_denominator_seconds
            if self.utilization_ratio != expected:
                raise ValueError("utilization ratio does not match its numerator/denominator")
            if self.productive_utilization_ratio != productive:
                raise ValueError("productive utilization ratio is inconsistent")
        return self


class RecoveryMetrics(StrictModel):
    """One interruption-to-full-restart episode in simulated time."""

    interruption_attempt_id: Identifier
    operation_id: Identifier
    disrupted_machine_id: Identifier
    disruption_event_id: Identifier | None = None
    disruption_seconds: Seconds
    resumed_attempt_id: Identifier | None = None
    resumed_machine_id: Identifier | None = None
    actual_basis: Literal[MetricBasis.ACTUAL, MetricBasis.CENSORED]
    actual_resumption_seconds: Seconds | None = None
    actual_recovery_duration_seconds: Seconds | None = None
    censored_at_seconds: Seconds | None = None
    censored_lower_bound_seconds: Seconds | None = None
    predicted_resumption_seconds: Seconds | None = None
    predicted_recovery_duration_seconds: Seconds | None = None

    @model_validator(mode="after")
    def validate_recovery(self) -> Self:
        if self.actual_basis is MetricBasis.ACTUAL:
            if (
                self.resumed_attempt_id is None
                or self.resumed_machine_id is None
                or self.actual_resumption_seconds is None
                or self.actual_recovery_duration_seconds is None
                or self.censored_at_seconds is not None
                or self.censored_lower_bound_seconds is not None
            ):
                raise ValueError("actual recovery requires a complete resumption identity")
            if self.actual_resumption_seconds - self.disruption_seconds != (
                self.actual_recovery_duration_seconds
            ):
                raise ValueError("actual recovery duration does not match its boundaries")
        else:
            if (
                self.actual_resumption_seconds is not None
                or self.actual_recovery_duration_seconds is not None
                or self.censored_at_seconds is None
                or self.censored_lower_bound_seconds is None
            ):
                raise ValueError("censored recovery requires censoring fields only")
            if self.censored_at_seconds - self.disruption_seconds != (
                self.censored_lower_bound_seconds
            ):
                raise ValueError("censored recovery lower bound does not match its boundaries")
        prediction_values = (
            self.predicted_resumption_seconds,
            self.predicted_recovery_duration_seconds,
        )
        if (prediction_values[0] is None) != (prediction_values[1] is None):
            raise ValueError("predicted recovery fields must be both present or both absent")
        if prediction_values[0] is not None and (
            prediction_values[0] - self.disruption_seconds != prediction_values[1]
        ):
            raise ValueError("predicted recovery duration does not match its boundaries")
        return self


class DecisionLatency(StrictModel):
    """Wall-clock decision latency kept separate from simulated recovery time."""

    start_simulated_seconds: Seconds
    end_simulated_seconds: Seconds
    simulated_elapsed_seconds: Seconds
    started_at_utc: WallClockTimestamp
    completed_at_utc: WallClockTimestamp
    wall_clock_elapsed_ms: int = Field(strict=True, ge=0)

    @model_validator(mode="after")
    def validate_simulated_elapsed(self) -> Self:
        if self.end_simulated_seconds - self.start_simulated_seconds != (
            self.simulated_elapsed_seconds
        ):
            raise ValueError("simulated decision latency does not match its boundaries")
        return self


class RunMetrics(StrictModel):
    """Replayable operational KPIs for one authoritative run prefix."""

    schema_version: Literal[1] = 1
    run_id: Identifier
    state_version: int = Field(strict=True, ge=0)
    plan_revision: int = Field(strict=True, ge=0)
    as_of_seconds: Seconds
    hard_horizon_seconds: int = Field(strict=True, gt=0)
    actual_window: MetricWindow
    prediction_horizon_seconds: int = Field(strict=True, gt=0)
    included_order_ids: tuple[Identifier, ...]
    orders: tuple[OrderMetrics, ...]
    actual_outcome_basis: Literal[MetricBasis.ACTUAL, MetricBasis.CENSORED]
    actual_makespan_seconds: Seconds | None = None
    actual_makespan_lower_bound_seconds: Seconds
    actual_weighted_tardiness_seconds: Seconds | None = None
    completed_order_weighted_tardiness_seconds: Seconds
    weighted_tardiness_lower_bound_seconds: Seconds
    prediction_basis: Literal[
        MetricBasis.ACTUAL,
        MetricBasis.PREDICTED,
        MetricBasis.UNAVAILABLE,
    ]
    predicted_makespan_seconds: Seconds | None = None
    predicted_weighted_tardiness_seconds: Seconds | None = None
    completed_order_count: int = Field(strict=True, ge=0)
    unfinished_order_count: int = Field(strict=True, ge=0)
    throughput_completed_orders: int = Field(strict=True, ge=0)
    throughput_window_seconds: Seconds
    throughput_orders_per_hour: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    wip_order_count: int = Field(strict=True, ge=0)
    wip_unit_count: int = Field(strict=True, ge=0)
    machines: tuple[MachineMetrics, ...]
    wasted_processing_seconds: Seconds
    recoveries: tuple[RecoveryMetrics, ...] = ()

    @model_validator(mode="after")
    def validate_aggregate(self) -> Self:
        if self.actual_window.end_seconds != self.as_of_seconds:
            raise ValueError("actual window must end at the report as-of boundary")
        if self.prediction_horizon_seconds != self.hard_horizon_seconds:
            raise ValueError("S06 prediction horizon is the authoritative hard horizon")
        if self.included_order_ids != tuple(item.order_id for item in self.orders):
            raise ValueError("included_order_ids must match ordered order metrics")
        if self.completed_order_count + self.unfinished_order_count != len(self.orders):
            raise ValueError("completed and unfinished counts must cover included orders")
        if self.actual_outcome_basis is MetricBasis.ACTUAL:
            if (
                self.unfinished_order_count != 0
                or self.actual_makespan_seconds is None
                or self.actual_weighted_tardiness_seconds is None
            ):
                raise ValueError("an actual aggregate requires every order to be complete")
        elif (
            self.unfinished_order_count == 0
            or self.actual_makespan_seconds is not None
            or self.actual_weighted_tardiness_seconds is not None
        ):
            raise ValueError("a censored aggregate cannot expose final actual values")
        predicted = (self.predicted_makespan_seconds, self.predicted_weighted_tardiness_seconds)
        if self.prediction_basis is MetricBasis.UNAVAILABLE:
            if any(value is not None for value in predicted):
                raise ValueError("unavailable aggregate prediction cannot contain values")
        elif None in predicted:
            raise ValueError("available aggregate prediction requires both objective values")
        if self.throughput_window_seconds != self.actual_window.duration_seconds:
            raise ValueError("throughput denominator must equal the common actual window")
        if self.throughput_window_seconds == 0:
            if self.throughput_orders_per_hour is not None:
                raise ValueError("zero-duration throughput rate must be null")
        elif self.throughput_orders_per_hour != (
            self.throughput_completed_orders * 3600 / self.throughput_window_seconds
        ):
            raise ValueError("throughput rate does not match count/window")
        if self.wasted_processing_seconds != sum(
            item.wasted_processing_seconds for item in self.machines
        ):
            raise ValueError("total wasted processing must equal per-machine waste")
        return self
