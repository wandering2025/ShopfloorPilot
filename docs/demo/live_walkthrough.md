# Live DeepSeek demonstration

Run commands from the repository root after [setup](../../README.md). This guide
uses real, paid DeepSeek inference. Selected local data is transmitted to
DeepSeek: the operator request, allowlisted state summaries, observable machine
evidence, retrieved guidance and tool results. Evaluator-only fault truth and
future interventions are not model inputs. Do not enter proprietary data in this
synthetic demo. Credentials stay in backend memory; never paste them into the
dashboard, commands, screenshots or tracked files.

The model interprets requests and selects typed tools. CP-SAT creates schedules,
an independent validator checks them, and deterministic services enforce
approval and state changes. A successful live run is evidence about this bounded
workflow, not industrial diagnosis or better scheduling by the model.

## Frozen settings and cost boundary

S13 froze `scenarios/evaluation.json` (`s13-fair-evaluation-v3`). The requested and
returned model ID was `deepseek-flash`; the recorded served version was
`DeepSeek-V4.1-Flash-1017`. A provider alias can change remotely. Preserve the
following settings when reproducing that evaluation. Verify the current tariff
at the [official pricing page](https://api-docs.deepseek.com/quick_start/pricing/)
before a new paid campaign. These rates were recorded for **2026-10-21**; they
are not a promise about later prices. A changed model, tariff, prompt, tool
contract or limit requires fresh validation and an accurately labeled result.

```bash
export DEEPSEEK_API_KEY_FILE=/path/to/private/deepseek_apikey.txt
export DEEPSEEK_BASE_URL=https://api.deepseek.com
export DEEPSEEK_MODEL=deepseek-flash
export DEEPSEEK_THINKING=disabled
unset DEEPSEEK_API_KEY DEEPSEEK_REASONING_EFFORT
export SHOPFLOOR_PROVIDER_CURRENCY=USD
export SHOPFLOOR_PROVIDER_WORKFLOW_BUDGET=0.0125
export SHOPFLOOR_PROVIDER_RUN_BUDGET=0.0125
export SHOPFLOOR_PROVIDER_INPUT_CACHE_HIT_RATE=0.006
export SHOPFLOOR_PROVIDER_INPUT_CACHE_MISS_RATE=0.30
export SHOPFLOOR_PROVIDER_OUTPUT_RATE=1.20
export SHOPFLOOR_PROVIDER_TARIFF_DATE=2026-10-21
export SHOPFLOOR_PROVIDER_TARIFF_SOURCE=https://api-docs.deepseek.com/quick_start/pricing/
export SHOPFLOOR_PROVIDER_MAX_ATTEMPTS=8
export SHOPFLOOR_PROVIDER_MAX_TOOL_EXECUTIONS=16
export SHOPFLOOR_PROVIDER_MAX_RETRIES_PER_REQUEST=2
export SHOPFLOOR_PROVIDER_ATTEMPT_TIMEOUT_SECONDS=30
export SHOPFLOOR_PROVIDER_ACTIVE_SECONDS=120
export SHOPFLOOR_PROVIDER_MAX_TOKENS=32000
export SHOPFLOOR_PROVIDER_MAX_OUTPUT_TOKENS=1024
export SHOPFLOOR_PROVIDER_RETRY_BACKOFF_SECONDS=1
export SHOPFLOOR_PROVIDER_RETRY_MAX_BACKOFF_SECONDS=4
```

On another machine, change only the key-file path to your private credential
file. The key itself is not an argument. Process variables override provider
env-file settings; a plain `uv run` does not load `.env` automatically, while
provider configuration can load `.env` or `--env-file`. Inspect variable names
and non-secret settings, never dump the environment.

These ceilings persist for each workflow across retries/restarts. The run budget
is shared by workflows of that manufacturing run; a reset creates another run
and therefore another run allowance. It is not a lifetime account or campaign
budget. Before repeating runs, bound their count and retain every outcome and
cost. Twenty S14 regression runs at USD 0.0125 each reserve at most USD 0.25,
within the specified USD 0.30 campaign ceiling; independent readiness and human
demonstrations must have their own accounted allowance. Do not silently raise
caps to rescue a failed case.

The prompt/tool/request digest is
`39ca0c3f442ccc2d26acc20e6e3aa3217e121af280af8b30446cd5b1710abed7`;
the workflow-driver digest is
`15953524f4d215dd06f3f01b8a729f91e1e7970cd4d3651ae57690ccc95e775e`.
The manifest lists the source hashes and shared five-second, one-worker solver
configuration. [S13 methodology](../evaluation/methodology.md) explains the
freeze and comparison.

## Optional readiness probe

This sends real requests and costs money; it proves the adapter/tool round trip,
not completion of a recovery workflow. Use a fresh output directory.

```bash
uv run shopfloor-pilot provider-check --live \
  --output .runtime/s14-live-readiness
```

Inspect `readiness.json`. Missing configuration produces `SKIPPED`, not a live
pass. Network/API errors also do not satisfy the gate. A historical READY badge
does not prove today's call succeeded.

## Human-approved CLI recovery

This is the exact resumable path for S14-A03. **Do not add
`--approval-policy deterministic`**: that would make it an evaluation-policy
run. A real human must review and execute each decision command. An automated
agent must not manufacture a `human:` actor or treat task authorization as
approval of an unseen proposal.

```bash
uv run shopfloor-pilot recovery --provider deepseek \
  --scenario machine_breakdown --seed 0 \
  --output .runtime/s14-live-human

uv run python -m json.tool .runtime/s14-live-human/approval_request.json
```

The first command stops at `awaiting_approval` / `human_approval_required` for
the deterministic initial plan, with revision zero and no applied plan. Review
the run ID, base state version, workflow ID, proposal hash, objective, deltas and
`currently_applicable`. Copy the **exact current** workflow ID and proposal hash
into the following template, replacing the three angle-bracket values:

```bash
uv run shopfloor-pilot recovery-decide \
  --output .runtime/s14-live-human \
  --workflow-id '<workflow_id from approval_request.json>' \
  --proposal-hash '<proposal_hash from approval_request.json>' \
  --actor 'human:<your name>' --decision approve
```

This applies the initial plan, injects the named synthetic fault at its decision
boundary and invokes DeepSeek. It should stop again for human review of the
recovery. Read the **new** `approval_request.json`, `decisions.json` and the
referenced trace under `traces/`. Check evidence references, supported claims,
order/resource deltas and candidate applicability. Execute `recovery-decide`
again with the new workflow ID and hash only after the human approves that
candidate. `--decision reject --reason '…'` records a rejection instead.

The scenario runner resumes execution and performs its explicitly scripted
scenario interventions; the dashboard uses a separate operator maintenance
confirmation. The final summary should be `completed`, with both
`invariants_passed` and `terminal_matches_expectation` true. Retain the two
distinct human decisions, provider trace/cost, `events.jsonl`, `metrics.json` and
`invariants.json`. Until both actual decisions and the live recovery exist,
S14-A03 remains incomplete. A replay, scripted fake or deterministic batch does
not close it.

## Dashboard version

Keep the exported settings in the launch shell:

```bash
SHOPFLOOR_PILOT_UI_RUNTIME_DIR=.runtime/s14-demo-live \
  uv run streamlit run src/shopfloor_pilot/ui/app.py \
  --server.address 127.0.0.1 --server.port 8501
```

Follow the [offline walkthrough](offline_walkthrough.md), replacing scripted
requests with **LIVE DeepSeek (paid, bounded)**. Use separate fresh runs for
urgent insertion and breakdown. Keep initial planning on the baseline, approve
it as the operator, then choose live DeepSeek for the free-text order or
abnormality request. The standard breakdown report can remain empty to use the
scenario's observable report. Inspect **Evidence & trace**, including source,
tool calls, resolved evidence IDs, actual/reserved cost, attempts, tokens and
error status. Human approval and maintenance confirmation remain separate.

API latency uses wall time, while simulated time stays held. A slow or failed
request should pause safely; use [troubleshooting](troubleshooting.md), or switch
explicitly to the labeled baseline/scripted demonstration. Do not describe that
switch as a successful live run.
