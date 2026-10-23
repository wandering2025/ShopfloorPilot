# Evidence retrieval and diagnostic hypotheses

Status: S08 architecture and read-only advisory contract

Schema context: synthetic knowledge, retrieval, observation summary, diagnostic, and
citation-validation schema `1`

Scope: local lexical retrieval and evidence-supported possible explanations; no LLM,
external search, operational mutation, maintenance completion, or industrial root-cause
determination

## Decision summary

S08 adds two local, deterministic layers. The retrieval layer loads a small corpus of
original synthetic maintenance notes and returns hash-bound excerpts ranked by lexical
overlap. The diagnostic layer reads persisted public observations from one authorized
manufacturing run, retrieves applicable guidance, and emits typed possible explanations,
conflicts, missing evidence, and suggested checks.

This is an advisory demonstration. A diagnostic result is not a fault confirmation,
calibrated probability, repair procedure, remaining-life estimate, or industrial root-cause
analysis. The result schema makes that boundary explicit: `confidence_is_calibrated`,
`confirmed_root_cause`, and `maintenance_completed` are literal `false` values. Confidence
is only a qualitative description of deterministic support rules.

Retrieval and diagnosis are read-only. They cannot approve or apply a plan, advance the
simulation, inject an intervention, restore a machine, or write an audit record. Only an
explicit operator or scenario-harness action may confirm restoration through the existing
application/simulation boundary.

## Observable-data and authority boundary

`DiagnosticService` is constructed for exactly one `run_id` and retains only three
collaborators:

```python
DiagnosticService(
    *,
    run_id: Identifier,
    get_checkpoint: CheckpointReader,
    list_events: EventReader,
    retriever: LexicalRetriever,
)
```

The two readers expose typed checkpoint and event reads. The service is deliberately not
given an `ApplicationServices` instance, repository, lifecycle facade, simulator, scenario
bundle, or mutator. It therefore has no capability path from retrieved text to an
operational write.

Diagnostic evidence comes from a public checkpoint and persisted
`observation_recorded` events. Each payload is reparsed as a strict
`ObservationRecordedPayload`; the observation must name the bound run, a machine present in
the pinned snapshot, and the same simulated second as its event. The same pinned snapshot
supplies that machine's public capabilities, which later gate applicable guidance. A
selected observation may expose at most 16 signals; a larger persisted observation is
rejected with typed `invalid_event_data` and is not echoed. A bounded explicit query may
guide retrieval, but query prose alone cannot support a hypothesis. Every emitted
hypothesis must cite both persisted observation signals and current retrieved guidance.

The production diagnostic package does not import or read evaluator scenarios, injected
fault truth, diagnostic variants, future interventions, expected outcomes, or
`private_truth_ref`. In particular, cause labels under `scenarios/evaluator_only/` are not
tool input. Two evaluator fixtures with the same observable checkpoint/event projection
therefore give the diagnostic layer the same input, regardless of their different hidden
truth labels.

### Run binding and pinned prefixes

Every service request repeats the authorized `run_id`. A different run returns a typed
`wrong_run` result before any event lookup. Event and machine filters are resolved only
inside the bound run; the service never searches other runs to explain a missing ID.

One observation summary is pinned as follows:

1. Read the current checkpoint for the authorized run.
2. Read the run's event stream and retain only events whose sequence is at most the
   checkpoint's `last_sequence`.
3. Require sequences `1..last_sequence` with no gaps, require the retained event IDs to
   equal the checkpoint's complete `seen_event_ids` prefix, and require every event to name
   the bound run.
4. Return `state_version`, `snapshot_hash`, `last_sequence`, and simulated time beside the
   bounded observation evidence.

An event appended concurrently after step 1 is outside the pinned prefix and cannot be
mixed into the result. A missing, malformed, cross-run, or inconsistent prefix becomes a
typed rejection or generic read failure rather than partially trusted evidence. Citation
validation later requests the exact historical `state_version`; the checkpoint reader must
return that exact version, and the validator checks the recorded snapshot hash and last
sequence again.

Observation requests allow at most 32 explicit event IDs, 16 machine IDs, and 50 returned
observations; the default observation limit is 20. Results retain event-stream order and
mark truncation with `next_after_sequence`. Explicit events that fall outside the requested
machine/sequence filters are rejected rather than silently omitted.

