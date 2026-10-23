# Versioned per-run evaluation evidence

These sanitized CSV files contain one row for every accepted or threshold-relevant
workflow. Full reports, traces and databases remain under ignored `.runtime/` paths;
their SHA-256 values are recorded in the S13 evidence notes.

- `offline_v3_baseline.csv`: 20/20 final-code deterministic-baseline rows.
- `offline_v3_scripted.csv`: 20/20 final-code scripted-provider rows.
- `paired_v3.csv`: 24/24 final-code paired baseline/DeepSeek rows.
- `reliability_v1_failed.csv`: all 100 first reliability attempts, including the
  contradictory urgent row.
- `reliability_v2_failed.csv`: all 100 second reliability attempts, including the
  budget-exhausted impossible row.
- `reliability_v3_accepted.csv`: all 100 accepted reliability attempts.

The CSV schema preserves campaign, arm, scenario, seed, repetition, attempt and
terminal status, success, safety fields, schema rejection count, provider usage,
latency, cost and semantic snapshot hash. It does not contain credentials or provider
prompt/response bodies.
