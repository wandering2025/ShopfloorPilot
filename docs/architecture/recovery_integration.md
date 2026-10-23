# Recovery integration and failure boundaries

Status: S11 architecture and consumer contract (S12 dashboard, S13 evaluation)

Scope: connect the S10 supervisor to initial planning, urgent-order insertion,
fault handling, and approved recovery, and show that stale work, duplicate
requests, cancellation, provider failure, and infeasibility cannot corrupt
operational state. S11 adds orchestration only. Validation stays in S03,
scheduling in S04, simulation in S05, and approval/application in S06.

## Decision summary

- `workflows.RecoveryCoordinator` is operator-side backend code over the full
  `ApplicationServices`. The supervisor still receives only
  `services.for_model()`.
- A **decision** is one S06 workflow record whose creation payload has
  `mode = "s11_recovery"`. The coordinator writes it before the supervisor
  collects any snapshot, so the decision time is fixed first.
- Status is derived only from SQLite: workflow status and payload, proposals,
  approvals, and approval consumptions. A restarted process sees exactly the
  same decisions.
- Approval is bound to the stored proposal hash and to the current state
  version. Application is S06 `apply_approved_plan`, using the recorded decision
  boundary and a deterministic idempotency key.
- Failure handling closes or annotates lifecycle records. It never writes
  operational state. A deterministic fallback runs only when a human selects it
  explicitly, is labeled as a fallback, and still needs a new approval.

## Request → approval → application sequence

```text
operator/harness            coordinator                   supervisor (facade only)        S06 / SQLite
----------------            -----------                   ------------------------        ------------
advance(T, …)  ─────────▶  refuse if a decision is open ─────────────────────────────────▶ events, CAS
open_decision  ─────────▶  read checkpoint; boundary B
                           ensure_workflow(payload: T, B, version, hash) ────────────────▶ workflow RUNNING
run_supervisor ─────────▶  worker thread  ───────────▶  observe → evidence → solve →
                                                        validate → persist_proposal ─────▶ proposal +
                                                                                           RUNNING→WAITING
                           settle: base_version == decision version?
approve(hash)  ─────────▶  pending binding + inspect_conflict(can_apply)
                           record_human_decision(approval-<proposal>) ───────────────────▶ approval
apply          ─────────▶  apply_approved_plan(key apply-<approval>, effective=B) ────────▶ one transaction:
                                                                                           deltas, plan_applied,
                                                                                           consume, audit,
                                                                                           WAITING→COMPLETED,
                                                                                           checkpoint, CAS
advance(…)     ─────────▶  allowed again (no open decision)
```

While a decision is open, the simulation is paused at `T`. Nothing in this
sequence reads wall-clock time to move simulated time. Provider latency and human
latency are recorded only as timestamps. `B` is computed by
`tools.context.decision_boundary_seconds`, which mirrors S06 `_effective_time`.
`B` is `T`, or `T + 1` once second `T` has been closed by `time_advanced`.

## Decision states

| Derived status | S06 workflow | Meaning | Operational effect |
| --- | --- | --- | --- |
| `open` | `running` | Paused at the boundary, with no proposal yet | none |
| `awaiting_approval` | `waiting_approval` | An immutable proposal is bound to the decision | none |
| `applied` | `completed` | Exactly one approval was consumed by one committed mutation | one event group |
| `paused` | `failed` (payload `outcome = blocked`) | A safe blocked result: infeasible, provider failure, budget, cycle, clarification, rejected request, or solver unknown | none |
| `cancelled` | `cancelled` (payload `outcome`) | `cancelled`, `timed_out`, `stale`, `rejected`, `superseded`, or `run_reset` | none |

A decision cannot be reopened. A new attempt always uses a new workflow ID, which
gives a new proposal ID and needs a new approval. When the supervisor ends in a
terminal other than awaiting approval, the coordinator settles the S10 workflow
explicitly. In S10 that workflow had been left `running`.

