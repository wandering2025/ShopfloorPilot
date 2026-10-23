# Supervisor workflow and typed tools

Status: S10 architecture and consumer contract (S11 integration, S12 dashboard)

Scope: one DeepSeek-backed supervisor that interprets supported order requests or
abnormality reports, gathers observable evidence, calls deterministic services
through a fixed typed tool registry, and persists an independently validated
proposal that **stops at awaiting human approval**.

## Decision summary

- One supervisor, a small explicit state machine, no LangGraph. Transitions are a
  closed table in `tools/context.py`; an illegal transition raises.
- The model never supplies authoritative values. It drafts verbatim request spans;
  deterministic code parses them, rescans the request, and resolves through the S01
  alias boundary. Planning, validation and persistence reuse S04 `solve_schedule`,
  S03 `validate_candidate`, and S06 `for_model().save_validated_proposal`.
- The registry holds no approval, application, simulation-advance, restoration,
  reset, shell, SQL, filesystem, Python, or network capability. Tool handlers receive
  only `ModelReadableApplicationServices`; passing the full service is a `TypeError`.
- Every provider attempt (including retries) and every model-emitted tool call,
  accepted or rejected, is charged to the S09 persistent ledger for the workflow.
- Traces live in a separate append-only SQLite file, sanitized before storage.

## Entry points

```python
from shopfloor_pilot.agent.workflow import (
    SupervisorTask,
    SupervisorBudget,
    SupervisorConfig,
    SupervisorWorkflow,
)
from shopfloor_pilot.tools import WorkflowKind

workflow = SupervisorWorkflow(
    facade=services.for_model(),  # restricted facade only
    catalog=bundle.catalog,
    scenario_clock=bundle.scenario.clock,  # `today` source; None => clarification
    seed=0,
    provider=provider,  # DeepSeekProvider or TemplateScriptedProvider
    ledger=BudgetLedger(path),  # S09 ledger
    pricing=token_rates,  # dated TokenRates
    budget=SupervisorBudget(currency, run_cap_micros, workflow_cap_micros, limits),
    trace=TraceStore(path, secrets=settings.secrets),
    run_id=run_id,
    workflow_id=workflow_id,  # also the S06 workflow ID; new ID per invocation
)
# One instance runs exactly once; use a new instance and workflow_id per task.
outcome = workflow.run(SupervisorTask(WorkflowKind.ORDER_REQUEST, request_text))
# or: workflow.run(SupervisorTask(WorkflowKind.ABNORMALITY, report, ("CNC02",)))
# workflow.cancel() from another thread or a barrier: cooperative, ledger-enforced
```

`SupervisorOutcome.public_dict()` is stable JSON: `terminal`, `reason_code`,
`details`, ids, `provider`, `evidence_label`, `observed`, `interpretation`,
`proposal` (id, hash, base version/hash, plan revision, objective, deltas), cited
`claims`, registered `evidence`, ledger `budget`, `approval_required`.

The offline demonstration/harness is `shopfloor_pilot.agent.harness.run_agent_scenario`
behind the CLI:

```bash
uv run shopfloor-pilot agent --provider scripted-fake --scenario normal --seed 0 \
  --output .runtime/agent-normal
uv run shopfloor-pilot agent --provider scripted-fake --scenario urgent_order --seed 0 \
  --bootstrap-policy deterministic --output .runtime/agent-urgent
uv run shopfloor-pilot agent --provider scripted-fake --scenario machine_breakdown --seed 0 \
  --bootstrap-policy deterministic --output .runtime/agent-fault
```

- `scripted-fake` never reads credentials or contacts a network; its artifacts carry
  the S09 `OFFLINE CONTRACT EVIDENCE ONLY` label. `deepseek` requires the complete
  explicit S09 paid settings and otherwise writes `SKIPPED` and exits 2 without a
  request. There is no silent switch between them.
- `--bootstrap-policy deterministic` is required for scenarios that need an applied
  initial plan before the decision point (`urgent_order`, `machine_breakdown`). It
  reuses the S06 baseline bootstrap with actor `policy:deterministic`, then advances
  to 3599 s (urgent) or the 2700 s fault boundary. Evaluator interventions are
  consumed only by the harness.
- `--approval-policy deterministic` is an explicit offline batch mode that records a
  `policy:deterministic` decision and applies through S06. Without it the run writes
  `approval_request.json` and stops.
- Artifacts: `state.sqlite3`, `provider_ledger.sqlite3`, `agent_trace.sqlite3`,
  `outcome.json`, `trace.json`, `lifecycle.json`, `summary.json`, and
  `approval_request.json` when waiting. `summary.json` includes the operational
  identity before/after the supervisor and `operational_state_unchanged_by_supervisor`.
