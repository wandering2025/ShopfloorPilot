"""Pure tabular and Plotly projections for the baseline dashboard."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

import plotly.graph_objects as go

from shopfloor_pilot.domain import (
    AttemptStatus,
    CandidateProposal,
    OperationalSnapshot,
    OperationStatus,
)
from shopfloor_pilot.metrics import RunMetrics

_COLORS = {
    "Scheduled": "#64748b",
    "Running": "#0ea5e9",
    "Completed": "#10b981",
    "Interrupted": "#f97316",
    "Changed assignment": "#e11d48",
    "Unchanged assignment": "#2563eb",
    "New assignment": "#8b5cf6",
}


def machine_rows(snapshot: OperationalSnapshot) -> list[dict[str, Any]]:
    running_by_machine = {
        item.machine_id: item.attempt_id
        for item in snapshot.attempts
        if item.status is AttemptStatus.RUNNING
    }
    return [
        {
            "Machine": item.machine_id,
            "Name": item.display_name,
            "Status": item.status.value,
            "Capabilities": ", ".join(capability.value for capability in item.capabilities),
            "Running attempt": running_by_machine.get(item.machine_id, "—"),
        }
        for item in snapshot.machines
    ]


def order_rows(snapshot: OperationalSnapshot) -> list[dict[str, Any]]:
    operations_by_order: dict[str, list[Any]] = defaultdict(list)
    for operation in snapshot.operations:
        operations_by_order[operation.order_id].append(operation)
    rows: list[dict[str, Any]] = []
    for order in snapshot.orders:
        operations = operations_by_order[order.order_id]
        completed = sum(item.status is OperationStatus.COMPLETED for item in operations)
        rows.append(
            {
                "Order": order.order_id,
                "Product": order.product_id,
                "Qty": order.quantity,
                "Status": order.status.value,
                "Release (s)": order.release_time_seconds,
                "Due (s)": order.due_time_seconds,
                "Priority": order.priority_weight,
                "Operations": f"{completed}/{len(operations)}" if operations else "not planned",
            }
        )
    return rows


def operation_rows(snapshot: OperationalSnapshot) -> list[dict[str, Any]]:
    attempts_by_operation: dict[str, list[Any]] = defaultdict(list)
    for attempt in snapshot.attempts:
        attempts_by_operation[attempt.operation_id].append(attempt)
    rows: list[dict[str, Any]] = []
    for operation in snapshot.operations:
        attempts = attempts_by_operation[operation.operation_id]
        latest = attempts[-1] if attempts else None
        rows.append(
            {
                "Operation": operation.operation_id,
                "Order": operation.order_id,
                "Step": operation.sequence_index + 1,
                "Capability": operation.required_capability.value,
                "Status": operation.status.value,
                "Machine": "—" if latest is None else latest.machine_id,
                "Attempt": "—" if latest is None else latest.attempt_id,
                "Start (s)": None
                if latest is None or latest.actual_start_seconds is None
                else latest.actual_start_seconds,
                "End (s)": None
                if latest is None or latest.actual_end_seconds is None
                else latest.actual_end_seconds,
            }
        )
    return rows


def event_rows(events: tuple[Any, ...]) -> list[dict[str, Any]]:
    return [
        {
            "Seq": item.event.sequence,
            "Time (s)": item.event.simulated_time_seconds,
            "Type": item.event.event_type.value,
            "Mutation": item.mutation_id,
            "Version": item.resulting_state_version,
        }
        for item in events
    ]


def machine_metric_rows(metrics: RunMetrics) -> list[dict[str, Any]]:
    return [
        {
            "Machine": item.machine_id,
            "Busy numerator (s)": item.utilization_numerator_seconds,
            "Calendar denominator (s)": item.utilization_denominator_seconds,
            "Gross utilization": None
            if item.utilization_ratio is None
            else round(item.utilization_ratio, 4),
            "Productive utilization": None
            if item.productive_utilization_ratio is None
            else round(item.productive_utilization_ratio, 4),
            "Downtime (s)": item.downtime_seconds,
            "Wasted (s)": item.wasted_processing_seconds,
            "Idle (s)": item.idle_seconds,
        }
        for item in metrics.machines
    ]


def proposal_change_rows(
    snapshot: OperationalSnapshot,
    candidate: CandidateProposal,
) -> list[dict[str, Any]]:
    current = {
        item.operation_id: item
        for item in snapshot.attempts
        if item.scheduled_window is not None and item.status is not AttemptStatus.CANCELLED
    }
    rows: list[dict[str, Any]] = []
    for assignment in candidate.assignments:
        prior = current.get(assignment.operation_id)
        changed = prior is None or (
            prior.machine_id != assignment.machine_id or prior.scheduled_window != assignment.window
        )
        rows.append(
            {
                "Operation": assignment.operation_id,
                "Old machine": "—" if prior is None else prior.machine_id,
                "New machine": assignment.machine_id,
                "Old window": "—"
                if prior is None or prior.scheduled_window is None
                else (
                    f"{prior.scheduled_window.start_seconds}–{prior.scheduled_window.end_seconds}"
                ),
                "New window": (
                    f"{assignment.window.start_seconds}–{assignment.window.end_seconds}"
                ),
                "Changed": "yes" if changed else "no",
            }
        )
    return rows


def future_attempt_options(snapshot: OperationalSnapshot) -> list[dict[str, Any]]:
    options: list[dict[str, Any]] = []
    now = snapshot.simulated_time_seconds
    for attempt in snapshot.attempts:
        window = attempt.scheduled_window
        if window is None or window.end_seconds <= now:
            continue
        start = max(now + 1, window.start_seconds)
        if start >= window.end_seconds:
            continue
        midpoint = start + max(1, (window.end_seconds - start) // 2)
        midpoint = min(midpoint, window.end_seconds - 1)
        options.append(
            {
                "label": (
                    f"{attempt.machine_id} · {attempt.operation_id} · "
                    f"{window.start_seconds}–{window.end_seconds} s"
                ),
                "machine_id": attempt.machine_id,
                "attempt_id": attempt.attempt_id,
                "operation_id": attempt.operation_id,
                "suggested_fault_seconds": midpoint,
                "window_end_seconds": window.end_seconds,
            }
        )
    return sorted(options, key=lambda item: (item["suggested_fault_seconds"], item["label"]))


def build_gantt_figure(
    snapshot: OperationalSnapshot,
    candidate: CandidateProposal | None = None,
) -> go.Figure:
    """Show numeric simulated seconds, separating prediction from actual history."""

    bars: dict[str, list[dict[str, Any]]] = defaultdict(list)
    lanes: set[str] = set()

    if candidate is None:
        for attempt in snapshot.attempts:
            if (
                attempt.scheduled_window is not None
                and attempt.status is not AttemptStatus.CANCELLED
            ):
                lane = f"Plan · {attempt.machine_id}"
                lanes.add(lane)
                bars["Scheduled"].append(
                    _bar(
                        lane,
                        attempt.scheduled_window.start_seconds,
                        attempt.scheduled_window.end_seconds,
                        attempt.operation_id,
                        attempt.attempt_id,
                        attempt.machine_id,
                    )
                )
            if attempt.actual_start_seconds is not None:
                end = attempt.actual_end_seconds
                if end is None:
                    end = snapshot.simulated_time_seconds
                if end > attempt.actual_start_seconds:
                    lane = f"Actual · {attempt.machine_id}"
                    lanes.add(lane)
                    label = {
                        AttemptStatus.COMPLETED: "Completed",
                        AttemptStatus.RUNNING: "Running",
                        AttemptStatus.INTERRUPTED: "Interrupted",
                    }.get(attempt.status, attempt.status.value.title())
                    bars[label].append(
                        _bar(
                            lane,
                            attempt.actual_start_seconds,
                            end,
                            attempt.operation_id,
                            attempt.attempt_id,
                            attempt.machine_id,
                        )
                    )
        title = "Applied plan versus actual execution"
        empty_message = "No applied plan yet — generate, approve, and apply a proposal."
    else:
        current = {
            item.operation_id: item
            for item in snapshot.attempts
            if item.scheduled_window is not None and item.status is not AttemptStatus.CANCELLED
        }
        for assignment in candidate.assignments:
            prior = current.get(assignment.operation_id)
            if prior is None:
                label = "New assignment"
            elif (
                prior.machine_id != assignment.machine_id
                or prior.scheduled_window != assignment.window
            ):
                label = "Changed assignment"
            else:
                label = "Unchanged assignment"
            lane = f"Proposed · {assignment.machine_id}"
            lanes.add(lane)
            bars[label].append(
                _bar(
                    lane,
                    assignment.window.start_seconds,
                    assignment.window.end_seconds,
                    assignment.operation_id,
                    assignment.attempt_id,
                    assignment.machine_id,
                )
            )
        for attempt in snapshot.attempts:
            if attempt.actual_start_seconds is None:
                continue
            end = attempt.actual_end_seconds or snapshot.simulated_time_seconds
            if end <= attempt.actual_start_seconds:
                continue
            lane = f"Actual history · {attempt.machine_id}"
            lanes.add(lane)
            label = "Completed" if attempt.status is AttemptStatus.COMPLETED else "Interrupted"
            bars[label].append(
                _bar(
                    lane,
                    attempt.actual_start_seconds,
                    end,
                    attempt.operation_id,
                    attempt.attempt_id,
                    attempt.machine_id,
                )
            )
        title = "Proposed schedule and preserved actual history"
        empty_message = "The proposal contains no remaining assignments."

    figure = go.Figure()
    for label, items in bars.items():
        figure.add_trace(
            go.Bar(
                name=label,
                orientation="h",
                y=[item["lane"] for item in items],
                x=[item["duration"] for item in items],
                base=[item["start"] for item in items],
                marker={"color": _COLORS.get(label, "#64748b")},
                customdata=[
                    [
                        item["operation"],
                        item["attempt"],
                        item["machine"],
                        item["start"],
                        item["end"],
                    ]
                    for item in items
                ],
                hovertemplate=(
                    "Operation %{customdata[0]}<br>Attempt %{customdata[1]}<br>"
                    "Machine %{customdata[2]}<br>%{customdata[3]}–%{customdata[4]} s"
                    "<extra>%{fullData.name}</extra>"
                ),
            )
        )
    if not bars:
        figure.add_annotation(
            text=empty_message,
            x=0.5,
            y=0.5,
            xref="paper",
            yref="paper",
            showarrow=False,
            font={"size": 15, "color": "#64748b"},
        )
    figure.add_vline(
        x=snapshot.simulated_time_seconds,
        line_dash="dash",
        line_color="#f59e0b",
        annotation_text=f"Now {snapshot.simulated_time_seconds} s",
        annotation_position="top",
    )
    figure.update_layout(
        title=title,
        barmode="overlay",
        height=max(360, 170 + 42 * len(lanes)),
        margin={"l": 20, "r": 20, "t": 60, "b": 30},
        legend={"orientation": "h", "y": 1.08, "x": 0},
        xaxis={
            "title": "Simulated time (seconds)",
            "range": [0, snapshot.hard_horizon_seconds],
            "rangemode": "tozero",
            "showgrid": True,
        },
        yaxis={"title": "Machine / basis", "automargin": True},
        hoverlabel={"namelength": -1},
    )
    return figure


def _bar(
    lane: str,
    start: int,
    end: int,
    operation: str,
    attempt: str,
    machine: str,
) -> dict[str, Any]:
    return {
        "lane": lane,
        "start": start,
        "end": end,
        "duration": end - start,
        "operation": operation,
        "attempt": attempt,
        "machine": machine,
    }


__all__ = [
    "build_gantt_figure",
    "event_rows",
    "future_attempt_options",
    "machine_metric_rows",
    "machine_rows",
    "operation_rows",
    "order_rows",
    "proposal_change_rows",
]
