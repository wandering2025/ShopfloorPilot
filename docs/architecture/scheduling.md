# Constraint scheduling

Status: S04. Schema context: S01 domain schema 1, S03 validator unchanged.

## Public API and boundary

```python
from shopfloor_pilot.scheduling import SolverConfig, solve_schedule

result = solve_schedule(
    snapshot,
    catalog,
    proposal_id="proposal-001",
    workflow_id="workflow-001",
    proposed_order_deltas=(),
    proposed_resource_deltas=(),
    not_before_seconds=None,
    config=SolverConfig(max_time_seconds=5.0, random_seed=0),
)
if result.is_usable:
    candidate = result.candidate
    validation = result.validation
```

The same function handles initial scheduling and dynamic replanning. Inputs are
immutable typed records. Caller-supplied proposal/workflow identity is part of the
candidate hash; identical business input with a different identity intentionally
produces a different hash. The scheduler reads no repository, evaluator fixture,
clock-driven manufacturing state, network, environment variable, or approval.
`perf_counter` measures wall latency only. It never applies a plan or advances time.

`not_before_seconds` is an optional execution-boundary floor for future work. `None`
preserves the S04 model and candidate exactly. A supplied value must be a strict
non-negative integer in the inclusive range from the snapshot's simulated time through
its hard horizon; invalid values return `invalid_input` before CP-SAT runs. The value is
reported separately on `SolverReport`, not placed in `SolverConfig`, and therefore is
never forwarded as a CP-SAT parameter.

`is_usable` means an independently validated proposal, still requiring S06 approval
and atomic revalidation/apply. Callers must not apply a solver report or treat native
status alone as permission. Every exposed candidate passes public S03
`validate_candidate`, including provenance, exact deltas, constraints, and independently
recomputed metrics. The reported scalar objective is also checked against those metrics.
Invalid outputs expose no candidate.

Supported deltas are precisely S03's pending order ADD and current-boundary MARK_DOWN
of an available machine. RESTORE, REPLACE_AVAILABILITY, order REPLACE, duplicate/stale
deltas, and marking down a running machine as a proposal are rejected. An actual
running-machine fault must first be recorded by the state/event boundary, including
its interrupted attempt. Replanning then consumes that new snapshot.

Before model construction, the public raw validator checks the empty future
assignment set. Only `missing_operation` findings are expected and ignored at this
stage; all other findings reject invalid input/history/deltas. This is not a new
validator API and does not share model construction or OR-Tools with S03.

## CP-SAT model

