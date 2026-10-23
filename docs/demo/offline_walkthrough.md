# Offline demonstration

Use this guide after the [README setup](../../README.md). All commands run from
the repository root. Dependency installation needs network access on an uncached
machine; the demonstrations below do not invoke an inference service. `--offline`
also prevents uv from downloading dependencies. All manufacturing data is synthetic.

There are three distinct modes:

| Mode | What it demonstrates | Approval |
| --- | --- | --- |
| Deterministic baseline | Process routes, CP-SAT scheduling, validation, simulation and replay; no LLM | A human in the dashboard; explicitly named policy in batch commands |
| Recorded agent session (offline replay) | Recorded tool calls exercise the supervisor and evidence UI; no DeepSeek capability claim | A human in the dashboard; explicitly named policy in batch commands |
| Replay | Previously recorded events and metrics; no new solve or inference | No operational writes or approval |

## Launch and initial planning

Choose an unused runtime directory for a new rehearsal. Reusing this directory
deliberately reopens its last active run.

```bash
SHOPFLOOR_PILOT_UI_RUNTIME_DIR=.runtime/s14-demo-offline \
  uv run --offline streamlit run src/shopfloor_pilot/ui/app.py \
  --server.address 127.0.0.1 --server.port 8501
```

Open `http://127.0.0.1:8501`. The application requires no GPU. The sidebar may display a historical readiness result; the
**Planner source** on each decision determines whether that decision is live.

1. Enter your own `human:<name>` or `operator:<name>` in **Operator identity**.
2. Select **Normal production**, then **Start fresh run / reset**. Record its run
   ID. Merely changing the scenario selector does not reset the active run.
3. In **Agent workflow & approval**, choose **Deterministic planner (no LLM)**,
   **Plain plan / replan (baseline only)**, then **Submit request (one task per
   decision)**.
4. Review constraint validation, the proposed schedule, predicted KPIs and the
   proposal hash. State version and plan revision remain zero. Advance is refused
   while this decision is open.
5. A real operator presses **Approve & apply this proposal**. The initial plan
   revision becomes one. Approval authorizes this exact candidate; the backend
   validates it again before applying it atomically.
6. In **Advance & maintenance**, advance 900 simulated seconds. In **Overview**,
   inspect machine/order state and Plan versus Actual bars. **Hold / refresh
   state**, browser refresh and time spent speaking do not advance simulation.

## Urgent insertion

Use a separate scenario from the breakdown story. The fixtures were designed as
independent cases; combining an urgent insertion with the later breakdown can
make the remaining finite horizon infeasible.

1. Select **Urgent order** and reset. Submit and approve its baseline initial plan.
2. Advance **3599** seconds. The dashboard inserts the request at the current
   explicit decision boundary; this is the S12 presentation walkthrough, not an
   assertion that its metrics equal the S13 batch intervention at 3600 seconds.
3. Choose **Recorded agent session (offline replay)**, **Order request**, the ordinary successful
   provider fixture and request `加急插单：铝制安装板 两件，90分钟内完成。` (Chinese for
   "Rush order: two aluminum mounting plates, finish within 90 minutes").
4. Submit. This is a recorded interpretation, not live inference.
   Inspect **Order deltas (become real only on application)**, the existing and
   proposed Gantts, affected operations and before/after predictions. The order
   remains a draft until approved application.
5. Approve the exact proposal. Advance by the remaining horizon shown in the
   controls. Compare the realized outcome with the earlier prediction in
   **Outcome & KPIs**.

For an entirely deterministic UI path, use the baseline **Order request** and its
typed product/quantity/due-date form. That demonstrates structured order entry,
not natural-language interpretation.

## Fault evidence and recovery

1. Reset to **Recoverable machine breakdown**, then submit and approve the
   baseline initial plan. This fixture uses the documented
   `scarce_capability_first` initial policy.
2. Press **Inject named fixture disruption** in **Advance & maintenance**. The
   scenario harness explicitly advances to 2700 seconds and faults CNC02.
3. Choose **Recorded agent session (offline replay)**, the ordinary successful provider fixture,
   and **Abnormality report / recovery**. Submit the request.
4. Inspect **Evidence & trace**: observable temperature/vibration, missing
   drive-current/lubrication fields, retrieved guidance, hypotheses and tool calls.
   Open cited IDs in the evidence inspector and check that they resolve. A
   hypothesis is not a confirmed physical root cause. Hidden injected truth and
   future interventions are excluded from the supervisor's inputs.
5. In the approval review, compare existing and proposed schedules on their
   shared time axis. Check validation, affected operations and predicted metrics.
   Approve the proposal. CNC02 remains down: plan approval is not repair approval.
6. The operator separately presses **Confirm maintenance complete for CNC02**.
   This synthetic scenario action records a restoration event with the operator
   identity; model prose cannot restore equipment.
7. Let the page finish updating, then read the **new remaining horizon** from the
   advance input. Maintenance confirmation consumes an explicit simulation
   boundary, so a remainder copied before confirmation can now exceed its limit.
   Advance by the current remaining horizon. Inspect the recovery episode and wasted
   processing time. Completed work remains fixed. Unaffected running operations
   are non-preemptive; an interrupted operation restarts its full duration as a
   new attempt, and consumed processing time is counted as waste.

## Safe impossible recovery

Reset to **No-feasible recovery**, approve its baseline initial plan and inject
the named disruption. Submit an abnormality request with the scripted source.
The decision must pause safely with no applicable recovery proposal. The
expected reason is `no_feasible_schedule`; the baseline CLI below uses
`no_feasible_recovery`. Both describe a deliberate fixture outcome, not success
at finishing production. Due dates are soft; the hard capabilities, machine
availability and finite horizon cause the safe block. Do not edit state or relax
constraints to make this case green.

## Repeatable command-line checks

Use a new output root each time; a nonempty output directory is refused. These
batch commands record `policy:deterministic`, not human approval.

```bash
for scenario in normal urgent_order machine_breakdown no_feasible_recovery; do
  uv run --offline shopfloor-pilot baseline \
    --scenario "$scenario" --seed 0 --approval-policy deterministic \
    --output ".runtime/s14-baseline/$scenario"
done

for scenario in normal urgent_order machine_breakdown no_feasible_recovery; do
  uv run --offline shopfloor-pilot recovery --provider scripted-fake \
    --scenario "$scenario" --seed 0 --approval-policy deterministic \
    --output ".runtime/s14-scripted/$scenario"
done

uv run --offline shopfloor-pilot replay \
  --events .runtime/s14-scripted/machine_breakdown/events.jsonl \
  --output .runtime/s14-scripted-replay
```

All eight scenario commands should exit zero: normal, urgent and breakdown
complete; impossible recovery reaches its expected block. Inspect `summary.json`
as well as the exit code. Recovery summaries must report
`invariants_passed: true` and `terminal_matches_expectation: true`. The replay
must report `replayed` and preserve the recorded final snapshot and operational
metrics. Generated databases and raw traces belong under ignored `.runtime/`.

Use [the live guide](live_walkthrough.md)
for real inference, and [troubleshooting](troubleshooting.md) for reset/restart,
negative cases and replay. Metric definitions and boundaries are in the
[architecture overview](../architecture/overview.md).
