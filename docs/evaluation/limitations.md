# Evaluation limitations

- The four scenarios are synthetic and the accepted live comparison has only three
  paired seeds per scenario. Reliability has 25 observations per scenario; even a
  25/25 result has a Wilson 95% lower bound of 0.8668.
- This is not industrial validation. It does not establish behavior under real PLC,
  MES, network, maintenance, staffing, safety-certification or long-horizon load.
- The batch approval actor is a named deterministic evaluation policy. Human factors
  are represented only by two separate S12 live demonstrations and are not estimated
  statistically.
- `deepseek-flash` is a provider alias. The requested and returned ID was recorded,
  but future provider-side alias changes can alter behavior without repository changes.
- Provider latency and cost are environment- and tariff-dependent. Cost is an estimate
  from reported usage with conservative rates; unknown usage is reserved at the cap.
- A successful workflow may include bounded recoverable schema or tool-result
  rejections. Those counts are reported and retained; success does not mean every
  model turn was valid.
- CP-SAT uses a five-second wall limit. Repeated deterministic inputs can have equal
  incumbents and metrics but differ between `feasible` and `optimal` proof labels.
- Normalized equality removes generated IDs and control metadata. It establishes
  business-equivalent inputs/results, not byte-identical fresh live model output.
- Raw full reports, traces and databases remain in ignored local runtime directories;
  versioned CSV evidence is intentionally concise and does not contain prompt bodies,
  authorization values or hidden reasoning.
- The results do not show that the LLM improves scheduling. All schedule quality,
  feasibility and objective outcomes belong to the common solver. The measured model
  role is interpretation, evidence use, orchestration and claim support.
