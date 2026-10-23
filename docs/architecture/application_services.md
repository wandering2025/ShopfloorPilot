# Application services, approvals, reports, and operational metrics

Status: S06 architecture and operator-facing contract

Schema context: domain, event, metric, report, and portable replay schema `1`

Scope: deterministic application services, approved plan application, the no-provider
baseline CLI, replayable reports, and KPI definitions

## Decision summary

`ApplicationServices` is the shared deterministic boundary used by the S06 baseline.
Its `for_model()` facade is the implemented restricted exposure for model-readable
adapters. SQLite remains authoritative; services do not keep a second writable copy
of a run. Scheduling creates an immutable proposal, a human or explicitly selected
batch policy records a separate decision, and only `apply_approved_plan` may turn that
exact approved proposal into operational events.

Plan application is one SQLite transaction. Order additions, supported resource
changes, the complete plan projection, approval consumption, audit evidence, event
envelopes, workflow completion, checkpoint, version compare-and-swap, and idempotency
result either all commit or all roll back. A proposal or approval by itself never
changes operational state.

Metrics and reports are derived from a version-zero checkpoint plus the persisted
event prefix. Portable replay carries that checkpoint and those exact envelopes, so
it does not load a scenario, sample randomness, call a provider, or solve again.

## Implemented service surface

The constructor and full production service surface are shown below. Type names are
the strict domain, event, storage, metric, report, scheduling, and simulation models
exported by their packages.

```python
ApplicationServices(
    repository: SQLiteStateRepository,
    catalog: DomainCatalog,
    seed: int = 0,
)

services.capabilities -> ServiceCapabilities
services.for_model() -> ModelReadableApplicationServices

services.create_run(
    initial_snapshot: OperationalSnapshot,
    *,
    created_at_utc: str | None = None,
) -> RunRecord
services.reset_run(
    source_run_id: str,
    new_initial_snapshot: OperationalSnapshot,
    *,
    created_at_utc: str | None = None,
) -> RunRecord
services.get_snapshot(run_id: str) -> OperationalSnapshot
services.get_checkpoint(
    run_id: str,
    state_version: int | None = None,
) -> SnapshotCheckpoint
services.list_events(
    run_id: str,
    after_sequence: int = 0,
) -> tuple[EventEnvelope, ...]
services.export_lifecycle(run_id: str) -> LifecycleExport
services.calculate_run_metrics(run_id: str) -> RunMetrics
services.calculate_decision_latency(
    *,
    start_simulated_seconds: int,
    end_simulated_seconds: int,
    started_at_utc: str,
    completed_at_utc: str,
) -> DecisionLatency
services.build_proposal_comparison(
    run_id: str,
    proposal_id: str,
    *,
    expected_proposal_hash: str | None = None,
    decision_latency: DecisionLatency | None = None,
) -> ProposalComparisonReport
services.export_proposal_reports(
    run_id: str,
    proposal_id: str,
    output_directory: str | Path,
    *,
    expected_proposal_hash: str | None = None,
    decision_latency: DecisionLatency | None = None,
) -> ProposalReportPaths
services.export_replay_bundle(
    run_id: str,
    path: str | Path,
) -> ReplayBundleHeader
ApplicationServices.load_replay_bundle(path: str | Path) -> ReplayBundle
ApplicationServices.replay_bundle(
    bundle_or_path: ReplayBundle | str | Path,
) -> ReplayResult

services.advance_simulation(
    run_id: str,
    target_seconds: int,
    advance_id: str,
    *,
    interventions: Sequence[ScheduledIntervention] = (),
    observations: Sequence[Observation | ObservationTemplate] = (),
) -> AdvanceResult
services.ensure_workflow(
    run_id: str,
    workflow_id: str,
    *,
    payload: Mapping[str, JsonValue] | None = None,
    updated_at_utc: str | None = None,
) -> WorkflowRecord
services.for_model().ensure_workflow(
    run_id: str,
    workflow_id: str,
    *,
    updated_at_utc: str | None = None,
) -> WorkflowRecord
services.mark_workflow_blocked(
    run_id: str,
    workflow_id: str,
    *,
    reason_code: str,
    details: Mapping[str, JsonValue] | None = None,
    updated_at_utc: str | None = None,
) -> WorkflowRecord
services.generate_proposal(
    run_id: str,
    workflow_id: str,
    proposal_id: str,
    *,
    proposed_order_deltas: Sequence[OrderDelta] = (),
    proposed_resource_deltas: Sequence[ResourceDelta] = (),
    not_before_seconds: int | None = None,
    config: SolverConfig | None = None,
    created_at_utc: str | None = None,
) -> SchedulingResult
services.save_validated_proposal(
    candidate: CandidateProposal,
    *,
    created_at_utc: str | None = None,
) -> CandidateProposal
services.inspect_conflict(
    run_id: str,
    proposal_id: str,
    expected_proposal_hash: str | None = None,
) -> ProposalConflictReport
services.record_human_decision(
    run_id: str,
    proposal_id: str,
    expected_proposal_hash: str,
    *,
    approval_id: str,
    decision: ApprovalDecision,
    actor: str,
    reason: str | None = None,
    decided_at_utc: str | None = None,
) -> ApprovalRecord
services.record_deterministic_policy_decision(
    run_id: str,
    proposal_id: str,
    expected_proposal_hash: str,
    *,
    approval_id: str,
    policy_name: str,
    decision: ApprovalDecision = ApprovalDecision.APPROVED,
    reason: str | None = None,
    decided_at_utc: str | None = None,
) -> ApprovalRecord
services.apply_approved_plan(
    run_id: str,
    proposal_id: str,
    expected_proposal_hash: str,
    approval_id: str,
    idempotency_key: str,
    effective_time_seconds: int | None = None,
) -> PlanApplicationResult
```

