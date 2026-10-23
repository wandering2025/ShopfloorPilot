# Agent operations dashboard walkthrough and checklist

This is the S12 operator walkthrough for the local Streamlit dashboard. It replaces
the button-level steps of [the S07 baseline walkthrough](baseline_walkthrough.md);
the S07 runtime, replay and reset concepts still apply.

The data is synthetic. Offline use needs no API key, network, GPU or LLM. The live
DeepSeek source is optional, is paid, and is bounded by explicit caps.

## Launch

From the repository root, offline:

```bash
uv run streamlit run src/shopfloor_pilot/ui/app.py --server.address 127.0.0.1
```

Optional environment variables (names only):

| Variable | Purpose |
| --- | --- |
| `SHOPFLOOR_PILOT_UI_RUNTIME_DIR` | Runtime root (default `.runtime/s07-dashboard`): `state.sqlite3`, `agent_trace.sqlite3`, `provider_ledger-<provider>.sqlite3`, `active_run.json`, exports |
| `SHOPFLOOR_PILOT_UI_SUPERVISOR_TIMEOUT` | Wall-clock seconds before a supervisor task is closed as `timed_out` (default 150) |
| `SHOPFLOOR_PILOT_UI_SLOW_FIXTURE_SECONDS` | Delay used by the offline "slow first response" fixture (default 8) |
| `SHOPFLOOR_PILOT_READINESS_REPORT` | Path of the S09 `readiness.json` to display; otherwise the newest under `.runtime/*/` |
| `DEEPSEEK_*`, `SHOPFLOOR_PROVIDER_*` | Live paid-run settings (see the S10/S11 handoffs). Without them the live source shows `SKIPPED` and sends nothing |

For a live run, export the paid-run settings before launching. The key is read from the
authorized ignored file into backend memory only. It never appears in the page, the
trace, the logs or screenshots.

## Screen map

- **Header and status strip.** Scenario, seed, run ID, latest decision source, live
  DeepSeek readiness (`READY`, `CONFIGURED` or `SKIPPED`), simulated time, state
  version, plan revision, workflow status (`idle`, `paused_at_decision`,
  `paused_blocked`), active or orphaned task. A red badge appears once the offline
  scripted fake has been used in the run.
- **Overview.** Machines, orders, the applied plan versus actual Gantt chart, and
  operation detail.
- **Agent workflow & approval.**
  - The running-task panel, with **Cancel this supervisor task**.
  - The current decision. Its review shows the existing and proposed schedules,
    affected operations, order deltas, before/after KPIs, agent statements and the
    proposal ID/hash. **Approve & apply this proposal** and **Reject this proposal**
    sit next to the exact hash.
  - The new-request form.
  - The decision history.
- **Evidence & trace.**
  - Observable temperature and vibration charts, with the raw values in a table.
  - The diagnosis: hypotheses labeled as unconfirmed, and missing or conflicting
    evidence.
  - Retrieved excerpts with their source IDs, and the agent claims.
  - The evidence inspector, which opens the authoritative source of any cited ID.
  - The chronological tool trace and the API panel: attempts, tools, tokens,
    latency, actual versus reserved cost, caps and errors.
- **Outcome & KPIs.**
  - Realized (actual) KPIs, with censored values marked.
  - Recovery episodes and per-machine utilization.
  - For each applied decision: its predicted makespan against the realized one.
- **Advance & maintenance.**
  - Bounded advance, which is refused while a decision is open.
  - Fixture or custom fault injection.
  - The separate **Operator maintenance confirmation**.
- **Audit & export.** The operational event log, the read-only lifecycle view, and
  replay export. The sidebar **Replay** view loads a bundle in isolation.

The sidebar **Operator identity** (`human:` or `operator:` prefix) is recorded on
every approval, rejection, cancel, fallback and maintenance confirmation.

## Planner sources

| Source | Request types | Label in the UI |
| --- | --- | --- |
| Deterministic baseline | Plain plan/replan; typed order form | "Deterministic planner (no LLM)" |
| Recorded agent session | Order request (fixed request fixtures); abnormality report | "Recorded agent session (offline replay)" plus a note on every replayed decision that the model replies are recorded, not live DeepSeek output |
| Live DeepSeek | Free-text order request; abnormality report | "LIVE DeepSeek (paid, bounded)"; refused with `SKIPPED` when settings are missing |

