# ShopfloorPilot authoritative state and event replay

Status: S02 architecture decision record

Schema version: `1`

Scope: local SQLite authority, atomic operational mutations, lifecycle persistence,
and deterministic replay

## Decision summary

One `SQLiteStateRepository` is the authoritative write boundary for operational
state. A successful operational command commits its event group, resulting
checkpoint, current-version compare-and-swap, idempotency outcome, and any typed
transaction participant records together. UI, simulator, tools, and later services
must read this repository rather than maintain independent writable state copies.

The event reducer is deterministic and has no network, wall-clock, random, or model
dependency. SQLite persistence and replay use the S01 schema-v1 domain models and
canonical hashes. This is intentionally a small manufacturing-state design, not a
generic event platform.

## Initialization and migration

`src/shopfloor_pilot/storage/schema.py` contains an ordered immutable migration list.
Version zero means an empty database; migration 1 creates the S02 schema. Each
migration has a name and SHA-256 checksum recorded in `schema_migrations`.
`PRAGMA user_version` is the database's version marker.

`initialize_schema` executes individual DDL statements inside `BEGIN IMMEDIATE`; it
does not use `executescript`. The migration ledger insertion and `user_version`
change therefore commit or roll back together. Repeating initialization at version 1
validates the ledger and makes no schema change. Negative, unknown, or newer versions
raise `SchemaVersionError`; this version neither guesses at nor downgrades them.

Initialize a local database from the repository root with the public API:

```bash
uv run python - <<'PY'
from pathlib import Path

from shopfloor_pilot.storage import SQLiteStateRepository

runtime_directory = Path(".runtime")
runtime_directory.mkdir(exist_ok=True)
repository = SQLiteStateRepository(runtime_directory / "shopfloor-pilot.db")
version = repository.initialize(applied_at_utc="2026-10-20T20:00:00+02:00")
print(version)
PY
```

The explicit timestamp makes setup evidence reproducible. Omitting it asks the
repository to supply current UTC. Repository connections use FULL synchronous mode;
`initialize()` selects WAL journaling after a successful migration validation.
Foreign-key enforcement and the bounded SQLite busy timeout are enabled on every
repository connection. Runtime
`.db`, `-wal`, and `-shm` files are local artifacts and must not be committed.

## Authoritative tables and run isolation

| Table | Authority and invariants |
| --- | --- |
| `schema_migrations` | Applied version, immutable name/checksum, and supplied application time. |
| `runs` | Stable run identity, immutable initial snapshot/hash, reset parent, and compare-and-swap `current_state_version`. |
| `snapshot_checkpoints` | Historical checkpoint for every `(run_id, state_version)`, including plan revision, final sequence/order key, seen event/mutation IDs, snapshot JSON, and canonical hash. `(run_id, last_sequence)` is unique. |
| `event_groups` | One operational command boundary: base/result versions, first sequence, event count, and command hash. A deferred same-run foreign key requires its resulting checkpoint by commit. |
| `operational_events` | Ordered event envelopes keyed by `(run_id, sequence)`, with unique event identity and unique index inside a mutation. Event type/priority pairs are constrained by the S01 order. |
| `idempotency_records` | Original command hash and serialized `MutationResult`, bound by a composite foreign key to the same run, mutation, base version, and result version. |
| `workflows` | Restorable supervisor lifecycle state. It is non-operational bookkeeping. |
| `proposals` | Immutable proposal JSON and hash, bound to a workflow and base checkpoint in the same run. Storage grants no authority to apply it. |
| `approvals` | Immutable decision JSON bound to the exact same-run proposal ID/hash and base version. Approval is not application. |
| `approval_consumptions` | Historical evidence that an approved record was consumed by one same-run operational event group. |
| `audit_records` | Per-run ordered audit records, optionally linked to a same-run workflow and annotated with the observed operational version. |

Run ownership is part of primary and foreign keys rather than a query convention.
Events, groups, checkpoints, workflows, proposals, approvals, consumptions, and audit
records cannot be attached across runs. Reset does not reuse or erase an identity:

```python
new_run = repository.reset_run(
    source_run_id="run-original",
    new_initial_snapshot=new_version_zero_snapshot,
)
```

`new_version_zero_snapshot.run_id` must differ from the source. The new run starts at
state version and sequence zero and retains `reset_from_run_id`; the source run and
its history remain intact. Matching integer versions never make records from two runs
interchangeable.

## Operational transaction and version rules

One non-empty event group is one successful operational mutation. Regardless of how
many events it contains:

- `resulting_state_version == base_state_version + 1` exactly once;
- all envelopes share the mutation ID, base/result versions, event count, run, and
  simulated second;
- event indexes are contiguous from zero and stream sequences are contiguous from the
  preceding checkpoint;
- a checkpoint is persisted for the resulting version; and
- `runs.current_state_version` moves with a compare-and-swap update.

Reads and standalone workflow, proposal, approval, and audit writes do not change
`state_version`. An approval consumption can join an operational mutation through
`OperationalTransaction`, but it does not add another increment. Every persisted
operational group does increment the version, including time advance and an
`observation_recorded` group whose reducer otherwise leaves snapshot business fields
unchanged.

`plan_revision` is independent. It changes only when a `plan_applied` event supplies
exactly `current plan_revision + 1`. A group may contain at most one such event. Other
event types leave the plan revision unchanged.

A minimal mutation uses the current version explicitly:

```python
from shopfloor_pilot.domain.models import EventType
from shopfloor_pilot.events import EventDraft
from shopfloor_pilot.storage import SQLiteStateRepository

repository = SQLiteStateRepository(".runtime/shopfloor-pilot.db")
snapshot = repository.get_snapshot("run-001")

result = repository.apply_mutation(
    run_id=snapshot.run_id,
    expected_state_version=snapshot.state_version,
    idempotency_key="advance-to-60",
    command_name="advance_time",
    command_payload={"target_time_seconds": 60},
    events=(
        EventDraft(
            simulated_time_seconds=60,
            event_type=EventType.TIME_ADVANCED,
            payload={"target_time_seconds": 60},
        ),
    ),
)
```

The returned immutable `MutationResult` contains the old/new versions, persisted
envelopes, and resulting checkpoint. For S06's atomic approval/apply boundary, pass a
local participant that calls `transaction.consume_approval(approval_id)` and, when
needed, `transaction.append_audit(record)`. The participant receives no raw SQLite
connection and becomes unusable after its callback returns.

The participant callback itself is not serialized or hashed. Every identifier and
input that determines participant side effects must therefore also be present in
`command_payload`. In particular, an approval-consuming command includes its
`approval_id` (and normally its `proposal_id`), and an audit-appending participant
includes the audit identifier and content it commits. The callback must use those same
values. Otherwise a changed callback closure could appear identical at the
idempotency boundary even though it names different side effects.

No network or provider call may occur inside `apply_mutation` or its participant.
Fetch remote output first, close that interaction, then enter the short local
transaction and revalidate the expected version, proposal hash, and approval binding.

## Idempotency, compare-and-swap, and concurrent writers

The repository hashes the run, expected version, command name, command payload, and
event drafts into one canonical request hash. Inside `BEGIN IMMEDIATE`, it checks
`(run_id, idempotency_key)` before comparing the expected version:

1. The same key and request hash returns the stored original outcome with
   `replayed_idempotent=True`, even though that request's expected version is now old.
2. The same key with changed content raises `IdempotencyConflictError` and performs no
   operational write.
3. A new key proceeds to the expected-version check. A mismatch raises
   `VersionConflictError` without silently rebuilding or retrying against newer state.

The implementation uses the idempotency key as the mutation ID; event IDs are derived
deterministically from run, mutation, and event index. The idempotency row is inserted
last in the same transaction, so an interrupted first attempt cannot leave a success
marker for an uncommitted mutation. Because Python participant code is deliberately
outside the request hash, `command_payload` is the required canonical declaration of
all participant-bound identifiers and effects; it is not optional descriptive
metadata.

`BEGIN IMMEDIATE` serializes local SQLite writers. If two new commands start from the
same expected version, only the winner can advance it; after obtaining the lock, the
other observes a stale version. Lock acquisition is bounded by
`busy_timeout_seconds` (default `1.0`); an expired SQLite busy/locked condition becomes
`DatabaseBusyError`. There is no unbounded wait and no automatic retry against a
different state.

## Atomic rollback and failpoint evidence

The mutation transaction orders its writes as event group/events, optional typed
participant records, resulting checkpoint, current-version CAS, idempotency result,
then commit. Any exception rolls the connection back. The internal, test-only
`_failure_injector` can raise at these named boundaries:

- `after_event_insert`;
- `after_snapshot_insert`; and
- `before_commit`.

Acceptance evidence should capture canonical checkpoint hash, event/group counts,
plan revision, current version, idempotency count, and participant-record counts
before injection and after reopening the temporary database. They must be identical.
This describes the evidence procedure; it is not a claim that a particular test run
passed. The schema's deferred group-to-checkpoint foreign key additionally prevents a
transaction from committing a resulting event group without its checkpoint.

