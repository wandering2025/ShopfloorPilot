# Evaluation results

## Accepted matrices

The final v3 offline matrix retained baseline 20/20 and scripted 20/20 expected
outcomes. Each arm completed all 15 feasible workflows and safely blocked all five
impossible workflows. There were zero invalid applications, hard violations, invariant
failures, truth leaks, schema errors or persisted claim contradictions. Scripted
success is infrastructure evidence, not model capability.

The v3 live paired matrix retained all 24 rows. Both baseline and DeepSeek reached the
expected outcome in all 12 cases. Normal, urgent, breakdown and impossible-recovery
results were 3/3 per arm. All 12 pairs had equal normalized semantic snapshots, actual
metrics and solver outcomes. DeepSeek used 67 attempts, 146,653 tokens and USD
0.027861. It resolved 46/46 citations and interpreted 6/6 applicable requests exactly.

## Accepted live reliability campaign

Scenario | expected outcome / denominator | Wilson 95% | citations | contradictions | mean cost USD
--- | ---: | --- | ---: | ---: | ---:
normal | 25/25 persisted | [0.8668, 1.0000] | 106/106 | 0 | 0.001513
urgent order | 25/25 persisted | [0.8668, 1.0000] | 123/123 | 0 | 0.001581
machine breakdown | 25/25 persisted | [0.8668, 1.0000] | 193/193 | 0 | 0.003331
no feasible recovery | 25/25 safe block | [0.8668, 1.0000] | 0/0 | 0 | 0.002196

All 100/100 planned v3 rows were attempted and retained. There were zero invalid
applications, invariant failures or truth leaks. One recoverable schema rejection and
twelve bounded tool-result rejections remain in the evidence. Totals were 516 provider
attempts, 1,101,753 tokens and USD 0.215533. Retrieval scored 9/9 answerable top-three
hits and 3/3 required abstentions. Exact latency, token and cost distributions are in
[`evidence/reliability_v3_accepted.md`](evidence/reliability_v3_accepted.md).

## Failures retained, not rewritten

Campaign | result | retained denominator | threshold miss | estimated USD
--- | --- | ---: | --- | ---:
reliability v1 | FAIL | 100/100 | one persisted event-time contradiction; urgent 24/25 | 0.229805
reliability v2 | FAIL | 100/100 | one budget-exhausted impossible row; safe block 24/25 | 0.254788
reliability v3 | PASS | 100/100 | none | 0.215533

V1 led to deterministic pre-persistence rejection of observation claims that
contradict the pinned snapshot. V2 led to phase-specific tool narrowing so varied
evidence queries cannot consume the token reserve before solving. These are supervisor
and guardrail improvements. They are not attributed to LLM scheduling skill.

The three reliability campaigns together cost an estimated USD 0.700126, inside the
original USD 2.00 campaign authorization. Including both the original and final paired
live batches, estimated S13 paid usage was USD 0.756511.

The original credential-free paired invocation kept 12/12 DeepSeek rows as `NOT_RUN`
and the original sandbox-network batch retained 0/12 provider successes; neither was
relabeled. The explicit no-network safe-failure suite passed 146/146 cases. Independent
offline repeats matched normalized business results, and same-stream replay matched
IDs and revisions in 80/80 runs.

## Human demonstration

The S12 dashboard separately demonstrated a human-approved live normal order and
machine-breakdown recovery. Each was approved and applied exactly once, at USD
0.002930 and USD 0.003890 respectively. Those n=1 interactions are not included in
the deterministic-policy batch results above.