`apply_approved_plan` also accepts an underscore-prefixed failure injector in tests;
that private rollback seam is not part of the production surface above.

`generate_proposal` invokes the local scheduler against the current snapshot. A
successful candidate has already passed independent validation and is persisted.
`save_validated_proposal` is the boundary for externally assembled deterministic
candidates: it validates against current authority before persistence. Both retain
the candidate as a proposal only; neither activates it. `inspect_conflict` is
read-only and reports hash, run, version, snapshot, revision, or validation conflicts
without refreshing, approving, or replacing the proposal.

`advance_simulation` is an operator/harness action. It rebuilds `SimulationRuntime`
from persisted authority for every call and then applies only the explicitly supplied
target, interventions, and observations. A restore intervention is therefore an
explicit operator or scenario-harness input; it is not inferred from telemetry.

## Capability separation

`ServiceCapabilities` is an allowlist used by adapters. The current sets are:

| Exposure | Methods |
| --- | --- |
| Model-readable facade | `ensure_workflow`, `generate_proposal`, `save_validated_proposal`, `inspect_conflict`, `get_snapshot`, `get_checkpoint`, `list_events`, `export_lifecycle`, `calculate_run_metrics`, `calculate_decision_latency`, `build_proposal_comparison` |
| Operator | `create_run`, `reset_run`, `record_human_decision`, `apply_approved_plan`, `advance_simulation`, `mark_workflow_blocked`, `cancel_workflow` (S11), `append_audit` (S11) |
| Named batch policy | `record_deterministic_policy_decision` |

`services.for_model()` returns `ModelReadableApplicationServices`, which exposes only
the first row. It has no decision, approval application, simulation advance,
intervention, blocked-terminal transition, file-export, or reset method. The
capability sets are disjoint by model validation. This is an API exposure boundary,
not an operating-system sandbox; adapters must hand out the restricted facade rather
than the full service object.

The model facade's `ensure_workflow` overload accepts neither status nor payload. It
can only atomically create an empty `running` workflow or return an existing `running`
record unchanged. The full service overload may supply initial bookkeeping payload,
but it is also `running`-only and never updates an existing record. Thus neither path
can manufacture or revive `waiting_approval`, `completed`, `failed`, or `cancelled`.

The full service also owns `export_proposal_reports`, `export_replay_bundle`, and the
static portable replay loader/reducer. Those file-oriented helpers are used by the
CLI but are deliberately absent from both the model facade and the capability
allowlists.

A human actor is normalized to a `human:` prefix and cannot claim a `policy:` or
`model:` identity. The deterministic batch actor is recorded as
`policy:<policy_name>`. Neither actor type is implicit: the caller must invoke a
decision method with an exact proposal hash.

## Workflow and proposal lifecycle

Workflow records are durable, run-scoped, non-operational bookkeeping. Their schema
supports `created`, `running`, `waiting_approval`, `completed`, `failed`, and
`cancelled`.

The implemented transitions are deliberately small:

1. `ensure_workflow` uses one `BEGIN IMMEDIATE` transaction to insert a `running`
   record or return an existing `running` record unchanged. Initial payload is stored
   only on insertion. Any non-running record is a conflict rather than a reset.
2. A successful `generate_proposal` or `save_validated_proposal` atomically persists
   the immutable candidate and compare-and-sets the workflow from `running` to
   `waiting_approval`, binding `pending_proposal_id` and `pending_proposal_hash` in its
   payload. An exact retry is idempotent. A different candidate cannot replace that
   binding, and a terminal workflow cannot be revived; either conflict rolls back the
   proposal insert as well.
3. Recording an approval or rejection persists an immutable `ApprovalRecord` but
   does not change the workflow or operational state. In particular, rejection alone
   does not create a terminal workflow; the usual workflow remains
   `waiting_approval`.
4. Successful `apply_approved_plan` changes that exact workflow from
   `waiting_approval` to `completed` in the same transaction as approval consumption,
   audit, events, checkpoint, and version CAS.
5. `mark_workflow_blocked` is an explicit operator action for a running workflow. It
   atomically changes `running` to schema-v1 status `failed` and adds
   `outcome = "blocked"`, `reason_code`, and `details` to the lifecycle payload. An
   exact repeat is idempotent, conflicting terminal content is rejected, and the
   operation emits no operational event or version change.

6. S11 adds `cancel_workflow(run_id, workflow_id, *, reason_code, outcome, details)`,
   an explicit operator action that compare-and-sets `running` or
   `waiting_approval` to `cancelled` with `outcome`/`reason_code`/`details` in the
   payload, without operational mutation. Approved application compare-and-sets
   `waiting_approval` inside its own transaction, so a cancelled workflow cannot be
   applied; proposal persistence already refuses closed workflows. Exact repeats are
   idempotent. S11 also adds the read-only `list_reset_successors(run_id)`,
   `preview_application(run_id, proposal_id, expected_proposal_hash)` (the exact
   projected snapshot an approved application would produce), and operator-side
   `append_audit(record, workflow_id=None)`. See
   [recovery_integration.md](recovery_integration.md).

No decision automatically produces `failed` or `cancelled`, and approval without
application does not produce `completed`. Because rejection leaves its exact binding
in `waiting_approval`, a revised candidate must use a new workflow ID; S06 has no
implicit supersede-after-rejection transition. The proposal/lifecycle pair is atomic
but non-operational. The stronger production mutation transaction starts at approved
application and additionally covers deltas, plan, events, checkpoint, version, audit,
approval consumption, and workflow completion.

Lifecycle export is run-scoped and contains workflows, proposals, approvals, audit
records, and approval-consumption records. Historical approvals in an export are
evidence, not permission to invent a new live mutation.

## Exact approval binding

`CandidateProposal.proposal_hash` covers all immutable proposal content:

- schema version, run, workflow, proposal, base state version, and base snapshot hash;
- next plan revision;
- every assignment and the objective;
- declared order deltas; and
- declared resource deltas.

Wall-clock creation metadata and the hash field itself are excluded. Changing a
schedule, order, resource change, identity, version, or revision therefore changes
the proposal hash.

An `ApprovalRecord` repeats the exact `(run_id, workflow_id, proposal_id,
proposal_hash, base_state_version)` binding and adds the decision, actor, reason, and
decision time. `apply_approved_plan` performs all of these checks before entering the
write transaction:

1. load the stored proposal in the requested run and compare the caller's expected
   hash;
2. load the named approval and require `decision == approved`;
3. require its run, workflow, proposal ID/hash, and base version to match the stored
   proposal exactly;
4. load the proposal's historical base checkpoint; and
5. independently validate the proposal and its exact declared deltas against that
   checkpoint.

Validation against the historical base makes an exact retry reproducible. It does
not weaken freshness: the repository still compares that base version with the live
run version inside the transaction. An approval for one proposal cannot authorize a
different proposal from the same version, and recomputing a valid hash for changed
content still requires a new matching approval.

## Atomic application, CAS, and idempotency

The application service currently materializes only supported operational deltas:
`OrderDeltaKind.ADD` becomes `order_registered`, and
`ResourceDeltaKind.MARK_DOWN` becomes `machine_down`. It then appends one
`plan_applied` event containing the independently validated complete operation and
attempt projection. Event priority gives the group this order at one simulated
second: resource mark-downs, order registrations, then plan application.