| Supervisor terminal | Coordinator action |
| --- | --- |
| `awaiting_approval` | Check that the proposal's base version equals the decision version. Otherwise cancel the decision as `stale`. |
| `stale_state` | `cancel_workflow(outcome="stale")` |
| `cancelled` | `cancel_workflow(outcome="cancelled")` |
| `infeasible`, `failed`, `budget_exhausted`, `cycle_detected`, `clarification_required`, `rejected` | `mark_workflow_blocked(reason_code=<supervisor reason>)`, which records a paused decision with `operational_state_preserved = true` |
| any terminal after the decision was already closed | Append the run-scoped `supervisor_result_discarded` or `late_supervisor_result_discarded` audit |

## Coordinator API

```python
from shopfloor_pilot.workflows import RecoveryCoordinator, DecisionKind, DecisionSource

coordinator = RecoveryCoordinator(services, wall_clock=..., batch_policy=None)
decision = coordinator.open_decision(
    run_id,
    workflow_id,
    kind=DecisionKind.FAULT_RECOVERY,
    source=DecisionSource.SUPERVISOR,
    provider="deepseek",
    focus_machine_ids=("CNC02",),
    with_order_delta=False,
)
step = coordinator.run_supervisor(decision, session_factory, timeout_seconds=None, deadline=None)
coordinator.approve(run_id, workflow_id, proposal_id=..., proposal_hash=..., actor="human:alice")
coordinator.reject(...)  # closes the decision as rejected
coordinator.apply(run_id, workflow_id, proposal_id=..., proposal_hash=..., approval_id=...)
coordinator.cancel(run_id, workflow_id, outcome="cancelled", reason_code=..., actor=...)
coordinator.advance(
    run_id, target, advance_id, interventions=..., observations=..., cancel_open_decisions=False
)
coordinator.confirm_restoration(run_id, "CNC02", confirmation_id=..., actor="operator:alice")
coordinator.run_deterministic_fallback(
    run_id, failed_workflow_id, workflow_id=..., actor="human:alice", order_deltas=()
)
coordinator.propose_deterministic(decision, generate)  # baseline/fallback sources only
coordinator.reset_run(source_run_id, new_run_id)
coordinator.decisions(run_id)
coordinator.run_status(run_id)
coordinator.restore(run_id)
```

Every operator action returns an `ActionResult` with a stable `status`. The
statuses are `approved`, `already_approved`, `rejected`, `applied`,
`already_applied`, `cancelled`, `stale`, `conflict`, and `failed`. Concurrent or
retried callers therefore get consistent answers:

- **Approve.** The approval ID is `approval-<proposal_id>`, so there is one
  decision per proposal. A second simultaneous approval returns
  `already_approved` with the stored actor. A stale state version returns
  `stale`, records no approval, and closes the decision.
- **Apply.** The default idempotency key is `apply-<approval_id>`. An exact retry
  returns S06 `idempotent`, which the coordinator reports as `already_applied`. A
  retry with a new key fails S06 CAS. The coordinator then sees the proposal's
  consumption and also reports `already_applied` with the original resulting
  version. Otherwise, S06 stale returns `stale` and closes the decision. Any
  other binding error returns `conflict`. Any exception inside the transaction
  returns `failed` / `transaction_failed`; S06 has already rolled everything
  back, so the request can be retried.
- **Cancel vs apply.** Apply compare-and-sets `waiting_approval → completed` in
  its own transaction, and cancel compare-and-sets the same source status.
  Exactly one of them wins.
- **Concurrent opens.** A decision that finds a concurrent sibling cancels itself
  as `superseded`. At most one decision stays open.

Actors are required and explicit:

| Action | Allowed actor prefix |
| --- | --- |
| approve / reject / fallback | `human:`, `operator:` |
| batch approval | only `approve_by_policy`, with `batch_policy` configured at construction and recorded as `policy:<name>` |
| restoration | `human:`, `operator:`, `harness:` |

Model (`model:`) and policy identities can never restore a machine or select a
fallback.

## Supervisor sessions and threads

