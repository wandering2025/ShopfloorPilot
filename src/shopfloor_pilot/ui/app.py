"""ShopfloorPilot agent operations dashboard (Streamlit).

Launch from the repository root with:

    uv run streamlit run src/shopfloor_pilot/ui/app.py --server.address 127.0.0.1

The page only renders reads and forwards explicit button actions to the
Streamlit-independent controller/workbench. Scheduling, budgets, approval and
application decisions all stay in backend services.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import streamlit as st

from shopfloor_pilot.services import ApplicationServiceError
from shopfloor_pilot.ui.agent_presenters import (
    attempt_rows,
    change_rows,
    cited_evidence_ids,
    claim_cards,
    comparison_gantts,
    decision_rows,
    diagnosis_of,
    diagnosis_summary,
    evidence_options,
    guidance_titles,
    hypothesis_cards,
    kpi_comparison_rows,
    realized_kpi_rows,
    recovery_rows,
    short_fingerprint,
    telemetry_figures,
    telemetry_table,
    tool_result_failures,
    trace_rows,
    unknown_signals,
    usage_summary,
)
from shopfloor_pilot.ui.controller import (
    DEFAULT_RUNTIME_ROOT,
    SCENARIO_IDS,
    DashboardController,
    DashboardInputError,
    DashboardView,
)
from shopfloor_pilot.ui.factory_twin import TwinTimelineError, build_twin_timeline_from_repository
from shopfloor_pilot.ui.presenters import (
    build_gantt_figure,
    event_rows,
    future_attempt_options,
    machine_metric_rows,
    machine_rows,
    operation_rows,
    order_rows,
)
from shopfloor_pilot.ui.twin_component import render_factory_twin
from shopfloor_pilot.ui.workbench import (
    FAKE_FIXTURE_LABELS,
    SOURCE_LABELS,
    AgentWorkbench,
    ApprovalOutcome,
    DecisionDetail,
    FakeFixture,
    LiveStatus,
    PlannerSource,
    RequestResult,
    RequestType,
)
from shopfloor_pilot.workflows import (
    ActionResult,
    ActionStatus,
    CoordinatorError,
    DecisionRecord,
    DecisionSource,
    DecisionStatus,
    lifecycle_view,
)

_SCENARIO_LABELS = {
    "normal": "Normal production",
    "urgent_order": "Urgent order",
    "machine_breakdown": "Recoverable machine breakdown",
    "no_feasible_recovery": "No-feasible recovery",
}
_REQUEST_LABELS = {
    RequestType.ORDER: "Order request",
    RequestType.ABNORMALITY: "Abnormality report / recovery",
    RequestType.REPLAN: "Plain plan / replan (baseline only)",
}
_DECISION_STATUS_LABELS = {
    DecisionStatus.OPEN: "Supervisor working — simulation paused",
    DecisionStatus.AWAITING_APPROVAL: "Awaiting explicit human approval",
    DecisionStatus.APPLIED: "Applied through S06",
    DecisionStatus.PAUSED: "Paused safely — nothing applied",
    DecisionStatus.CANCELLED: "Closed without application",
}
_CHART_CONFIG = {"displaylogo": False, "responsive": True}
_RUN_STATE_LABELS = {
    "idle": "Ready",
    "paused_at_decision": "Decision open",
    "paused_blocked": "Paused safely",
}


def _plain(code: object) -> str:
    """Readable text for a backend code such as ``awaiting_approval``."""

    text = str(code or "").replace("_", " ").strip()
    return text[:1].upper() + text[1:] if text else "—"


# --------------------------------------------------------------------- plumbing


def _runtime_root() -> str:
    configured = os.environ.get("SHOPFLOOR_PILOT_UI_RUNTIME_DIR")
    return str(Path(configured).expanduser().resolve()) if configured else str(DEFAULT_RUNTIME_ROOT)


@st.cache_resource(show_spinner=False)
def _workbench(runtime_root: str) -> AgentWorkbench:
    timeout = float(os.environ.get("SHOPFLOOR_PILOT_UI_SUPERVISOR_TIMEOUT", "150"))
    slow = float(os.environ.get("SHOPFLOOR_PILOT_UI_SLOW_FIXTURE_SECONDS", "8"))
    return AgentWorkbench(
        DashboardController(runtime_root),
        supervisor_timeout_seconds=timeout,
        slow_fixture_seconds=slow,
    )


def _friendly_error(error: BaseException) -> str:
    if isinstance(error, DashboardInputError):
        issues = error.details.get("issues")
        suffix = ""
        if isinstance(issues, list):
            rendered = [
                f"{item.get('field', 'input')}: {item.get('message', 'invalid value')}"
                for item in issues
                if isinstance(item, dict)
            ]
            if rendered:
                suffix = " " + " · ".join(rendered)
        missing = error.details.get("missing")
        if isinstance(missing, list) and missing:
            suffix += " Missing settings: " + ", ".join(str(item) for item in missing)
        return f"{error.code}: {error}{suffix}"
    if isinstance(error, CoordinatorError):
        return f"{error.code.value}: {error.message}"
    if isinstance(error, ApplicationServiceError):
        return f"{error.code.value}: {error}"
    return f"{type(error).__name__}: {error}"


def _set_flash(kind: str, message: str) -> None:
    st.session_state["flash"] = {"kind": kind, "message": message}


def _perform(action: Callable[[], Any], success: str | Callable[[Any], tuple[str, str]]) -> None:
    try:
        result = action()
    except Exception as exc:  # UI boundary keeps expected backend rejection readable.
        _set_flash("error", _friendly_error(exc))
    else:
        if callable(success):
            kind, message = success(result)
            _set_flash(kind, message)
        else:
            _set_flash("success", success)
    st.rerun()


def _show_flash() -> None:
    # A fixed slot keeps the element tree above the tabs structurally stable.
    slot = st.container()
    flash = st.session_state.get("flash")
    if not isinstance(flash, dict):
        return
    message = str(flash.get("message", ""))
    kind = flash.get("kind")
    with slot:
        _flash_message(kind, message)


def _flash_message(kind: object, message: str) -> None:
    if kind == "success":
        st.success(message, icon="✅")
    elif kind == "warning":
        st.warning(message, icon="⚠️")
    elif kind == "info":
        st.info(message, icon="ℹ️")
    else:
        st.error(message, icon="⛔")


def _actor() -> str:
    return str(st.session_state.get("actor", "human:operator")).strip()


def _style() -> None:
    st.markdown(
        """
        <style>
        :root { --ink: #102238; --muted: #52606f; --line: #dce4ec; }
        .stApp { background: linear-gradient(180deg, #f6f9fc 0, #ffffff 320px); }
        .block-container { max-width: 1560px; padding-top: 1.2rem; padding-bottom: 3rem; }
        .pilot-hero {
            border: 1px solid #d7e1eb; border-radius: 14px; padding: 16px 22px;
            background: linear-gradient(135deg, #0b243d 0%, #123c61 70%, #12627a 100%);
            color: white; margin-bottom: 10px;
        }
        .pilot-hero h1 { margin: 0; font-size: 1.55rem; letter-spacing: -.02em; }
        .pilot-hero p { margin: .35rem 0 0; color: #d6e6f4; font-size: .95rem; }
        .pilot-badge {
            display: inline-block; border-radius: 999px; padding: 3px 10px; margin: 0 6px 6px 0;
            background: #e6f4f1; color: #0b6b5d; font-size: .76rem; font-weight: 700;
            letter-spacing: .03em; text-transform: uppercase;
        }
        .pilot-badge.synthetic { background: #fff0dc; color: #8a4a08; }
        .pilot-badge.paused { background: #e7eefc; color: #294f9b; }
        .pilot-badge.recorded { background: #ece9fb; color: #43318f; }
        .pilot-badge.offline { background: #e8eef6; color: #2f4560; }
        .pilot-badge.live { background: #e3f1ff; color: #0b4f8a; }
        div[data-testid="stMetric"] {
            background: #fff; border: 1px solid var(--line); border-radius: 12px;
            padding: 10px 12px;
        }
        div[data-testid="stMetricValue"] { font-size: 1.35rem; }
        div[data-testid="stMetricValue"] [data-testid="stMarkdownContainer"],
        div[data-testid="stMetricValue"] [data-testid="stMarkdownContainer"] p {
            white-space: normal; overflow-wrap: anywhere; overflow: visible; text-overflow: clip;
        }
        div[data-testid="stDataFrame"] { border: 1px solid var(--line); border-radius: 10px; }
        </style>
        """,
        unsafe_allow_html=True,
    )


# ----------------------------------------------------------------------- header


def _latest_supervisor(decisions: tuple[DecisionRecord, ...]) -> DecisionRecord | None:
    return next(
        (item for item in reversed(decisions) if item.source is DecisionSource.SUPERVISOR), None
    )


def _hero(view: DashboardView, live: LiveStatus, decisions: tuple[DecisionRecord, ...]) -> None:
    latest = decisions[-1] if decisions else None
    source = "none yet" if latest is None else _source_text(latest)
    live_badge = (
        '<span class="pilot-badge live">Live DeepSeek ready</span>'
        if live.configured
        else '<span class="pilot-badge offline">Offline demo mode</span>'
    )
    recorded_badge = ""
    if any(item.provider == PlannerSource.SCRIPTED_FAKE.value for item in decisions):
        recorded_badge = '<span class="pilot-badge recorded">Recorded agent session</span>'
    run_tag = short_fingerprint(view.context.run_id, 6)
    st.markdown(
        f"""
        <div class="pilot-hero">
          <h1>ShopfloorPilot · Agent operations</h1>
          <p>{_SCENARIO_LABELS[view.context.scenario_id]} · run {view.context.generation}
          (…{run_tag}) · latest decision: {source}</p>
        </div>
        <span class="pilot-badge synthetic">Synthetic demonstration data</span>
        <span class="pilot-badge paused">Clock advances only on request</span>
        {live_badge}
        {recorded_badge}
        """,
        unsafe_allow_html=True,
    )


def _status_strip(view: DashboardView, status: dict[str, Any]) -> None:
    snapshot = view.snapshot
    columns = st.columns(6)
    columns[0].metric("Simulated time", f"{snapshot.simulated_time_seconds:,} s")
    columns[1].metric("State version", snapshot.state_version)
    columns[2].metric("Plan revision", snapshot.plan_revision)
    columns[3].metric(
        "Workflow status", _RUN_STATE_LABELS.get(status["state"], _plain(status["state"]))
    )
    columns[4].metric(
        "Orders complete", f"{view.metrics.completed_order_count}/{len(snapshot.orders)}"
    )
    columns[5].metric("WIP", f"{view.metrics.wip_order_count} orders")
    detail = f"Reason: {_plain(status['reason_code'])}"
    if status["active_tasks"]:
        detail += f" · active task: {', '.join(status['active_tasks'])}"
    if status["orphaned_open"]:
        detail += f" · ORPHANED decision (worker gone): {', '.join(status['orphaned_open'])}"
    st.caption(
        detail + " · Simulated seconds never follow wall time; waiting for the provider or a "
        "human does not advance the clock."
    )


# ----------------------------------------------------------------- agent tab


def _render_task_panel(workbench: AgentWorkbench) -> None:
    tasks = workbench.active_tasks()
    watched = st.session_state.get("watched_tasks", [])
    if not tasks:
        if watched:
            # The "task started" message is obsolete once the task has settled.
            st.session_state["watched_tasks"] = []
            st.session_state.pop("flash", None)
            st.rerun(scope="app")
        return
    st.session_state["watched_tasks"] = [item.task_id for item in tasks]
    for task in tasks:
        started = datetime.fromisoformat(task.started_at_utc)
        elapsed = (datetime.now(UTC) - started).total_seconds()
        label = SOURCE_LABELS[task.source]
        fixture = "" if task.fake_fixture is None else f" · fixture {task.fake_fixture.value}"
        with st.container(border=True):
            st.markdown(
                f"**Agent task running** · {label}{fixture} · "
                f"wall time {elapsed:,.0f} s (simulation paused)"
            )
            st.caption(
                "Refreshing, rerunning, reconnecting or clicking again only reads this task; "
                "the backend allows one active task per decision."
            )
            if st.button("Cancel this supervisor task", key=f"cancel-{task.task_id}"):
                _perform(
                    lambda tid=task.task_id: workbench.cancel(tid, actor=_actor()),
                    _cancel_message,
                )


def _cancel_message(result: ActionResult) -> tuple[str, str]:
    if result.status is ActionStatus.CANCELLED:
        return (
            "success",
            "Decision cancelled. A late provider response will only be "
            "recorded as discarded; it can never become an actionable proposal.",
        )
    return "error", f"Cancel refused: {result.reason_code}"


def _render_agent(workbench: AgentWorkbench, view: DashboardView, live: LiveStatus) -> None:
    decisions = workbench.decisions()
    active = workbench.active_tasks()
    if active:
        st.fragment(run_every=2.0)(_render_task_panel)(workbench)
    latest = decisions[-1] if decisions else None
    if latest is not None:
        _render_current_decision(workbench, view, latest, running=bool(active))
    else:
        st.info(
            "No decision yet. Every schedule — including the initial plan — needs an explicit "
            "human approval before anything runs."
        )
    st.divider()
    _render_request_form(
        workbench, view, live, blocked=latest is not None and latest.status.is_open
    )
    if decisions:
        with st.expander("Decision history (this run)", expanded=False):
            st.dataframe(decision_rows(decisions), hide_index=True, width="stretch")


def _render_current_decision(
    workbench: AgentWorkbench, view: DashboardView, decision: DecisionRecord, *, running: bool
) -> None:
    detail = workbench.decision_detail(decision.workflow_id)
    source = decision.provider or decision.source.value
    history = [item.workflow_id for item in workbench.decisions()]
    ordinal = history.index(decision.workflow_id) + 1 if decision.workflow_id in history else 0
    st.subheader(f"Current decision · {decision.kind.value.replace('_', ' ')}")
    cols = st.columns([2, 2, 1, 1])
    cols[0].markdown(f"**Decision** #{ordinal} of {len(history)}")
    cols[1].markdown(f"**Source** {_source_text(decision)}")
    cols[2].markdown(f"**Decision t** {decision.decision_time_seconds:,} s")
    cols[3].markdown(f"**Base v** {decision.base_state_version}")
    status_text = _DECISION_STATUS_LABELS[decision.status]
    if decision.provider == PlannerSource.SCRIPTED_FAKE.value:
        st.info(
            "**Recorded agent session.** The agent workflow, typed tools, evidence checks, "
            "schedule validation and approval gate all run for real. The model's replies are "
            "replayed from a recording instead of being generated live, so this is not live "
            "DeepSeek output.",
            icon="🎞️",
        )
    if decision.source is DecisionSource.DETERMINISTIC_FALLBACK:
        origin = (
            history.index(decision.fallback_for_workflow_id) + 1
            if (decision.fallback_for_workflow_id in history)
            else None
        )
        st.info(
            f"**Deterministic planner fallback** for decision #{origin or '?'}: chosen by an "
            "operator, not counted as an LLM result, and it still needs approval.",
            icon="↩️",
        )
    if decision.status is DecisionStatus.OPEN:
        orphaned = not running and not workbench.active_tasks()
        if orphaned and decision.source is DecisionSource.SUPERVISOR:
            st.error(
                "This decision is open but its supervisor worker is gone (process restart). "
                "It cannot resume; cancel it explicitly before continuing."
            )
            if st.button("Cancel orphaned decision", key="cancel-orphan"):
                _perform(
                    lambda: workbench.cancel(decision.workflow_id, actor=_actor()),
                    _cancel_message,
                )
        else:
            st.info(f"{status_text}. Results appear here when the task finishes.")
        return
    if decision.status is DecisionStatus.AWAITING_APPROVAL and detail.review is not None:
        _render_review(workbench, view, detail)
        return
    if decision.status is DecisionStatus.APPLIED:
        st.success(
            f"{status_text}: the approval was consumed once at the "
            f"recorded boundary {decision.decision_boundary_seconds:,} s. Advance time in "
            "'Advance & maintenance' to realize it."
        )
        return
    reason = decision.reason_code or decision.outcome or "unknown"
    if decision.status is DecisionStatus.PAUSED:
        st.error(
            f"{status_text}. Reason: **{reason}**. The previous applied schedule, orders, "
            "machines, time and version are unchanged and remain inspectable in Overview.",
            icon="⏸️",
        )
        terminal = detail.terminal or {}
        unresolved = (terminal.get("details") or {}).get("unresolved_fields")
        if unresolved:
            issues = (terminal.get("details") or {}).get("issue_codes") or []
            st.warning(
                "Clarification needed — unresolved fields: "
                + ", ".join(str(item) for item in unresolved)
                + (f" (issues: {', '.join(str(item) for item in issues)})" if issues else "")
                + ". Submit a corrected request below; no order was drafted."
            )
        _render_next_actions(workbench, decision, source)
        return
    outcome = decision.outcome or "closed"
    if outcome == "stale":
        st.error(
            f"Stale proposal: operational state changed after it was generated ({reason}). "
            "It can no longer be approved; regenerate it against the current state.",
            icon="🕒",
        )
        if st.button("Regenerate proposal (new decision, new approval)", key="regenerate"):
            _perform(
                lambda: workbench.regenerate(decision.workflow_id, actor=_actor()),
                _request_message,
            )
    else:
        st.info(
            f"{status_text}: **{outcome}** ({reason}). Operational state was not changed by this "
            "decision."
        )
        if outcome == "timed_out":
            _render_next_actions(workbench, decision, source)


def _render_next_actions(workbench: AgentWorkbench, decision: DecisionRecord, source: str) -> None:
    st.markdown("**Valid next actions**")
    cols = st.columns(2)
    if decision.source is DecisionSource.SUPERVISOR and cols[0].button(
        "Run deterministic baseline fallback (explicit, labeled)", key="run-fallback"
    ):
        _perform(
            lambda: workbench.run_fallback(decision.workflow_id, actor=_actor()),
            _request_message,
        )
    cols[1].caption(
        "Or submit a new request below. Advancing time is also allowed: nothing is pending."
    )


def _source_text(decision: DecisionRecord) -> str:
    if decision.source is DecisionSource.SUPERVISOR:
        return SOURCE_LABELS[PlannerSource(decision.provider or "scripted-fake")]
    if decision.source is DecisionSource.DETERMINISTIC_FALLBACK:
        return "Deterministic fallback (operator-selected)"
    return SOURCE_LABELS[PlannerSource.BASELINE]


def _render_review(workbench: AgentWorkbench, view: DashboardView, detail: DecisionDetail) -> None:
    review = detail.review
    assert review is not None
    decision = detail.decision
    candidate = review.candidate
    request = review.request
    applicable = bool(request["currently_applicable"])
    top = st.columns(4)
    top[0].metric("Proposal", f"Plan revision {candidate.proposed_plan_revision}")
    top[1].metric(
        "Base → current version", f"{candidate.base_state_version} → {review.current.state_version}"
    )
    top[2].metric("Proposed revision", candidate.proposed_plan_revision)
    top[3].metric("Constraint validation", "PASS" if applicable else "STALE / INVALID")
    with st.expander("Technical identifiers", expanded=False):
        st.markdown(
            f"Workflow `{decision.workflow_id}`  \nProposal ID `{candidate.proposal_id}`  \n"
            f"Immutable proposal hash `{candidate.proposal_hash}`  \n"
            f"Base snapshot hash `{candidate.base_snapshot_hash}`"
        )
        st.caption(
            "Approval is bound to this exact hash and the current state version; the backend "
            "checks both again before applying."
        )
    if not applicable:
        st.warning(
            "Stale-state warning: this proposal no longer matches current authority "
            f"({', '.join(request['conflict_reasons']) or 'validation'}). Approval will be "
            "refused and the decision closed; regenerate afterwards."
        )
    else:
        st.success("Independent S03 validation passes against the current authoritative snapshot.")

    before, after = comparison_gantts(review.current, candidate)
    st.plotly_chart(before, width="stretch", config=_CHART_CONFIG, key="gantt-before")
    st.plotly_chart(after, width="stretch", config=_CHART_CONFIG, key="gantt-after")
    st.caption(
        "Both charts share machine lanes and one simulated-seconds axis (same range in both). Gray "
        "and aqua bars are fixed history (completed / running) and never move; hatched orange "
        "= changed, dotted violet = new, crossed violet = restart of interrupted work, blue = "
        "unchanged."
    )
    changes = change_rows(review.current, candidate, review.projected)
    st.markdown("**Affected orders / operations**")
    if changes:
        st.dataframe(changes, hide_index=True, width="stretch")
    else:
        st.caption("No remaining assignment changes.")
    if request["order_deltas"]:
        st.markdown("**Order deltas (become real only on application)**")
        st.dataframe(
            [
                {
                    "Kind": item["kind"],
                    "Order": item["order"]["order_id"],
                    "Product": item["order"]["product_id"],
                    "Qty": item["order"]["quantity"],
                    "Due (s)": item["order"]["due_time_seconds"],
                    "Priority": item["order"]["priority_weight"],
                }
                for item in request["order_deltas"]
            ],
            hide_index=True,
            width="stretch",
        )
    st.markdown("**Before / after KPIs (predicted)**")
    st.dataframe(kpi_comparison_rows(review), hide_index=True, width="stretch")
    point = review.comparison.before if review.comparison is not None else None
    window = (
        ""
        if point is None
        else f" · prediction window [{point.prediction_window.start_seconds}, "
        f"{point.prediction_window.end_seconds}) s"
    )
    st.caption(
        f"Comparison point: scenario {view.context.scenario_id}, seed "
        f"{view.bundle.scenario.seed}, snapshot v{review.current.state_version} at "
        f"t={review.current.simulated_time_seconds:,} s{window}. Predictions only — realized "
        "outcomes appear in 'Outcome & KPIs' after time advances. Utilization is defined by "
        "S06 on actual execution only."
    )
    warnings = request.get("due_date_warnings") or []
    if warnings:
        st.warning(
            "Soft due-date misses predicted (not infeasibility): "
            + ", ".join(
                f"{item['order_id']} due {item['due_time_seconds']} s → "
                f"{item['predicted_completion_seconds']} s"
                for item in warnings
            )
        )
    claims = request.get("claims") or []
    if claims:
        st.markdown("**What the agent reported** (sources are listed under Evidence & trace)")
        st.table(claim_cards(claims, detail.tool_results))
    repair = request.get("repair_recommendation")
    if repair and repair.get("hypothesis_evidence_ids"):
        st.caption(
            "Repair recommendation only; the machine is not restored by this plan. Restoring "
            "it needs the separate operator maintenance confirmation."
        )

    with st.container(border=True):
        st.markdown(
            f"**Decide on exactly this proposal** · plan revision "
            f"{candidate.proposed_plan_revision} · fingerprint "
            f"`{short_fingerprint(candidate.proposal_hash)}` · base v"
            f"{candidate.base_state_version} · as `{_actor()}`"
        )
        approve, reject = st.columns(2)
        if approve.button("Approve & apply this proposal", type="primary", key="approve-proposal"):
            _perform(
                lambda: workbench.approve(
                    decision.workflow_id,
                    proposal_id=candidate.proposal_id,
                    proposal_hash=candidate.proposal_hash,
                    actor=_actor(),
                ),
                _approval_message,
            )
        if reject.button("Reject this proposal", key="reject-proposal"):
            _perform(
                lambda: workbench.reject(
                    decision.workflow_id,
                    proposal_id=candidate.proposal_id,
                    proposal_hash=candidate.proposal_hash,
                    actor=_actor(),
                ),
                lambda result: (
                    ("success", "Proposal rejected; operational state is unchanged.")
                    if result.status is ActionStatus.REJECTED
                    else ("error", f"Reject refused: {result.status.value} {result.reason_code}")
                ),
            )
        st.caption(
            "Approval is bound to this hash and the current state version; the backend "
            "revalidates and applies atomically through S06. Cancelling instead keeps state."
        )
        if st.button("Cancel decision without approving", key="cancel-awaiting"):
            _perform(
                lambda: workbench.cancel(decision.workflow_id, actor=_actor()), _cancel_message
            )


def _approval_message(outcome: ApprovalOutcome) -> tuple[str, str]:
    approval = outcome.approval
    if outcome.applied and outcome.application is None:
        return (
            "info",
            "Already applied earlier — this repeated click changed nothing (one application only).",
        )
    if outcome.applied:
        application = outcome.application
        assert application is not None
        return (
            "success",
            f"Approved by {approval.actor} and applied ({application.status.value}): state "
            f"v{application.previous_state_version} → v{application.resulting_state_version} at "
            f"t={application.effective_time_seconds} s.",
        )
    if approval.status is ActionStatus.STALE or (
        outcome.application is not None and outcome.application.status is ActionStatus.STALE
    ):
        return (
            "error",
            "Stale: operational state changed since this proposal was generated. Nothing was "
            "approved or applied; use 'Regenerate proposal'.",
        )
    if outcome.application is not None:
        return (
            "error",
            f"Approved, but application failed safely ({outcome.application.reason_code}). "
            "Nothing changed; retry Approve & apply.",
        )
    return "error", f"Approval refused: {approval.status.value} ({approval.reason_code})."


def _request_message(result: RequestResult) -> tuple[str, str]:
    kind = {
        "started": "info",
        "proposal_ready": "success",
        "already_open": "warning",
        "blocked": "error",
    }[result.status]
    return kind, result.message


def _render_request_form(
    workbench: AgentWorkbench, view: DashboardView, live: LiveStatus, *, blocked: bool
) -> None:
    st.subheader("New request")
    if blocked:
        st.caption("A decision is open. Close it (approve, reject or cancel) before a new request.")
    sources = list(PlannerSource)
    source = st.radio(
        "Planner source",
        sources,
        format_func=lambda item: SOURCE_LABELS[item],
        horizontal=True,
        key="planner-source",
    )
    if source is PlannerSource.DEEPSEEK:
        if live.configured:
            settings = live.settings
            st.info(
                f"{live.label}. Model {settings.get('model')}, caps "
                f"{settings.get('workflow_cap_micros', 0) / 1e6:.4f} per workflow / "
                f"{settings.get('run_cap_micros', 0) / 1e6:.4f} per run "
                f"{settings.get('currency')}, tariff {settings.get('tariff_date')}."
            )
        else:
            st.warning(
                f"{live.label}. Missing: {', '.join(live.missing) or '—'}. Submitting is refused "
                "without sending any request."
            )
    request_types = (
        [RequestType.REPLAN, RequestType.ORDER]
        if source is PlannerSource.BASELINE
        else [RequestType.ORDER, RequestType.ABNORMALITY]
    )
    request_type = st.radio(
        "Request type",
        request_types,
        format_func=lambda item: _REQUEST_LABELS[item],
        horizontal=True,
        key=f"request-type-{source.value}",
    )
    kwargs: dict[str, Any] = {"source": source, "request_type": request_type}
    if source is PlannerSource.SCRIPTED_FAKE:
        fixture = st.selectbox(
            "Offline provider fixture",
            list(FakeFixture),
            format_func=lambda item: FAKE_FIXTURE_LABELS[item],
            key="fake-fixture",
        )
        kwargs["fake_fixture"] = fixture
    if request_type is RequestType.ORDER and source is PlannerSource.SCRIPTED_FAKE:
        fixtures = list(workbench.request_fixtures())
        chosen = st.selectbox(
            "Request fixture (the scripted fake replays its recorded interpretation)",
            [item.fixture_id for item in fixtures],
            format_func=lambda fid: next(
                f"[{item.category}] {item.request_text}"
                for item in fixtures
                if item.fixture_id == fid
            ),
            key="request-fixture",
        )
        kwargs["fixture_id"] = chosen
    elif request_type is RequestType.ORDER and source is PlannerSource.DEEPSEEK:
        kwargs["request_text"] = st.text_area(
            "Order request (free text, ≤500 characters; interpreted into a typed draft)",
            value="Urgent: 1 aluminum mounting plate within 2 hours.",
            max_chars=500,
            key="live-request-text",
        )
    elif request_type is RequestType.ORDER:
        kwargs["typed_order"] = _typed_order_inputs(view)
    elif request_type is RequestType.ABNORMALITY:
        down = [item.machine_id for item in view.snapshot.machines if item.status.value == "down"]
        st.caption(
            "Focus machines: " + (", ".join(down) if down else "none down — nothing to diagnose")
        )
        if source is PlannerSource.DEEPSEEK:
            kwargs["request_text"] = st.text_area(
                "Operator report (empty = standard report for the down machines)",
                value="",
                max_chars=500,
                key="live-abnormality-text",
            )
    if st.button(
        "Submit request (one task per decision)",
        type="primary",
        disabled=blocked,
        key="submit-request",
    ):
        _perform(lambda: workbench.request_decision(actor=_actor(), **kwargs), _request_message)


def _typed_order_inputs(view: DashboardView) -> dict[str, object]:
    products = [item.product_id for item in view.bundle.catalog.products]
    first, second, third = st.columns(3)
    order_id = first.text_input("Order ID", value="ORD-URGENT-UI", key="typed-order-id")
    product_id = second.text_input(
        "Product ID", value=products[0], help=f"Supported: {', '.join(products)}", key="typed-prod"
    )
    quantity = third.number_input("Quantity", min_value=1, max_value=10, value=1, step=1)
    fourth, fifth, sixth = st.columns(3)
    release = fourth.number_input(
        "Release (simulated s)", min_value=0, value=view.snapshot.simulated_time_seconds, step=1
    )
    due = fifth.number_input(
        "Due (simulated s)",
        min_value=0,
        value=min(view.snapshot.hard_horizon_seconds, view.snapshot.simulated_time_seconds + 7200),
        step=1,
    )
    priority = sixth.number_input("Priority weight", min_value=1, max_value=100, value=10, step=1)
    return {
        "order_id": order_id,
        "product_id": product_id,
        "quantity": int(quantity),
        "release_time_seconds": int(release),
        "due_time_seconds": int(due),
        "priority_weight": int(priority),
    }


# -------------------------------------------------------------- evidence tab


def _render_evidence(workbench: AgentWorkbench, view: DashboardView) -> None:
    decisions = [item for item in workbench.decisions() if item.source is DecisionSource.SUPERVISOR]
    focus = sorted(
        {machine for item in decisions for machine in item.focus_machine_ids}
        or {item.machine_id for item in view.snapshot.machines if item.status.value == "down"}
    )
    st.subheader("Observable telemetry")
    machines = [item.machine_id for item in view.snapshot.machines]
    selected = st.multiselect(
        "Machines",
        machines,
        default=focus or machines[:1],
        # The key follows the focus set so a new diagnosis focus resets the selection.
        key=f"telemetry-machines-{'-'.join(focus)}",
    )
    rows = workbench.telemetry(selected)
    if rows:
        figures = telemetry_figures(
            rows, view.snapshot.hard_horizon_seconds, view.snapshot.simulated_time_seconds
        )
        columns = st.columns(len(figures))
        for column, (signal, figure) in zip(columns, figures, strict=True):
            column.plotly_chart(figure, width="stretch", config=_CHART_CONFIG, key=f"tel-{signal}")
        with st.expander("Telemetry values (raw units, as recorded)", expanded=False):
            st.dataframe(telemetry_table(rows), hide_index=True, width="stretch")
    else:
        st.info("No observations recorded for the selected machines yet (empty, not zero).")

    if not decisions:
        st.info("No supervisor decision yet: diagnosis, evidence and trace appear after one runs.")
        return
    names = {
        item.workflow_id: (
            f"Decision {number} · {item.kind.value.replace('_', ' ')} · {_source_text(item)}"
        )
        for number, item in enumerate(workbench.decisions(), start=1)
        if item.source is DecisionSource.SUPERVISOR
    }
    ids = [item.workflow_id for item in decisions]
    workflow_id = st.selectbox(
        "Agent decision",
        ids,
        index=len(ids) - 1,
        format_func=lambda item: names.get(item, "Decision"),
        key="evidence-wf",
    )
    detail = workbench.decision_detail(workflow_id)
    claims = (detail.supervisor_audit or {}).get("claims") or []

    for diagnosis in diagnosis_of(detail.tool_results):
        summary = diagnosis_summary(diagnosis)
        with st.container(border=True):
            st.subheader(f"{summary['Machine']} · {summary['Finding']} diagnosis")
            columns = st.columns(3)
            for column, name in zip(
                columns, ("Root cause", "Confidence", "Maintenance"), strict=True
            ):
                column.metric(name, summary[name])
            st.caption(
                "These are rule-based hypotheses from observable signals, not a confirmed "
                "cause. Restoring the machine needs the operator's maintenance confirmation."
            )
        hypotheses = hypothesis_cards(diagnosis)
        if hypotheses:
            st.markdown("**Possible explanations**")
            st.table(hypotheses)  # wraps long sentences instead of clipping them
        unknowns = unknown_signals(diagnosis)
        if unknowns:
            st.markdown("**Not yet known**")
            st.table(unknowns)
    if claims:
        st.markdown("**What the agent reported**")
        st.table(claim_cards(claims, detail.tool_results))

    with st.expander("Technical details — sources, identifiers, tool trace, API usage"):
        _render_technical_details(workbench, workflow_id, detail, claims)


def _render_technical_details(
    workbench: AgentWorkbench, workflow_id: str, detail: DecisionDetail, claims: Any
) -> None:
    label = detail.trace.get("evidence_label") or "—"
    st.caption(f"Evidence label recorded by the agent: {label} · workflow `{workflow_id}`")
    excerpts = guidance_titles(detail.tool_results)
    if excerpts:
        st.markdown("**Reference guidance retrieved**")
        st.dataframe(excerpts, hide_index=True, width="stretch")

    st.markdown("**Source inspector**")
    evidence_ids = cited_evidence_ids(claims, detail.tool_results)
    if evidence_ids:
        options = evidence_options(evidence_ids, detail.tool_results)
        chosen = st.selectbox(
            "Open a source",
            evidence_ids,
            format_func=lambda item: options[item],
            key="evidence-open",
        )
        source = workbench.resolve_evidence(workflow_id, chosen)
        verdict = (
            "not cross-checked"
            if source.matches_trace is None
            else ("matches the tool result" if source.matches_trace else "MISMATCH")
        )
        st.markdown(
            f"`{source.evidence_id}` → **{source.authority}** · resolved: **{source.resolved}** · "
            f"{verdict}"
        )
        st.json(dict(source.content), expanded=False)
    else:
        st.caption("This decision cited no evidence (for example, it failed before evidence).")

    st.markdown("**Tool trace (chronological, hidden reasoning excluded)**")
    rows = trace_rows(detail.trace)
    if rows:
        st.dataframe(rows, hide_index=True, width="stretch", height=280)
    failures = tool_result_failures(detail.trace)
    if failures:
        st.markdown("**Rejected tool calls**")
        st.dataframe(failures, hide_index=True, width="stretch")
    _render_usage(detail)


def _render_usage(detail: DecisionDetail) -> None:
    st.markdown("**API attempts, tokens, latency and cost**")
    usage = detail.usage
    if usage is None:
        st.info("No provider ledger entry for this decision.")
        return
    st.caption(f"Provider: {usage['provider']} · billing basis: {usage['billing_basis']}.")
    summary = usage_summary(usage)
    items = list(summary.items())
    for start in range(0, len(items), 3):
        columns = st.columns(3)
        for column, (name, value) in zip(columns, items[start : start + 3], strict=False):
            column.metric(name, value)
    if usage["attempts"]:
        st.dataframe(attempt_rows(usage), hide_index=True, width="stretch")
    terminal = detail.terminal or {}
    if terminal:
        st.caption(
            f"Terminal: {terminal.get('terminal')} ({terminal.get('reason_code')}). Credentials, "
            "prompts and hidden reasoning are never shown."
        )


# --------------------------------------------------------------- outcome tab


def _render_outcome(workbench: AgentWorkbench, view: DashboardView) -> None:
    metrics = view.metrics
    st.subheader("Realized outcomes (actual, from the event log)")
    st.dataframe(realized_kpi_rows(metrics), hide_index=True, width="stretch")
    st.caption(
        f"Actual window [{metrics.actual_window.start_seconds}, "
        f"{metrics.actual_window.end_seconds}) simulated seconds. Unfinished values are "
        "censored, never estimated."
    )
    recoveries = recovery_rows(metrics)
    st.markdown("**Recovery episodes (interruption → restart)**")
    if recoveries:
        st.dataframe(recoveries, hide_index=True, width="stretch")
    else:
        st.caption("No interruption has occurred in this run.")
    st.dataframe(machine_metric_rows(metrics), hide_index=True, width="stretch")
    applied = [item for item in workbench.decisions() if item.status is DecisionStatus.APPLIED]
    if applied:
        lifecycle = {item.proposal_id: item for item in view.lifecycle.proposals}
        st.markdown("**Applied decisions: predicted at approval vs realized**")
        st.dataframe(
            [
                {
                    "Decision": f"{item.kind.value.replace('_', ' ')} · {_source_text(item)}",
                    "Source": item.provider or item.source.value,
                    "Predicted makespan (s)": (
                        f"{lifecycle[item.applied_proposal_id].objective.makespan_seconds:,}"
                        if item.applied_proposal_id in lifecycle
                        else "—"
                    ),
                    "Realized makespan (s)": "censored (run unfinished)"
                    if metrics.actual_makespan_seconds is None
                    else f"{metrics.actual_makespan_seconds:,}",
                }
                for item in applied
            ],
            hide_index=True,
            width="stretch",
        )
    st.plotly_chart(
        build_gantt_figure(view.snapshot),
        width="stretch",
        config=_CHART_CONFIG,
        key="outcome-gantt",
    )


# ------------------------------------------------------- advance / maintenance


def _render_controls(
    workbench: AgentWorkbench, view: DashboardView, status: dict[str, Any]
) -> None:
    controller = workbench.dashboard
    snapshot = view.snapshot
    pending = bool(status["open_decisions"])
    st.subheader("Bounded execution")
    if pending:
        st.warning(
            "Simulation is paused at an open decision; advancing is refused until it is "
            "approved, rejected or cancelled."
        )
    remaining = controller.remaining_seconds(snapshot)
    maximum = max(1, remaining)
    step = st.number_input(
        "Advance by simulated seconds",
        min_value=1,
        max_value=maximum,
        value=min(900, maximum),
        step=1,
        disabled=remaining == 0,
    )
    advance, hold = st.columns(2)
    if advance.button(
        "Advance",
        type="primary",
        disabled=snapshot.plan_revision == 0 or remaining == 0 or pending,
        key="advance-run",
    ):
        _perform(
            lambda: controller.advance_by(int(step)),
            f"Run advanced by {int(step):,} simulated seconds.",
        )
    if hold.button("Hold / refresh state", key="hold-state"):
        _perform(controller.pause, "State refreshed; simulated time did not advance.")

    st.divider()
    st.subheader("Disruption injection (scenario harness)")
    options = future_attempt_options(snapshot)
    if view.context.scenario_id in {"machine_breakdown", "no_feasible_recovery"}:
        if st.button(
            "Inject named fixture disruption", key="inject-fixture-fault", disabled=pending
        ):
            _perform(
                controller.inject_fixture_disruption,
                "Named fixture disruption injected only after explicit operator action.",
            )
    if options:
        with st.expander("Custom mid-operation fault", expanded=False):
            labels = [item["label"] for item in options]
            selected_label = st.selectbox("Scheduled operation", labels, key="fault-attempt")
            selected = next(item for item in options if item["label"] == selected_label)
            fault_time = st.number_input(
                "Fault at simulated second",
                min_value=snapshot.simulated_time_seconds + 1,
                max_value=snapshot.hard_horizon_seconds - 1,
                value=int(selected["suggested_fault_seconds"]),
                step=1,
            )
            if st.button("Inject selected fault", key="inject-fault", disabled=pending):
                _perform(
                    lambda: controller.inject_fault(str(selected["machine_id"]), int(fault_time)),
                    "Fault injected through the simulator; request a recovery decision next.",
                )

    st.divider()
    st.subheader("Operator maintenance confirmation")
    st.caption(
        "Separate from proposal approval. Agent text (for example 'repair complete') can never "
        "restore a machine; only this explicit confirmation records a scoped machine_restored "
        "event with your operator identity."
    )
    down = [
        item.machine_id
        for item in snapshot.machines
        if item.status.value in ("down", "maintenance")
    ]
    if not down:
        st.info("No machine is down or in maintenance.")
        return
    machine_id = st.selectbox("Machine under maintenance", down, key="restore-machine")
    if st.button(
        f"Confirm maintenance complete for {machine_id}",
        key="restore-button",
        disabled=pending,
    ):
        _perform(
            lambda: workbench.confirm_maintenance(machine_id, actor=_actor()),
            f"{_actor()} confirmed {machine_id} back in service (machine_restored event).",
        )


# -------------------------------------------------------------- factory twin


@st.cache_data(max_entries=8, show_spinner=False)
def _twin_payload(
    database_path: str,
    run_id: str,
    state_version: int,
    scenario_id: str,
    _repository: Any,
    _catalog: Any,
) -> dict[str, Any]:
    # Committed history is append-only, so (database, run, version) fully
    # determines the read-only projection.
    return build_twin_timeline_from_repository(
        _repository,
        run_id,
        _catalog,
        scenario_id=scenario_id,
        state_version=state_version,
    ).payload


def _render_factory_twin(
    workbench: AgentWorkbench, view: DashboardView, status: dict[str, Any]
) -> None:
    controller = workbench.dashboard
    st.caption(
        "Visual digital-twin **prototype**: a read-only playback of this run's committed SQLite "
        "event stream. Status cards, scene and event list below share one playback clock. "
        "The view cannot change state; the buttons here are the same operator actions as in "
        "**Advance & maintenance**, and plans still require approval in **Agent workflow & "
        "approval**."
    )
    _render_twin_actions(workbench, view, status)
    try:
        payload = _twin_payload(
            str(controller.repository.database_path),
            view.context.run_id,
            view.snapshot.state_version,
            view.context.scenario_id,
            controller.repository,
            view.bundle.catalog,
        )
    except TwinTimelineError as exc:
        st.error(f"Factory view unavailable; the recorded stream could not be replayed: {exc}")
        return
    verification = payload["verification"]
    if not verification["passed"]:
        # Fail closed: never animate a stream that does not reproduce stored authority.
        st.error(
            "Factory view not rendered: replaying the committed events does not reproduce the "
            f"stored state ({'; '.join(verification['mismatches']) or 'hash mismatch'}). "
            "Operational data is unchanged; preserve the runtime directory for inspection."
        )
        return
    render_factory_twin(payload, key=f"factory-twin-{view.context.run_id}")


def _render_twin_actions(
    workbench: AgentWorkbench, view: DashboardView, status: dict[str, Any]
) -> None:
    controller = workbench.dashboard
    snapshot = view.snapshot
    pending = bool(status["open_decisions"])
    remaining = controller.remaining_seconds(snapshot)
    plan_end = controller.plan_completion_seconds(snapshot)
    blockers = controller.plan_blockers(snapshot)
    needs_recovery = (
        "Request and approve a recovery plan first: " + "; ".join(blockers) + "."
        if blockers
        else None
    )
    step_column, advance_column, complete_column, fault_column, restore_column = st.columns(
        [1.1, 1, 1.35, 1.3, 1.45]
    )
    step = step_column.number_input(
        "Advance by (simulated s)",
        min_value=1,
        max_value=max(1, remaining),
        value=min(900, max(1, remaining)),
        step=1,
        disabled=remaining == 0,
        key="twin-advance-step",
    )
    if advance_column.button(
        "Advance and watch",
        type="primary",
        disabled=snapshot.plan_revision == 0 or remaining == 0 or pending,
        key="twin-advance",
    ):
        _perform(
            lambda: controller.advance_by(int(step)),
            f"Run advanced by {int(step):,} simulated seconds.",
        )
    if complete_column.button(
        "Run plan to completion and watch",
        disabled=plan_end is None or pending or bool(blockers),
        help=needs_recovery
        or (
            None
            if plan_end is None
            else f"Advances to {plan_end:,} s, the last planned completion of the applied plan."
        ),
        key="twin-run-plan",
    ):
        _perform(
            controller.advance_to_plan_completion,
            "Run advanced to the applied plan's last planned completion.",
        )
    if view.context.scenario_id in {"machine_breakdown", "no_feasible_recovery"}:
        if fault_column.button(
            "Inject fixture fault and watch",
            disabled=pending or snapshot.plan_revision == 0,
            key="twin-inject-fixture",
        ):
            _perform(
                controller.inject_fixture_disruption,
                "Named fixture disruption injected only after explicit operator action.",
            )
    down = [
        item.machine_id
        for item in snapshot.machines
        if item.status.value in ("down", "maintenance")
    ]
    degraded_target = controller.degraded_production_target() if down else None
    if degraded_target is not None and fault_column.button(
        f"Continue production without {down[0]} and watch",
        type="primary",
        disabled=pending or bool(blockers),
        help=needs_recovery
        or (
            f"Runs the approved plan to {degraded_target:,} s with {down[0]} still down "
            "(as long as the normal segment before the fault)."
        ),
        key="twin-degraded",
    ):
        _perform(
            controller.advance_degraded_production,
            f"Production continued on the approved plan without {down[0]}.",
        )
    if down and restore_column.button(
        f"Confirm {down[0]} maintenance complete",
        disabled=pending,
        key="twin-restore",
    ):
        _perform(
            lambda: workbench.confirm_maintenance(down[0], actor=_actor()),
            f"{_actor()} confirmed {down[0]} back in service (machine_restored event).",
        )
    if blockers and not pending:
        st.info(
            "The applied plan cannot run as it stands ("
            + "; ".join(blockers)
            + "). Request a recovery decision in **Agent workflow & approval**; the "
            "plan buttons unlock once a recovery plan is approved."
        )
    if pending:
        st.info(
            "A decision is open, so the simulation is paused. Review and approve or reject it in "
            "**Agent workflow & approval**; the factory view then plays the committed changes."
        )
    elif snapshot.plan_revision == 0:
        st.info("No plan is applied yet: request and approve an initial plan first.")


# ---------------------------------------------------------------- other tabs


def _render_overview(view: DashboardView) -> None:
    left, right = st.columns([1, 1.6])
    with left:
        st.subheader("Machines")
        st.dataframe(machine_rows(view.snapshot), hide_index=True, width="stretch")
    with right:
        st.subheader("Orders")
        st.dataframe(order_rows(view.snapshot), hide_index=True, width="stretch")
    st.plotly_chart(
        build_gantt_figure(view.snapshot),
        width="stretch",
        config=_CHART_CONFIG,
        key="current-gantt",
    )
    with st.expander("Operation detail", expanded=False):
        rows = operation_rows(view.snapshot)
        if rows:
            st.dataframe(rows, hide_index=True, width="stretch")
        else:
            st.info("No operations exist until an approved proposal is applied.")


def _render_audit(workbench: AgentWorkbench, view: DashboardView) -> None:
    controller = workbench.dashboard
    st.subheader("Operational event history")
    rows = event_rows(controller.repository.list_events(view.context.run_id))
    if rows:
        st.dataframe(rows, hide_index=True, width="stretch")
    else:
        st.info("No operational events yet. Proposal and approval records are non-operational.")
    with st.expander("Proposal lifecycle view (read-only, not actionable)", expanded=False):
        st.json(lifecycle_view(view.lifecycle), expanded=False)
    if st.button("Export current run for replay", key="export-replay"):
        try:
            path = controller.export_replay()
        except Exception as exc:
            _set_flash("error", _friendly_error(exc))
        else:
            st.session_state["replay_path"] = str(path)
            _set_flash("success", f"Replay bundle exported to {path}")
        st.rerun()


def _render_replay(controller: DashboardController, live: DashboardView) -> None:
    st.subheader("Isolated replay view")
    st.caption(
        "Loading a bundle validates hashes and reduces recorded events only. It cannot mutate "
        "the live run, invoke planning, or call a provider."
    )
    default_path = str(
        st.session_state.get(
            "replay_path",
            controller.runtime_root / "exports" / live.context.run_id / "events.jsonl",
        )
    )
    replay_path = st.text_input("Replay bundle path", value=default_path, key="replay-input")
    if st.button("Load and verify replay", type="primary", key="load-replay"):
        try:
            result = controller.load_replay(replay_path)
        except Exception as exc:
            _set_flash("error", _friendly_error(exc))
        else:
            st.session_state["replay_result"] = result
            _set_flash("success", "Replay verified without touching live authority.")
        st.rerun()
    result = st.session_state.get("replay_result")
    if result is None:
        st.info("Export a run or enter an existing portable events.jsonl bundle.")
        return
    snapshot = result.checkpoint.snapshot
    columns = st.columns(4)
    columns[0].metric("Replay run", result.run_id)
    columns[1].metric("Events", result.event_count)
    columns[2].metric("Replay version", snapshot.state_version)
    columns[3].metric("Replay time", f"{snapshot.simulated_time_seconds:,} s")
    st.success(
        f"Live run remains {live.context.run_id} at state version {live.snapshot.state_version}."
    )
    st.plotly_chart(
        build_gantt_figure(snapshot), width="stretch", config=_CHART_CONFIG, key="replay-gantt"
    )


def _refresh_if_status_changed(workbench: AgentWorkbench, rendered_status: dict[str, Any]) -> None:
    # A background task can finish after the header read but before its panel is
    # rendered. With no active task left, no fragment would refresh that stale header.
    if workbench.run_status() != rendered_status:
        st.rerun(scope="app")


def main() -> None:
    st.set_page_config(
        page_title="ShopfloorPilot · Agent operations",
        page_icon="🏭",
        layout="wide",
        initial_sidebar_state="expanded",
    )
    _style()
    workbench = _workbench(_runtime_root())
    controller = workbench.dashboard
    try:
        view = controller.ensure_active()
        status = workbench.run_status()
        decisions = workbench.decisions()
    except Exception as exc:
        st.error(f"Dashboard startup failed safely: {_friendly_error(exc)}")
        st.stop()
    live = workbench.live_status()

    with st.sidebar:
        st.header("Run control")
        st.text_input(
            "Operator identity (human:/operator: prefix)", value="human:operator", key="actor"
        )
        selected_scenario = st.selectbox(
            "Scenario",
            SCENARIO_IDS,
            index=SCENARIO_IDS.index(view.context.scenario_id),
            format_func=lambda item: _SCENARIO_LABELS[item],
        )
        if st.button("Start fresh run / reset", key="reset-run"):
            _perform(
                lambda: workbench.reset(selected_scenario, actor=_actor()),
                "Fresh run created with a new identity; open decisions of the old run were "
                "closed as run_reset and its late results cannot become actionable.",
            )
        st.caption(
            f"Run {view.context.generation} · state v{view.snapshot.state_version} · "
            f"…{short_fingerprint(view.context.run_id, 6)}"
        )
        with st.expander("Technical identifiers", expanded=False):
            st.markdown(f"Run ID `{view.context.run_id}`")
        page = st.radio("View", ("Operations", "Replay"), key="page")
        st.divider()
        st.caption(f"Live model: {live.label}")
        if live.readiness:
            st.caption(
                f"Last live check: {str(live.readiness.get('status')).lower()} at "
                f"{live.readiness.get('checked_at_utc')}"
            )
        st.caption("Local only · credentials are never rendered")

    _hero(view, live, decisions)
    _show_flash()
    _status_strip(view, status)

    if page == "Replay":
        _render_replay(controller, view)
        _refresh_if_status_changed(workbench, status)
        return

    overview, twin, agent, evidence, outcome, controls, audit = st.tabs(
        [
            "Overview",
            "Factory twin",
            "Agent workflow & approval",
            "Evidence & trace",
            "Outcome & KPIs",
            "Advance & maintenance",
            "Audit & export",
        ],
        key="main-tabs",
    )
    with overview:
        _render_overview(view)
    with twin:
        _render_factory_twin(workbench, view, status)
    with agent:
        _render_agent(workbench, view, live)
    with evidence:
        _render_evidence(workbench, view)
    with outcome:
        _render_outcome(workbench, view)
    with controls:
        _render_controls(workbench, view, status)
    with audit:
        _render_audit(workbench, view)
    _refresh_if_status_changed(workbench, status)


if __name__ == "__main__":
    main()
