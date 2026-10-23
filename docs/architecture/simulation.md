# Deterministic simulation and interruption handling

Status: S05. Schema context: S01 domain schema 1, S02 state/event authority,
S03 validation, and S04 scheduling.

## Authority and runtime model

`SQLiteStateRepository` is the authority for simulated time, plan revision,
machines, orders, operations, attempts, observations, and the ordered business-event
stream. `SimulationRuntime` never owns a second writable copy of manufacturing state.
Its SimPy `Environment` is a disposable timing projection used to wake deterministic
dispatchers at absolute integer seconds.

Every dispatcher rereads the latest persisted checkpoint before it can write. A
successful boundary is committed through the S02 compare-and-swap mutation API, and
the returned checkpoint becomes the runtime's new view. Reads also synchronize with
SQLite and rebuild the SimPy projection when another accepted write is detected.
Concurrent changes during a command raise `ConcurrentRuntimeError` after rebuilding
the local projection; the runtime does not silently retry a command against a new
state version. The one exception is an identical activation using the same durable
idempotency key: a race discovered during synchronization or by SQLite's idempotency
record returns the existing activation as `idempotent`, never as a second plan.

This gives the ownership split below:

| Concern | Owner |
| --- | --- |
| Operational truth and simulated clock | SQLite checkpoint and events |
| Event identity, ordering, idempotency, and state-version CAS | S02 repository |
| Wake-up times for scheduled starts and running completions | Disposable SimPy environment |
| Schedule feasibility and complete projected attempts | S03 validator |
| Recovery schedule construction and `not_before_seconds` | S04 scheduler |
| Scenario-only cause labels and future interventions | Evaluator/scenario harness, never the runtime projection |

No wall-clock thread advances the simulation. Sleeping, reading, pausing, or rendering
state has no operational effect.

## Public API

The public package exports `SimulationRuntime`, typed result/projection models, and
typed simulation errors. The principal entry points are:

```python
from shopfloor_pilot.simulation import SimulationRuntime

runtime = SimulationRuntime(repository, run_id, catalog, seed=17)

activation = runtime.activate_plan(
    candidate,
    idempotency_key="activate-plan-r1",
)
result = runtime.advance_to(
    300,
    advance_id="advance-through-300",
    interventions=(),
    observations=(),
)

public_state = runtime.pause()  # RuntimeProjection; no time movement
assert runtime.read() == runtime.projection()
assert runtime.projection() == runtime.project_public_state()

restored = SimulationRuntime.rebuild(repository, run_id, catalog, seed=17)
# `from_repository(...)` is an alias for the same rebuild operation.
```

`activate_plan` returns `ActivationResult`; `advance_to` returns `AdvanceResult`.
Both expose the resulting `snapshot` and allowlisted `projection`. Applied results
also expose the persisted `EventEnvelope` values through `events` and their domain
events through `domain_events`. `CommandStatus` distinguishes `applied`,
`idempotent`, and `no_op` outcomes.

`snapshot`, `now`, `pause`, `read`, `projection`, and `project_public_state` are
read-only from an operational perspective. `RuntimeProjection` contains current
machines, orders, operations, attempts, observations, derived blocked attempts, and
allowlisted event metadata. It does not expose arbitrary event payload dictionaries.

### Plan activation

Activation independently calls `validate_candidate` against the current authoritative
snapshot. Stale provenance raises `StalePlanError`; another invalid constraint raises
`InvalidPlanError`. Neither failure changes the checkpoint or event stream. The S05
boundary accepts no proposed order or resource deltas and raises
`UnsupportedPlanDeltaError` if either is present.

At the current simulated second, activation atomically persists `plan_applied`, any
start that is due and eligible at that second, and the corresponding synthetic
observations. A future `effective_time_seconds` is allowed only at exactly the next
integer second (`current + 1`). It pre-stages the `plan_applied` fact at that boundary
but does not move the simulated clock or start work. The subsequent explicit
`advance_to` processes starts at the effective second. A farther future plan event
would make intermediate S02 ordering impossible. Future activation is also rejected
if it would skip a running completion or contains an assignment before its effective
boundary. Once priority-60 plan application is pre-staged, callers cannot later insert
a priority-10-through-50 fact at that same second.