## Event ordering, grouping, and append rules

The total ordering key remains the S01 contract:

```text
(simulated_time_seconds, event_type_priority, sequence)
```

| Priority | Event type | Meaning |
| ---: | --- | --- |
| 10 | `operation_completed` | Finish full-duration running work before a same-second fault. |
| 20 | `machine_down` | Mark the resource down before any interruption it causes. |
| 30 | `operation_interrupted` | Preserve consumed time as wasted partial work. |
| 40 | `machine_restored` | Return a down/maintenance machine to available. |
| 50 | `order_registered` | Add one typed, unique order. |
| 60 | `plan_applied` | Replace the active operations/attempts with the next revision. |
| 70 | `operation_started` | Start a scheduled attempt on an available compatible machine. |
| 80 | `observation_recorded` | Validate and retain an observation event without embedding evaluator truth. |
| 90 | `time_advanced` | Move the simulated clock strictly forward. |

`materialize_event_group` requires one or more drafts at one simulated second, sorts
them stably by priority, assigns contiguous sequences, and rejects time before the
snapshot or after the hard horizon. The first new ordering key must be strictly after
the checkpoint's last key. Consequently, a lower-priority event cannot later be
appended retroactively at a second whose higher-priority events were already
committed. Same-priority input order is resolved by sequence.

`reduce_event_group` accepts only a complete group beginning at `event_index == 0`.
It rejects truncated groups, inconsistent group metadata, reused mutation IDs,
duplicate event IDs, missing/non-contiguous sequences, run mismatch, invalid order,
and more than one plan application. A checkpoint stores the canonical snapshot hash,
last sequence/order metadata, and complete seen event/mutation identity histories so
suffix replay can detect references back into its prefix.

## Payload meanings and reduction rules

Payloads are strict, extra-forbidding schema-v1 models:

- `time_advanced`: `target_time_seconds` must equal the event time, move strictly
  forward, and remain within the hard horizon.
- `machine_down`: names a known machine, sets it to `down`, and may replace its
  availability windows. A machine already down is a conflict.
- `operation_interrupted`: names a running attempt on a machine already marked down.
  Elapsed work must be strictly between zero and full duration; it becomes
  `consumed_seconds`, the attempt/operation become interrupted, and the order becomes
  blocked. A later plan must create a new full-duration restart attempt under S01.
- `machine_restored`: moves a known down or maintenance machine to `available` and may
  replace availability windows.
- `order_registered`: appends one typed order and rejects a duplicate order ID.
- `plan_applied`: supplies the next plan revision and complete operation/attempt
  collections. It cannot alter completed or running operations, nor running,
  completed, interrupted, or cancelled attempt history.
- `operation_started`: starts a scheduled attempt whose operation is pending, ready,
  or interrupted on an available machine. A scheduled window, when present, must
  start at the event second. Attempt, operation, machine, and order states update
  together.
- `operation_completed`: requires an actually running attempt, a running machine, and
  elapsed time exactly equal to full planned duration. It records full consumption,
  releases the machine, and completes the order only when all its operations are
  complete.
- `observation_recorded`: checks observation run, machine, and simulated time. The
  observation remains in the event stream; it does not add evaluator-only truth or a
  mutable observation cache to `OperationalSnapshot`.

## Replay and failure behavior

The pure entry points are:

```python
from shopfloor_pilot.events import (
    materialize_event_group,
    reduce_event_group,
    replay_events,
)
```

- `materialize_event_group(checkpoint, mutation_id, drafts)` validates drafts and
  creates deterministic envelopes.
- `reduce_event_group(checkpoint, envelopes)` applies exactly one complete group and
  returns a new checkpoint.
- `replay_events(checkpoint, envelopes)` applies zero or more complete groups.

Repository replay supports both paths:

```python
full = repository.replay_run("run-001", from_state_version=0)
suffix = repository.replay_run("run-001", from_state_version=known_version)
```

The first reconstructs from the version-zero anchor. The second loads that historical
checkpoint and only reduces events after its `last_sequence`. Both use the same pure
reducer and contact no API, clock, or random source. Canonical equality includes run
identity, state/plan versions, sequences, attempts, machines, orders, actual timing,
and outcomes; cross-run semantic normalization remains a separate comparison mode.

Replay fails closed and returns no partially accepted result:

| Failure | Typed error |
| --- | --- |
| Unsupported envelope/event/draft schema | `UnsupportedSchemaError` |
| Missing or structurally invalid envelope | `MalformedEnvelopeError` |
| Payload incompatible with its event type | `MalformedPayloadError` |
| Missing, duplicate, or non-contiguous sequence | `SequenceConflictError` |
| Non-monotonic simulated-time/priority/sequence key | `OrderingConflictError` |
| Duplicate identity or wrong run | `EventConflictError` |
| Truncated/inconsistent group or version boundary | `GroupVersionConflictError` |
| Validly shaped event impossible in current state | `ReductionConflictError` |

Stored event envelopes and checkpoints are hash-checked while loading. Relational and
serialized seen-ID histories must agree; inconsistencies raise `RecordConflictError`
rather than producing a plausible snapshot.

## Operational replay versus lifecycle restoration

Operational replay reconstructs manufacturing state only. It does not recreate
workflow, proposal, approval, audit, or consumption history from operational events.
Those records have a separate lifecycle projection:

```python
restored = repository.restore_run("run-001")
historical = restored.lifecycle
```

With no version argument, `restore_run` returns the current checkpoint plus lifecycle
records. With `from_state_version=N`, it returns checkpoint `N`, the contiguous
operational suffix after it, and the lifecycle records; a caller can pass that suffix
to `replay_events`.

`save_proposal` and `save_approval` validate same-run workflow, checkpoint/hash, and
proposal bindings but do not mutate production state. Exported or restored approvals
are historical display/recovery data only. They never become a capability token and
cannot authorize a live mutation. Live application must re-enter
`apply_mutation`, then call `OperationalTransaction.consume_approval`; that method
requires an approved decision and revalidates proposal hash, workflow, base version,
base snapshot hash, run, and one-time consumption inside the current transaction. It
also requires the resulting checkpoint's `plan_revision` to equal the proposal's
`proposed_plan_revision`; consuming approval beside a different or absent plan
revision is rejected and the whole mutation rolls back.

## Detached reads and public-data boundary

Domain, event, and storage records use strict frozen Pydantic models. Repository reads
deserialize fresh objects from stored JSON rather than returning shared references.
Every stored JSON document is type-validated. Stored JSON is not accepted as an
unchecked alternative to relational state: every field also materialized in a
relational column must match that projection, and every stored content hash must match
the canonical decoded record. In particular:

- checkpoint snapshot identity, state version, and plan revision must match their
  relational key/projection; its canonical hash must match, and serialized seen event
  and mutation histories must equal the ordered relational histories;
- each loaded event envelope must pass its typed model and stored canonical-hash
  check;
- workflow, proposal, approval, and audit records must match their relational
  identities/bindings and stored content hashes; candidate proposals additionally
  validate their immutable embedded proposal hash;
- an idempotency result must match its row's run/key, command hash, mutation, base and
  result versions, and the same-run event group to which those fields are bound; and
- assembled lifecycle/restoration models revalidate same-run identity, uniqueness,
  proposal/approval bindings, consumption bindings, and suffix continuity.

A malformed model, hash mismatch, relational identity mismatch, or inconsistent
history raises `RecordConflictError` at the repository boundary or a typed replay error
for an imported stream. Reads fail closed: they do not return the successfully decoded
prefix, silently repair the row, or substitute a plausible snapshot. Caller-side object
replacement or mutation of a caller-owned nested container cannot write through to
SQLite. Persistence changes require an explicit repository method and transaction.

The storage API is an internal authority, not a public projection. UI and agent tools
must continue to use the S01 allowlisted public projection rather than serializing a
database row, lifecycle export, evaluator fixture, or arbitrary snapshot. Future
interventions, injected fault truth, cause labels, private paths, credentials, and
provider secrets do not belong in public views or event evidence.

## Crash recovery and operating boundary

The database is initialized for SQLite WAL journaling with FULL synchronous mode.
After an ordinary process failure or restart, SQLite exposes the last committed state;
an in-flight transaction is rolled back during recovery. The repository can then load
the current checkpoint, verify stored hashes/identity histories, replay a suffix, and
load lifecycle records separately. `integrity_check()` exposes SQLite's integrity
check for diagnostics.

This is a transactional crash-recovery assumption, not a guarantee against sudden
power loss, filesystem/controller failure, media corruption, manual database edits,
or loss of the database and WAL files. Deployment backup and restore policy is outside
S02. Never copy only part of a live SQLite/WAL set and claim it is a valid backup.

S02 is CPU-only. It requires no GPU, renderer, local model, DeepSeek call, driver
change, or system graphics change. The authoritative transaction must stay short and
local; all network work occurs outside it.
