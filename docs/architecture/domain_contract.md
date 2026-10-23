# ShopfloorPilot domain contract

Status: S01 decision record

Schema version: `1`

Scope: typed data and deterministic fixtures only

## Decision summary

ShopfloorPilot uses immutable, strict Pydantic v2 records as the shared language between route generation, state/replay, validation, scheduling, simulation, services, evaluation, and UI projections. S01 validates data shape, local invariants, fixture references, and projection boundaries. It does not schedule, simulate, persist, retrieve evidence, or apply proposals.

The contract intentionally models only the synthetic MVP cell. It is not a general manufacturing ontology and does not claim factory calibration.

## Units and time

- All operational time is an integer number of simulated seconds.
- Every closed reservation or availability period is a half-open interval `[start_seconds, end_seconds)`. Therefore `[0,10)` and `[10,20)` are adjacent and legal; they do not overlap.
- Negative times, zero-length windows, and zero-duration operations are invalid.
- Wall-clock waiting never advances simulated time. Wall-clock fields have the suffix `_at_utc` or `_elapsed_ms` and are excluded from operational canonical equality.
- Every scenario owns a `ScenarioClock` with an explicit `origin_label`, `start_seconds`, and `shift_boundary_seconds`. The supported phrase `today` maps to that shift boundary. Without a scenario clock it requires clarification; the developer machine's date is never consulted.
- `due_time_seconds` is soft. Tardiness contributes to the objective but does not make a schedule infeasible.
- `hard_horizon_seconds` is hard. Work that cannot fit within compatible availability before the horizon is infeasible even if its due date is later.

## Identity and versioning

The identifiers below have different lifecycles and must not be substituted for one another.

| Field | Meaning |
| --- | --- |
| `run_id` | Identity of one manufacturing/evaluation run. Reset creates a new value. |
| `workflow_id` | Identity of one supervisor invocation or continuation, linked to a run. It is not the run identity. |
| `state_version` | Monotonic operational snapshot version. S02 defines transaction-level increment rules. |
| `plan_revision` | Revision of the active applied plan inside a run. It is not a state version. |
| `proposal_id` | Identity of one proposed candidate. It grants no permission to mutate state. |
| `proposal_hash` | SHA-256 over immutable canonical proposal content, excluding only wall-clock creation metadata and the hash field itself. |
| `attempt_id` / `attempt_number` | Identity and generation of one execution attempt for an operation. A restart creates a new attempt. |
| `event_id` / `sequence` | Identity and total-order sequence within an operational event stream. |

A `CandidateProposal` binds `run_id`, `workflow_id`, `base_state_version`, `base_snapshot_hash`, `proposed_plan_revision`, assignments, `proposed_order_deltas`, and `proposed_resource_deltas` into its content hash. Changing any of those values invalidates the candidate. An `ApprovalRecord` repeats the run/workflow/version/proposal/hash binding and records only a decision. Approval is not application; S06 owns the atomic apply boundary.

## Cell, capabilities, and routes

The committed catalog is [`scenarios/templates/catalog.json`](../../scenarios/templates/catalog.json).

| Resource | Kind | Capabilities |
| --- | --- | --- |
| `CNC01` | processing | drilling, tapping |
| `CNC02` | processing | milling, slotting |
| `CNC03` | processing | drilling, milling |
| `QC01` | inspection | inspection |

`CNC03` is the explicit milling alternative for `CNC02` and drilling alternative for `CNC01`. `QC01` is a constrained resource, not a label attached after processing.

| Product ID / English canonical name | Predefined route `(capability, seconds)` |
| --- | --- |
| `prod_aluminum_mounting_plate` / Aluminum Mounting Plate | milling 900, drilling 420, inspection 180 |
| `prod_steel_bracket` / Steel Support Bracket | slotting 720, drilling 360, inspection 180 |
| `prod_actuator_base` / Actuator Base | milling 1080, tapping 480, inspection 240 |

Every route has contiguous zero-based `sequence_index` values and ends in inspection. An unknown product or mismatched route returns a typed rejection; no process sequence is invented.

## Quantity semantics