Consequently, an urgent draft order and the plan that schedules it share one
mutation ID, one base/result version pair, and one commit. The draft order is absent
before approval. There is no intermediate committed state containing the order
without its plan, or the plan without its order.

The service also computes the expected resulting snapshot hash. Its transaction
participant consumes the exact approved proposal and requires that result hash, then
appends an audit containing the approval/proposal IDs, proposal/base hashes, hashes of
the order and resource delta tuples, and the result hash, and compare-and-sets the
workflow from `waiting_approval` to `completed`.

`SQLiteStateRepository.apply_mutation` runs under `BEGIN IMMEDIATE` and commits:

- the event group and exact event envelopes;
- approval consumption and the application audit;
- the workflow compare-and-set from `waiting_approval` to `completed`;
- the resulting checkpoint;
- the compare-and-swap update of `runs.current_state_version`;
- and the stored idempotency outcome.

An exception at any point rolls all of these back. The previous plan, orders,
history, approval-consumption set, audit set, workflow status, and current version
remain authoritative, and retrying the same request can commit once.

Idempotency is checked before live version CAS:

- the same `(run_id, idempotency_key)` and canonical request hash returns the original
  `MutationResult` with status `idempotent` and does not rerun the participant;
- the same key with different command content raises `idempotency_conflict`; and
- a new key from an old version raises `stale_state`.

`BEGIN IMMEDIATE` serializes SQLite writers. Two exact same-key writers yield one
commit plus one idempotent replay. Two distinct approved proposals from one base
version yield at most one commit; the other observes the CAS conflict. Lock waiting is
bounded by the repository busy timeout and surfaces as `database_busy`.

Application time is the current simulated boundary when its event priority is still
open, otherwise the immediately following second. A caller may explicitly select
only current time or current time plus one, within the hard horizon. Resource
mark-down deltas must use the current boundary, assignments cannot start before the
application boundary, and a future application cannot skip a due running completion.

## Crash and restart behavior

The database commit is the activation boundary. No success depends on an in-memory
post-commit callback. If the process disappears after commit, a new
`ApplicationServices` instance rebuilds `SimulationRuntime` from the committed
checkpoint and event history on the next `advance_simulation` call. The accepted plan
then executes once. Repeating the same advance ID returns its idempotent outcome and
does not duplicate operation starts or completions.

If the process fails before commit, SQLite rollback leaves the previous checkpoint
and plan active. The approved request can be retried. Failure-injection tests cover
after event insertion, after checkpoint insertion/CAS, and immediately before commit.

## CLI and scenario-harness layering

The console entry point is `shopfloor-pilot = shopfloor_pilot.cli:main` and has two
commands:

```text
shopfloor-pilot baseline \
  --scenario {normal,urgent_order,machine_breakdown,no_feasible_recovery} \
  --seed INTEGER \
  --approval-policy deterministic \
  --output DIRECTORY

shopfloor-pilot replay --events EVENTS.jsonl --output DIRECTORY
```

The command layer calls these Python harness functions:

```python
run_baseline(
    scenario_id: str,
    *,
    seed: int,
    approval_policy: str,
    output: Path,
    wall_clock: Callable[[], datetime] | None = None,
) -> BaselineRunResult
run_replay(events_path: Path, *, output: Path) -> ReplayRunResult
```

The injectable clock must return timezone-aware datetimes. It exists to make decision
latency deterministic in tests; omitted in the real command, it reads live UTC.

Every option shown is required. `baseline` loads hash-verified local fixtures,
initializes a dedicated SQLite database, creates the run, generates and validates
each candidate, records the explicit named policy decision, exports its comparison,
applies through the same service boundary as a human decision, and advances the
persisted simulator. Reads, metrics, lifecycle export, proposal reports, and event
export also pass through `ApplicationServices`. It uses the local CP-SAT scheduler
but no LLM/provider, API key, or network connection.

`--approval-policy deterministic` is a scenario-harness policy, not a default
application permission. Omitting it is a usage error. Every accepted plan still has
an approval record with actor `policy:deterministic` and is consumed by the normal
atomic apply path.

The baseline has two additional, explicitly reported initial-planning policies. They
are scenario-harness policy, not general application-service behavior:

- `positive_time_origin_floor` is used only by `urgent_order`. It asks the existing
  solver for an initial plan no earlier than second 1, leaving a later external-event
  boundary available. The floor is the fixed public policy value `1`; it is not read
  from the evaluator intervention schedule.
