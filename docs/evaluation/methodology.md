# Evaluation methodology

## Frozen comparison contract

The final accepted contract is `scenarios/evaluation.json`, manifest
`s13-fair-evaluation-v3`. Its file SHA-256 is
`9394f48a9ea29eddebb65be50a3152a210eb87a33a6222fa60c63e9fd28ce9a4` and its
canonical SHA-256 is
`75cd37ff6a2f95d7c10ca1eb766cd4e81da4011c3a9f1281a1b6ead386dc05d7`.
The manifest was committed before its accepted campaigns ran. V1 and v2 are archived
because their complete reliability campaigns exposed real failures; v3 changes only
the guarded orchestration contract, not the scenarios, seeds, solver, thresholds or
budgets.

Four scenarios are fixed: normal order, urgent insertion, recoverable machine
breakdown and impossible recovery. Offline evaluation uses seeds 0–4. Paired live
evaluation uses seeds 0–2. Reliability uses five repetitions of seeds 0–4 for each
scenario, or 25 workflows per scenario and 100 total.

## Arms and attribution

Arm | Meaning | Capability claim
--- | --- | ---
baseline | Fixed deterministic policy over structured order input or observable alarms; no LLM | Reference workflow behavior only
scripted | Labeled fake provider exercising the exact infrastructure contract | Infrastructure evidence only; never an LLM result
DeepSeek | Real `deepseek-flash`, thinking disabled | Interpretation, evidence selection, tool/schema orchestration and supported claims only
solver | Shared OR-Tools CP-SAT engine | Owns feasibility, scheduling decisions and objective quality

Baseline and DeepSeek use the same scenario snapshot, intervention time, solver
version/objective, five-second solver budget, one worker, solver seed, observation
boundary, allowed actions, independent validator, coordinator and deterministic batch
approval policy. Baseline receives frozen structured input or an observable-alarm rule;
DeepSeek receives a semantically equivalent natural-language request. Simulation is
paused during inference and approval, so network latency cannot change the plant
instance.

## Metrics and failure accounting

Every planned row remains in the denominator. Preflight failures become `NOT_RUN`;
provider, budget and guardrail failures remain failed rows; impossible schedules count
only when the solver reaches the expected safe block. The harness records terminal
distribution, hard constraints, independent invariants, interpretation, retrieval,
citations and semantic claim audit, schema/result rejections, solver status,
actual/predicted/censored metrics, provider and active latency, tokens and conservative
cost. Wilson 95% intervals use the planned per-scenario denominator.

The independent audit replays the event stream, checks each applied plan, validates
one application per consumed approval, verifies completed/running/interrupted work and
decision boundaries, resolves every final cited evidence ID, and scores unsupported or
contradictory claims separately. Unfinished outcomes remain censored and are never
reported as zero tardiness.

## Budgets and reproducibility

DeepSeek is capped at USD 0.0125 per workflow. Paired live worst case is USD 0.1500.
Reliability worst case is USD 1.2500 inside the preflighted USD 2.00 campaign cap;
unknown usage reserves the full workflow cap. Pricing uses conservative peak rates of
USD 0.006/M cached input, USD 0.30/M uncached input and USD 1.20/M output, dated
2026-10-21, from `https://api-docs.deepseek.com/quick_start/pricing/`.

Raw databases, traces and full JSON reports are ignored under `.runtime/`. Concise
per-run CSV files and report hashes are versioned in `docs/evaluation/evidence/`. The separate human-approved dashboard demonstration
from S12 is reported as demonstration evidence and is not included in batch rates.
