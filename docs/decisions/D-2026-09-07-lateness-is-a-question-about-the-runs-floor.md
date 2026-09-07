# D-2026-09-07-lateness-is-a-question-about-the-runs-floor — a continuation chunk cannot say a file will never be fetched

**Status:** accepted · **Date:** 2026-09-07 · **Builds on:**
`D-2026-09-06-a-bound-applied-after-the-read-is-not-a-bound-on-it` (which measured this and left it
as a `BACKLOG.md` row), `D-2026-08-27-a-refused-record-is-a-question-somebody-will-ask` (the
rejection ledger is what a chemist is shown), D-120 (a new source costs zero core edits).

## Context

`ingest/eln/adapter.is_late_arrival` answers *will any scheduled run ever fetch this file* — payload
behind the fetch floor, mtime after it, so the cursor has passed it and no later run will offer it.
It was handed the **chunk's** floor, and a drain advances that floor per chunk.

## What was measured

A bulk-copy backfill is exactly the shape that breaks it: mtime is the copy time, the payload
timestamps are old. On a 3,000-file JSON drop at the shipped batch of 100, driven chunk by chunk:
**43,471** rejection writes across 30 chunks, growing 99, 199, 299 … per chunk, every one of them
about an entry the drain had already ingested. Wave 7 measured the same drain at 2.55 s → **9.99 s**
with the ledger attached. Each false row tells a chemist asking why a record is missing that no
scheduled run will fetch it — about a record that is in the corpus.

## Decision

The scan belongs to the chunk whose floor is the **run's**, which is the chunk that reaches behind
the cursor by `eln_sync_overlap_seconds` — the same chunk the overlap replay belongs to, for the
same reason. A continuation chunk's floor cannot answer the question: every file between the two
floors was fetched by this very run.

Suppressing it there loses nothing, which is why suppression rather than a re-judged floor is
enough. A file with payload behind the run floor is examined on chunk 1 against `mtime >= run_floor`;
on chunk 3 the test is `mtime >= chunk_floor`, which is *stricter*. So the true detections of every
continuation chunk are a subset of chunk 1's, and what continuation chunks add is exactly the false
set.

`fetch_new_entries` therefore takes `report_late_arrivals: bool = True` — **a capability, asked for,
not a protocol requirement**, on the terms `accepts_a_limit` and `fetch_was_truncated` already set:
an out-of-tree adapter written to the published signature must not be handed an argument it never
declared. Only `False` is ever passed, because `True` is what every adapter already does. The probe
is `accepts_a_late_arrival_switch`, and the `inspect.signature` body both probes shared is now one
private function rather than two copies.

`_BoundedIngest` is **told** which chunk it is (`first_chunk=apply_overlap`) rather than inferring
it from `since >= self._since`. That derivation looks free and is wrong at a supported setting:
`eln_sync_overlap_seconds` is `ge=0`, and at zero a run's first chunk has no rewind behind it, so
the inference would silence late-arrival reporting entirely, for every chunk, in a deployment that
had merely turned the overlap window off.

Overloading `limit` to mean "this is a continuation" stays refused, as the row that carried this
said: two spellings of one thing, and the second spelling is the one nobody reads.

## What holds it

`tests/test_ingest_rejections.py::test_a_bulk_backfill_does_not_re_refuse_the_files_it_has_already_ingested`,
driven through the real activity over two chunks with a real ledger, watched failing against the
unfixed source with **3 ledger rows naming entries the first chunk had ingested**. Two chunks is
the minimum that can show it and the maximum that stays a unit test; the growth is arithmetic from
there. Re-measured after the fix on the same 3,000-file corpus: **0** rejection rows.

## What is deliberately not done

The directory scan itself. Every chunk still reads every file, because a drop directory has no
index to bound a read with — the residual the ADR above states, unchanged by this.