The scripted fake also offers labeled failure fixtures:

- slow first response;
- malformed tool-call arguments;
- provider outage;
- budget exhaustion;
- a false "repair complete" claim.

## Walkthrough (normal → urgent → breakdown → recovery)

The urgent and breakdown stages use their own scenarios, as in the S11 runner. An
urgent order inserted before the breakdown fault makes recovery genuinely infeasible.
That combination is a useful paused-state demonstration.

1. **Normal, initial plan.**
   1. Select **Normal production** and press **Start fresh run / reset**.
   2. In **Agent workflow & approval**, submit a baseline plan, a scripted-fake order
      fixture, or a live order.
   3. Check that state version and plan revision stay `0`, the workflow status is
      `paused_at_decision`, and **Advance** is disabled.
   4. Review the proposal, then press **Approve & apply this proposal**. The
      revision becomes `1`.
2. **Urgent order.**
   1. Reset to **Urgent order**, then submit and approve the baseline initial plan.
   2. Advance 3599 s.
   3. Submit the scripted-fake fixture "加急插单：铝制安装板 两件，90分钟内完成。" (Chinese: "Rush
      order: two aluminum mounting plates, finish within 90 minutes.") or a live request.
   4. The review shows the new order under **Order deltas (become real only on
      application)**, and changed operations as hatched orange bars.
   5. Approve, then advance by the remaining horizon. **Outcome & KPIs** shows the
      realized makespan next to the predicted one.
3. **Breakdown.**
   1. Reset to **Recoverable machine breakdown**, then approve the baseline initial
      plan. It uses the accepted `scarce_capability_first` policy.
   2. In **Advance & maintenance**, press **Inject named fixture disruption**. CNC02
      goes down at 2700 s.
4. **Recovery.**
   1. Submit an **Abnormality report / recovery**.
   2. In **Evidence & trace**, check the CNC02 telemetry trend and the hypotheses,
      each marked "not a confirmed root cause". Check the missing drive-current and
      lubrication fields.
   3. Open each evidence reference in the inspector. Each one must show `resolved:
      True`; observations and guidance chunks must also say "matches the tool
      result".
   4. Approve the recovery. CNC02 stays **down**.
   5. In **Advance & maintenance**, press **Confirm maintenance complete for CNC02**.
      This records one `machine_restored` event and a `restoration_confirmed-…`
      audit with your identity.
   6. Advance by the remaining horizon and read the realized recovery episode.

## Safety checks to repeat

- **Repeated clicks, refreshes and second tabs.** Click Submit or Approve several
  times, reload, or open a second tab. A request while a decision is open returns
  the open decision (`already_open`), so there is never a second task or paid
  workflow. A repeated approval reports "Already applied earlier". Exactly one
  `plan_applied` event exists per decision.
- **Cancel and reset.** Use the slow fixture, then **Cancel this supervisor task**
  or **Start fresh run / reset**. The late response is recorded only as
  `supervisor_result_discarded` or `late_supervisor_result_discarded` on the old
  run, and the new run shows no decision.
- **Server restart during a task.** The status strip reports an **ORPHANED**
  decision. Press **Cancel orphaned decision**; this also closes its ledger row.
- **Stale approval.** If another writer changes the state, **Approve & apply**
  reports "Stale" and closes the decision. **Regenerate proposal** creates a new
  decision that needs its own approval.
- **Failures.** Malformed, outage, budget and infeasible results show "Paused
  safely — nothing applied" with the reason and the unchanged state. **Run
  deterministic baseline fallback (explicit, labeled)** opens a separate decision
  marked "Deterministic planner fallback", and it still needs approval.
- **Live gate.** Without settings, choosing the live source shows `SKIPPED` and
  submitting reports `live_not_ready`; no ledger file is created. With settings, the
  API panel shows the actual reconciled cost separately from the estimated
  reservation, and the billing basis is provider-reported usage.

## Automated checks

```bash
uv run pytest tests/ui -q
uv run ruff check src/shopfloor_pilot/ui tests/ui
```

`tests/ui/test_agent_workbench.py` covers the backend behaviour of S12-A01–A09 and
A11. `tests/ui/test_streamlit_app.py` drives the real buttons with Streamlit
`AppTest`.
