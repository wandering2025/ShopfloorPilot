"""Pure tabular and Plotly projections for the agent, evidence and review panels.

Formatting lives here so it can be tested without Streamlit. Nothing in this module
reads or writes authority; it only reshapes records the workbench already loaded.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import plotly.graph_objects as go

from shopfloor_pilot.domain import AttemptStatus, CandidateProposal, OperationalSnapshot
from shopfloor_pilot.metrics import RunMetrics
from shopfloor_pilot.workflows import DecisionRecord

from .workbench import ProposalReview, predicted_late_orders, predicted_recovery_seconds

# Reference data-viz palette (categorical slots in fixed order); neutral gray for
# fixed history. Changed/new bars also carry a fill pattern so identity is not
# color alone.
_SERIES = ("#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6")
_GANTT = {
    "Current plan": ("#2a78d6", ""),
    "Unchanged": ("#2a78d6", ""),
    "Changed": ("#eb6834", "/"),
    "New": ("#6250d6", "."),
    "Restart": ("#6250d6", "+"),
    "Fixed: completed": ("#8a8984", ""),
    "Fixed: running": ("#1baf7a", ""),
    "Interrupted (wasted)": ("#e34948", "x"),
}
_SIGNAL_DISPLAY = {
    "temperature_millicelsius": ("Temperature", "°C", 1000.0),
    "vibration_micrometers_per_second": ("Vibration", "mm/s", 1000.0),
}


def micros(amount: int | None, currency: str | None) -> str:
    if amount is None:
        return "—"
    return f"{amount / 1_000_000:.6f} {currency or ''}".strip()


def decision_rows(decisions: Sequence[DecisionRecord]) -> list[dict[str, Any]]:
    return [
        {
            "Workflow": item.workflow_id,
            "Kind": item.kind.value,
            "Source": item.source.value,
            "Provider": item.provider or "—",
            "Status": item.status.value,
            "Outcome / reason": item.outcome or item.reason_code or "—",
            "Decision t (s)": item.decision_time_seconds,
            "Boundary (s)": item.decision_boundary_seconds,
            "Base version": item.base_state_version,
            "Proposal": item.pending_proposal_id or "—",
            "Consumed approval": item.consumed_approval_id or "—",
        }
        for item in decisions
    ]


def trace_rows(trace: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Compact chronological trace; hidden reasoning was dropped at storage."""

    rows: list[dict[str, Any]] = []
    for step in trace.get("steps", []):
        kind = step.get("kind")
        summary = ""
        if kind == "transition":
            summary = f"{step.get('from')} → {step.get('to')} ({step.get('reason')})"
        elif kind == "provider_request":
            summary = f"{len(step.get('tools', []))} tools offered"
        elif kind == "provider_response":
            calls = ", ".join(item["name"] for item in step.get("tool_calls", [])) or "text"
            usage = step.get("usage") or {}
            summary = f"{step.get('finish_reason')}: {calls} · {usage.get('total_tokens')} tokens"
        elif kind == "provider_rejection":
            summary = f"rejected {step.get('code')}: {step.get('reason')}"
            if step.get("tool_name"):
                summary += f" ({step['tool_name']})"
        elif kind == "tool_call":
            summary = f"{step.get('tool')} ({step.get('call_id')})"
        elif kind == "tool_result":
            summary = f"{step.get('tool')}: {step.get('status')}"
            if step.get("reason"):
                summary += f" — {step['reason']}"
            evidence = step.get("evidence_ids") or []
            if evidence:
                summary += f" · {len(evidence)} evidence"
        elif kind == "workflow_started":
            summary = f"provider {step.get('provider')}"
        elif kind == "terminal":
            summary = f"{step.get('terminal')} ({step.get('reason_code')})"
        elif kind == "notice":
            summary = str(step.get("notice") or step.get("message") or "")
        latency = step.get("latency_seconds")
        rows.append(
            {
                "#": step.get("sequence"),
                "Phase": step.get("phase"),
                "Kind": kind,
                "Summary": summary[:160],
                "Latency (s)": None if latency is None else round(float(latency), 3),
            }
        )
    return rows