The S09 ledger and S10 trace store hold thread-bound SQLite connections. For
this reason `run_supervisor` takes a `SessionFactory`, and the factory builds
the `SupervisorWorkflow`, provider, ledger, and trace **inside** the worker
thread. `SupervisorSession.ledger_path` lets `cancel()` cancel the ledger row
through its own handle. `SupervisorWorkflow.cancel()` now tolerates the
cross-thread `sqlite3.ProgrammingError`; its flag is checked before each attempt
and after each response.

When `timeout_seconds` expires, or the optional `deadline` event is set by an
operator control, timer, or deterministic test barrier, the coordinator marks the in-flight run as
abandoned and cancels the decision as `timed_out`. When the worker later
finishes, it appends `late_supervisor_result_discarded` to the **same run**.
`wait_for_late_result()` joins the worker for tests and for shutdown.

## Restoration authority

The supervisor can only recommend a repair. The `supervisor_outcome_recorded`
audit stores the hypothesis IDs with `confirmed_restoration = false`. The S10
claim check rejects affirmative "restored/repaired" assertions, and the tool
registry has no restoration tool. Capacity returns only through
`confirm_restoration`. That call has an explicit actor and checks that the
machine is currently down or in maintenance. It then advances to the first
second after both the clock and any pre-staged plan event, with one
`machine_restored` intervention whose ID is `restore-<confirmation_id>`, and
appends a `restoration_confirmed-<id>` audit. Restoration applies no schedule.
Using the restored capacity needs a new decision, proposal, and approval.

## Failure boundaries

| Failure | Result | Preserved |
| --- | --- | --- |
| Provider outage after bounded retries | decision `paused` (`provider_failure`) | Full operational identity: snapshot hash, version, revision, time, orders, machines, operations, attempts, events |
| Malformed or invalid tool calls ×3 | `paused` (`repeated_invalid_turns`) | same |
| Tool, attempt, token, or money budget | `paused` (`budget_exhausted`) | same |
| Solver unknown or unavailable | `paused` (`solver_unknown`) | same |
| Infeasible recovery | `paused` (`no_feasible_schedule`) | same |
| Stale state at any barrier | `cancelled` / `stale` | Applied schedule. Only the concurrent change itself is visible. |
| Transaction failure during apply | `failed` / `transaction_failed`; decision still awaiting | Everything, including approval consumption and the order draft |
| Process death inside the transaction | SQLite rollback on next open; retry applies once | Everything |
| Process death after commit | Restart sees `applied`; any retry returns `already_applied` | One event group |

Only workflow, audit, approval, and proposal records may change in these cases.
`run_status()` reports `paused_blocked` together with the reason code. A
deterministic fallback appears as a separate decision with
`source = deterministic_fallback` and `fallback_for_workflow_id`, plus a
`deterministic_fallback_selected` audit with `counts_as_llm_success = false`.

Hard horizon violations are rejected by the independent validator
(`hard_horizon_violation`). Due dates are soft. A missed due date never makes a
candidate infeasible. `review.due_date_warnings` reports each predicted miss
from the exact projection that application would produce
(`ApplicationServices.preview_application` plus
`metrics.forecast_order_outcomes`). Realized tardiness appears in the run KPIs.

## Restart and replay

- **Restart.** `RecoveryCoordinator(services)` in a new process rebuilds every
  decision from SQLite. `restore(run_id)` lists awaiting approvals, applied
  decisions, and orphaned open decisions whose supervisor lived in a dead
  process, and states `approvals_from_history_are_actionable = false`. Awaiting
  decisions still pass the normal fresh-state checks in `approve` and `apply`.
- **Operational replay.** `events.jsonl` (S06 portable bundle) restores
  operational state standalone: attempts, interruptions, restarts, wasted work,
  revisions, and metrics. It contains no lifecycle records.
- **Lifecycle export.** `lifecycle.json` (the S06 export) and `lifecycle_view.json`
  (`workflows.lifecycle_view`) show proposal status (`applied`,
  `awaiting_approval`, `stale`, `superseded`, `rejected`, `timed_out`,
  `run_reset`, `blocked`, …), approval actors, and the consuming mutation. The
  view is marked `actionable: false`. No API imports historical approvals, and a
  fresh database built from a replay bundle has no proposal or approval that
  could be applied.

