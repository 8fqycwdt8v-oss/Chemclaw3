# D-2026-09-09-a-definition-change-shelves-a-row-it-does-not-delete — A definition change shelves a row, it does not delete it

**Status:** accepted · **Date:** 2026-09-09 · Corrects the safety property stated in
`infra/sql/004_fingerprint_definition.sql` and repeated in `science/fingerprints/store.py`.

## Context

`molecule_fingerprints` was keyed on `id` alone, with `definition` a column the upsert overwrote.
The **read** path is definition-scoped and correct — that half is real. The **write** path was
last-writer-wins:

```
after writer A (ecfp:r2:b2048):  rows=1   A.count=1   B.count=0
after writer B (ecfp:r3:b2048):  rows=1   A.count=0   B.count=1
table: [('CCO','ethanol@B','ecfp:r3:b2048')]
A superseded_count: 1   B superseded_count: 0     A find_similar: []
```

`004` states the safety property as *"a mismatched backfill only makes stale rows fall out of
similarity search (safe), never returns a wrong score"*, and `store.py` repeats it: *"the stale rows
simply fall out of search until they are re-indexed."* **Both sentences describe rows that still
exist. They do not.** Two writers on different definitions — a rolling upgrade that changes
`ecfp_radius`, two pods on different images, a second site — each destroy the other's row, and each
side's `superseded_count` reports the other's population as "stale, re-index me" with **no state
either side can re-index from.**

## Decision

**The `definition` joins the primary key** on `molecule_fingerprints` (`(id, definition)`),
`reaction_fingerprints` and `corpus_reactions` (`(source, id, definition)`) — the pattern `039` and
`041` already established one directory over with `embedding_key` and `chunking_key`. The
in-memory reference store had the identical defect and is fixed with it, since it is the oracle the
SQL is asserted against.

The alternative — an upsert that refuses a definition change — is **not implementable** here: with
`ON CONFLICT (id) DO UPDATE … WHERE t.definition = EXCLUDED.definition`, a re-index becomes a silent
no-op on every existing id, so the corpus could never be rebuilt. Enabling it would need a re-index
flag plumbed through the ingest path.

`_all` becomes `DISTINCT ON (<key>) … ORDER BY <key>, (definition = current) DESC`, deduped in both
backends. Without it the shelf **doubles the substructure corpus** — driven, five molecules under
two generations returned 10 rows against 5.

## What this costs, stated rather than hidden

**A finished rebuild leaves the superseded generation in the table permanently**, because the
runtime role holds no `DELETE` on either fingerprint table. `superseded_count()` keeps counting it
and `index_partial` stays True, so every search says PARTIAL until someone disposes of it. That is
not a corner case: the operator-log test asserted the opposite and had to change.

It was deliberately **not** papered over by making the probes an anti-join ("ids with no current
row"). The cheap `min/max` probe cannot express that, and the anti-join is O(n) per search in
exactly the healthy case `D-2026-09-09-a-rebuild-nothing-counts-reports-as-finished` measured at
26.32 ms and replaced with 0.55 ms. So the conservative signal stands — over-cautious is the right
direction for the one tool whose job is *"have we seen this before"* — and the operator **log**,
paid once per process, names both states and both actions, with the disposal statement in the
migration.

`index_partial` stays a boolean for the same reason: after this change the cheap numbers no longer
mean "the searchable fraction", because a shelved-but-rebuilt generation makes
`records/(records+superseded)` read as 50% of a corpus that is wholly searchable. An honest fraction
needs the anti-join this module already refused.

Two further costs: a second full copy of the index on disk until disposal, and the `approximate`
arm's over-fetch permanently divided by the number of generations held, because its inner
`ORDER BY … LIMIT` cannot see the definition. The shipped arm is `exact` and is unaffected.

**Rollback** (`094_fingerprint_definition_identity.sql`): unlike `056`, `063` and `093`, this one stops the previous image writing at all — its
`ON CONFLICT (id)` / `(source, id)` no longer plans against the widened key, so every fingerprint
and corpus-reaction write fails with `InvalidColumnReference`. Roll forward, or re-add the old key
by hand.

## Measured beside it: the bulk-load path

50,000 `bit(2048)` rows, server-side `INSERT … SELECT` in both arms so neither number contains a
round trip or an event loop:

| | | |
|---|---|---|
| A: insert with the HNSW index live | 450.1 s | 111.1 rows/s |
| B1: insert into an unindexed table | 0.5 s | 94,854.5 rows/s |
| B2: `CREATE INDEX` afterwards | 132.2 s | |
| **B total** | **132.7 s** | **376.7 rows/s — 3.4x** |

B2 ran with `max_parallel_maintenance_workers = 0`, because this container ships docker's default
64 MB `/dev/shm` and a parallel build fails with `DiskFull`, so **3.4x is a floor**.

**B1 settles a design question rather than merely reporting a speed-up**: the write is 0.4% of arm
A, so the entire 111–251 rows/s is HNSW insertion. Batching `add_many` would buy nothing
measurable, and the measurement is recorded in its docstring so a later session does not "optimise"
the loop. The drop-load-create shape belongs in the re-index job when it is built.

## Left open

- **`corpus_molecules` still has the defect**, deliberately: it is written by
  `CorpusMolecules._UPSERT`'s own `ON CONFLICT (id)`, which would stop planning against a widened
  key. One migration plus that statement, in one commit.
- **ANN recall is unmeasured.** Uniform random bit vectors give each query one true neighbour by
  construction, so the question is neither confirmed nor falsified. It needs a real ECFP corpus with
  realistic neighbourhood density.
- `063`'s prose is stale and immutable: it says `PostgresFingerprintStore.add` deletes the unsourced
  twin when it writes a sourced row. It does not, and `store.py` explains why not.
