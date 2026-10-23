# Reset, restart, replay and troubleshooting

Run commands from the repository root after [setup](../../README.md). Preserve
failed artifacts and their labels. Runtime databases, full traces and credentials
are private local artifacts under ignored paths, not source-release contents.

## Reset and restart

**Reset in the dashboard:** choose a scenario, then press **Start fresh run /
reset**. The new run has a new identity and reset lineage. Old decisions are
closed; a late supervisor result from the old run cannot become actionable in
the new one. Record the old run ID or export it before reset if you want to show
its results. Do not delete `state.sqlite3` to reset.

**Restart the server:** press Ctrl-C in its terminal, then repeat the launch
command with the **same** `SHOPFLOOR_PILOT_UI_RUNTIME_DIR`. SQLite and
`active_run.json` preserve the current run. A refresh, second tab or restart does
not create a fresh production run. If a background supervisor was interrupted,
the UI identifies the decision as **ORPHANED**; use **Cancel orphaned decision**
before submitting a new request. Cancellation closes the associated ledger row;
it does not refund or erase recorded provider usage.

```bash
SHOPFLOOR_PILOT_UI_RUNTIME_DIR=.runtime/s14-demo-offline \
  uv run --offline streamlit run src/shopfloor_pilot/ui/app.py \
  --server.address 127.0.0.1 --server.port 8501
```

For a fully separate rehearsal, choose a new runtime directory. Existing run
artifacts remain available. For the CLI, `recovery-decide --output <same path>`
resumes a paused human-review run; starting `recovery` again requires a fresh
output path. Review the current approval request each time, because the old hash
does not approve a new candidate.

## Export and replay

In **Audit & export**, press **Export current run for replay**. Record the
displayed `events.jsonl` path. Select sidebar **Replay**, enter that exact path
and press **Load and verify replay**. Verify the replay heading, run ID, Gantt,
metric basis and event count. The displayed live run identity/version must remain
unchanged. This loads a portable recorded bundle in isolation; it neither solves
again nor calls DeepSeek.

For a CLI-generated bundle, after the offline walkthrough:

```bash
uv run --offline shopfloor-pilot replay \
  --events .runtime/s14-scripted/machine_breakdown/events.jsonl \
  --output .runtime/s14-replay-check
```

The output must report `replayed`. Compare the original and replay
`final_snapshot.json` and operational `metrics.json`. Audit/provider wall-clock
metadata is not simulated manufacturing performance. Replay verifies recorded
identity/revisions and business outcomes; comparison of independently initialized
runs uses the separately documented semantic normalization. A corrupt/incomplete
bundle must fail verification; restore a known original export rather than
editing its checksums or events.

## Factory twin view

| Symptom | Meaning and action |
| --- | --- |
| "WebGL is unavailable … using the 2D canvas renderer" | The browser offers no WebGL context. The 2D view shows the same state and checks. Choose **Renderer → 2D (no WebGL)** to avoid WebGL deliberately. |
| Projection check stays "runs when playback reaches the latest committed state" | Playback is not at the live group. Press **Live ⏭** or let playback finish. |
| "Factory view not rendered: replaying the committed events does not reproduce the stored state" | The server replay check failed, so the view is deliberately not shown. Treat the event stream or projection as suspect; preserve the runtime directory and report the listed fields. |
| "✗ Visual projection differs …" | What the view displays does not equal stored SQLite state (for example a drawn colour or position, or card text). Treat the view as wrong, not the database; reload, and report the listed fields if it persists. |
| View did not play after an action in another tab | Playback advances only while the view is visible; open **Factory twin** and it continues from its cursor. **Auto-play new events** must be ticked. |
| Old layout or behaviour after updating the code | The browser may cache component files; reload the page. |
| Renderer choice persists between sessions | The choice is a per-browser preference in local storage; change it in the view. |

## Expected failures and useful next actions

