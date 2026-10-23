# Deterministic baseline dashboard walkthrough

> **Superseded controls.** The dashboard now routes every proposal through the
> S11 decision coordinator. "Create initial proposal / Approve candidate / Apply
> approved plan" are replaced by **Submit request** and **Approve & apply this
> proposal**; restoration is **Confirm maintenance complete**. See
> [the agent dashboard walkthrough](agent_dashboard_walkthrough.md). The concepts
> below (durable run pointer, reset lineage, held clock, isolated replay) still apply.

This walkthrough uses only synthetic fixtures, local SQLite state, SimPy, CP-SAT,
Streamlit, and Plotly. It does not require an API key, network connection, LLM, GPU,
or real manufacturing equipment.

## Launch

From the repository root:

```bash
uv run streamlit run src/shopfloor_pilot/ui/app.py
```

Open the reported local URL (normally `http://localhost:8501`). The header must say
**Offline deterministic mode**, **Synthetic demonstration data**, and **Held between
explicit advances**. Record the full run ID, state version, plan revision, and
simulated seconds before acting.

The app creates the first local run only when no durable dashboard context exists.
Refreshes, tab changes, and a second browser session reopen that run. **Start fresh run
/ reset** is the only reset control; it creates a new run ID linked to the prior run
and never overwrites prior history.

## Normal run

1. Select **Normal production**, then deliberately reset if a clean run is required.
2. In **Plan & approval**, choose **Create initial proposal**. Confirm state version
   and plan revision remain zero while validation says PASS and the proposed Gantt is
   visible.
3. Choose **Refresh validation (read-only)** twice. Confirm neither the state version
   nor event history changes.
4. Choose **Approve candidate**. Approval is recorded, but the plan is still not
   applied and state version remains zero.
5. Choose **Apply approved plan**. Confirm plan revision becomes one and the applied
   Gantt appears.
6. In **Advance & disruptions**, advance by a bounded interval such as 900 simulated
   seconds. Return to **Overview** and compare Plan versus Actual lanes, order and
   operation status, completed count, WIP, and actual/predicted/censored KPI labels.
7. Use **Hold / refresh state** and browser refresh. Simulated time must not change.

## Urgent order and recovery proposal

1. Start a fresh **Urgent order** run and apply its initial plan as above.
2. Enter a supported Product ID and explicit order fields, or choose **Load fixture
   urgent order as draft**.
3. Confirm the new order is absent from the authoritative Orders table while its
   candidate and before/after predictions are visible.
4. Approve, then separately apply. Confirm the order and revised plan appear together
   at one version boundary.
5. Rejecting instead must leave orders, applied plan, and operational history intact;
   use **Generate a new proposal** for a deliberate revision.

## Mid-operation fault

1. With an applied plan, open **Advance & disruptions**. Select a future scheduled
   operation; the default fault time is strictly inside its planned window.
2. Choose **Inject selected fault**. The simulator advances explicitly to that second,
   marks the machine down, and records interrupted work when the selected attempt is
   active.
3. Inspect machine/order/operation state and orange Interrupted bars. Generate a
   recovery proposal and inspect changed assignments plus before/after predictions
   before approving anything.
4. Restoration requires **Confirm maintenance complete / restore**; model prose or UI
   refresh never returns a machine to service.

## No-feasible recovery and stale approval

For the blocked case, start **No-feasible recovery**, apply the initial plan, and
choose **Inject named fixture disruption**. Generate a recovery proposal. The UI must
show `scheduling_infeasible`, persist a failed/blocked workflow, show no Apply action,
and retain the prior plan/history.

For the stale case, leave an approved proposal unapplied in browser A. In browser B,
advance the same run. Clicking the already-rendered Apply control in browser A must be
rejected by the backend as stale; it must not generate or apply a replacement. Reload
browser A and use a deliberate replan.

## Export and isolated replay

1. In **Audit & export**, choose **Export current run for replay** after a breakdown.
2. Select the sidebar **Replay** view and load the reported `events.jsonl` path.
3. Confirm replay run identity, event count, snapshot, Gantt, and metric basis appear.
4. Confirm the live run ID and state version shown by the replay page remain unchanged.

Replay verifies the portable bundle and reduces recorded events only. It never opens
the live database as a write target, solves a schedule, samples a fixture, or invokes
an inference provider.

