# D-2026-08-29-a-bound-derived-twice-is-two-bounds — the refusal ledger is written where the chunk is known

**Status:** accepted · **Date:** 2026-08-29

## The defect

`D-2026-08-27-a-refused-record-is-a-question-somebody-will-ask` put a rejection ledger behind ELN
ingest so a chemist asking about a record that never arrived gets the reason instead of "I have no
such record". `OrdJsonAdapter.fetch_new_entries` recorded three kinds of refusal: a file it could
not read, a file that arrived too late to ever be fetched, and a message it could not map.

The third one was measured to be expensive — the fetch returns *everything* past the cursor and
`durable/eln_sync.py::_BoundedIngest` truncates it afterwards, so a 100k-entry backfill re-mapped
all 100k entries once per 100-entry chunk. The fix was to bound the pre-flight:
`self._unmappable(entries[: settings.eln_sync_batch_size])`.

**That bound is a second derivation of somebody else's bound, from less information, and the two
disagree.** `_BoundedIngest.fetch_new_entries` sorts by `(created_at, entry_id)` and returns *every*
overlap entry (`created_at <= since`) plus a batch-size slice of the new ones. The adapter sorted by
`created_at` alone and took a flat prefix of the whole list. Overlap entries always sort first, so
the adapter's budget was spent on them:

| | adapter pre-flight | what `_BoundedIngest` processes |
| --- | --- | --- |
| 5 overlap + 200 new, batch 100 | 5 overlap + **95** new | 5 overlap + **100** new |

The last five new entries of every such chunk were mapped, refused, and left with **no ledger row**.
A second, independent divergence rode along: two different sort keys pick different prefixes when a
batch boundary lands on a `created_at` tie.

**And it does not heal.** `ElnSyncWorkflow` stores `summary.next_cursor` after every chunk and the
cursor advances past a rejection — that is deliberate, a rejection is deterministic bad data. So a
missed entry falls behind `since` and no later fetch ever offers it again. The record is absent from
the corpus *and* the system has no record of having seen it, which is precisely the state the ledger
exists to prevent, reached by the mechanism built to fill it.

Reproduced before the fix, through the real activity over a real drop directory
(`tests/test_ingest_rejections.py::test_every_processed_refusal_reaches_the_ledger`): 2 overlap
entries and 6 new against a batch size of 4 — the chunk refused six records and the ledger held
four, missing `new-2` and `new-3`.

## Why the adapter cannot be fixed in place

It is handed the fetch **floor** — `since` minus `eln_sync_overlap_seconds` — and nothing else. It
does not know the run's cursor, so it cannot separate the overlap window from the new entries; it
does not know the chunk limit, which is its caller's configuration and not part of the `ElnAdapter`
contract. *Any* slice it takes is a guess at its caller's, and no shared helper closes that: a
function both sides call still needs `since` and `limit` on the side that has neither.

The general rule: **when two places derive "which items does this step process", one of them is
wrong the moment the composition changes.** The fix is not to make the second derivation more
careful. It is to delete it.

## The decision

The unmappable refusals are recorded by `durable/eln_sync.py::sync_eln_entries`, from
`summary.rejected`, after `sync_entries` returns. The adapter keeps only the refusals nothing
downstream can see: a file it could not read and a file that arrived too late, neither of which
becomes a `RawEntry` at all.

Three properties follow, and each is why this shape rather than a shared bound:

- **The two sets are one by construction.** `summary.rejected` *is* what this chunk processed and
  refused; there is no second list to keep in step, and no sort key to agree on.
- **The double mapping is gone entirely**, rather than bounded. `_unmappable` re-mapped every entry
  the sync was about to map two lines later, in the same process. The measured cost that motivated
  the bound (68 µs an entry, 0.136 s over 2,000) is now zero, not merely capped.
- **The ledger reason is the sync's own reason.** The recorded set is also wider in the direction the
  ledger wants: an entry whose record or fingerprint failed, and one stamped implausibly far in the
  future, are records this system was offered and would not take, exactly like a message it could
  not map. They were invisible to a pre-flight that only called `map_to_ord`.

**Not folded into `ingest/eln/sync.py`.** That loop is backend-agnostic core with every dependency
injected, which is what makes it testable in memory; a database write it did not take as a parameter
would end that property. The durable layer already owns the run's I/O, and `record_refusals` never
raises, so a ledger outage still cannot cost the corpus an entry.

## What this gives up, deliberately

Four callers fetch with no bound at all — `durable/memory_jobs.py::read_corpus`,
`cli/live_data.py` (two sites) and `ingest/eln/validate.py`. They no longer trigger an unmappable
pre-flight, where before the bound they mapped the whole corpus and after it mapped the oldest 100
entries of the entire corpus forever. That is the right loss: the ledger's claim is that a record is
**absent from the corpus**, and only the drain that ingests decides that. A `make eln-validate` run
writing rows asserting absence was never a thing anybody asked for, and a miner reading the corpus is
not offering records to be refused.

Nothing is lost for the ORD corpus itself: the drain reaches every entry eventually — the cursor
advances chunk by chunk — and each chunk files its own refusals when it gets there.

**It also generalises, and that half was already written down.** `docs/planning/BACKLOG.md` carried a
row — "carry the rejection ledger to the site that covers every source" — naming
`durable/eln_sync.py` as the one site that would cover every adapter at once, "which already holds
`IngestSummary.rejected` (id, reason, timestamp) in an async activity needing no pre-flight: one
call, and the per-adapter writers become redundant rather than multiplied". That is this change,
reached from the opposite direction: the defect made the move necessary rather than merely tidier.
The row is deleted in the same commit.

So `eln-json`, the warehouse adapter, `sync.py`'s future-timestamp refusal and `ingest.py`'s
`IngestError` — none of which had a ledger row before, all of which refuse records a chemist can ask
about — are covered now. What stays in the ORD adapter is only what `IngestSummary` cannot see: an
unreadable file and a late arrival never become a `RawEntry` at all.

## Consequences

- `OrdJsonAdapter._unmappable` is deleted; `fetch_new_entries` maps nothing at all, asserted by
  `tests/test_ingest_rejections.py::test_the_fetch_maps_nothing_at_all`.
- Five ledger tests that drove a hand-built adapter now drive the real activity over a manifest, via
  `_ord_source`/`_drain`. That is a cost of this decision and worth stating: a ledger row is a
  property of the *drain* now, so a test about one has to run the drain.
- `settings.eln_sync_batch_size` has one reader again, in the layer that owns the bound.
