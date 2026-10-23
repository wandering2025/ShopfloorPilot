# S01 synthetic scenario fixtures

`manifest.json` is the typed, SHA-256-verified index. Public initial data lives in each named scenario directory. Future intervention schedules, latent fault truth, and signal-generation specifications live only under `evaluator_only/` and must never be passed directly to agent/tool consumers.

Use `shopfloor_pilot.domain.fixtures.load_all_scenarios()` to validate manifest coverage, file hashes, schemas, uniqueness, and cross-references. Use `project_public_view(bundle, simulated_time_seconds)` for the allowlisted observation projection. Do not serialize an `EvaluatorScenario` as public state.

All data is synthetic. Time uses integer seconds and half-open intervals. Telemetry uses scaled integers with explicit units. Each initial order unit expands to one complete predefined route chain in S03.

See [`docs/architecture/domain_contract.md`](../docs/architecture/domain_contract.md) for the full decision record and truth-isolation boundary.
