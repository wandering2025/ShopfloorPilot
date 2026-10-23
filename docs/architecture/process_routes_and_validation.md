# Finite process routes and independent validation

Status: S03 interface and violation-code reference

Schema context: S01 domain schema `1`

## Boundary and dependency rule

S03 turns only supported structured order data into the three catalog routes and
validates schedules without constructing or importing a solver model. The validation
package depends on the immutable domain/canonical contracts and finite route expansion;
it does not depend on OR-Tools, a scheduling package, SQLite, events, the wall clock,
randomness, or a model provider.

Natural-language interpretation remains S10 work. S03 never invents a product,
capability, duration, operation, repair confirmation, or order/resource change.

## Route expansion

```python
from shopfloor_pilot.planning import expand_process_route

result = expand_process_route(request, catalog)
```

`request` is an immutable `Order` or a structured mapping with `order_id`,
`product_id`, `quantity`, `release_time_seconds`, `due_time_seconds`, and
`priority_weight`. `requested_route_id`, `status`, and `operation_parameters` are
optional. Missing required fields return `clarification_required`; present but invalid
or unsupported values return `rejected`. A failed result has no order, route ID, or
partial operations.

The catalog is authoritative. If `operation_parameters` are supplied, they must cover
every predefined operation exactly once and repeat its exact capability and duration.
They are assertions, not overrides. Quantity produces one independent immediate-
predecessor chain for every physical unit. IDs are deterministic SHA-256-derived values
over a versioned namespace, order ID, unit index, and operation-template ID. Identical
structured input and catalog produce byte-identical output.

Route failures use `RouteIssueCode` from `shopfloor_pilot.planning`:

| Group | Stable codes |
| --- | --- |
| Required identity/product | `missing_order_id`, `invalid_order_id`, `missing_product`, `unsupported_product` |
| Required order values | `missing_quantity`, `invalid_quantity`, `missing_release_time`, `invalid_release_time`, `missing_due_time`, `invalid_due_time`, `missing_priority`, `invalid_priority`, `invalid_order_status` |
| Finite boundary | `unsupported_request_field`, `route_mismatch`, `invalid_route_parameters` |
| Exact route assertions | `duplicate_route_parameter`, `missing_route_parameter`, `unknown_route_parameter`, `unknown_capability`, `capability_mismatch`, `invalid_duration`, `duration_mismatch` |

## Validator entry points

```python
from shopfloor_pilot.validation import validate_candidate, validate_schedule

schedule_report = validate_schedule(
    snapshot=snapshot,
    catalog=catalog,
    assignments=assignments,
    projected_order_deltas=declared_order_deltas,
    projected_resource_deltas=declared_resource_deltas,
)

candidate_report = validate_candidate(
    snapshot=snapshot,
    catalog=catalog,
    candidate=candidate,
    projected_order_deltas=declared_order_deltas,
    projected_resource_deltas=declared_resource_deltas,
)
```

`validate_schedule` is the raw hard-constraint boundary and deliberately accepts an
assignment sequence so malformed duplicate inputs can be diagnosed. `validate_candidate`
adds proposal provenance, content-hash, next-revision, declared-delta equality, and
objective checks. Both functions are pure and return a `ValidationReport`; they never
apply a plan or write state.

Candidate assignments contain future executable work only. Completed operations and
unaffected running operations remain solely in authoritative snapshot history. They
occupy their actual/fixed intervals and cannot appear in a candidate. Interrupted work
must use a distinct successor attempt and a full-duration interval; a valid report
derives the next attempt number and `restart_of_attempt_id`. An already-created
scheduled successor can retain its identity and lineage when it is rescheduled.
Otherwise scheduled attempts are replaceable. Completed, running, interrupted, and
cancelled attempt history is retained in a valid complete projection for the later
`plan_applied` payload.

Fixed history is checked rather than trusted as hidden solver state. Lifecycle status,
attempt generations and restart chronology, fixed-to-fixed precedence, machine overlap,
calendar/release/horizon bounds, and the bidirectional relation between a running
machine and exactly one running attempt must agree. A blocked operation selected by an
accepted recovery plan is projected back to the startable `pending` state.

An already-applied scheduled restart is valid with an operation in `interrupted`,
`pending`, or `ready` state only when it is the single next generation and points to the
latest interrupted attempt. Every immutable interrupted generation is checked against
its route predecessor independently, so a legal future restart cannot conceal invalid
past precedence.