## Scenario runner

```bash
uv run shopfloor-pilot recovery --provider scripted-fake --scenario <normal|urgent_order|machine_breakdown|no_feasible_recovery> \
  --seed 0 [--approval-policy deterministic] --output <empty dir>
uv run shopfloor-pilot recovery-decide --output <dir> --workflow-id <id> --proposal-hash <sha256> \
  --actor human:<name> --decision approve|reject
# The brief's form is equivalent to `recovery ... --approval-policy deterministic`:
uv run shopfloor-pilot agent --provider scripted-fake --scenario machine_breakdown --seed 0 \
  --approval-policy deterministic --output .runtime/recovery
```

The Python entry points are `workflows.scenario_runner.run_recovery_scenario` and
`decide_recovery_scenario`.

| Scenario | Phases (decision kind / source) | Expected terminal |
| --- | --- | --- |
| `normal` | initial planning / supervisor order request (`normal-en-plate-today`) | `completed` / `scenario_completed` |
| `urgent_order` | initial / deterministic baseline (`positive_time_origin_floor`); advance to 3599; urgent / supervisor (`urgent-zh-plate-90min`), applied at 3600 | `completed` |
| `machine_breakdown` | initial / deterministic baseline (`scarce_capability_first`); advance to the 2700 fault; recovery / supervisor (CNC02), applied at 2701 | `completed` |
| `no_feasible_recovery` | initial / deterministic baseline; advance to the 3000 double fault; recovery / supervisor (CNC02, CNC03) | `blocked` / `no_feasible_schedule` |

Without `--approval-policy`, the runner stops at the first proposal, including
initial planning. It writes `approval_request.json` with the proposal, deltas,
objective, due-date warnings, claims, repair recommendation, evidence label, and
current applicability. `recovery-decide` records the human decision, applies it,
and resumes. A repeated decide call is idempotent. Pre-advances and the final
advance use durable advance IDs, so a resumed run never duplicates time
movement.

The output directory contains `state.sqlite3`, `provider_ledger.sqlite3`,
`agent_trace.sqlite3`, `recovery_manifest.json`, `summary.json`,
`decisions.json`, `invariants.json`, `events.jsonl`, `lifecycle.json`,
`lifecycle_view.json`, `metrics.json`, `final_snapshot.json`,
`traces/<workflow>.json`, `planning_policy.json` (fault scenarios), and
`approval_request.json` while waiting.

`summary.json` (`command = "recovery"`) carries `status`, `reason_code`,
`expected_terminal`, `terminal_matches_expectation`, `invariants_passed`,
`failed_invariants`, the per-decision `source`, `provider`, `status`, `outcome`,
`decision_time_seconds`, `decision_boundary_seconds`, and
`consumed_approval_id`, the `evidence_label` (offline vs live), the approval
policy, the final identity, and the replay hash.

Exit codes are as follows. Exit 0 covers an expected terminal with all
invariants passing, a stop that awaits human approval, and an operator-chosen
rejection. Exit 4 covers any other terminal or a failed invariant. Exit 2/3
cover usage and input errors (S06 codes). With `deepseek` and incomplete paid
settings, the runner writes `SKIPPED` (gate `S11-A11`) and exits 2 without
sending a request.

## Invariants (`workflows.check_run_invariants`)

| Code | Check |
| --- | --- |
| `applied_plans_pass_independent_validator` | Every consumed proposal re-validates against its historical base checkpoint. |
| `one_plan_application_per_consumed_approval` | The `plan_applied` mutations equal the consumption mutations, and no proposal is consumed twice. |
| `approvals_by_human_or_named_policy_only` | Every approval actor is `human:`, `operator:`, or `policy:`. |
| `completed_work_never_changes` | A completed attempt is byte-identical in every later checkpoint. |
| `unaffected_running_work_fixed` | Each running attempt keeps its machine, actual start, and window across every plan application. |
| `interrupted_attempts_restart_full_duration` | Consumed time equals elapsed time, and the successor is the next generation of the same operation with the full planned duration. |
| `wasted_work_counted_once` | KPI waste equals the sum of interrupted elapsed time, and the metrics reject overlap or work while a machine is down. |
| `replay_reproduces_current_checkpoint` | Replay from version zero gives the current snapshot hash. |
| `decisions_bound_to_recorded_boundary` | Applied ⇔ consumed; proposal base equals the decision version; application time equals the decision boundary. |