| Symptom | Expected behavior | Recovery |
| --- | --- | --- |
| Missing DeepSeek key/model/tariff/caps | Live selection is `SKIPPED`; submitting returns `live_not_ready`. CLI recovery can return `SKIPPED` with exit 2 before starting. | Follow the complete [live settings](live_walkthrough.md). A readiness badge from a past run is not current proof. Use the no-key baseline meanwhile. |
| API outage, timeout, malformed output or exhausted budget | The decision pauses safely with its reason; no failed proposal is applied. Attempts and usage remain recorded. | Inspect the reason/caps in Evidence & trace. Use **Regenerate proposal** only after resolving the cause, or explicitly choose **Run deterministic baseline fallback (explicit, labeled)**. The fallback is a separate decision and still needs human approval. |
| `no_feasible_schedule` / `no_feasible_recovery` | Impossible fixture safely blocks; its expected batch outcome may exit 0. There is no recovery Apply action. | Inspect down machines, remaining hard horizon and validation. Use a fresh recoverable fixture for the presentation; preserve the impossible result as evidence. Due-date lateness alone is not infeasibility. |
| `Stale` on approval | The backend rejects a candidate based on an older operational version and closes the decision; no replacement is auto-approved. | Inspect the current state, choose **Regenerate proposal**, then review and approve the new hash. Do not reuse the prior approval or edit the database. |
| Advance is disabled | An open decision deliberately holds simulation. | Approve, reject or cancel that decision first. Approval waiting and API wall time are not simulated production time. |
| A previously copied advance amount exceeds the current maximum | Maintenance confirmation or another explicit action has consumed a simulation boundary; the input may retain its prior valid value. | Wait for the page update, read the current input maximum and enter that remainder. Verify the displayed advance amount before pressing Advance. |
| Duplicate Submit / Approve | Existing open decision is returned (`already_open`), or “Already applied earlier” is shown. | Inspect the decision history. Do not create a new run merely to retry an acknowledged application. |
| Nonempty CLI output path | The command refuses to overwrite an earlier run. | Use a new output directory, or `recovery-decide` for an existing paused human run. Keep the original failed record. |
| Port 8501 already in use | Streamlit cannot bind that port. | Stop your own earlier server or launch with `--server.port 8502` and open that port. Do not terminate unrelated processes. |
| An interrupted operation appears to lose progress | This is the explicit restart policy; consumed work is waste and the new attempt has full duration. | Inspect operation attempt IDs and interrupted bars. Do not interpret the restart as incremental completion. |
| A KPI is null/censored or predicted | The run has unfinished work, or the number is a plan forecast. | Advance explicitly when permitted and compare the realized result later. Never report missing outcomes as zero tardiness. |

## Reproduce the negative cases without real API calls

The dashboard's **Recorded agent session** offers **provider outage**, malformed
arguments and budget-exhaustion fixtures. Start a fresh breakdown run, approve
its initial plan, inject its disruption, select one failure fixture and submit
an abnormality request. The old plan, operational state and event history at the
decision boundary must remain unchanged by the failed supervisor task. New
audit/decision records are expected. The visible failed decision must retain its
reason and the fake-source label.

For missing configuration, start the dashboard in a shell without provider
settings and without a populated provider `.env` (a clean checkout is suitable),
select live DeepSeek and submit. It must refuse before sending requests. Do not
hide or rename someone else's credential file merely to stage this case.

The supported UI holds simulation while a decision is open, so ordinary
two-tab advance is deliberately refused. The stale-state regression therefore
uses a controlled second backend writer in a test. Do not bypass the UI lock or
change a live demo database to manufacture this failure. Run the exact no-key
tests:

```bash
uv run --offline pytest -q \
  tests/ui/test_streamlit_app.py::test_live_source_without_settings_is_skipped_without_request \
  tests/ui/test_streamlit_app.py::test_outage_pauses_with_reason_and_offers_labeled_fallback \
  tests/ui/test_agent_workbench.py::test_a07_infeasible_recovery_is_paused_and_never_applied \
  tests/ui/test_agent_workbench.py::test_a04_approval_binds_hash_reject_is_inert_and_stale_is_refused \
  tests/ui/test_agent_workbench.py::test_a06_cancel_and_reset_keep_late_results_non_actionable \
  tests/ui/test_agent_workbench.py::test_a05_restart_reports_orphan_and_cancel_closes_its_ledger
```

These test missing settings, outage/fallback, impossible recovery, hash/stale
protection, reset/cancel and restart/ledger behavior. They are local regression
evidence; they do not establish a human-approved live run or a visible-browser
presentation.