`quantity` is a strict integer in `1..10`. Route expansion in S03 creates one complete operation chain per physical unit, using `unit_index` values `1..quantity`. It does not multiply one indivisible operation's duration by quantity. Operation IDs remain stable for identical structured input. The committed eight-order fixtures include one quantity-two order to exercise this rule.

## Operations, attempts, and interruption

An `Operation` describes required capability, full duration, predecessors, unit, and lifecycle status. An `OperationAttempt` describes one resource assignment/generation and preserves scheduled and actual timing.

- Completed operations and attempts are immutable history.
- An unaffected running attempt remains fixed and non-preemptive during replanning.
- A machine fault interrupts a running attempt at the intervention second. The interrupted attempt records `actual_start_seconds`, `actual_end_seconds`, and partial `consumed_seconds`; consumed time is wasted work.
- Recovery creates a new `attempt_id` with incremented `attempt_number` and `restart_of_attempt_id`. Its `planned_duration_seconds` and reserved interval must equal the original full operation duration. Partial progress is not credited.
- A scheduled attempt has no actual time or consumed work. A running attempt has an actual start but no actual end. A completed/interrupted attempt has both.

`src/shopfloor_pilot/domain/examples.py` provides a frozen example containing completed history, an unaffected running attempt, an interrupted attempt, a full-duration successor on `CNC03`, a version-bound candidate, separate order/resource deltas, and a separate approval record.

## Availability, precedence, and objective

Machine availability is expressed as sorted, non-overlapping half-open windows. Later validators and the solver must enforce:

- exact operation coverage and precedence;
- machine capability compatibility;
- release times and availability windows;
- resource non-overlap;
- positive full durations;
- fixed completed and unaffected running work;
- full-duration restart after interruption;
- the finite hard horizon.

The scheduling objective is lexicographic: minimize weighted total tardiness first, then makespan. `priority_weight` is a strict integer in `1..100`. S04 owns the bounded formulation, solver status mapping, and proof; S01 only fixes the vocabulary and distinguishes `feasible_tardy` from `hard_horizon_infeasible`.

## Event ordering proposal

Events are ordered by `(simulated_time_seconds, event_type_priority, sequence)`. S02 may encode this directly but must not change its observable meaning without updating consumers and tests.

| Priority | Event |
| --- | --- |
| 10 | `operation_completed` |
| 20 | `machine_down` |
| 30 | `operation_interrupted` |
| 40 | `machine_restored` |
| 50 | `order_registered` |
| 60 | `plan_applied` |
| 70 | `operation_started` |
| 80 | `observation_recorded` |
| 90 | `time_advanced` |

Completion wins at its exact boundary; a fault at that same second affects the machine after the completion. A machine-down fact precedes the interruption it causes. `sequence` is the final tie-breaker and S02 must reject missing/conflicting stream sequence.

## Status and failure vocabulary

General typed outcomes use:

- `success`
- `rejected`
- `clarification_required`
- `conflict`
- `infeasible`
- `unknown`
- `cancelled`
- `provider_failure`

Non-success `StatusResult` values require one or more `FailureDetail` records. Stable S01 failure codes cover unsupported product/route, missing or invalid quantity, ambiguous time, unknown capability, invalid duration/window, duplicate identity, stale state, hash mismatch, hard horizon, absent capable resource window, and provider unavailability. Solver-specific optimal/incumbent/native statuses remain S04 work.

`FeasibilityResult` keeps these cases distinct:

- `feasible_on_time`: zero tardiness within the hard horizon;
- `feasible_tardy`: positive tardiness but otherwise hard-feasible;
- `hard_horizon_infeasible`: hard contradiction, with a typed failure.

## Canonical and semantic serialization

`src/shopfloor_pilot/domain/canonical.py` provides pure functions. They do not mutate models, access the clock, generate identity, or read environment state.

1. `canonical_json_bytes` converts models/enums to JSON, removes allowlisted wall-clock metadata, sorts dictionary keys, rejects NaN, uses UTF-8, and emits no insignificant whitespace.
2. `canonical_hash` is lowercase SHA-256 of those bytes. Exact replay retains run/workflow/proposal/event/attempt identity, versions, revisions, and event sequence.
3. `semantic_json_bytes` is for independent-run or differently chunked comparisons. It additionally removes control-dependent revision/sequence counters and consistently normalizes generated `run_id`, `workflow_id`, proposal/approval/event/observation/attempt IDs.

