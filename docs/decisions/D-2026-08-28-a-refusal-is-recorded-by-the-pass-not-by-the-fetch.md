# D-2026-08-28-a-refusal-is-recorded-by-the-pass-not-by-the-fetch — the rejection ledger is written by the sync, which maps once and knows its own source

## Status

Accepted. Supersedes one consequence of
`D-2026-08-27-a-refused-record-is-a-question-somebody-will-ask` — *where* a refusal is recorded.
The ledger itself, its shape, its per-source cap and its reader are untouched and still that ADR's.

## Context

`docs/planning/BACKLOG.md` carried two rows about the ORD adapter. They read as unrelated — one a
cost, one a naming defect — and they are the same defect: **the refusal ledger was written by the
fetch, and a fetch is the one layer that can answer neither question a ledger row needs.**

### What it cost, measured on this HEAD

`OrdJsonAdapter.fetch_new_entries` ended in `_unmappable`, which mapped *every* entry the fetch was
about to return, so the refusals could be found from an `async` caller (`map_to_ord` is synchronous
by the `ElnAdapter` contract, and the ledger write is awaited). The adapter's own docstring priced
this at "~6.5 ms on a full 100-entry chunk". The per-entry figure was right and the unit was not,
because the bound it named is not applied where it assumed: `eln_sync_batch_size` is applied by
`durable/eln_sync.py::_BoundedIngest` **after** the adapter returns.

Measured over 5,000 synthetic ORD exports built from the shipped
`data/eln-exports/ord/ord-2026-001.json`, three runs:

| run | whole fetch | `_unmappable` | share | per entry |
| --- | --- | --- | --- | --- |
| 0 | 0.969 s | 0.317 s | 32.7% | 63 µs |
| 1 | 1.712 s | 0.304 s | 17.8% | 61 µs |
| 2 | 0.851 s | 0.320 s | 37.6% | 64 µs |

A 100k-entry backfill drains in ~1,000 activity attempts at the shipped batch size, and each one
re-mapped all 100k: ~6.2 s per chunk, **~1.7 hours** of pure re-mapping added to the drain, for
work the sync then does again one map at a time. `record_refusals` was likewise handed the whole
directory's refusals on every chunk.

### What it got wrong, structurally

`ingest_rejections.source` is documented as the *registry* source name; the key is `(source,
entry_id)` and the eviction cap is per-source. An ingest half is built from `manifest.config` alone
and is never told which source it is — `registry._build_retrieve_half` passes `name=` and
`_build_ingest_half` does not — so the adapter filed under a module constant,
`LEDGER_SOURCE = "eln-ord"`. Two ORD drop directories, which the seam exists to make possible with
no core edit, would have shared one 1,000-row bucket and mis-attributed each other's refusals. The
test guarding it read *this repository's* manifests, so a site adding the second source failed the
test here rather than the code taking the name as an argument.

## Decision

**The rejection ledger is written by `sync_entries`.** It is the only layer that holds all three
things a row needs, and it already holds them:

1. the entry mapped **exactly once** — the loop maps to ingest, and its one `except` is where the
   refusal is already worded;
2. only the entries **this bounded chunk saw**, because the bound is applied above it;
3. `source`, the registry source name, which is already a required keyword argument.

**An adapter reports what only it can see, and records nothing.** The two refusals that never
become a `RawEntry` — a file that would not parse, and one that arrived after the cursor carrying an
older timestamp — are held by the fetch and read through an optional protocol,
`adapter.RefusingFetch` / `fetch_refusals()`, in the exact shape `BoundedFetch` /
`fetch_was_truncated` already had for the sibling question. The walk through the seam's wrappers is
now one function, `_through_wrappers`, because both capabilities need it and the subtle half — the
`inner` chain and the cycle guard — is identical.

`LEDGER_SOURCE` and `_unmappable` are deleted.

## Consequences

- The pre-flight is gone: measured, `map_to_ord` is now called **0** times inside a 5,000-entry
  fetch, and every refusal is still recorded.
- **The ledger covers more than it did, not less.** It is now fed by every ingest source rather than
  by the one adapter that recorded its own, and by every refusal the sync makes rather than only the
  ones a fetch can see — a future-stamped entry, a record that would not build, a reaction that
  would not index were all refused records with no ledger row at all, which is the ledger's stated
  purpose missing on three of its cases.
- A refusal is filed the first time its own chunk runs, rather than on every chunk of a backfill.
- **It closes a third BACKLOG row that had already named this site**, "Carry the rejection ledger to
  the site that covers every source": `json_adapter::map_to_ord` — the adapter the live 119.43%
  record actually arrives through — plus `sync.py`'s future-timestamp refusal and `ingest.py`'s
  `IngestError` all refused into a log line only, and the row's own conclusion was that the one
  place covering every adapter is where `IngestSummary.rejected` is built. That is the same
  conclusion the cost row forces, reached from the other end; the write lands in
  `ingest/eln/sync.py` rather than in `durable/eln_sync.py` because the durable layer must not
  modify the backend-agnostic loop (G6), and `sync_entries` is where both the reason and the
  source name already are.
- `record_refusals` upserts with `executemany` (psycopg 3 pipelines the batch) instead of a Python
  loop. Measured over 100 upserts on one warm connection, median of ten trials, three runs:
  91.7/83.8/69.1 ms as a loop against 36.6/54.0/37.8 ms pipelined — 1.6x to 2.5x. A real gain and
  not a large one; the spread is the sandbox, and it is worth taking because it is the same amount
  of code.
- `tests/test_ingest_rejections.py` drives the real sync pass now rather than the adapter's fetch,
  which is what makes the tests evidence about the production producer. The manifest-reading test is
  replaced by one that syncs **two** ORD directories under two source names, neither declared in
  this repository — the case the old test could not express — and by one that bounds a chunk to two
  entries over a directory of six and asserts two ledger rows.

## Alternatives considered

**Pass `name=manifest.name` to the ingest half, as `_build_retrieve_half` does.** It closes the
naming half and not the cost half, and it forces two adapters that record nothing to accept a
parameter they never read — a dead argument on the seam every new source implements. The retrieve
half's unconditional pass is right *there* because every retrieve half needs its name for the
document index; no ingest half needs one once the pass writes the ledger.

**Bound the pre-flight to `eln_sync_batch_size` inside the adapter.** The adapter cannot know that
number is the bound: `_BoundedIngest` also lets the whole overlap window through uncapped, and an
adapter reading a durable-layer setting to guess what its caller will keep is a coupling that is
wrong the moment a second caller exists — `durable/memory_jobs.py::read_corpus` is that second
caller today, and it applies no bound at all.