- Exit 0 for `awaiting_approval`, `clarification_required`, `rejected` (typed safe
  outcomes); exit 4 for every other terminal; exit 2/3 for usage/input errors.

## Phases, tools, and terminals

| Phase | Offered tools | Leaves when |
| --- | --- | --- |
| `interpret` (order only) | `submit_order_interpretation` | success → `observe`; clarification/rejection terminal |
| `observe` | `get_observable_snapshot` | snapshot pinned → `gather_evidence` |
| `gather_evidence` | snapshot, `summarize_event_log`, `solve_schedule`; order: `list_supported_routes`; abnormality: `summarize_observations`, `retrieve_guidance`, `diagnose_machine` | `solve_schedule` → `plan` |
| `plan` (transient) | — | candidate → `validate`; infeasible → `infeasible`; solver unknown/rejected → `failed` |
| `validate` | `validate_candidate`, `persist_proposal` | persisted → `awaiting_approval` |

Terminals: `awaiting_approval`, `clarification_required`, `rejected`, `infeasible`,
`stale_state`, `failed`, `cancelled`, `budget_exhausted`, `cycle_detected`. None of
them is an approved/applied state. On non-approval terminals the S06 workflow record
stays `running` with no proposal; marking it blocked is an operator action. S11's
`workflows.RecoveryCoordinator` performs that settlement (blocked, stale, or cancelled)
and runs the supervisor in a worker thread built by a session factory; see
[recovery_integration.md](recovery_integration.md).

| Tool | Class | State version | Notes / limits |
| --- | --- | --- | --- |
| `submit_order_interpretation` | draft | — | spans ≤ 80 chars, ≤ 3 due spans, ≤ 5 capabilities |
| `get_observable_snapshot` | read-only | pins | ≤ 8 KB; machines, orders, attempt summary, decision boundary |
| `summarize_event_log` | read-only | required | ≤ 20 events up to the pinned `last_sequence` |
| `list_supported_routes` | read-only | — | catalog products, aliases, routes, capable machines |
| `summarize_observations` | read-only | required | S08 run-bound observations; ≤ 4 machines |
| `retrieve_guidance` | read-only | required | S08 lexical retrieval, top_k ≤ 3, excerpts ≤ 400 chars, `untrusted_excerpt` |
| `diagnose_machine` | read-only | required | S08 hypotheses + `validate_citations`; flags stay literal false |
| `solve_schedule` | read-only | required | S04 on the pinned snapshot + exactly the declared draft delta; ≤ 2 per workflow; unpersisted |
| `validate_candidate` | read-only | required | S03 against live state with the same declared delta |
| `persist_proposal` | proposal write | required | needs a valid validation of the exact candidate hash and cited claims; S06 `save_validated_proposal` |

All schemas set `additionalProperties: false`, bound every string and array, and
require `run_id`. Registry checks, in order: prohibited name, allowlist, phase and
workflow kind, JSON Schema, run scope, pinned `state_version` and live
version/plan-revision equality, machine references against the pinned snapshot,
handler, result size. A handler's state change is a deferred `effect` applied only
after every check passes. A live version or plan-revision change after observation
yields `stale_state`; recovery requires a new workflow ID, a fresh snapshot, a new
proposal, and a fresh human approval (S06 rejects reuse of an old approval).

## Interpretation contract

`OrderInterpretationDraft` fields are verbatim spans (`product_text`,
`quantity_text`, `due_texts`, `priority_text`) plus `request_type`, `priority`,
optional `requested_route_id`, `requested_capabilities`, `requested_machine_ids`.
Normalization is NFKC + casefold + collapsed whitespace.

- A span that does not occur in the request → `span_not_in_request` (rejected).
- Product: the span or the longest supported alias it contains, via S01
  `resolve_order_request`; several products → rejected.
- Quantity: exactly one number (digits, one–ten, 一…十/两); vague or ranged
  language anywhere ("a few", "几件", "2-3") → `ambiguous_quantity`.
- Due: every expression in the whole request and in the drafted spans is resolved;
  none → `missing_due_time`; different seconds → `conflicting_due_time`.
  `within/in N hours|minutes`, `N小时内`, `N分钟内` resolve to
  `decision_boundary + N·unit`; `today`/`今天`/`end of shift` resolve to the scenario
  `shift_boundary_seconds`; without a clock → `today_without_clock`. The host date is
  never read.