- `scarce_capability_first` is used only to bootstrap the two fault scenarios. From
  the public catalog topology and current authoritative orders, it identifies a
  differentiating capability served by exactly one machine, creates a temporary
  due-date/weight priority view, and lets the existing CP-SAT engine produce the
  assignments. It then rebinds those assignments to the untouched authoritative
  orders, recomputes the original objective, validates again, and uses the ordinary
  approval/apply path. `planning_policy.json` records the selection inputs and states
  `uses_evaluator_future_truth = false`, `authoritative_orders_changed = false`, and
  `original_objective_optimal = false`.

Both initial-planning policies consume only the public catalog, public orders, and
current snapshot. They never inspect evaluator-only future faults, restoration times,
urgent-order payloads, future observations, expected outcomes, or other future truth.
The scenario harness alone reads evaluator interventions and observation templates.
It injects them through `advance_simulation` when their simulated boundary is reached;
they are not available through the model facade and are not diagnoses inferred from
observations. At the urgent-order boundary, the harness supplies the evaluator order
as a proposed `ADD` delta, still leaving it non-operational until exact approval and
atomic application.

In `no_feasible_recovery`, a proven infeasible recovery is the fixture's expected safe
`blocked` result and has process exit zero. The harness calls
`mark_workflow_blocked`, which records the failed/blocked lifecycle outcome without
operational mutation. No recovery candidate exists, is approved, or is applied.

Output must be a dedicated absent or empty directory. The CLI rejects the scenario
fixture tree, the repository root, non-directory paths, and non-empty directories;
it does not overwrite user artifacts.

## Baseline and report artifacts

A baseline output directory contains these stable artifacts:

| Artifact | Contents |
| --- | --- |
| `state.sqlite3` | Authoritative run, event, checkpoint, idempotency, and lifecycle state. |
| `events.jsonl` | Self-contained portable replay bundle described below. |
| `final_snapshot.json` | Final authoritative operational snapshot. |
| `metrics.json` | `RunMetrics` recomputed from version zero and the complete exported event stream. |
| `lifecycle.json` | Run-scoped workflows, proposals, approvals, consumptions, and audits. |
| `summary.json` | Structured command status, reason, versions, time, snapshot hash, event count, and artifact map. |
| `planning_policy.json` | Present when the urgent or fault bootstrap policy is used. |
| `proposals/<phase>/proposal_before.json` | Current-plan projection at the proposal base snapshot. |
| `proposals/<phase>/proposal_after.json` | Candidate projection at that same snapshot and horizon. |
| `proposals/<phase>/proposal_comparison.json` | Both projections, actual-at-snapshot metrics, predicted delta, and optional decision latency. |

Phases are `initial`, `urgent`, and `recovery` when those candidates exist. The
three proposal artifacts are built after the phase decision and before application,
so both plan projections remain bound to the candidate's exact base checkpoint. The
artifact map in `summary.json` uses relative paths. A replay output contains a
verified copy of `events.jsonl`, `final_snapshot.json`, `metrics.json`, and
`summary.json`; it does not create a database or lifecycle records.

## Portable replay contract

`events.jsonl` is not a list of bare events. Its first line is a strict
`ReplayBundleHeader` with `record_type = "replay_header"` and:

- the run ID and embedded version-zero `SnapshotCheckpoint`;
- event count and canonical operational stream hash;
- expected final snapshot hash; and
- embedded `RunMetrics` plus its canonical hash.

Every following line is a strict `ReplayEventRecord` with `record_type = "event"`
and one full serialized `EventEnvelope`. The stream hash binds the initial checkpoint
and ordered canonical operational content. By the repository-wide canonical contract,
it deliberately excludes non-authoritative `recorded_at_utc` wall-clock metadata;
this is operational replay integrity, not byte-for-byte transport authentication.
Blank, malformed, cross-run, truncated, extra, reordered, or operationally modified
records fail closed through schema, count, sequence, replay, or hash checks.

`ApplicationServices.load_replay_bundle` validates JSONL structure and bound hashes;
`ApplicationServices.replay_bundle` reduces only the embedded envelopes from the
embedded checkpoint, recalculates metrics, and requires the final checkpoint hash and
full metrics/hash to match the header. These static service entry points do not need
a repository or catalog. Their reporting implementation imports no scheduler or
scenario sampler and makes no API call. The replay command verifies first, then
writes the verified artifacts to its explicit empty output directory.