The implementation uses the pinned OR-Tools package directly. No JobShopLib layer is
needed. References consulted: Google's [job-shop guide](https://developers.google.com/optimization/scheduling/job_shop)
and [CP-SAT status documentation](https://developers.google.com/optimization/cp/cp_solver).
The code is project-specific; no third-party source was copied.

- Routes are expanded from the explicit catalog in sorted order. Quantity has one
  complete chain per unit. Rejected orders are excluded; completed orders remain in
  historical completion metrics.
- Each future operation has shared integer start/end variables and a Boolean per
  compatible machine/calendar-window alternative. Exactly one alternative is selected.
  Its optional interval has the full route duration and fits wholly in one window.
  Machine capability must exist in both snapshot and catalog.
- Release time and current simulated time bound every future start. Calendar windows
  are clipped to the hard horizon. When supplied, the execution-boundary floor also
  bounds every future start, so the effective lower bound is the maximum of snapshot
  time, `not_before_seconds`, order release, and calendar-window start.
  DOWN/MAINTENANCE/projected MARK_DOWN resources have no future alternatives. An empty
  alternative set is a CP-SAT contradiction.
- Precedence uses predecessor completion. Per-machine `NoOverlap` includes future
  optional intervals and all actual historical occupancy. QC has the same resource
  constraints as processing machines. Intervals are half-open.
- Completed work uses actual start/end. Running work uses actual start plus full
  planned duration, regardless of an obsolete scheduled window. Neither appears
  in candidate assignments. Interrupted work retains actual wasted occupancy and
  creates a full-duration future attempt; consumed time is never subtracted.
- Existing scheduled attempts can be moved and retain identity/lineage. Otherwise
  attempt IDs deterministically hash a versioned namespace, run, operation, and next
  generation. S03 derives and validates complete projected attempts/operations.
- Snapshot calendars must retain retrospective coverage for fixed actual history,
  as required by S03. A down resource's history is preserved.

## Objective, units, bounds, and budget

Let `H` be the absolute hard horizon in integer simulated seconds. For order `j`,
`Cj` is the latest final-inspection completion across its units, `dj` its soft due
date, and `wj` its integer priority in 1..100. Define:

```text
T = sum_j wj * max(0, Cj - dj)             (priority-weighted simulated seconds)
Cmax = max(all operation completion times) (absolute simulated second)
B = H + 1
minimize B*T + Cmax
U_T = sum_j wj * max(0, H - dj)
U_objective = B*U_T + H
```

`0 <= Cmax <= H`. Improving integer `T` by one decreases the scalar by `H+1`;
even the largest possible makespan worsening is at most `H`. Thus weighted tardiness
strictly precedes makespan. This is one optimization, not two successive five-second
calls. Completed history contributes its fixed metrics. Empty work uses current time
as makespan, matching S03. Due dates never restrict feasibility.

Python computes bounds with arbitrary-precision integers before model construction.
S04 accepts `H <= 10^9` and `U_objective <= 2^50`; larger cases return `invalid_input`
with `numeric_bounds_exceeded`, never `infeasible`. These conservative limits leave
ample int64 headroom and keep reported integer objective values exactly representable
as doubles. Future window bounds are clipped before creating intervals; durations
that cannot fit create no alternative, avoiding oversized arithmetic. Due dates beyond
H have zero tardiness without inserting a huge negative constant. Native model
validation is an additional guard; model errors are not infeasibility proofs.

The default total search budget is five wall seconds per candidate, configurable as a
finite positive float. Exactly one solver call receives that budget. Seed defaults
to 0; one worker and zero relative/absolute objective-gap limits are fixed by the typed
configuration. The report retains all explicit settings, installed solver version,
native numeric/name status, objective/best bound when available, multiplier/upper bound,
native response statistics, solver wall time, and total wrapper elapsed time.
The native objective and best bound use scalar units; candidate metrics use simulated
seconds and priority-weighted simulated seconds.
Other native parameters remain the defaults of the reported pinned OR-Tools version.

Preflight, construction, extraction, and independent validation are outside the search
budget and included in total elapsed time. S04-A10 checks a two-second wrapper allowance
on this MVP host; this is measured responsiveness, not a hard real-time guarantee.

## Outcomes

| Outcome | Native state | Candidate |
| --- | --- | --- |
| `optimal` | OPTIMAL (4) | Independently valid; scalar/lexicographic optimum proved |
| `feasible` | FEASIBLE (2) | Independently valid incumbent; optimality unproved |
| `infeasible` | INFEASIBLE (3) | None; hard model contradiction proved |
| `unknown` | UNKNOWN (0) | None; no incumbent, often budget exhaustion, no proof of infeasibility |
| `model_invalid` | MODEL_INVALID (1), or pre-solve native model validation | None |
| `invalid_input` | Solver not called | None; invalid history/deltas/identity or numeric range |
| `validation_failed` | Native incumbent status preserved | None; extracted output/metrics rejected |

The production adapter never reads solution variable values on UNKNOWN, INFEASIBLE,
or MODEL_INVALID. UNKNOWN alone does not establish why search ended; no false timeout
or infeasibility diagnosis is inferred. Feasible/non-optimal and no-incumbent handling
are tested through controlled adapter responses, not hardware-sensitive tiny budgets.
`_adapter` is an internal regression seam, not a user/plugin scheduling backend.

## Verification and limitations

`uv run pytest tests/scheduling tests/validation` exercises hand-computable optima,
the four S01 scenarios at explicit hand-built boundaries, calendars/alternatives/QC,
fixed history, full restarts, soft dates, hard horizons, statuses, corruption, numeric
bounds, purity, and five-run reproducibility/latency.

The breakdown boundary at 2700 completes all CNC02-only slotting first; otherwise a
permanent CNC02 failure would correctly make the remaining slotting infeasible.
Order 004's milling is interrupted at 2700 after 180 seconds and restarts for 900
seconds on CNC03. QC runs from 2600 to 2780 and stays fixed. These fixtures are
hand-built scheduler inputs, not an S05 simulator or forecasts inferred from hidden
evaluator truth. Only test fixture assembly reads evaluator interventions.

S05 event priority 90 records `time_advanced` after all lower-priority facts at a
simulated second and thereby closes that second. After a fault is committed at time
`T`, recovery planning still uses and hashes the authoritative snapshot at `T`, but
new simulator callbacks must activate at `T+1` or later. S05 therefore calls the same
pure scheduler with `not_before_seconds=T+1`; independent validation still runs against
the actual `T` snapshot. This floor changes only future assignments: fixed completed,
interrupted, and unaffected running history remains untouched.

Deterministic construction, fixed identity/seed, one worker, and pinned versions
produce identical tested tiny/normal candidates on the recorded host. Arbitrary
wall-time-limited search is not guaranteed identical across hardware, system load,
solver versions, or cases. In particular, a valid incumbent can improve near the
deadline. No arbitrary-instance optimality, performance, physical safety, or industrial
calibration guarantee is claimed.
