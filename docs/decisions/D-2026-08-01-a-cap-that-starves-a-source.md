# D-2026-08-01-a-cap-that-starves-a-source — A cap that starves a source

**Status:** accepted · **Date:** 2026-08-01 · **Supersedes:** D-057, in the one claim that
`gather_evidence` ranks by `EvidenceChunk.score` before the cap · **Implements:** the full-codebase
review's retrieval and audit findings

## Context

Two defects, both invisible on the success path, both settled by measuring rather than arguing.

### The retriever cap starved a whole source

`gather_evidence` concatenated the per-source results in config order and truncated to a flat cap,
after sorting the union on `EvidenceChunk.score`. Measured over a 60-note corpus with all three legs
returning hits (graph 45, lexical 8, vector 7; cap 40, `retrieval_top_k` 8):

| mode | surviving graph | lexical | vector |
|---|---|---|---|
| `graph` (the default), before | 38 | **0** | 2 |
| `graph`, score sort removed | 40 | **0** | **0** |
| `hybrid` (RRF) | 25 | 8 | 7 |
| `graph`, after | 25 | 8 | 7 |

The lexical leg contributed **zero** readable chunks to every default-mode answer.

Two earlier rounds on this each had half of it. A review blamed the score sort; a verifier showed the
sort was not causally responsible — correctly, since removing it makes the outcome *worse*, so the
sort was mitigating rather than causing. Neither had counted, so neither found the actual cause:
**a flat concatenation in config order under a hard cap**, with the sort making it slightly less bad
while introducing a second problem of its own.

That second problem is that `EvidenceChunk.score` is not comparable across sources. It carries a
note's `confidence` from the graph leg, a `ts_rank` from lexical, a cosine from vector and a
Tanimoto from fingerprints — the field's own docstring says so. Sorting a union on it ranks four
different quantities against each other. `hybrid` was never affected, because RRF ranks by
*position*, which is comparable.

### A cancelled tool call wrote no audit row

`audit.py` caught `Exception`, which misses `CancelledError` — and D-130 establishes that both a
client disconnect and the turn deadline deliver exactly that. So the GxP trail under-reported
*attempted* tool calls precisely on teardown.

The obvious fix does not work: a plain `await _emit(...)` under a wider `except` is itself cancelled
at the sink's first suspension point, so it writes nothing.

## Decision

**Truncation is round-robin across sources, not a flat cut.** `_interleave_dedup` takes each
source's own ranked list and gives every source its best hit before any source gets its second, with
the same `(note_id, content)` dedup as before. An exhausted source stops consuming a slot, so the
budget flows to whoever still has hits. With a single source it is that source's list, unchanged.

**The cross-source score sort is removed**, and `EvidenceChunk.score`'s docstring now says it orders
a source's own list and nothing wider. This incidentally repairs a second defect: on a widened
search `GraphRetriever` ranks by term coverage first, and the global sort discarded that ordering.

**A cancelled tool call writes a row with a third `outcome`, `"cancelled"`,** through a shielded
writer following `api/runner.py`'s existing `asyncio.shield` teardown pattern. Distinguishing
cancelled from `ok` and from `error` matters more than the row's existence: a trail that recorded a
teardown as an error would be wrong in the other direction.

**No SQL CHECK on `outcome`.** There was never one — the only enumeration was an inline comment. The
table is hash-chained and append-only, so policing three literals would cost a migration per future
outcome, and the producer is one module. The comment now says that is deliberate.

**The observation anchor keeps `min(cluster)`, and its docstring stops claiming stability.**
Disjointness buys collision-freedom, not anchor stability: the id moves when a lower-sorting reaction
joins, or when a bridging reaction merges two single-linkage clusters. A merge-stable key is not
available without new state — a single-linkage cluster's identity *is* its membership, which is
exactly what a merge changes — and the redundancy it would buy against is bounded by
`observation_retire_after_days`, self-healing, and always outranked, since `open_observations` orders
by support and a superset strictly contains its subset. The docstring's `first_seen` and duplicate-PR
justifications are recorded as *not* applying, with the verification: nothing reads `first_seen`, and
`ObservationSynthesisWorkflow` promotes on every pass, so a row that crossed both thresholds was
already out of the open/promotable queries before any later run could move its anchor.

## Consequences

- The default `graph` mode now returns the same source mix as `hybrid`, which is what a reader of
  either mode's description would have expected all along.
- Answers change. A default-mode turn that previously saw 38 graph chunks now sees 25 graph, 8
  lexical and 7 vector. That is the point, and it is a behaviour change worth stating.
- `docs/planning/BACKLOG.md`'s KM-5 row ("`GraphRetriever` scores every machine note at the default,
  so the truncation ordering is a no-op") is now doubly stale and is corrected alongside this.

## Alternatives rejected

- **Proportional allocation by source size.** Rewards a source for returning many weak hits, which is
  what the graph leg does — it is the largest producer and the one that was crowding the others out.
- **Keep the sort and raise the cap.** Treats a symptom, keeps four incomparable quantities in one
  ordering, and makes every answer more expensive.
- **A wider `except` for the audit row.** Measured: the write is cancelled at the sink's first
  suspension point and nothing reaches the table. Pinned by a mutation that a single-cancellation
  test does not catch, which is why a second test exists.
