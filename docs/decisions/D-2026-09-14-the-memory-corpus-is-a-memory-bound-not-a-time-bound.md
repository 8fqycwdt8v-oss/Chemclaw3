# D-2026-09-14-the-memory-corpus-is-a-memory-bound-not-a-time-bound — what a corpus read really costs

**Status**: accepted

## Context

`BACKLOG.md` said a memory run reads every source whole, **three times** — once per miner activity
per scheduled run — and framed the cost as redundant work.

## Two things in that sentence are stale, and the third is the wrong cost

**There is no scheduled run.** `D-2026-08-25` removed the timer; `durable/schedules.py` says so in
as many words, and `synthesize_memory(kind)` starts **one** workflow per call. Three reads happen
only when a person asks for all three kinds, which is three deliberate asks, not a loop.

**And the cost is memory, not time.** Measured against a 10,000-record ORD drop directory:

| cap | reactions held | wall clock | traced peak |
| ---: | ---: | ---: | ---: |
| 1 | 1 | 3.1 s | **144.2 MB** |
| 1,000 | 1,000 | 3.6 s | 165.9 MB |
| 5,000 | 5,000 | 5.1 s | 268.5 MB |
| unbounded | 10,000 | 6.9 s | **396.8 MB** |

Two slopes fall out of that table. A mapped `OrdReaction` costs **25.3 kB** resident; the adapter's
own page of `RawEntry` costs **14.4 kB** and is the 144 MB floor at cap 1. So the corpus this
repository ships is 2 reactions and a decade of a real ELN is ~500,000: **~20 GB in one activity's
process**, of which ~7 GB is the adapter's page. The read does not get slow, the pod is killed —
and three reads of that are not the problem.

## Decision

`memory_corpus_max_reactions` (default 100,000) bounds what one read **holds**. Hitting it makes
the read *incomplete* rather than raising, which is a mechanism `CorpusRead` already has and every
miner already honours: a pass that saw part of the corpus must not be written down as the whole
record. A deployment over the bound gets partial knowledge that says it is partial, instead of a
worker that dies with no note at all.

**Where the check goes is the whole of whether it bounds anything**, and the first version got it
wrong. Checked between pages, it let a **one-page source through entirely**: driven on the 10,000
record corpus at a cap of 2,500, `read_corpus` returned all 10,000 and marked the read incomplete
about a corpus it had already materialised. `OrdJsonAdapter.fetch_new_entries` accepts `limit` and
**ignores it**, deliberately — its docstring says an unsorted scan would return an arbitrary subset
and advance the cursor past what it skipped — so for a drop directory the page *is* the corpus. The
check is per entry now: 10,000 → 2,500 reactions, 396.8 MB → **204.4 MB**, 6.9 s → 4.2 s.

## What this does not bound, stated because a partial control that reads as a whole one is the
## defect this wave keeps finding

The cap bounds the miner's half and **cannot bound the adapter's page**: 144 MB of the 397 was
already spent before the first entry was mapped, and no argument `read_corpus` can pass changes
that for a source that does not page. At 500,000 entries that floor is ~7.2 GB.

So `memory_corpus_max_reactions` is necessary and not sufficient. The sufficient fix is the one the
old row already identified without a number — a streaming or fetch-by-id adapter protocol, which
every source pays for — and it now has one. `BACKLOG.md` carries it with these figures as its
trigger.

## What keeps it true

- `tests/test_memory_jobs.py::test_the_corpus_read_stops_at_its_bound_and_says_the_pass_was_partial`
  — a one-page source of 50 entries at a cap of 10 returns 10 and reports `complete=False`, and at
  cap 0 returns all 50 as complete. Driven: moving the check back outside the mapping loop reddens
  it (which is the defect the first version had), and dropping `capped` from `complete` reddens it.