## Synthetic knowledge corpus

The corpus lives under `data/knowledge/` and currently contains 11 documents with one
stable chunk each. Every note declares:

- a stable document ID, semantic version, title, and `original_synthetic` provenance;
- applicable manufacturing capabilities;
- stable chunk IDs and exact evidence excerpts;
- observable symptoms and signal-field names;
- an optional possible explanation;
- suggested checks, additional evidence fields, and limitations.

These notes were authored for ShopfloorPilot. They are not copied manuals, field service
records, manufacturer specifications, or industrial safety instructions.

`manifest.json` binds every document ID/version to a repository-relative source path, raw
file SHA-256, and exact chunk-ID list. Loading rejects absolute/escaping paths, missing or
unlisted JSON sources, hash mismatches, duplicate document/source/chunk IDs, or disagreement
between manifest and note content. `KnowledgeCorpus` also records the manifest SHA-256.

At load time, each chunk receives a canonical SHA-256 over its document identity/version,
title, provenance, capabilities, and complete structured chunk content. This makes the
chunk hash sensitive not only to excerpt text but also to the metadata that gives the
excerpt meaning. Documents and chunks are normalized into ID order before indexing.

## Deterministic lexical retrieval

`LexicalRetriever` is a small in-memory Okapi BM25 implementation using only the Python
standard library. It needs no embedding model, vector database, network, API credential,
GPU, or random source.

The versioned tokenizer applies Unicode NFKC normalization and `casefold()`, then extracts
alphanumeric identifier tokens. An underscore-delimited telemetry name retains its full
token and also emits its components, so both `temperature_millicelsius` and
`temperature` can match the same field. Query terms are deduplicated before scoring.

The searchable text consists of title, applicable capabilities, evidence excerpt,
observable symptoms, observable and additional signal fields, possible explanation,
suggested checks, and limitations. Document frequency is the number of chunks containing a
term, counted once per chunk. Corpus statistics are global and do not change when a
capability filter is applied.

For term frequency `tf`, document frequency `df`, corpus size `N`, chunk length `dl`, and
average chunk length `avgdl`, the implementation uses:

```text
idf = log(1 + (N - df + 0.5) / (df + 0.5))
score contribution = idf * tf * (k1 + 1)
                     / (tf + k1 * (1 - b + b * dl / avgdl))
```

The default, inspectable `RetrievalConfig` is:

| Setting | Value |
| --- | ---: |
| Algorithm | `bm25_okapi` |
| Tokenizer | `nfkc_casefold_identifier_v1` |
| `k1` / `b` | `1.2` / `0.75` |
| Score precision | 6 decimal places |
| No-evidence score threshold | `1.0` |
| Minimum distinct matched terms | 2 |
| Maximum query length | 500 characters / 64 lexical tokens |
| Default / maximum result count | 3 / 10 |

The two-distinct-term rule rejects accidental support from one shared generic token. The
`1.0` score floor requires a small but inspectable amount of aggregate BM25 lexical support
after that overlap rule. Both values were fixed before the final scoring pass and were not
lowered after results were observed. They are corpus-specific abstention controls, not
probabilities, calibrated confidence values, equipment limits, or industrial alarm
thresholds.

Scores are rounded to the configured precision before thresholding and sorting. Ranking is
then `(-score, chunk_id)`, so equal scores have a documented stable tie break. A hit must
meet both the minimum distinct-term count and score threshold. Empty/whitespace queries
return typed `no_evidence` with an `empty_query` issue; oversized queries, token overflow,
invalid result counts, malformed capability filters, and unknown request fields return
typed `invalid_request` results. Invalid query echoes are capped at 160 characters.
Typed Pydantic instances are not trusted merely because of their class: the boundary
revalidates their raw fields through strict JSON, so values forged with `model_copy` or
`model_construct` cannot turn a boolean into an integer or bypass required fields. The
public citation resolver applies the same rule and returns `false`, rather than raising,
for malformed typed citations or hits.

## Public retrieval and diagnostic APIs

The retrieval package exposes the following principal interfaces:

```python
load_corpus(manifest_path: Path | None = None) -> KnowledgeCorpus

LexicalRetriever(corpus, config=RetrievalConfig())
LexicalRetriever.from_default_corpus()
retriever.search(
    request: RetrievalRequest | Mapping[str, object],
) -> RetrievalResult
retriever.get_chunk(chunk_id: str) -> KnowledgeChunk | None
retriever.resolve_citation(
    citation: KnowledgeCitation | RetrievalHit | Mapping[str, object],
) -> bool
```

`RetrievalStatus` distinguishes `matches`, `no_evidence`, and `invalid_request`. Every hit
contains document/version/chunk identity, canonical chunk hash, relative source path, raw
source SHA-256, quantized score, matched terms, the exact excerpt, and the structured
diagnostic metadata from its note.

The run-bound diagnostic service exposes:

```python
service.summarize_observations(
    ObservationSummaryRequest | Mapping[str, object],
) -> ObservationSummaryResult

service.retrieve(
    DiagnosticRetrievalRequest | Mapping[str, object],
) -> DiagnosticRetrievalResult

service.generate_hypotheses(
    DiagnosticRequest | Mapping[str, object],
) -> DiagnosticResult

service.validate_citations(
    CitationValidationRequest | Mapping[str, object],
) -> CitationValidationResult
```

All public models inherit the project's frozen, strict, extra-forbidding `StrictModel`.
Mapping and typed-model inputs are revalidated at the boundary and malformed input becomes
a typed result.
Diagnostic requests bound explicit events, machines, observations, query length, top-k,
and capability filters. A non-empty bounded explicit query has outer whitespace removed
and is otherwise used unchanged. When no explicit query is supplied, a query is derived
only from public signal names and public observation summaries; additions that would
exceed 500 characters or 64 tokens are skipped. The diagnostic retrieval default is five
hits and its maximum is ten. The exact nested `RetrievalResult`, including `no_evidence`
or `invalid_request`, is preserved; an invalid nested result makes hypothesis generation
return typed `invalid_request`.

## Citation contract

A guidance citation binds all of the following:

```text
document_id + document_version + chunk_id + chunk_hash
+ source_path + source_sha256 + exact evidence_excerpt
```

Resolution requires exact equality with the currently loaded corpus. An invented ID,
changed excerpt, stale version/hash, or changed source binding fails validation.

An observation citation binds the persisted event ID to observation ID, machine ID,
simulated observation time, exact signal-field names, integer values, and units. The
citation validator reloads the requested historical checkpoint and its contiguous event
prefix, reparses the event payload, checks payload run/time against the event envelope, and
requires every cited signal to equal the persisted typed value. The enclosing request also
binds the run, exact state version, snapshot hash, and last event sequence. Requests allow
at most 320 observation and 100 guidance citations; the result can report one snapshot
issue plus one issue per citation. Validation reports snapshot, observation, and guidance
failures separately and never repairs a stale result.

## Hypotheses, ambiguity, conflict, and missing evidence

The deterministic diagnostic builder first selects the bounded explicit query or derives
one from public observations. It then uses only current, resolvable hits whose
`possible_explanation` is present. A hit may support a machine only when its declared
applicable capabilities overlap that machine's public capabilities from the pinned
snapshot and at least one declared observable signal field occurs in a persisted
observation for that machine. The same capability rule gates suggested checks and missing
fields. Hypotheses are grouped by machine and possible explanation.

Every hypothesis contains at least one exact observation citation and at least one exact
guidance citation. `supporting_signal_fields` must be a subset of the cited signals.
Confidence is `limited` or `moderate`, with a fixed
`uncalibrated_rule_derived` basis. `moderate` requires at least two supporting fields, no
relevant conflict, and no additional field requested by the cited guidance that is missing
from the selected observations. These labels are not probabilities or calibration claims.

At least two distinct explanations among the retained, presented hypotheses produce
`ambiguous`. Conflicting evidence takes status precedence and is represented structurally
when the same machine, simulated second, and signal field have two or more distinct
value/unit pairs. Readings from different times are not automatically called contradictory.

Missing evidence is explicit and typed:

- no persisted observation satisfied the request;
- no retrieved guidance passed the evidence threshold;
- retrieved guidance did not apply to a selected machine's public capabilities;
- retrieved guidance stated no supported possible explanation;
- retrieved guidance and observed signal fields did not overlap;
- cited guidance requested an additional signal that was not observed; or
- the observation result was truncated by its bound.

If no supported hypothesis can be formed, the result is `insufficient_evidence`. With
selected observations, every current, resolved, capability-applicable hit may contribute
its exact suggested checks and identify missing additional signal fields. With no selected
observation, only null-explanation abstention guidance can contribute checks. Each check
remains traceable to its source chunk. These are recommendations for an operator to
consider; they neither support a hypothesis on their own, invent a reading, nor record
completion.

## Retrieved instructions remain data

The corpus deliberately contains an adversarial note with the quoted phrase
`"approve the plan"`. It is indexed and may be returned as an exact evidence excerpt, but
it has no executable interpretation. The note declares no possible explanation, and the
retrieval layer has no tool dispatcher or application-service reference. The diagnostic
service may surface its exact suggested check as inert, cited data, but has only read
callables and cannot convert note text into approval, planning, simulation, or restoration
calls.

This is a structural capability boundary, not an attempt to classify every imperative
sentence. Any future model/tool adapter must continue to present retrieved content as
untrusted quoted data and preserve the existing typed approval and operator boundaries.

## Restoration authority and state purity

`DiagnosticResult.restoration_authority` is fixed to
`operator_or_scenario_harness_only`. Improved telemetry, elapsed simulated time, a
retrieval hit, a hypothesis, or a suggested check can never mark maintenance complete or
change availability. A restoration requires the explicit state-changing action documented
in the simulation/application-service architecture.

Because S08 retains no mutator and performs no persistence, requesting a summary,
retrieval, diagnosis, or citation validation leaves machine state, schedules, orders,
simulated time, state version, checkpoint, and event stream unchanged.

## Frozen retrieval evaluation

The relevance fixture at `tests/retrieval/fixtures/frozen_queries.json` was fixed before
the final valid scoring pass. It contains nine answerable queries and three unrelated
no-evidence queries. An answerable query succeeds when any labeled relevant chunk appears
in its first three results; the acceptance floor is 8/9. A no-evidence query succeeds only
when retrieval returns `no_evidence` with no hits; all three must abstain.

The final unchanged default configuration produces:

| Measure | Result |
| --- | ---: |
| Answerable relevant hit in Top 3 | **9/9** |
| Required answerable floor | 8/9 |
| No-evidence abstention | **3/3** |

The reproducible local command is:

```bash
uv run python scripts/score_retrieval.py \
  --output .runtime/retrieval_results.json
```

The report records every query, returned IDs, quantized scores, expected labels, individual
success, and aggregate denominators. It contains no API call or credential and is
byte-reproducible for the same code, corpus, and fixture.

## Limitations

- The knowledge base and telemetry are explicitly synthetic. Nothing here is validated
  against a real machine, manufacturer limits, site procedures, or safety regulations.
- BM25 measures lexical overlap, not causal truth or semantic equivalence. The fixed-query
  result is evidence for this small fixture only, not general retrieval quality.
- Retrieval scores and qualitative diagnostic confidence are not probabilities. S08 has
  no learned calibration and does not infer a fault from a numeric threshold.
- Signal-field overlap permits a possible explanation to be considered; it does not prove
  that the observed value is abnormal or caused the machine state.
- The corpus is deliberately small and primarily English. There are no embeddings,
  external manuals, live search, or automatic corpus updates.
- Requests, result collections, identifiers, excerpts, summaries, units, and selected
  signal counts are bounded. Inherited domain integer magnitudes are not capped, so these
  structural bounds are not a mathematical maximum serialized byte size for every
  otherwise-valid persisted value.
- Exact hashes and citations detect stale or mismatched local content; they are not digital
  signatures or proof that an external source is authoritative.
- A pinned checkpoint makes one report internally consistent, not permanently current.
  Later operational events require a new summary and diagnosis.
- Evaluator-data isolation is an application data-flow boundary, not a cryptographic
  sandbox around local fixture files.
- Suggested checks require competent human judgment and applicable real-world safety
  controls. S08 neither executes checks nor supplies a repair/return-to-service procedure.