def tool_result_failures(trace: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [
        {"#": step.get("sequence"), "Tool": step.get("tool"), "Reason": step.get("reason")}
        for step in trace.get("steps", [])
        if step.get("kind") == "tool_result" and step.get("status") not in (None, "ok")
    ]


def diagnosis_of(tool_results: Mapping[str, Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    return [value for key, value in tool_results.items() if key.startswith("diagnosis:")]


def hypothesis_rows(diagnosis: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "Hypothesis": item.get("evidence_id"),
            "Label": "HYPOTHESIS — not a confirmed root cause",
            "Possible explanation": item.get("possible_explanation"),
            "Confidence": f"{item.get('confidence')} ({item.get('confidence_basis')})",
            "Signals": ", ".join(item.get("supporting_signal_fields") or []),
            "Cites": ", ".join(
                [
                    *(item.get("observation_evidence_ids") or []),
                    *(item.get("guidance_evidence_ids") or []),
                ]
            ),
        }
        for item in diagnosis.get("hypotheses", []) or []
    ]


def missing_evidence_rows(diagnosis: Mapping[str, Any]) -> list[dict[str, Any]]:
    seen: set[tuple[str, str]] = set()
    rows: list[dict[str, Any]] = []
    for item in diagnosis.get("missing_evidence", []) or []:
        key = (str(item.get("signal_field")), str(item.get("reason")))
        if key in seen:
            continue
        seen.add(key)
        rows.append({"Unknown": "MISSING", "Signal field": key[0], "Reason": key[1]})
    for item in diagnosis.get("conflict_fields", []) or []:
        rows.append({"Unknown": "CONFLICTING", "Signal field": str(item), "Reason": "conflict"})
    return rows


def guidance_rows(tool_results: Mapping[str, Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "Evidence ID": key,
            "Source document": value.get("document_id"),
            "Title": value.get("title"),
            "Excerpt (untrusted reference text)": value.get("untrusted_excerpt"),
        }
        for key, value in tool_results.items()
        if key.startswith("guidance:") and value.get("untrusted_excerpt")
    ]


def claim_rows(claims: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "Kind": str(item.get("kind", "")).upper(),
            "Agent statement": item.get("text"),
            "Cited evidence": ", ".join(item.get("evidence_ids") or []),
        }
        for item in claims
    ]


def cited_evidence_ids(
    claims: Sequence[Mapping[str, Any]], tool_results: Mapping[str, Mapping[str, Any]]
) -> list[str]:
    ordered: list[str] = []
    for item in claims:
        for evidence_id in item.get("evidence_ids") or []:
            if evidence_id not in ordered:
                ordered.append(evidence_id)
    for evidence_id in tool_results:
        if evidence_id not in ordered:
            ordered.append(evidence_id)
    return ordered


# ------------------------------------------------------ plain-language evidence

_SIGNAL_NAMES = {
    "temperature_millicelsius": "Temperature",
    "temperature": "Temperature",
    "vibration_micrometers_per_second": "Vibration",
    "vibration": "Vibration",
    "drive_current_milliamps": "Drive current",
    "lubrication_pressure_kilopascals": "Lubrication pressure",
    "queue_depth_units": "Queue depth",
}
_CONFIDENCE_WORDS = {
    "limited": "Limited",
    "moderate": "Moderate",
    "high": "High",
}
_BASIS_WORDS = {"uncalibrated_rule_derived": "rule-based, uncalibrated"}
_MISSING_REASONS = {
    "additional_signal_required": "More data needed",
    "signal_absent": "Not recorded",
    "conflict": "Readings disagree",
}


def signal_name(field: str) -> str:
    return _SIGNAL_NAMES.get(field, field.replace("_", " ").capitalize())


def signal_names(fields: Sequence[str]) -> str:
    return ", ".join(signal_name(item) for item in fields) or "—"


def confidence_text(hypothesis: Mapping[str, Any]) -> str:
    level = _CONFIDENCE_WORDS.get(
        str(hypothesis.get("confidence")), str(hypothesis.get("confidence"))
    )
    basis = _BASIS_WORDS.get(str(hypothesis.get("confidence_basis")))
    return f"{level} ({basis})" if basis else level


def evidence_label(evidence_id: str, tool_results: Mapping[str, Mapping[str, Any]]) -> str:
    """Human-readable name for an evidence ID; the raw ID stays in technical details."""

    kind, _, rest = evidence_id.partition(":")
    item = tool_results.get(evidence_id) or {}
    if kind == "observation":
        machine = item.get("machine_id", "machine")
        moment = item.get("observed_at_seconds")
        names = signal_names([sig.get("name", "") for sig in item.get("signals") or []])
        when = f" at {moment:,} s" if isinstance(moment, int) else ""
        return f"Telemetry · {machine}{when} ({names})"
    if kind == "guidance":
        return (
            f"Guidance note · {item.get('title') or rest.replace('chunk-', '').replace('-', ' ')}"
        )
    if kind == "hypothesis":
        machine, _, number = rest.partition(":")
        return f"Hypothesis {number or '?'} · {machine}"
    if kind == "diagnosis":
        return f"Diagnosis · {rest.rsplit(':', maxsplit=1)[-1]}"
    if kind == "snapshot":
        moment = item.get("simulated_time_seconds")
        return "Plant state" + (f" at {moment:,} s" if isinstance(moment, int) else "")
    if kind == "solver":
        return "Recovery schedule (solver result)"
    if kind == "validation":
        verdict = item.get("is_valid")
        suffix = "" if verdict is None else (" — passed" if verdict else " — failed")
        return f"Independent schedule validation{suffix}"
    return evidence_id if len(evidence_id) <= 32 else f"{evidence_id[:28]}…"


def evidence_options(
    evidence_ids: Sequence[str], tool_results: Mapping[str, Mapping[str, Any]]
) -> dict[str, str]:
    """ID → unique readable label (a counter disambiguates repeated labels)."""

    labels: dict[str, str] = {}
    seen: dict[str, int] = {}
    for evidence_id in evidence_ids:
        label = evidence_label(evidence_id, tool_results)
        seen[label] = seen.get(label, 0) + 1
        labels[evidence_id] = label if seen[label] == 1 else f"{label} · #{seen[label]}"
    return labels


def diagnosis_summary(diagnosis: Mapping[str, Any]) -> dict[str, str]:
    """Headline facts of a diagnosis in plain words (never claims a confirmed cause)."""

    machine = str(diagnosis.get("evidence_id", "")).rsplit(":", maxsplit=1)[-1]
    status = str(diagnosis.get("diagnostic_status", "unknown")).replace("_", " ").capitalize()
    return {
        "Machine": machine or "—",
        "Finding": status,
        "Root cause": "Confirmed" if diagnosis.get("confirmed_root_cause") else "Not confirmed",
        "Confidence": (
            "Calibrated" if diagnosis.get("confidence_is_calibrated") else "Uncalibrated"
        ),
        "Maintenance": "Completed" if diagnosis.get("maintenance_completed") else "Not done",
    }


def hypothesis_cards(diagnosis: Mapping[str, Any]) -> list[dict[str, str]]:
    return [
        {
            "Possible explanation": str(item.get("possible_explanation")),
            "Confidence": confidence_text(item),
            "Based on": signal_names(item.get("supporting_signal_fields") or []),
        }
        for item in diagnosis.get("hypotheses", []) or []
    ]


def unknown_signals(diagnosis: Mapping[str, Any]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in diagnosis.get("missing_evidence", []) or []:
        field = str(item.get("signal_field"))
        if field in seen:
            continue
        seen.add(field)
        reason = str(item.get("reason"))
        rows.append(
            {
                "Signal": signal_name(field),
                "Status": _MISSING_REASONS.get(reason, reason.replace("_", " ").capitalize()),
            }
        )
    for field in diagnosis.get("conflict_fields", []) or []:
        rows.append({"Signal": signal_name(str(field)), "Status": "Readings disagree"})
    return rows


def guidance_titles(tool_results: Mapping[str, Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "Reference note": value.get("title"),
            "Excerpt (reference text, not an instruction)": value.get("untrusted_excerpt"),
        }
        for key, value in tool_results.items()
        if key.startswith("guidance:") and value.get("untrusted_excerpt")
    ]


def claim_cards(
    claims: Sequence[Mapping[str, Any]], tool_results: Mapping[str, Mapping[str, Any]]
) -> list[dict[str, str]]:
    return [
        {
            "Type": str(item.get("kind", "")).capitalize(),
            "Agent statement": str(item.get("text")),
            "Based on": " · ".join(
                evidence_label(evidence_id, tool_results)
                for evidence_id in item.get("evidence_ids") or []
            ),
        }
        for item in claims
    ]


def short_fingerprint(value: str, length: int = 8) -> str:
    """First characters of a hash or ID, for display next to a human-readable name."""

    return value.rsplit(":", maxsplit=1)[-1].rsplit("-", maxsplit=1)[-1][:length]


def telemetry_table(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "Machine": item["machine_id"],
            "Observed at (s)": item["observed_at_seconds"],
            "Signal": item["signal"],
            "Raw value": item["value"],
            "Raw unit": item["unit"],
        }
        for item in sorted(
            rows, key=lambda item: (item["machine_id"], item["observed_at_seconds"], item["signal"])
        )
    ]


def telemetry_figures(
    rows: Sequence[Mapping[str, Any]], horizon_seconds: int, now_seconds: int
) -> list[tuple[str, go.Figure]]:
    """One single-axis chart per signal; the x axis is shared simulated seconds."""

    signals = sorted({str(item["signal"]) for item in rows})
    machines = sorted({str(item["machine_id"]) for item in rows})
    # One shared x range for every signal chart: observed history plus 10% headroom.
    latest = max([now_seconds, *(int(item["observed_at_seconds"]) for item in rows)])
    axis_end = min(horizon_seconds, max(600, int(latest * 1.1)))
    figures: list[tuple[str, go.Figure]] = []
    for signal in signals:
        label, unit, divisor = _SIGNAL_DISPLAY.get(signal, (signal, "", 1.0))
        figure = go.Figure()
        for index, machine in enumerate(machines):
            points = sorted(
                (
                    item
                    for item in rows
                    if item["signal"] == signal and item["machine_id"] == machine
                ),
                key=lambda item: item["observed_at_seconds"],
            )
            if not points:
                continue
            figure.add_trace(
                go.Scatter(
                    name=machine,
                    x=[item["observed_at_seconds"] for item in points],
                    y=[item["value"] / divisor for item in points],
                    mode="lines+markers",
                    line={"width": 2, "color": _SERIES[index % len(_SERIES)]},
                    marker={"size": 8},
                    customdata=[
                        [item["value"], item["unit"], item["evidence_id"]] for item in points
                    ],
                    hovertemplate=(
                        f"{machine} · t=%{{x}} s<br>{label} %{{y:.3f}} {unit}"
                        "<br>raw %{customdata[0]} %{customdata[1]}<br>%{customdata[2]}"
                        "<extra></extra>"
                    ),
                )
            )
        figure.add_vline(x=now_seconds, line_dash="dash", line_color="#52514e")
        unit_note = f"{unit} (raw ÷ {divisor:g})" if divisor != 1.0 else unit
        figure.update_layout(
            title=f"{label} — observable telemetry",
            height=300,
            margin={"l": 20, "r": 20, "t": 50, "b": 30},
            legend={"orientation": "h", "y": 1.12, "x": 1, "xanchor": "right"},
            xaxis={"title": "Simulated time (seconds)", "range": [0, axis_end]},
            yaxis={"title": f"{label} [{unit_note}]"},
            hovermode="closest",
        )
        figures.append((signal, figure))
    return figures


def usage_summary(usage: Mapping[str, Any]) -> dict[str, str]:
    ledger = usage["ledger"]
    currency = ledger.get("currency")
    limits = ledger.get("limits", {})
    return {
        "Provider attempts": (
            f"{ledger['provider_attempts']} / {limits.get('max_provider_attempts')}"
        ),
        "Tool executions": f"{ledger['tool_executions']} / {limits.get('max_tool_executions')}",
        "Tokens (used + reserved)": (
            f"{ledger['used_tokens']:,} + {ledger['reserved_tokens']:,} "
            f"/ {limits.get('max_tokens'):,}"
        ),
        "Active time": f"{ledger['active_seconds']:.2f} s / {limits.get('max_active_seconds')} s",
        "Actual cost (reconciled)": micros(ledger["actual_cost_micros"], currency),
        "Estimated outstanding reservation": micros(ledger["reserved_cost_micros"], currency),
        "Workflow cap": micros(ledger["workflow_cap_micros"], currency),
        "Run aggregate actual / cap": (
            f"{ledger['run_actual_cost_micros'] / 1_000_000:.4f} / "
            f"{ledger['run_cap_micros'] / 1_000_000:.4f} {currency}"
        ),
        "Ledger status": f"{ledger['status']}"
        + (f" · last error {ledger['last_error_code']}" if ledger.get("last_error_code") else ""),
    }


def attempt_rows(usage: Mapping[str, Any]) -> list[dict[str, Any]]:
    currency = usage["ledger"].get("currency")
    return [
        {
            "Request": item["request_id"].rsplit("-", maxsplit=1)[-1],
            "Attempt": item["attempt"],
            "State": item["state"],
            "Used tokens (actual)": item["used_tokens"],
            "Reserved tokens (estimate)": item["reserved_tokens"],
            "Actual cost": micros(item["actual_cost_micros"], currency),
            "Reserved cost (estimate)": micros(item["reserved_cost_micros"], currency),
            "Latency (s)": item["latency_seconds"],
            "Error": item["error_code"] or "—",
        }
        for item in usage["attempts"]
    ]


# ------------------------------------------------------------------ schedules


def _fixed_bars(snapshot: OperationalSnapshot) -> list[tuple[str, dict[str, Any]]]:
    bars: list[tuple[str, dict[str, Any]]] = []
    for attempt in snapshot.attempts:
        if attempt.actual_start_seconds is None:
            continue
        if attempt.status is AttemptStatus.COMPLETED and attempt.actual_end_seconds is not None:
            label, end = "Fixed: completed", attempt.actual_end_seconds
        elif attempt.status is AttemptStatus.RUNNING:
            label = "Fixed: running"
            end = attempt.actual_start_seconds + attempt.planned_duration_seconds
        elif attempt.status is AttemptStatus.INTERRUPTED and attempt.actual_end_seconds:
            label, end = "Interrupted (wasted)", attempt.actual_end_seconds
        else:
            continue
        if end > attempt.actual_start_seconds:
            bars.append(
                (label, _bar(attempt.machine_id, attempt.actual_start_seconds, end, attempt))
            )
    return bars


def _bar(machine: str, start: int, end: int, item: Any) -> dict[str, Any]:
    return {
        "lane": machine,
        "start": start,
        "end": end,
        "operation": item.operation_id,
        "attempt": item.attempt_id,
    }


def _started_operations(snapshot: OperationalSnapshot) -> set[str]:
    return {
        item.operation_id
        for item in snapshot.attempts
        if item.status in (AttemptStatus.RUNNING, AttemptStatus.COMPLETED)
    }


def schedule_changes(snapshot: OperationalSnapshot, candidate: CandidateProposal) -> dict[str, str]:
    """Operation → Changed / New / Restart / Unchanged for assignments that are not fixed."""

    current = {
        item.operation_id: item
        for item in snapshot.attempts
        if item.status is AttemptStatus.SCHEDULED and item.scheduled_window is not None
    }
    fixed = _started_operations(snapshot)
    interrupted = {
        item.operation_id for item in snapshot.attempts if item.status is AttemptStatus.INTERRUPTED
    }
    result: dict[str, str] = {}
    for assignment in candidate.assignments:
        if assignment.operation_id in fixed:
            continue
        prior = current.get(assignment.operation_id)
        if prior is None and assignment.operation_id in interrupted:
            result[assignment.operation_id] = "Restart"
        elif prior is None:
            result[assignment.operation_id] = "New"
        elif (
            prior.machine_id != assignment.machine_id or prior.scheduled_window != assignment.window
        ):
            result[assignment.operation_id] = "Changed"
        else:
            result[assignment.operation_id] = "Unchanged"
    return result


def comparison_gantts(
    snapshot: OperationalSnapshot, candidate: CandidateProposal
) -> tuple[go.Figure, go.Figure]:
    """Existing and proposed schedules on one lane order and one simulated-seconds axis."""

    lanes = [item.machine_id for item in snapshot.machines]
    fixed = _fixed_bars(snapshot)
    started = _started_operations(snapshot)
    before: list[tuple[str, dict[str, Any]]] = list(fixed)
    for attempt in snapshot.attempts:
        if (
            attempt.status is AttemptStatus.SCHEDULED
            and attempt.scheduled_window is not None
            and attempt.operation_id not in started
        ):
            window = attempt.scheduled_window
            before.append(
                (
                    "Current plan",
                    _bar(attempt.machine_id, window.start_seconds, window.end_seconds, attempt),
                )
            )
    changes = schedule_changes(snapshot, candidate)
    after: list[tuple[str, dict[str, Any]]] = list(fixed)
    for assignment in candidate.assignments:
        label = changes.get(assignment.operation_id)
        if label is None:
            continue
        after.append(
            (
                label,
                _bar(
                    assignment.machine_id,
                    assignment.window.start_seconds,
                    assignment.window.end_seconds,
                    assignment,
                ),
            )
        )
    now = snapshot.simulated_time_seconds
    horizon = shared_axis_end(snapshot, [item for _, item in (*before, *after)])
    return (
        _gantt(before, lanes, horizon, now, f"Existing schedule · state v{snapshot.state_version}"),
        _gantt(
            after,
            lanes,
            horizon,
            now,
            f"Proposed schedule · plan revision {candidate.proposed_plan_revision}",
        ),
    )


def shared_axis_end(snapshot: OperationalSnapshot, bars: Sequence[Mapping[str, Any]]) -> int:
    """One x-axis end for both charts: all bars of either chart plus 5%, within the horizon."""

    latest = max([snapshot.simulated_time_seconds, *(item["end"] for item in bars)])
    return min(snapshot.hard_horizon_seconds, max(600, int(latest * 1.05)))


def _gantt(
    bars: Sequence[tuple[str, dict[str, Any]]],
    lanes: Sequence[str],
    horizon: int,
    now: int,
    title: str,
) -> go.Figure:
    figure = go.Figure()
    for label in _GANTT:
        items = [item for name, item in bars if name == label]
        if not items:
            continue
        color, pattern = _GANTT[label]
        figure.add_trace(
            go.Bar(
                name=label,
                orientation="h",
                y=[item["lane"] for item in items],
                x=[item["end"] - item["start"] for item in items],
                base=[item["start"] for item in items],
                marker={
                    "color": color,
                    "line": {"color": "#ffffff", "width": 2},
                    "pattern": {
                        "shape": pattern,
                        "fillmode": "overlay",
                        "fgcolor": "#ffffff",
                        "fgopacity": 0.7,
                        "size": 7,
                    },
                },
                customdata=[
                    [item["operation"], item["attempt"], item["start"], item["end"]]
                    for item in items
                ],
                hovertemplate=(
                    "%{fullData.name}<br>Operation %{customdata[0]}<br>Attempt %{customdata[1]}"
                    "<br>Machine %{y}<br>%{customdata[2]}–%{customdata[3]} s<extra></extra>"
                ),
            )
        )
    if not bars:
        # Keep the machine lanes visible so an empty schedule still reads as one.
        figure.add_trace(
            go.Scatter(
                x=[0] * len(lanes),
                y=list(lanes),
                mode="markers",
                marker={"opacity": 0},
                showlegend=False,
                hoverinfo="skip",
            )
        )
        figure.add_annotation(
            text="No scheduled or executed work yet",
            x=0.5,
            y=0.5,
            xref="paper",
            yref="paper",
            showarrow=False,
            font={"size": 14, "color": "#52514e"},
        )
    figure.add_vline(
        x=now,
        line_dash="dash",
        line_color="#52514e",
        annotation_text=f"Now {now} s",
        annotation_position="top right",
    )
    figure.update_layout(
        title=title,
        barmode="overlay",
        height=max(400, 220 + 44 * len(lanes)),
        margin={"l": 20, "r": 20, "t": 60, "b": 130},
        legend={"orientation": "h", "y": -0.42, "x": 0, "yanchor": "top"},
        xaxis={
            "title": {"text": "Simulated time (seconds)", "standoff": 12},
            "range": [0, horizon],
            "showgrid": True,
        },
        yaxis={
            "title": "Machine",
            "type": "category",
            "categoryorder": "array",
            "categoryarray": list(reversed(lanes)),
            "automargin": True,
        },
        bargap=0.35,
    )
    return figure


def change_rows(
    snapshot: OperationalSnapshot,
    candidate: CandidateProposal,
    projected: OperationalSnapshot | None = None,
) -> list[dict[str, Any]]:
    """Affected operations and orders; unchanged and fixed work is omitted."""

    changes = schedule_changes(snapshot, candidate)
    operations = {item.operation_id: item for item in (projected or snapshot).operations}
    order_of = {key: item.order_id for key, item in operations.items()}

    def step_name(operation_id: str) -> str:
        item = operations.get(operation_id)
        if item is None:
            return "—"
        unit = f" · unit {item.unit_index}" if item.unit_index > 1 else ""
        return f"Step {item.sequence_index + 1} · {item.required_capability.value}{unit}"

    current = {
        item.operation_id: item
        for item in snapshot.attempts
        if item.status is AttemptStatus.SCHEDULED and item.scheduled_window is not None
    }
    rows: list[dict[str, Any]] = []
    for assignment in candidate.assignments:
        label = changes.get(assignment.operation_id)
        if label in (None, "Unchanged"):
            continue
        prior = current.get(assignment.operation_id)
        rows.append(
            {
                "Change": label,
                "Order": order_of.get(assignment.operation_id, "—"),
                "Operation": step_name(assignment.operation_id),
                "Old machine": "—" if prior is None else prior.machine_id,
                "Old window (s)": "—"
                if prior is None or prior.scheduled_window is None
                else f"{prior.scheduled_window.start_seconds}–{prior.scheduled_window.end_seconds}",
                "New machine": assignment.machine_id,
                "New window (s)": (
                    f"{assignment.window.start_seconds}–{assignment.window.end_seconds}"
                ),
            }
        )
    return rows


def kpi_comparison_rows(review: ProposalReview) -> list[dict[str, Any]]:
    """Prediction-only before/after KPIs at one snapshot and one comparison point."""

    comparison = review.comparison
    before_objective = None if comparison is None else comparison.before.objective
    after_objective = review.candidate.objective
    actual = None if comparison is None else comparison.actual_at_snapshot

    def row(name: str, unit: str, before: Any, after: Any, basis: str) -> dict[str, Any]:
        delta = after - before if isinstance(before, int) and isinstance(after, int) else None
        return {
            "KPI": name,
            "Unit": unit,
            "Before (existing plan)": "—" if before is None else f"{before:,}",
            "After (proposal)": "—" if after is None else f"{after:,}",
            "Δ after − before": "—" if delta is None else f"{delta:+,}",
            "Basis": basis,
        }

    rows = [
        row(
            "Makespan",
            "simulated s",
            None if before_objective is None else before_objective.makespan_seconds,
            after_objective.makespan_seconds,
            "predicted",
        ),
        row(
            "Weighted total tardiness",
            "weighted s",
            None if before_objective is None else before_objective.weighted_total_tardiness_seconds,
            after_objective.weighted_total_tardiness_seconds,
            "predicted",
        ),
        row(
            "Orders predicted late",
            "orders",
            predicted_late_orders(review.current),
            predicted_late_orders(review.projected),
            "predicted (soft due)",
        ),
        row(
            "Recovery time (interruption → restart)",
            "simulated s",
            predicted_recovery_seconds(review.current),
            predicted_recovery_seconds(review.projected),
            "predicted (S06 definition; — = none)",
        ),
    ]
    if actual is not None:
        rows.append(
            row(
                "WIP at comparison point",
                "orders",
                actual.wip_order_count,
                actual.wip_order_count,
                "actual at snapshot",
            )
        )
    return rows


def realized_kpi_rows(metrics: RunMetrics) -> list[dict[str, Any]]:
    def seconds(value: int | None) -> str:
        return "censored (run unfinished)" if value is None else f"{value:,}"

    utilization = [
        item.utilization_ratio for item in metrics.machines if item.utilization_ratio is not None
    ]
    return [
        {"KPI": "Outcome basis", "Value": metrics.actual_outcome_basis.value, "Unit": ""},
        {"KPI": "Actual makespan", "Value": seconds(metrics.actual_makespan_seconds), "Unit": "s"},
        {
            "KPI": "Actual weighted tardiness",
            "Value": seconds(metrics.actual_weighted_tardiness_seconds),
            "Unit": "weighted s",
        },
        {
            "KPI": "Completed / WIP orders",
            "Value": f"{metrics.completed_order_count} / {metrics.wip_order_count}",
            "Unit": "orders",
        },
        {
            "KPI": "Mean gross utilization",
            "Value": "—" if not utilization else f"{sum(utilization) / len(utilization):.1%}",
            "Unit": "busy / calendar capacity",
        },
        {
            "KPI": "Wasted processing",
            "Value": f"{metrics.wasted_processing_seconds:,}",
            "Unit": "s",
        },
    ]


def recovery_rows(metrics: RunMetrics) -> list[dict[str, Any]]:
    return [
        {
            "Interrupted operation": item.operation_id,
            "Machine": item.disrupted_machine_id,
            "Disruption (s)": item.disruption_seconds,
            "Basis": item.actual_basis.value,
            "Actual recovery (s)": item.actual_recovery_duration_seconds,
            "Censored lower bound (s)": item.censored_lower_bound_seconds,
            "Predicted recovery (s)": item.predicted_recovery_duration_seconds,
            "Resumed on": item.resumed_machine_id or "—",
        }
        for item in metrics.recoveries
    ]


__all__ = [
    "attempt_rows",
    "change_rows",
    "cited_evidence_ids",
    "claim_cards",
    "claim_rows",
    "comparison_gantts",
    "confidence_text",
    "decision_rows",
    "diagnosis_of",
    "diagnosis_summary",
    "evidence_label",
    "evidence_options",
    "guidance_rows",
    "guidance_titles",
    "hypothesis_cards",
    "hypothesis_rows",
    "kpi_comparison_rows",
    "micros",
    "missing_evidence_rows",
    "realized_kpi_rows",
    "recovery_rows",
    "schedule_changes",
    "short_fingerprint",
    "signal_name",
    "signal_names",
    "telemetry_figures",
    "telemetry_table",
    "tool_result_failures",
    "trace_rows",
    "unknown_signals",
    "usage_summary",
]