An activation idempotency key is durable. Repeating the same key and content returns
an idempotent result; reusing it for different content raises `CommandConflictError`.

This is deliberately a low-level execution/test harness. It does **not** consume a
human approval and does not make proposal deltas authoritative. S06 owns the
production boundary that binds approval to run, base version, and proposal hash, then
revalidates and atomically applies the approved plan and authorized deltas. Production
callers must use that S06 service rather than treat `activate_plan` as approval.

## Explicit, inclusive advancement

`advance_to(T, ...)` processes every known callback, intervention, and observation at
seconds up to and including `T`. The implementation deliberately drains SimPy with
`peek()` and `step()` because ordinary `Environment.run(until=T)` would exclude normal
events scheduled exactly at `T`. The final checkpoint's simulated time must equal
`T`; backward movement and targets beyond the hard horizon are rejected.

Each boundary handled by an advance dispatcher is one atomic S02 event group, sorted
by the established event priority. A future pre-staged plan is an earlier activation
group at that same second; the later advance group can append priorities after 60.

| Priority | Event | Boundary effect |
| ---: | --- | --- |
| 10 | `operation_completed` | Complete valid full-duration work and release its machine. |
| 20 | `machine_down` | Make a faulted machine unavailable. |
| 30 | `operation_interrupted` | Interrupt still-running work on that newly down machine. |
| 40 | `machine_restored` | Apply an explicit restoration confirmation. |
| 50 | `order_registered` | Register an explicitly injected order. |
| 60 | `plan_applied` | Activate a validated plan; this is normally its own activation group. |
| 70 | `operation_started` | Start eligible work on an available, unreserved machine. |
| 80 | `observation_recorded` | Persist explicit or generated synthetic telemetry. |
| 90 | `time_advanced` | Move the authoritative clock and close the second. |

Within one priority, construction is deterministic: callbacks and interventions use
their typed identity sort keys, explicit observations sort by observation ID, and
generated observations sort by machine ID. S02 sequence preserves that input order.
The group is reduced atomically, so readers never observe a fault of running work
without its same-group interruption, or a start without the other facts accepted at
that second.

The ordering has an intentional edge case: if an operation's full-duration completion
and a machine fault share second `T`, completion wins. That attempt completes, the
machine then goes down, and the completed attempt is not also interrupted. A new start
at `T` on the faulted machine is suppressed; a start on another eligible resource may
still proceed after its predecessor's same-second completion.

### Priority-90 closed boundaries and recovery floors

Here “priority 90” means the `time_advanced` event priority, not a statistical p90.
After an advance commits `time_advanced` at `T`, S02's last ordering key is at priority
90. A later command cannot append a lower-priority fact retroactively at `T`; it gets
`BoundaryClosedError`. This is what makes a processed second closed.

A recovery proposal after a fault at `T` is still based on and hashed against the
authoritative snapshot whose simulated time is `T`. Its new work must start at a later
open second. Use S04's explicit floor and then the same effective boundary:

```python
from shopfloor_pilot.scheduling import solve_schedule

fault_snapshot = fault_result.snapshot
recovery_second = fault_snapshot.simulated_time_seconds + 1
scheduled = solve_schedule(
    fault_snapshot,
    catalog,
    proposal_id="proposal-recovery-1",
    workflow_id="workflow-recovery-1",
    not_before_seconds=recovery_second,
)
if scheduled.is_usable:
    runtime.activate_plan(
        scheduled.candidate,
        idempotency_key="activate-recovery-1",
        effective_time_seconds=recovery_second,
    )
    runtime.advance_to(recovery_second, advance_id="advance-to-recovery-1")
```

`not_before_seconds=T+1` changes only future assignment lower bounds. Completed,
interrupted, and unaffected running history remains fixed, and candidate provenance
continues to name the real checkpoint at `T`. If `T` is already the hard horizon,
there is no legal later recovery boundary.

## Callback validity and duplicate prevention