Semantic normalization deliberately retains order, operation, machine and product IDs, event types and payloads, simulated times, resources, actual attempt content, and business outcomes. It must not be used to hide substantive divergence. Dictionary insertion order never affects either hash.

## Public/evaluator-only isolation

Public initial scenario definitions are under:

```text
scenarios/<scenario_id>/scenario.json
```

Evaluator-only future schedules and injected truth are under:

```text
scenarios/evaluator_only/<scenario_id>.json
```

Expectation contracts are stored at `scenarios/<scenario_id>/expectations.json`. They are evaluator/test assertions, not public tool or agent observations. Production tool projections must use `PublicScenarioView`, whose fields are exactly:

```text
schema_version
scenario_id
run_id
simulated_time_seconds
observations
```

`project_public_view` builds that type from an explicit simulated second and filters observations at the boundary. It never copies `interventions`, `fault_truth`, `cause_label`, `private_truth_ref`, unavailable-until values, private paths, or future observations. This is an allowlist, not a blacklist of currently known secrets.

Synthetic telemetry uses integer scaled values with explicit units such as `mdegC`, `um_per_s`, `mA`, and `kPa`. A local `random.Random` instance seeded by scenario/seed/observation/signal adds only the documented integer jitter. It does not change initial operational data or scheduled interventions and does not consume global randomness.

The machine-breakdown evaluator includes an intentionally ambiguous variant: the public temperature/vibration summary supports both spindle-bearing degradation and tool imbalance. Its sentinel truth label remains evaluator-only. A diagnostic hypothesis is never a repair confirmation.

## Scenario contracts

The typed and hash-verified manifest is [`scenarios/manifest.json`](../../scenarios/manifest.json). It records SHA-256 values, seeds, units, and expected outcomes for every committed JSON fixture. The loader rejects missing, unlisted, path-escaping, or hash-mismatched fixture files.

| Scenario | Seed | Evaluator intervention | Expected assertion |
| --- | ---: | --- | --- |
| `normal` | 101 | checkpoint at 7200 | all initial orders complete and every route reaches QC |
| `urgent_order` | 202 | priority plate order added at 3600 | a state/version-bound replan is required |
| `machine_breakdown` | 303 | `CNC02` fault at 2700 | consumed work is wasted; 900-second milling restarts on `CNC03` |
| `no_feasible_recovery` | 404 | `CNC02` and `CNC03` unavailable from 3000 through 14400 | the only milling resources have no usable window before the hard horizon |

The last scenario's contradiction is capability/window/horizon based. It is not merely a missed due date. Solver proof remains S04 work.

## Supported aliases and clarification

Alias resolution is an explicit table in `aliases.py`, not an unconstrained parser.

| Canonical English product | Supported examples |
| --- | --- |
| Aluminum Mounting Plate | `Aluminum Mounting Plate`, `mounting plate`, `铝制安装板`, historical short form `铝安装板` |
| Steel Support Bracket | `Steel Support Bracket`, `steel bracket`, `钢制支架` |
| Actuator Base | `Actuator Base`, `执行器底座` |

Missing product/quantity and `today` without a scenario clock return `clarification_required`. Unknown products, fuzzy prose, invalid quantity, and a route that does not match the predefined product route return `rejected`. S10 may draft structured input from language later, but it cannot bypass this deterministic boundary.

## Excluded physics and constraints

The MVP omits transport time, setup/changeover time, tooling and operator scarcity, material inventory/lot constraints, buffers, energy, geometric collision, wear propagation, calibrated temperature/vibration models, remaining-life prediction, arbitrary CAD/CAM, G-code, PLC/robot control, and physical safety control. Synthetic signals demonstrate evidence flow only; they do not validate root cause.

These omissions must remain visible in evaluation and UI text. Adding one later requires an explicit contract/schema change, migration consideration, and affected acceptance tests.