## Metric window and basis

`calculate_run_metrics(initial_checkpoint, events)` requires the run's version-zero
checkpoint and an ordered operational event prefix. It first replays that prefix.
The common actual duration window is:

```text
[initial_checkpoint.snapshot.simulated_time_seconds, replayed_as_of_seconds)
```

Events at `replayed_as_of_seconds` are included in the observed state and completion
counts, while elapsed durations remain half-open. Thus work completed at second 10
after running on `[0,10)` contributes ten processing seconds and is complete in the
as-of-10 report. The forecast horizon is always the snapshot's authoritative
`hard_horizon_seconds`; proposal projection windows are `[as_of, hard_horizon)`.

Metric bases are explicit:

- `actual`: resolved from persisted execution events;
- `predicted`: derived from the currently applied plan but not yet observed;
- `censored`: a final actual outcome is not yet observable at the report boundary;
- `unavailable`: the current event prefix contains no complete plan-based forecast.

## Completion, makespan, and tardiness

For order `i`, completion is the maximum completed-at time of all terminal route
operations across its units. When complete:

```text
T_i = priority_weight_i * max(0, completion_i - due_i)
```

The order's actual and resolved prediction values are identical. For an unfinished
order, actual completion and actual tardiness are null, `actual_basis` is `censored`,
and its observable tardiness lower bound is:

```text
priority_weight_i * max(0, as_of - due_i)
```

Its current-plan predicted completion uses the latest applicable terminal attempts:
completed actual end, running actual start plus full planned duration, or scheduled
window end. An interruption without a scheduled/running successor makes that order's
prediction unavailable. Available predicted tardiness uses the same weighted formula
with predicted completion.

Aggregate final actual makespan and weighted total tardiness are emitted only when
every included order is complete:

```text
actual_makespan = max(actual order completions)
actual_weighted_tardiness = sum(T_i)
```

If any order is unfinished, both final actual fields are null and the aggregate basis
is `censored`. `actual_makespan_lower_bound_seconds` is the maximum of `as_of` and
completed-order completion times. The report separately exposes exact completed-order
tardiness and the sum of completed exact values plus unfinished lower bounds. It never
fills an unfinished order with zero.

Aggregate prediction is available only when every order has an actual or current-plan
completion. It is the maximum predicted completion and the sum of predicted weighted
tardiness. Once every order completes, its aggregate prediction basis resolves to
`actual`. For an empty run, both actual and predicted makespan equal the initial
simulated time and both tardiness totals are zero.

## Throughput and WIP

Throughput counts completed orders, not units, over the common actual window:

```text
throughput_orders_per_hour = completed_order_count * 3600 / window_duration_seconds
```

The count and denominator are always included. The rate is null for a zero-duration
window.

WIP contains orders whose release time is at or before `as_of` and whose status is
neither `completed` nor `rejected`. `wip_order_count` counts those order identities;
`wip_unit_count` sums their quantities. Future unreleased orders are not WIP.

## Machine utilization, idle, downtime, and waste

Machine accounting uses the same actual window. Availability calendars and machine
down/restore transitions are reconstructed piecewise from the initial checkpoint and
events. For each machine:

```text
scheduled_capacity = duration of authoritative calendar intervals in the window
off_calendar = window_duration - scheduled_capacity
downtime = elapsed duration in down/maintenance status
capacity_lost_to_downtime = intersection(downtime, scheduled calendar)
```

Completed and interrupted attempts contribute their clipped actual intervals. A
running attempt contributes `[actual_start, as_of)`. Scheduled and cancelled attempts
contribute no actual busy time. Because restart policy discards partial progress, the
entire actual interval of an interrupted attempt is waste; completed and currently
running intervals are productive.

```text
busy = productive_processing + wasted_processing
idle = scheduled_capacity - busy - capacity_lost_to_downtime
utilization = busy / scheduled_capacity
productive_utilization = productive_processing / scheduled_capacity
```

Utilization is therefore gross busy utilization and includes interrupted waste in
its numerator. Downtime that intersects scheduled capacity stays in the denominator,
so it reduces utilization; down time outside the authoritative calendar is reported
as downtime but not capacity loss. Both ratios are null when scheduled capacity is
zero. Overlapping actual attempts, work while down, work outside the calendar, and
negative accounting residuals are rejected rather than normalized.

