# D-144 — Token accounting was priced-blind: one total where the bill has four line items

**Status:** accepted · **Context:** REV-10. `chemclaw_tokens_total` collapsed a turn's usage into a
single number before the counter ever saw it, and the two cache fields were not read at all.

**The number could not answer the question it was added for.** Its own comment says it exists so
that "what is this deployment costing per hour" stops being a question only the provider's bill can
answer (AG-11). A single total cannot answer that, because the bill has different prices per line:
output tokens cost several times input, and a cache read costs roughly a tenth of a fresh input
token. Two deployments — one caching well, one not caching at all — publish *identical*
`chemclaw_tokens_total` while their invoices differ several-fold. The metric was measuring volume
and being read as cost.

MAF has reported all four dimensions since the beginning: `UsageDetails` carries
`cache_read_input_token_count` and `cache_creation_input_token_count` beside the input/output pair.
Nothing read past `total_token_count`.

**Decision:** publish the four priced dimensions as their own counters —
`chemclaw_input_tokens_total`, `chemclaw_output_tokens_total`, `chemclaw_cache_read_tokens_total`,
`chemclaw_cache_write_tokens_total` — keeping `chemclaw_tokens_total` as the total.

Three things this deliberately does *not* do:

- **It does not change what the budget guard meters.** `budget.record` still takes the total, so the
  runaway-cost refusal behaves exactly as before. This splits what is *published*, not what is
  enforced — a guard and a cost report are different jobs, and changing both at once would make a
  429 regression indistinguishable from a metrics change.
- **The cache counts are not folded into `input`.** A provider that reports them has already
  excluded cache reads from `input_token_count`; adding them would re-price the cheap tokens as
  expensive ones, which is the same error as the total, applied harder.
- **A zero is not published.** Each counter is incremented only when its value is non-zero, so an
  `openai_compatible` endpoint that reports no cache fields leaves those two counters untouched
  rather than publishing a flat `0`. That is the rule `service/metrics.py` already states for gauges
  — it refuses to emit an unbound one because "a fabricated zero would be indistinguishable from a
  genuinely idle service" — and precisely the failure REV-19 found in the counters that *were*
  declared. A `cache_read` of 0 and "this provider does not report caching" must not look the same.

**Per-model and per-profile attribution stays open**, and it is the half of REV-10 this does not
close. The registry has no label support at all, so `chemclaw_tokens_total{model=...}` is not
expressible today; adding labels is a change to the exposition format and the registry's storage,
not to the reading. Recorded in `BACKLOG.md` rather than bolted on: four separate counters answer
"what is it costing", and labels answer "costing *on what*", which is a different and larger change.

Written as a `_TurnUsage` dataclass rather than four accumulators threaded through the runner, so
adding a fifth dimension — `reasoning_output_token_count` is already in `UsageDetails` and will
matter when a reasoning model is configured — is one field rather than one more variable at four
call sites.
