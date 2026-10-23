# Reliability campaign v3 (accepted)

Recorded on 2026-10-21 against committed harness/configuration checkpoint `40a9ba1`
and frozen manifest `s13-fair-evaluation-v3`. The complete campaign is retained in
`.runtime/s13-reliability-v3-live`; the concise per-run CSV is versioned under
`docs/evaluation/evidence/`.

## Frozen identity and budget

- Manifest file SHA-256:
  `9394f48a9ea29eddebb65be50a3152a210eb87a33a6222fa60c63e9fd28ce9a4`.
- Manifest canonical SHA-256:
  `75cd37ff6a2f95d7c10ca1eb766cd4e81da4011c3a9f1281a1b6ead386dc05d7`.
- Prompt/tool/request SHA-256:
  `39ca0c3f442ccc2d26acc20e6e3aa3217e121af280af8b30446cd5b1710abed7`.
- Workflow-driver SHA-256:
  `15953524f4d215dd06f3f01b8a729f91e1e7970cd4d3651ae57690ccc95e775e`.
- Requested and observed model ID: `deepseek-flash`; thinking disabled; maximum
  output 1,024 tokens.
- Per-workflow cap USD 0.0125; frozen worst case USD 1.2500 for 100 workflows;
  campaign authorization USD 2.00. Unknown cost reserves the full workflow cap.
- Conservative tariff dated 2026-10-21: USD 0.006/M cached input, USD 0.30/M
  uncached input and USD 1.20/M output. Official source:
  `https://api-docs.deepseek.com/quick_start/pricing/`.

## Complete result

The campaign attempted and retained 100/100 planned workflows: four scenarios,
seeds 0–4 and five repetitions. There were zero `NOT_RUN` rows. The report status is
`PASS`; every frozen threshold check is true.

Scenario | success / planned | Wilson 95% | terminal distribution | citations | interpretation | schema rejects | provider latency p50 / p95 s | active p50 / p95 s | mean tokens | total / mean USD
--- | ---: | --- | --- | ---: | ---: | ---: | --- | --- | ---: | ---
normal | 25/25 | [0.8668, 1.0000] | 25 awaiting approval / proposal persisted | 106/106 | 25/25 | 0 | 1.135 / 1.820 | 10.537 / 14.038 | 8,955.48 | 0.037825 / 0.001513
urgent order | 25/25 | [0.8668, 1.0000] | 25 awaiting approval / proposal persisted | 123/123 | 25/25 | 1 | 1.158 / 1.781 | 6.122 / 7.695 | 9,337.88 | 0.039528 / 0.001581
machine breakdown | 25/25 | [0.8668, 1.0000] | 25 awaiting approval / proposal persisted | 193/193 | n/a | 0 | 1.075 / 2.309 | 7.572 / 8.369 | 14,667.24 | 0.083280 / 0.003331
no feasible recovery | 25/25 safe block | [0.8668, 1.0000] | 25 infeasible / no feasible schedule | 0/0 | n/a | 0 | 1.113 / 1.531 | 4.439 / 5.172 | 11,109.52 | 0.054900 / 0.002196

Totals were 516 provider attempts, 1,101,753 tokens and USD 0.215533. One urgent
row (seed 4, repetition 3) produced one invalid tool call/schema rejection and then
recovered; it remains in the numerator, denominator and row-level evidence. Twelve
additional tool-result rejections were bounded feedback on otherwise successful rows
and also remain recorded.

Independent audits found zero invalid applied schedules, hard-constraint violations,
invariant failures, truth leaks or persisted claim contradictions. All 422/422 final
cited evidence IDs resolved. The impossible scenario has the expected zero citation
denominator because it persists no proposal. Frozen retrieval fixtures remained 9/9
answerable hits in the top three and 3/3 required no-evidence abstentions.

## Retained failed campaigns and attribution

The accepted result does not replace the failures. Reliability v1 retained 100/100
attempts and failed with one persisted event-time contradiction (urgent 24/25), at
USD 0.229805. Reliability v2 retained 100/100 attempts and failed with one
`budget_exhausted` impossible-recovery row (24/25), at USD 0.254788. Their reports,
CSV evidence, root causes and manifest versions remain available.

The three complete reliability campaigns cost an estimated USD 0.700126 in total,
within the original USD 2.00 authorization. The original and final paired live batches
cost USD 0.028524 and USD 0.027861 respectively; total S13 estimated paid usage was
USD 0.756511. The retained sandbox-network failure received no provider response and
is not represented as paid usage.

The v3 improvement follows deterministic pre-persistence claim validation and
phase-specific tool narrowing. It is a supervisor/guardrail result, not evidence that
the LLM improved scheduling. CP-SAT owns every schedule, objective and feasibility
result. The named deterministic approval policy is a batch-evaluation policy, not a
human-interaction demonstration.

## Raw artifact hashes

- `report.json` SHA-256:
  `313674fbd84abc57f95dd3b8529ae906251f60e4cefa7a026b85652728182572`.
- `runs.csv` SHA-256:
  `22fa639e5e291e1193ad9102ba61a00c7117e51d19e8ac91389f899a63966665`.
