# D-2026-09-14-a-counter-of-commits-is-not-a-counter-of-notes — what `WriteOutcome` has to carry

**Status**: accepted.

## Context

`chemclaw_notes_recorded_total` is declared as *"Notes written into the knowledge graph"*, and
`record_note` increments it once per call, guarded on `outcome.written`. The guard is deliberate and
still right: counting an attempt would show a busy, working system during exactly the outage the
metric exists to reveal, and `tests/test_metrics_bridge.py` holds both of those directions.

`D-2026-09-13-the-lock-is-not-the-bound-the-commit-is` then added `BatchingNoteWriter` for the
backfill path, which merges N notes into one commit. Against a `written: bool`, every note but the
one that fills a batch returns `written=False`.

## The finding

**Driven on real git against a real bare remote: 50 notes, 1 commit, and the counter moves by
1.0** — while 50 notes are on disk. At the shipped `backfill_commit_batch_size` the counter is a
count of *commits*, so an operator watching a backfill reads a number fifty times too small, and
the 4,251-document backfill that ADR measured would have moved it by about 86.

No test covered the counter under batching. Each half was covered alone: the batching tests assert
commits and files, the metric tests assert the counter on the unbatched path.

## Decision

**`WriteOutcome` carries a count, not a flag.** `notes: int` is how many notes this write put in
the graph — 1 on the ordinary path, 0 for the idempotent no-op *and* for a batch that has not
flushed, `len(batch)` on a flush that committed. `written` stays, as a **derived property**
(`notes > 0`), so its twenty-odd readers are untouched and the two facts cannot disagree: storing
both would be the same fact twice, which is how they would drift.

`extra="forbid"` goes on the model for one specific reason rather than for tidiness: `written=`
used to be a constructor argument, and a silently-ignored keyword would leave a no-op reporting one
note. Every construction site is now explicit about the count and the four in `git_writer.py` say
which case they are.

`count_notes_recorded(outcome)` is the one function that books it, with **two callers on
purpose**: `record_note` for the ordinary path, and `cli/backfill_corpus` for the final `flush()` —
a batch's last commit lands on a call `record_note` never sees, so without it the tail of every run
would be missing.

**The batch reports 0 when the inner write was a no-op.** Re-running a backfill over documents
already in the corpus is the frequent legitimate case; a `len(batch)` that ignored it would turn
"notes recorded" into "notes offered", which is the attempt-counting the metric was declared to
avoid.

Measured after: the same 50 notes in the same 1 commit move the counter by **50.0**.

## Consequences

- A `NoteWriter` implemented outside this tree now has to say how many notes its commit carried.
  That is the point: the defect was a shape that could not express the answer.
- The counter is comparable across the two write paths for the first time — which is what makes
  "how much has this backfill written" answerable from a scrape.

## What keeps it true

- `tests/test_backfill_batching.py::test_the_counter_counts_notes_and_not_commits` — seven notes in
  three commits, asserting both numbers, on real git. Either alone is satisfiable by a defect:
  counting commits gives 3, and dropping the batching gives 7 == 7 with the saving gone.
- `::test_a_batch_that_changed_nothing_counts_nothing` — the no-op direction, which a plain
  `len(batch)` gets wrong.
- `tests/test_metrics_bridge.py` — the three unbatched cases, unchanged: a recorded note moves it
  by one, a failed write does not move it, a no-op does not move it.