- Priority: `urgent` needs a quoted urgency span (urgent, rush, ASAP, 加急, 紧急,
  插单, …); urgency language with `normal` → `priority_conflict`. Weights are the
  documented policy `normal=2`, `urgent=10`.
- Unsupported process words (weld, anodize, paint, laser, 焊, …), unknown or
  off-route capabilities, and machine pins → rejected.
- Success yields `order_id = order-req-<sha256(workflow_id)[:16]>`, release time =
  decision boundary, and applicable constraints (route steps with capable machines,
  hard horizon, soft due date, approval required).

The decision boundary mirrors S06 `_effective_time`: the current simulated second if
its first event priority is still open, otherwise the next second. Solver
`not_before_seconds` uses it, so an approved proposal is applicable at that boundary.

## Evidence and claims

Tools register evidence IDs: `interpretation:`, `route:`, `snapshot:v<n>:<hash12>`,
`event_log:`, `observation:<event_id>`, `guidance:<chunk_id>`,
`hypothesis:<machine>:<n>`, `diagnosis:`, `solver:<candidate>`,
`validation:<candidate>`. Only registered IDs are citable. `persist_proposal` claims
(1–6, each ≤ 240 chars, 1–6 IDs) must satisfy:

| Claim kind | Allowed evidence kinds | Extra rule |
| --- | --- | --- |
| `observation` | snapshot, event_log, observation | — |
| `hypothesis` | hypothesis, observation, guidance, diagnosis | must cite a hypothesis |
| `plan` | validation, solver, interpretation, route, snapshot | must cite `validation:<current candidate>` |

At least one plan claim is required; abnormality workflows also need an observation
or hypothesis claim. Affirmative outcome assertions ("is approved", "has been
restored", "root cause", 已批准, 已修复, …) are rejected; common negations pass.
Cited hypotheses are re-validated with S08 `validate_citations` against their exact
pinned version. Rejections return `allowed_evidence_kinds` so a model can correct
itself. Retrieved excerpts and hypothesis text are marked `untrusted_text`.

## Budget, retries, and cycles

The S09 `BoundedProvider` enforces attempts, per-request retries, tokens, attempt
and active deadlines, and monetary caps. The supervisor adds:

- each tool call → `record_tool_execution` before the registry runs; refusal →
  `budget_exhausted`; tool time is charged as active time;
- identical call (name + canonical arguments) repeated without workflow progress →
  after 2 repeats `cycle_detected` (`repeated_calls_without_progress`);
- 3 consecutive invalid provider turns (unknown tool, malformed JSON, schema failure,
  no tool call) → `cycle_detected` (`repeated_invalid_turns`);
- awaiting approval → ledger `pause_for_human` (waiting excluded from active time);
- reopening an exhausted workflow ID grants no new allowance.

Each request carries the system prompt, one user message with the task envelope and
a compact digest (phase, allowed tools, pinned state, interpretation, ≤ 24 evidence
summaries, candidate, ≤ 3 notices, next step), and the previous assistant turn with
its tool results. The full history is not replayed, which keeps S09's byte-level
token reservation within the 32,000-token default.

## Trace store and UI projection

`agent.trace.TraceStore` (table `agent_trace`, primary key `(workflow_id, sequence)`)
records `workflow_started`, `provider_request` (public message projection, tool
names, byte size), `provider_response`, `provider_rejection`, `tool_call`,
`tool_result` (with newly registered evidence IDs), `transition`, `notice`, and
`terminal` (with ledger snapshot). Keys `reasoning`/`reasoning_content` are dropped
and configured secrets plus `Bearer …`/`sk-…` patterns are redacted.

`project_trace(records)` returns the S12 view `{"schema_version": 1, "view":
"supervisor_trace", workflow_id, run_id, workflow_kind, provider, evidence_label,
terminal, steps[]}`; each step has `sequence`, `kind`, `phase` and kind-specific
fields (e.g. `tool`, `status`, `reason`, `evidence_ids`, `from`/`to`).
`normalize_for_comparison` drops only `recorded_at_utc`, `latency_seconds`,
`active_seconds`, `created_at_utc`, and `budget` for scripted-run comparison.

## Known limitations

- The span parser covers the documented phrases only; other phrasing asks for
  clarification rather than guessing.
- The outcome-assertion check is lexical; it can reject an unusual negation
  ("nothing is approved"), which costs a retry but never authority.
- Wall-time-limited CP-SAT solves (the 9-order normal case returns `feasible` at the
  5 s limit) matched across repeated runs on this host, but S04 does not guarantee
  identical incumbents across hosts or load.
- A live success is a bounded smoke run, not a statement of DeepSeek quality across
  requests; live output can differ run to run.
