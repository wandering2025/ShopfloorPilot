# Provider boundary and readiness gate

Created: 2026-10-21\
Scope: S09. The application uses remote DeepSeek inference only. The OpenAI
Python SDK is used solely as an OpenAI-compatible HTTP client against DeepSeek;
no OpenAI model or endpoint is used.

## Verified provider facts (2026-10-21)

| Item | Selected value |
| --- | --- |
| OpenAI-compatible base URL | `https://api.deepseek.com` |
| Model IDs documented | `deepseek-flash` (DeepSeek-V4.1-Flash-1017), `deepseek-v4-pro` (DeepSeek-V4-Pro-0813) |
| Readiness model | `deepseek-flash`, configured explicitly, never inferred |
| Tool mode | Standard (non-beta) function schemas; local JSON Schema validation always applies |
| Beta strict mode | Not used; `/beta` endpoint is not selected |
| Thinking mode | Explicitly configurable; disabled by default for the readiness probe |
| SDK | `openai==3.35.1`, OpenAI-compatible transport only; `max_retries=0` |
| Endpoint/tool docs | <https://api-docs.deepseek.com/>, <https://api-docs.deepseek.com/guides/tool_calls/> |
| Thinking docs | <https://api-docs.deepseek.com/guides/thinking_mode/> |
| Error codes | <https://api-docs.deepseek.com/quick_start/error_codes/> |
| Token usage | <https://api-docs.deepseek.com/quick_start/token_usage/> |
| Tariff source | <https://api-docs.deepseek.com/quick_start/pricing/> |

Official peak `deepseek-flash` tariff checked on 2026-10-21, per 1M tokens:
cache-hit input USD 0.006, cache-miss input USD 0.30, output USD 1.20.
Off-peak rates are half; peak hours are 01:00–04:00 and 06:00–10:00 UTC on
weekdays. S09 deliberately reserves and reconciles at the **higher peak** rates
so a configured cap cannot be exceeded by a time-of-day change. Rates, tariff
date, currency, and source are explicit configuration; the code does not invent
or silently refresh them.

`tool_choice="required"` is documented for non-thinking mode and is rejected by
the service in thinking mode. The readiness probe disables thinking and forces
the fixture tool so the round trip is deterministic in shape. In thinking mode
with tools, `reasoning_content` must be returned in subsequent requests; the
message model preserves it in a `repr=False` field and includes it only in the
wire projection. It is never written to logs, audit dictionaries, readiness
reports, or UI evidence.

## Interface

`src/shopfloor_pilot/providers/` contains:

- `models.py`: typed `ProviderMessage`, `ProviderRequest`, `ToolSchema`,
  `ProviderToolCall`, `TokenUsage`, `ProviderResponse`, `ProviderTurn`, and
  validated tool calls.
- `validation.py`: conversation correlation checks, request size bounds,
  response-shape checks, JSON parsing, and local JSON Schema validation.
- `deepseek.py`: one-attempt transport; maps 401/402/400/422 to permanent
  failures and 429/5xx/connection/timeout to classified transient failures.
- `bounded.py`: shared ledger enforcement, at most two retries per request,
  bounded exponential backoff, 30-second attempt and 120-second active
  deadlines, and conservative cost/token reservations.
- `fake.py`: `ScriptedFakeProvider`, whose artifacts carry an explicit
  `OFFLINE CONTRACT EVIDENCE — not DeepSeek quality or live-readiness` label.
- `config.py`: explicit paid-run settings and a provider-only env-file loader.
- `readiness.py`: labeled offline check and opt-in live readiness probe.
- `src/shopfloor_pilot/budgets.py`: the persistent SQLite ledger.

Every tool schema is a JSON object schema with `additionalProperties: false`.
Arguments are parsed and validated locally before the supervisor receives a
validated tool call. Malformed JSON, non-object arguments, missing required
fields, extra fields, unknown tools, duplicate IDs, mismatched tool-result names,
orphan results, unanswered calls, `tool_choice` violations, oversized requests,
and unsupported response shapes produce typed errors.

## Budget and persistence semantics

The ledger is a separate SQLite database (default
`.runtime/provider_ledger.sqlite3`), keyed by persistent `workflow_id` and linked
to the manufacturing/evaluation `run_id`. It does not modify the authoritative
operational database or its schema. Defaults per workflow:

- 8 provider attempts including retries
- 16 tool executions
- at most 2 retries per request (3 attempts total)
- 30 seconds per API attempt
- 120 seconds of active execution
- 32,000 reserved/reconciled tokens
- explicit workflow and parent-run monetary caps in a configured currency

Retry/backoff and tool work count toward active execution; explicit
human-approval waiting is excluded. The tightest applicable remaining limit is
checked before work starts. Attempts, retries, tokens, time, currency, and tool
counts are committed to SQLite, so a restart or workflow continuation cannot
grant a fresh allowance. Persisted limits/caps are immutable; reopening with
different values is rejected. Any in-flight attempt found after a restart is
charged at its reserved tokens/cost and full timeout, which is conservative.
Retry backoff is committed before sleeping, and an in-flight tool execution
found after restart is marked abandoned and conservatively consumes the
remaining active-time allowance. Tool completion receipts are bound to the
recorded execution ID, workflow ID, and tool name.

`BoundedProvider.complete(..., request_id=...)` requires the supervisor to
persist one stable ID for each logical request and reuse it after restart. This
keeps the per-request retry count authoritative across process boundaries.

Before any paid request the adapter reserves cache-miss input plus maximum output
cost using the configured dated rates. Reported usage is reconciled exactly once;
if usage is absent, the full reservation is retained as consumed. Currency
amounts are integer micro-units so floating-point rounding cannot under-reserve.
Prompt reservation uses the exact request projection's UTF-8 byte length plus
fixed/per-item protocol margins. A byte-level tokenizer cannot emit more content
tokens than input bytes before merges, avoiding the under-reservation risk of
language-specific character-ratio heuristics.

## Commands

Offline (no credentials or network; visibly labeled fake evidence):

```bash
uv run shopfloor-pilot provider-check --output .runtime/provider-readiness-offline
```

Live (opt-in, requires complete explicit settings; the user may provide the key
through `DEEPSEEK_API_KEY` or `DEEPSEEK_API_KEY_FILE`):

```bash
uv run shopfloor-pilot provider-check --live --output .runtime/provider-readiness
```

Required live settings include the endpoint, model, currency, workflow and run
budgets, dated rates, and tariff source. Missing settings produce `SKIPPED`
without any request. The live report contains only provider/model/time/usage/
cost/checks and deliberately omits prompts, completions, reasoning text,
authorization headers, and credentials.

Paid configuration accepts only the official `api.deepseek.com` host and the
selected standard root endpoint; alternate hosts, ports, and paths (including
`/beta`) fail before a credential can be sent. A readiness `PASS` additionally
requires both turns to report usage and to match the configured provider/model
identity; the report records that validated response identity rather than a
hard-coded label.

## Known boundaries

- The offline fake proves local orchestration, correlation, and error handling
  only. It is not evidence of DeepSeek quality or live readiness.
- A live success is a bounded smoke probe, not a general service-level guarantee.
- The ledger is single-writer; callers open a workflow once and do not reopen it
  while an attempt is in flight.
- Tool argument validation is local and authoritative; provider-side strict mode
  is intentionally not relied on.