Start and completion callbacks carry enough identity to compare themselves with
persisted state, but callback objects are not authority. Immediately before emitting a
fact, dispatch checks the exact persisted attempt identity and lifecycle fields:

- a start requires the same attempt, operation, machine, scheduled start/end window,
  `scheduled` status, completed predecessors, an available unreserved machine, and
  full `[start, end)` containment in that machine's latest persisted availability
  calendar;
- a completion requires the same attempt, operation, machine, actual start, scheduled
  window, absolute full-duration completion second, `running` status, and a running
  machine.

These persisted fields are the cancellation/generation guard. Replanning may leave an
old SimPy wake-up in memory, but moving its scheduled window makes the old start a
no-op. Interrupting an attempt changes its persisted status, so its old completion is
a no-op. A successor attempt has a distinct attempt generation and cannot satisfy the
old callback's identity check.

S02's reducer and idempotency rules are a second line of defense: an attempt cannot be
started or completed twice, mutation event identities are deterministic, and a reused
command identity with changed content conflicts. Successive accepted plans preserve
unaffected running attempts and their actual occupancy. Together these rules prevent
duplicate starts, completions, reservations, and overlapping actual execution.

## Fault, restart, and blocked-work semantics

A mid-operation fault first records `machine_down`, then interrupts the running
attempt in the same group. The interrupted attempt keeps its actual start, receives
the fault second as its actual end, and records elapsed seconds as consumed **wasted
work**. The operation/order becomes interrupted or blocked; consumed work is not
credited to a later attempt.

Recovery is not automatic. A subsequent valid plan must create the next attempt
generation, link `restart_of_attempt_id` to the interrupted attempt, and reserve the
operation's full original duration on a compatible available machine. Completed work
cannot be rescheduled, and unaffected running work remains non-preemptive and fixed.

The runtime does not infer repair from elapsed time, `unavailable_until_seconds`, a
diagnosis, or telemetry. A machine remains down until an explicit
`machine_restored` intervention is accepted. If the only capable machine remains down
through the hard horizon, its work stays scheduled/interrupted and visibly blocked;
the runtime invents neither a restoration nor a completion. `blocked_attempts` reports
the derived reason (`machine_down`, `machine_maintenance`, `machine_busy`,
`outside_availability_window`, `predecessor_incomplete`, or
`missed_scheduled_start`) without mutating the schedule.

## Durable commands, CAS, and partial advances

A forward advance has a caller-supplied `advance_id` and a canonical fingerprint over
its target, seed, public intervention content, and explicit observations. Its started
and completed audit records are committed inside the same SQLite transactions as
boundary facts. Repeating a completed advance with the same public content is
idempotent; reusing the ID with different content raises `CommandConflictError`.

Each intervention has its own durable `intervention_id` and public fingerprint.
Repeating the same identity/content, even under a later advance call, does not inject
the fault, restoration, order, or checkpoint twice. Conflicting public reuse fails.
New facts targeting an elapsed second raise `PastInjectionError`. A `checkpoint`
intervention is audit-only and therefore requires a forward boundary whose event group
can carry its audit; requesting a new checkpoint at the already-current second raises
`BoundaryClosedError`.

One `advance_to` can cross several callback seconds. It is intentionally not one large
database transaction: every simulated second is its own short CAS mutation. If a
later boundary fails or the process stops, earlier boundary facts remain valid and
committed. The absence of the completed audit marks the command partial. Repeating the
same `advance_id` and content rebuilds from SQLite, skips already-audited interventions,
and continues from the last committed checkpoint. It never rolls back real earlier
progress or grants a changed command the old identity.

Before the first boundary commits, the runtime validates the complete pending
intervention batch in time order: machine transitions must be valid, machines and
products must exist, added orders must be unique/pending/released by their boundary,
and duplicate faults or duplicate restorations for one machine at one second are
rejected. This prevents a predictable later invalid intervention from causing partial
progress. Per-boundary CAS conflicts,
unexpected runtime failure, or process termination can still leave a legitimately
partial multi-boundary advance, so the durable resume contract remains necessary.

## Rebuild and replay

