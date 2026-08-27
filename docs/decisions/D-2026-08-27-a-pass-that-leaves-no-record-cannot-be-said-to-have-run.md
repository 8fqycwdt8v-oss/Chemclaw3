# D-2026-08-27-a-pass-that-leaves-no-record-cannot-be-said-to-have-run — the data path emits what it did, and what it could not do

## Status

Accepted.

## Context

A review of the data path — `ingest/`, `retrieval/`, `kg/`, `publish/` and the `core/` modules
under them — asked one question of the source rather than of the documentation: what does a running
deployment emit?

The measurement that framed everything else: a grep for `perf_counter` or `monotonic` across those
packages returned **two** hits, both a cache TTL in `kg/graph.py`. **Not one duration was measured
anywhere in roughly 26,000 lines.** The system's only two latency histograms were both recorded
from `api/` and `agent/`, and `core/metrics.py`'s standing answer — "anything finer is what the
trace pipeline is for" — described a pipeline that does not reach these packages at all: the whole
repository holds two first-party spans.

The specific findings that followed all have the same shape. A subsystem does its work correctly,
and the difference between doing it and not doing it is invisible from outside:

- **`SyncReport` was built, returned, and logged nowhere.** Twelve counters, including the
  per-extension skips whose own module docstring calls them the answer that is never silence. A
  sweep over a terabyte share emitted a few WARNINGs about what it could not read and was otherwise
  silent; the only place the result existed was the workflow result in the Temporal UI, which
  `durable/publish.py` already concedes "is not something anyone watches". The ELN sync logged one
  line that omitted the duration, the entries fetched, the resulting cursor, and **`source`** —
  which is a parameter of the very function emitting it, so with two ELN sources you could not tell
  which line belonged to which. The label drain logged only failures, so a clean pass logged
  nothing at all.
- **Ingest lag was not observable.** `sync_cursors` carries a cursor and an `updated_at` and
  nothing read either for monitoring, so the wedge `ingest/eln/sync.py` documents at length — a
  fetch that keeps returning the same amended page, so the cursor never advances — is
  indistinguishable from a quiet source. Its own comment says so: "Nothing reports it — the log
  reads `ingested=N rejected=0`".
- **Retrieval counted the wrong thing.** `chemclaw_evidence_source_chunks_total` counts what a
  retriever *handed over*, pre-merge, which is measured before RRF and before the budget cap.
  `D-2026-08-01-a-cap-that-starves-a-source` measured *surviving* chunks (graph 38, lexical 0,
  vector 2) — so the metric added in that defect's name does not cover it. Reintroduce
  `retrieval_source_weights` at the value that ADR itself measured and no series moves.
- **The documented outbox backlog formula was wrong three ways.**
- **Embeddings were entirely uninstrumented**, `core/db.py` had no timing at all, migrations
  emitted nothing while they ran, and `publish/drivers/http.py` declared a logger it used zero
  times.

## Decision

Instrument the data path at the seams every caller already goes through, rather than at each
caller. Specifically:

1. **One `ingest.finished` record per pass**, from the three ingest loops (the document share, the
   ELN sync, the label drain), carrying every field of that pass's report plus `source`,
   `duration_s` and `next_cursor`, and incrementing
   `chemclaw_ingest_records_total{source,outcome}`. Emitted from a wrapper around the pass, so the
   four early returns in the document sync leave one record between them and **the absence of a
   record means the pass did not run**.
2. **`chemclaw_ingest_cursor_lag_seconds{source}`**, from the cursor each pass loads as well as the
   one it stores. The *cursor* is cached and `now() - cursor` is computed at scrape time, which is
   still no query per scrape and is strictly better than caching the lag: a frozen "3600 s behind"
   cannot distinguish a source an hour behind from a sync that stopped running an hour ago.
3. **`chemclaw_evidence_source_kept_total{source}`**, counted after the budget cap, with a **seeded
   zero for every source that was asked**. That zero is not invented — it is observed — and without
   it the starved leg is absent from the metric rather than reading zero, so the ratio
   `kept / chunks` has no denominator at the moment it matters. Beside it,
   `chemclaw_evidence_source_seconds{source}` on every path *including the ones that raise*,
   because a vector store that is timing out and one that is empty both return `[]`.
4. **`chemclaw_db_query_duration_seconds{operation}` and `chemclaw_db_query_failures_total{kind}`
   in `core/db.py::connection`**, with `kind` in `unavailable`/`cancelled`/`deadlock`/`error` and a
   WARNING past a new `pg_slow_query_seconds`. The unit measured is the **block**, not the
   statement, because that is what `connection()` can honestly see and it is also the quantity the
   pool cares about. `operation` defaults to `unspecified` rather than being required: thirty call
   sites in twenty-two modules predate it, and an unlabelled hole in the distribution is worse than
   a coarse label.