## Upstream changes for recovery integration

| Change | Reason |
| --- | --- |
| `ApplicationServices.cancel_workflow` (operator) | S06 could not close a stale, rejected, timed-out, superseded, or reset `waiting_approval` workflow. |
| `ApplicationServices.append_audit` (operator) | Run-scoped, non-operational audit without reaching into the repository. |
| `ApplicationServices.list_reset_successors`, `SQLiteStateRepository.list_reset_successors` | Make reset lineage obsolete-run aware. |
| `ApplicationServices.preview_application`, `metrics.forecast_order_outcomes` | Prediction-only due-date warnings computed from the exact application projection. |
| `SupervisorWorkflow.cancel` tolerates `sqlite3.ProgrammingError`; `run_id` / `workflow_id` properties | Cross-thread cancellation of a supervisor whose ledger is thread-bound. |
| `baseline._generate` / `_scarce_capability_first_initial` take an optional `workflow_id` (defaults unchanged) | Deterministic initial planning fills the S11 decision workflow. |
| CLI `recovery`, `recovery-decide`, and `agent --approval-policy deterministic` without `--bootstrap-policy` | The S11 runner. That `agent` form was a usage error in S10. |

## Live probe findings built into the contract

The S11 close-out probe (70 live runs) changed these behaviours:

- **Decision-boundary settling.** At a closed second `T` the boundary is `T + 1`.
  If a running operation completes exactly then, S06 cannot pre-stage the plan.
  `open_decision` refuses with `decision_boundary_blocked` (`advance_to_seconds`),
  and `settle_decision_boundary(run_id, prefix)` advances through the completion
  with durable `<prefix>-settle-<t>` IDs. The runner calls it before every
  decision and logs `boundary_settle`. The decision time is then the settled
  second, recorded honestly.
- **Outcome-claim filter.** `tools.supervisor_tools.forbidden_claim_phrase` excuses
  a copula assertion only when a negation directly precedes it ("has not been
  applied") or opens its clause ("no cause is confirmed", "… and nothing is
  confirmed"). Rejections carry the matched `phrase` and a rephrasing `hint`.
- **Model feedback.** Repeated-call notices reach the model. A `malformed_json`
  rejection says the arguments were probably truncated. `evidence_kind_mismatch`
  names `claim_kind` and `allowed_for_this_claim`. The order and abnormality
  `next_step` hints say when the evidence is already sufficient.
- **Semantic claim audit.** `workflows.claim_audit.audit_claims(claims, snapshot)`
  scores machine-status words, order counts, `t=` times (a time after the snapshot
  is a contradiction; earlier history is fine) and state versions as
  consistent / ambiguous / contradiction. "Unavailable" for a busy machine is
  *ambiguous*. The audit is an evaluation aid, not an authority.

## Known limitations

- An open decision can be cancelled, but its supervisor cannot resume after a
  process restart because its working state lives in memory. The decision is
  reported as orphaned and needs an explicit cancel.
- Rejection ends the scenario run as `blocked/proposal_rejected`. A follow-up
  proposal needs a new decision through the coordinator API; the CLI does not
  re-plan automatically.
- The coordinator's open-decision guard is an orchestration rule, not an OS
  lock. A writer that bypasses it, such as a direct `services.advance_simulation`
  call, is still caught by version and CAS checks. Tests exercise this
  defense-in-depth path deliberately.
- Restoration audits commit after the restoration advance, not in the same
  transaction. The `machine_restored` event and its `restore-<id>` intervention
  ID are the authority. The audit records the actor.