`SimulationRuntime.rebuild(...)` reads the latest persisted checkpoint, creates a new
SimPy environment at `snapshot.simulated_time_seconds`, and reconstructs only:

- start callbacks for persisted `scheduled` attempts at their absolute window starts;
- completion callbacks for persisted `running` attempts at
  `actual_start_seconds + planned_duration_seconds`.

No `Environment`, `Process`, generator, timeout, or callback object is serialized.
An overdue persisted running attempt is treated as invalid runtime state rather than
silently completed. Because absolute times and attempt guards are persisted, rebuilt
and uninterrupted runtimes emit the same subsequent business facts and can continue
after an exact event replay.

There are two deliberately different comparison contracts:

1. **Exact same-stream replay.** Replaying one run's envelopes from its version-zero
   checkpoint must reproduce the exact latest checkpoint, including run/event/mutation
   identities, sequences, state and plan revisions, timestamps, attempts, and outcomes.
2. **Semantic independent/chunked comparison.** Independently initialized runs
   normalize generated run/proposal/event/observation identities and wall-clock
   metadata. Differently chunked advances additionally exclude control-dependent
   `time_advanced` events, audit rows, event sequence/mutation identities, and
   state-version counts. They still compare business event type/time/content, attempt
   generations and lineage, actual start/end/consumption, resources, orders, and final
   outcomes.

Normalization must never hide a different start time, interruption, resource,
completion, or order result. Chunking naturally creates more priority-90 control
events and therefore more state versions; it is not byte-identical replay. Determinism
also assumes the same schema/code, catalog, seed, and accepted public inputs. The
synthetic runtime is not a calibrated physical model or a real-time execution engine.

## Synthetic telemetry and privacy boundary

The runtime generates observations for machines touched by starts, completions,
faults, and restorations. Values are deterministic hashes of a versioned namespace,
seed, machine, signal, and simulated second; this permits semantic comparison across
independent run identities. The generated observation ID additionally includes the
run identity. Generated signals are integer `temperature_millicelsius` (`mdegC`) and
`vibration_micrometers_per_second` (`um_per_s`); summaries explicitly say
“Synthetic” and may state only the current operational availability.

Caller-supplied `Observation` or `ObservationTemplate` values are accepted only for a
known run/time and a machine present in both the static catalog and the synchronized
authoritative snapshot, using this finite name/unit allowlist. This whole-input
preflight happens before SimPy can commit an earlier boundary in the same advance:

| Signal name | Unit |
| --- | --- |
| `temperature`, `temperature_millicelsius` | `mdegC` |
| `vibration`, `vibration_micrometers_per_second` | `um_per_s` |
| `drive_current_milliamps` | `mA` |
| `lubrication_pressure_kilopascals` | `kPa` |
| `queue_depth_units` | `units` |

Template jitter is deterministic for the same seed and identity. Direct observation
summaries are labeled synthetic before persistence, and caller wall-clock timestamps
are removed. The public projection exposes typed observations and a fixed
`PublicRuntimeEvent` metadata schema, never arbitrary raw payloads.

`private_truth_ref`, hidden cause labels, and `unavailable_until_seconds` do not enter
event payloads, public command fingerprints, audits, or projections. Future
intervention schedule content is likewise absent from events and projections before
its boundary. A stored opaque command fingerprint can commit to all public inputs of
an in-progress multi-boundary advance without disclosing those inputs. Current machine
unavailability and safe observable signal values do remain visible. This separation
prevents evaluator leakage; it does not turn local synthetic fixture files into a
cryptographic security boundary, and an observed anomaly or diagnosis is never a
repair confirmation.

## Verification anchors

The implementation-level behavior is exercised by:

- `tests/simulation/test_execution.py`: completion, inclusive boundaries,
  reproducibility, chunking, and pause/read purity;
- `tests/simulation/test_interruptions.py`: stale callbacks, full-duration restart,
  successive plans, same-second priority, blocked work, and rejection safety;
- `tests/simulation/test_projection_boundary.py`: synthetic telemetry and private
  scenario-data exclusion; and
- `tests/replay/test_s05_runtime_restart.py`: rebuild, exact replay, and continued
  execution after replay.