5. **Three outbox gauge families instead of a subtraction** — see below.
6. Everything smaller in the same register: the embedding seam counts and times every provider
   call and names itself on failure; the migration runner announces the advisory-lock wait *before*
   it waits and each file *before* it applies it; the HTTP sink times every delivery and classifies
   the response; unresolved vector-store points are counted and named; the KG indexer's per-file
   warnings get a denominator.

## The outbox formula, executed

`durable/publish_results.py` stated that the backlog "is
`chemclaw_results_queued_total - chemclaw_results_published_total`, which is already exact and
costs nothing; a gauge would need a `COUNT(*)` on every scrape to say the same thing", and
`docs/guides/runbook.md` repeated it in bold. Executed, with ten rows queued against a destination
that refused every attempt: **queued=10, published=0, failures=50, and the true pending row count
was 0.**

Three independent errors:

- A row retired to `state='failed'` never increments `published`, so the difference reads 10
  forever. `failures_total` cannot correct it, because `mark_failed` adds `len(ids)` **per
  attempt** — ten rows over five attempts is fifty.
- **The two counters live in different processes.** `queued` is incremented in the connector worker
  that finished the calculation and `published` in the `background-jobs` worker that drains, while
  `METRICS` is an in-memory per-process singleton. Restarting either pod resets one side of the
  subtraction; restarting the calc worker makes the difference **negative**.
- `publish/backfill.py` increments `queued` from a short-lived CLI nothing ever scrapes.

The replacement is a count **and an age**, refreshed on the drain pass and read from the queue
itself. The age is the number that separates "a backlog of five that turns over every second" from
"a backlog of five that has not moved since Tuesday", and the objection that argued against a gauge
never applied to it: the partial index `result_publications_pending` — `(sink, enqueued_at) WHERE
state = 'pending'` — already exists, so `min(enqueued_at)` is a read of its leading edge.
`chemclaw_results_dead_lettered_total` is incremented where `_MARK_FAILED` actually flips a row,
which `RETURNING state` makes exact rather than inferred.

`pending_counts()` — documented as "the gauge an operator watches for a stuck destination" and
holding **zero callers anywhere**, its comment naming a CLI that does not exist — is deleted, and
its query lives on inside the refresh that does have a caller.

## Consequences

- A pass that emits no `ingest.finished` record has not run. That is a stronger statement than any
  counter, and it is why the record is emitted from the wrapper rather than from the success path.
- `chemclaw_ingest_records_total{source="labels"}` deliberately does **not** attribute label-drain
  rows to the corpus they came from: those rows were already counted under that source when the ELN
  sync ingested them, and counting them twice would make one series mean two passes.
- An expired git credential is no longer classified as a network partition. `_is_auth_failure`
  reads git's own stderr, and the phrase list is chosen to be wrong in the safe direction — a
  missed phrase keeps today's behaviour, a false positive would make a genuine blip permanent.
- `ReembedReport` gains `stalled` rather than changing `has_more`. The progress gate on `has_more`
  is right (a deterministic batch would otherwise be handed back forever), and the cost of that
  gate — a total provider outage arriving as "up to date" — is a third state, not a different
  value for the two that exist.
- Two call sites this workstream could not reach are named rather than left implied:
  `agent/research_tools.py` must call `fanout.record_kept_chunks` after `_within_budget`, and
  `durable/publish_results.py` should call `outbox.refresh_backlog()` on its drain pass — it
  already happens via `outbox.claim`, so the explicit call is a clarification rather than a
  requirement.

## Alternatives considered

**A per-scrape `COUNT(*)` for the outbox.** Rejected for the reason the original comment gives, and
it is a real reason: the count is the cheap half of what an operator needs and would still be paid
every fifteen seconds. Refreshing on the drain pass costs one indexed read per sink per pass.

**Timing individual statements in `core/db.py`.** `connection()` hands out a connection; it does
not see the statements run on it. A per-statement timer would need a cursor wrapper on every
caller, which is the thirty-call-site change this seam exists to avoid — and the block is the
quantity that explains pool saturation anyway.

**Splitting `chemclaw_result_publish_failures_total` by stage.** Correct, and not available: the
counter is declared unlabelled, and adding a label is an edit to `core/metrics.py`, which another
workstream owns. Each of the three sites in `publish/` now names its stage in its log line
(`publish[enqueue:sinks]`, `publish[enqueue:write]`, `publish[delivery]`) so the four meanings are
at least separable in the trail; the label remains the right fix.
