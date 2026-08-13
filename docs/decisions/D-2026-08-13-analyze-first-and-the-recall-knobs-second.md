# D-2026-08-13-analyze-first-and-the-recall-knobs-second — Two pgvector recall knobs exist, default to emitting nothing, and are documented as the residual after `ANALYZE`

**Status:** accepted · **Date:** 2026-08-13 · Closes the `hnsw.ef_search` row left by `D-2026-08-08-a-derived-index-must-record-what-derived-it`.

## Context

`D-2026-08-08-a-derived-index-must-record-what-derived-it` moved the `note_id` tie-break to an outer
sort, which restored the HNSW index the inner one had disabled (243 ms → 3.6 ms at N=20,000) — and
with it, approximate recall, which the accidental sequential scan had been hiding. The same change
then applied to `document_chunks.search_dense` (228 ms → 2.5 ms at 20,000 chunks). Since then the
tree has had an ANN index on its dense path and **no way to trade latency back for recall**: the
BACKLOG row said, in its own title, "`hnsw.ef_search` is not a setting".

The eligibility predicate makes the question sharper. With HNSW in use, `within=` is a **post**
filter over the `ef_search` candidate list rather than a bound on what the index scan considers, so
a selective scope can leave fewer than `retrieval_top_k` candidates alive and the search returns
short.

**The measurement is what decides the shape of this decision, and it did not say what the row
assumed.** Re-measured at N=20,000 with the tables `ANALYZE`d: **0 of 20** queries short on the note
index in every configuration tried, and 1–2 of 20 on the document index (whose eligibility is an
`EXISTS` the planner cannot collapse into a key scan). Before `ANALYZE`, the same statements went
short on **13 of 20 and 20 of 20**. The large shortfalls were **stale planner statistics, not ANN
recall.**

## Decision

**1. Two settings, `hnsw_ef_search` and `hnsw_iterative_scan`, both defaulting to "emit nothing".**
`0` and `"off"` mean *do not set it*: pgvector's own defaults (40, `off`) stand, no extra round trip
is made, and the dense path costs byte-for-byte what it did before this existed. Nothing changes for
any deployment until an operator sets one.

**2. `ANALYZE` is documented as the first thing to reach for, in the config, in the module, and
here.** These two knobs address only the small residual after it. A knob whose documentation does
not say when *not* to use it is a knob that gets used first.

**3. Applied transaction-locally, via `set_config(name, value, is_local => true)`.** Two reasons,
both load-bearing:

- `SET` accepts no placeholders, so the alternative is interpolating an operator-supplied value
  into statement text.
- `db.connection` commits on exit and pooled connections are reused, so a **session-level** `SET`
  would leak one query's widened candidate list onto every later borrower of that connection —
  including the unscoped searches that never asked for it.

One `unnest` over two arrays applies however many are set in a single round trip, and nothing is
sent when none are.

**4. On the dense note query only.** The lexical leg's `ts_rank` over a GIN index is exact and has
no such parameter, and an upsert is not a search.

**5. `hnsw_ef_search` is capped at 400, which is *not* pgvector's maximum (1000).** Above roughly
200–400 the planner's cost estimate for the index scan can exceed a sequential scan and it abandons
the index entirely — so a larger value silently buys the *opposite* of the recall it was set for.
The 200–400 band is sourced from the August-2026 external retrieval review and is **not measured
here**; 400 is its upper edge, taken as the ceiling so the documented safe range is expressible and
the pathological range is not. Stating the provenance of a number the ceiling rests on is the point:
a future measurement can move it, and will know what it is moving.

**6. `strict_order` is the mode to reach for first.** This index's hits are re-sorted by score
downstream, so `relaxed_order` looks free — but a relaxed scan changes *which* rows come back, and a
recall knob that also perturbs the ranking makes the next measurement ambiguous.

## The deployment consequence, stated because it is easy to miss

`hnsw.iterative_scan` **requires pgvector ≥ 0.8**. pgvector reserves the `hnsw.` prefix, so setting
an unknown parameter under it is an *error* rather than an ignored placeholder. That is exactly why
`off` emits no statement at all: a deployment on the `pgvector >= 0.7` floor the fingerprint
migrations state (`infra/sql/002_molecule_fingerprints.sql`) keeps working untouched, and **only a
deployment that opts in needs the newer server.** The dev stack's `pgvector/pgvector:pg16`
(`infra/docker-compose.yml`) and the live lane, which `docs/archive/live-full-stack-2026-08-04.md`
records at pgvector 0.8.6, both satisfy it — but neither is a *pin*, so a deployment that turns the
knob on should check its own server rather than inherit this sentence.

Had the setting defaulted to anything but `off`, this ADR would have been raising the project's
minimum Postgres extension version — which is a deployment decision, not a retrieval one.

## Consequences

- **The `within=` scale row in `DEFERRED.md` keeps its scale half and loses its recall half.** The
  recall question is answered here (measured small, and now tunable); the array-size question is
  still a scale question with its own trigger.
- `tests/test_retrieval_hnsw_tuning.py` pins the properties rather than the values: both knobs off
  by default, each emitted only when set, the ceiling and the three-mode literal enforced by the
  settings model, both ENV-overridable, the parameters transaction-local on a *shared* connection
  (the leak in decision 3, asserted rather than reasoned), and the dense search actually running
  under them.
- Two new `CHEMCLAW_*` keys appear in `.env.example` with the "reach for `ANALYZE` first" caveat
  beside them, because an operator meets the knob there before they meet the module.
