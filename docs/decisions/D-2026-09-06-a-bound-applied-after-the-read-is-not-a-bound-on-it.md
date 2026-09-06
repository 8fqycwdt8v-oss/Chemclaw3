# D-2026-09-06-a-bound-applied-after-the-read-is-not-a-bound-on-it — the ELN chunk size reaches the adapter, and the file drops say why they cannot use it

**Status:** accepted · **Date:** 2026-09-06 · **Builds on:**
`D-2026-08-29-a-bound-derived-twice-is-two-bounds` (a pre-flight that re-derives its caller's chunk
from less information than the caller had), `D-2026-08-26-the-driver-s-signature-is-the-schema`
(the warehouse adapter names no table), D-054 (per-source cursors) ·
**Corrects** `JsonExportAdapter.fetch_new_entries`' closing paragraph, which said bounding the read
"is a `BACKLOG.md` row" — there was no such row, and the answer for a file drop is not a deferral.

## Context

`durable/eln_sync.py::_BoundedIngest` caps a chunk at `eln_sync_batch_size` **after** the adapter
has returned, so every chunk of a drain re-reads the whole outstanding set to keep a hundredth of
it. Waves 1–3 left this open for want of a `limit` through the `ElnAdapter` interface.

## What was measured

A drain of a real-shaped JSON drop at the shipped batch of 100: 1,000 files, 10 chunks, **0.31 s**;
3,000 files, 30 chunks, **2.55 s** — 3x the corpus for 8.2x the work, which is O(corpus²/batch) in
file reads. One scan of 3,000 files is 62 ms (11 ms glob+sort, 28 ms read, 14 ms parse, 9 ms model),
so the per-chunk cost is small and the *product* is what grows.

The warehouse adapter's read was already bounded by the binding's own `fetch_limit`, but by the
wrong number: a continuation chunk asked for 500 rows (up to 5,000 at the binding's ceiling) to keep
100, and every fetched key becomes a bind parameter in each child relation's `IN (...)` list, so the
related-table reads were over-read by the same 5x–50x.

## Decision

`ElnAdapter.fetch_new_entries` takes `limit: int | None = None` — **a capability, not a
requirement**, on the same terms as `fetch_was_truncated` beside it. An adapter that cannot bound
its read may ignore it, because the caller truncates anyway. What an adapter may never do is return
a non-prefix subset: the entries it withholds must all be later, in `entry_window` order, than
every entry it returns.

`_BoundedIngest` offers the bound only when `since >= self._since` — exactly a chunk with no
overlap rewind behind it. On a run's *first* chunk the caller's floor sits `eln_sync_overlap_seconds`
before the cursor, so a limit applied at that floor would be spent on the overlap replay and could
return a chunk of nothing but already-ingested entries: a fetch reporting itself truncated while the
cursor cannot advance, which is the wedge the workflow's own guard stops loudly. Every continuation
chunk — which is what a large drain is made of — is bounded.

The warehouse adapter turns it into its `LIMIT`. The tie-crossing pages keep the binding's own
`fetch_limit`, because a continuation page exists to get *past* a block of rows sharing one
watermark and shrinking it would shrink the block the fetch can cross; the caller's bound is on the
ordinary read, not on the recovery path.

**The two file-drop adapters accept it and ignore it, and that is the answer rather than a missing
feature.** Their scan is ordered by *filename* and an entry's window lives inside the payload, so
breaking the scan at `limit` files returns an arbitrary subset of the outstanding entries rather
than the oldest ones — the cursor then advances past every discarded entry with an earlier window
and no later fetch offers those files again. That is the same permanent, silent loss
`_BoundedIngest`'s own docstring measured at 50 of 150 when its cap read the wrong timestamp. The
proposed "the file list is already sorted, so the scan can break" is that defect re-introduced. A
bounded read here needs an index the directory does not have, so the residual is stated with its
measurement instead of deferred.

## What holds it

`tests/test_warehouse_adapter.py::test_a_bounded_chunk_asks_the_warehouse_for_the_chunk_and_not_for_the_page`,
driven against the fake that honours WHERE/ORDER BY/LIMIT (the plain fake answers every statement
with the whole table and cannot tell a bounded page from an unbounded one). Watched failing against
the unfixed adapter: `LIMITs bound: [150]` where the chunk asked for 5. The unbounded arm is
asserted in the same test, so a caller reading a whole corpus still gets the binding's page.

## What is deliberately not done

A separate defect found while measuring and **not fixed here**: on a bulk-copy backfill (files whose
mtime is the copy time and whose payload timestamps are old), `is_late_arrival` re-qualifies every
*already-ingested* file on every later chunk, because the chunk cursor has advanced past their
payload timestamps while their mtime is still after it. Measured on the same 3,000-file corpus, that
turned a 2.55 s drain into 9.99 s and wrote a growing set of false `ingest_rejections` rows —
99, then 199, then 299 — each claiming no scheduled run will fetch an entry that has already been
ingested. The honest fix needs the adapter to know the *run's* floor rather than the chunk's, which
is a second parameter, and overloading `limit` to mean "this is a continuation" is two spellings of
one thing. It is a `BACKLOG.md` row with the measurement on it.