For this finite schema, snapshot machine availability is authoritative for candidate and
fixed windows alike. A replacement calendar therefore must retain retrospective
coverage for immutable actual history; otherwise validation fails closed. A future
state schema that versions calendars can replace this explicit S03 assumption.

A valid report exposes complete projected operations/attempts plus independently
calculated weighted tardiness and makespan. An invalid report exposes only sorted
violations and metrics, never an apply-ready projection. Due-date misses contribute to
the metric but never create a hard violation.

## Delta rules

Candidate delta fields declare candidate content; they do not grant authority. The
separately supplied `projected_*_deltas` are the exact upstream changes against which
the candidate was generated. Candidate and supplied tuples must match canonically.
Approval and application remain S06 responsibilities.

The finite S03/S02-compatible delta set is intentionally small:

- `OrderDeltaKind.ADD` is supported for a unique pending order with a supported route.
- `ResourceDeltaKind.MARK_DOWN` is supported for a known resource at the current
  simulated second.
- order `REPLACE`, resource `RESTORE`, and `REPLACE_AVAILABILITY` are rejected. S02 has
  no order-update or standalone availability-update event, and maintenance restoration
  must first be confirmed by an operator/scenario event in authoritative state.

## Stable schedule violation codes

Every `ScheduleViolation` contains a stable code and relevant operation, attempt,
machine, and interval references when applicable. Reports sort violations
deterministically.

| Group | Stable code | Meaning |
| --- | --- | --- |
| Candidate provenance | `proposal_hash_mismatch` | Immutable candidate content no longer matches its hash. |
| Candidate provenance | `run_id_mismatch` | Candidate and snapshot run identities differ. |
| Candidate provenance | `base_state_version_mismatch` | Candidate was built from another operational version. |
| Candidate provenance | `base_snapshot_hash_mismatch` | Candidate's base hash is not the supplied snapshot. |
| Candidate provenance | `plan_revision_mismatch` | Proposed revision is not exactly current revision plus one. |
| Declared changes | `order_delta_mismatch` | Candidate order deltas differ from the explicitly supplied projection. |
| Declared changes | `invalid_order_delta` | Order delta kind/content cannot be projected safely. |
| Declared changes | `resource_delta_mismatch` | Candidate resource deltas differ from the explicitly supplied projection. |
| Declared changes | `resource_restore_forbidden` | A proposal attempted to declare a repair/restoration. |
| Declared changes | `invalid_resource_delta` | Resource delta is unsupported, unknown, stale, or otherwise invalid. |
| Coverage | `missing_operation` | Required future operation has no assignment. |
| Coverage | `duplicate_operation` | One operation appears in multiple assignments. |
| Coverage | `unknown_operation` | Assignment names no route-derived or snapshot operation. |
| Coverage | `extra_operation` | Snapshot/candidate contains a known but non-route operation. |
| Attempt identity | `duplicate_attempt` | Candidate reuses one attempt ID across assignments. |
| Attempt identity | `attempt_id_conflict` | Candidate collides with immutable attempt history. |
| Resource | `unknown_machine` | Assignment names no snapshot machine. |
| Resource | `capability_mismatch` | Machine lacks the operation's required capability. |
| Time | `duration_mismatch` | Assignment window is not the operation's full duration. |
| Resource/time | `machine_overlap` | Candidate or fixed occupancy overlaps on one machine. |
| Route/time | `precedence_violation` | Successor begins before its predecessor completes. |
| Order/time | `release_time_violation` | Work begins before its order release. |
| State/time | `start_before_snapshot` | Future work begins before current simulated time. |
| Resource/time | `calendar_violation` | Work is outside one complete availability window or uses a down/maintenance resource. |
| Time | `hard_horizon_violation` | Work ends after the finite hard horizon. |
| Fixed history | `completed_operation_rescheduled` | Candidate attempts to move completed work. |
| Fixed work | `running_operation_rescheduled` | Candidate attempts to move unaffected running work. |
| Fixed history | `fixed_history_mismatch` | Order, operation, attempt-generation, machine-running, or fixed-time history is internally inconsistent. |
| Interruption | `restart_duration_mismatch` | Interrupted work does not restart for full duration. |
| Candidate metrics | `objective_mismatch` | Claimed tardiness or makespan differs from independent calculation. |

Intervals remain half-open. Therefore one assignment ending at `t` and another starting
at `t` do not overlap; work ending exactly at a calendar or hard-horizon boundary is
legal.