Run-level `wasted_processing_seconds` is the sum of per-machine waste.

## Recovery and decision latency

One recovery record is created for each interrupted attempt. The disruption boundary
is that attempt's actual end. A direct successor is identified only by
`restart_of_attempt_id`.

If the successor has started:

```text
actual_recovery_duration_seconds =
    successor.actual_start_seconds - interrupted.actual_end_seconds
```

If it has not started, actual recovery is censored at `as_of`; the lower bound is
`as_of - disruption`. A scheduled successor additionally supplies a separately
labeled predicted resumption and predicted recovery duration. Zero simulated
recovery is valid when resumption occurs at the same simulated boundary.

Decision/diagnosis/approval waiting is a different metric:

```python
calculate_decision_latency(
    start_simulated_seconds=...,
    end_simulated_seconds=...,
    started_at_utc=...,
    completed_at_utc=...,
)
```

`simulated_elapsed_seconds` is the difference between the simulated bounds.
`wall_clock_elapsed_ms` is calculated from timezone-aware ISO-8601 timestamps and
floored to whole milliseconds. A proposal reviewed while simulation is paused can
legitimately have zero simulated elapsed time and positive wall-clock latency. Wall
time is never added to recovery time or used to advance the simulation.

The baseline stores `candidate.created_at_utc` at the phase request start and ends the
interval at the persisted approval decision, so this wall duration includes local
proposal generation before the explicit policy decision. It uses the proposal-base
snapshot time for both simulated bounds, so baseline proposal reports record zero
simulated decision time and a separate non-negative wall-clock duration. The
deterministic scenario tests inject a stepped clock and assert 125 ms; the normal CLI
uses its timezone-aware UTC wall clock.

## Before/after proposal reports

`build_proposal_comparison` requires an event prefix that exactly reconstructs the
supplied current checkpoint, a candidate bound to that checkpoint and next revision,
and a valid independent report whose objective equals the candidate objective.

Both sides identify the run, snapshot state version/hash/time, plan revision, included
orders, objective basis/value, and the common `[snapshot_time, hard_horizon)`
prediction window. The before objective comes from the applied plan's current
prediction and may be explicitly unavailable. The after objective is the candidate's
validated prediction and includes its proposal ID/hash.

When both objectives exist, `predicted_change` is exactly `after - before`; negative
values mean a forecast reduction under the lower-is-better objective. The comparison
always has `realization_status = "not_observed"` and
`realized_improvement = null`. Actual-at-snapshot metrics are included once and held
constant across both sides. Optional `DecisionLatency` retains the separate simulated
and wall-clock decision intervals.

## Stable command exits and output

Successful results are emitted as one JSON object on standard output. Expected
failures are emitted as one JSON object on standard error with `status = "failed"`, a
stable `reason_code`, message, numeric exit code, and details.

| Exit | Name | Meaning |
| ---: | --- | --- |
| 0 | `SUCCESS` | Completed baseline, verified replay, or expected safe blocked fixture. |
| 1 | `SOFTWARE_ERROR` | Unexpected uncaught software failure. |
| 2 | `USAGE` | Parser/argument error, missing option, or unsupported explicit approval policy. |
| 3 | `INPUT_ERROR` | Rejected validated input such as an out-of-range seed, unsafe/non-empty output, or nonexistent replay file. |
| 4 | `WORKFLOW_FAILURE` | Deterministic service/simulation failure or rejected/tampered replay. |
| 5 | `ARTIFACT_FAILURE` | Metrics, reporting, storage, or filesystem artifact failure. |

Application-service failures retain their stable `ServiceErrorCode` as the CLI reason
when surfaced through the command boundary. The safe `no_feasible_recovery` result is
not an exception: its summary reports `status = "blocked"` and
`reason_code = "no_feasible_recovery"` with exit zero.

## Verification boundary

The S06 tests exercise explicit/no approval, human versus policy actors, stale and
wrong-run/hash rejection, old approval versus changed content, urgent-order atomicity,
all transaction failpoints, exact and concurrent idempotency, competing proposal CAS,
restart after commit, all four offline scenarios, portable normal/fault replay,
tamper rejection, report labeling, and hand-calculated metrics.

These contracts provide deterministic orchestration and accounting for the synthetic
MVP. They do not add physical safety control, calibrated failure prediction, root-cause
proof, setup/transport/inventory constraints, or permission for a model to approve or
apply work.
