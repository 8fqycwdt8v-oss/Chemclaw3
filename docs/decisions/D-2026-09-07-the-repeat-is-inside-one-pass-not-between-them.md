# D-2026-09-07-the-repeat-is-inside-one-pass-not-between-them — the retention sweep's resume position lives for one pass, and a durable watermark is the wrong bound

**Status:** accepted · **Date:** 2026-09-07 · **Builds on:**
D-2026-09-06-a-superseded-checkpoint-is-a-copy-not-a-record (which bounded the depth and named this
as the piece left open),
D-2026-08-05-a-sweep-that-commits-once,
D-2026-08-01-the-count-lives-in-the-test-not-in-the-prose ·
**Corrects** two comments in `durable/retention.py` that named a durable watermark as the missing
piece, and `_ANALYZE_THREADS`' claim to run once a pass.

## Context

`docs/planning/BACKLOG.md` carried this as *"the retention sweep has no durable resume watermark, so
a sparse pass still visits every thread"*, and closed on the design question: **"a watermark is a row
this job has nowhere to keep."** The candidates were a small table, a row in `sync_cursors`, and a
Temporal search attribute.

Measuring first is what made the answer different from all three.

## What was measured

Every figure below is on a real `checkpoints` table in an isolation schema, thread ids uuid-shaped
so expiry is uncorrelated with `thread_id` order, `ANALYZE` as the sweep runs it, cap 500 — the
shipped `retention_max_sessions_per_pass`.

**The sparse pass the row is named after is irreducible, and it is not the cost.** 20,000 threads x
3 checkpoints, 2% expired: the shipped statement reads all 60,000 rows (20,975 buffers,
19,600 groups discarded by the `HAVING`) in **66.5 ms** to retire 400 threads. No position in
`thread_id` order changes that, because expiry lives in a JSON field of a table no migration may
index — `retention.py`'s own docstring measured that: the only buildable form stores the *text*,
which `max((checkpoint->>'ts')::timestamptz)` never reads. Finding a scattered minority means
visiting everyone.

**The repeat is inside one drain.** `_prune_expired_rows` sweeps until the backlog is empty, and
every sweep started at the beginning of `thread_id` order — so sweep *k* walked past the *k-1*
batches it had already disposed of. 200,000 threads x 3, 2,000 expired, four sweeps:

| | scan per sweep | scan total | end to end |
|---|---|---|---|
| restart at the top | 280 / 438 / 666 / 881 ms | 2,265 ms | 3,747 ms |
| resume where it stopped | 294 / 235 / 231 / 237 ms | 996 ms | 1,516 ms |

The growth is the whole point: the shipped arm's per-sweep cost rises linearly, the resumed arm's is
flat. The ratio is `(sweeps + 1) / 2`, so it is set by the **backlog**, not by the table. On a first
pass after enabling retention — 50,000 threads, 20,000 expired, 40 sweeps — it is **2,698 ms against
357 ms** of scanning.

**And the end-to-end figure exposed a second defect the row did not name.** At 40 sweeps the drain
cost 13,821 ms against 11,655 ms, because `ANALYZE checkpoints` runs *per sweep* and spent **10.8 s
of 13.8 s**. `_ANALYZE_THREADS`' comment said "every pass, immediately before asking the question",
and that sentence was true when a pass was one sweep; `_prune_expired_rows` made a pass a loop and
nothing re-read it. With the analyze moved to the sweep that starts at the top of the table, the same
drain is **13,499 -> 1,492 ms**.

**The control.** A sparse single-sweep pass — the steady state, where no resume position is ever
set — measures 333 ms before and 338 ms after. Unchanged, which is what it must be.

## Decision

**The resume position is a value threaded through one pass, and is deliberately not durable.**
`_EXPIRED_THREADS` gains `WHERE thread_id > %s`; `_prune_checkpoints` takes where the previous sweep
stopped and returns where this one did; `_prune_expired_rows` carries it across the sweeps of one
pass and starts every pass at `""`.

Three things follow, and they are why this is not the row's small table:

1. **Coverage is whole by construction.** Every pass begins at the beginning of `thread_id` order,
   so there is no starvation case to reason about — no parked cursor, no wrap, no "a thread that
   expires below the position waits for the cycle".
2. **A durable position buys one table scan per pass**, and only for a backlog so large that a pass
   cannot drain it inside `retention_timeout_seconds`: the deletions of pass *N* are still deleted
   in pass *N+1*, so a pass restarting at the top walks a prefix that has nothing left in it. That
   is a scan, not a Sigma-k.
3. **And it would cost more than it buys.** A durable position that came back under its cap has
   drained only the region *above* itself, so it needs either a wrap scan — measured worse than the
   shipped statement at two sweeps, 2T against 1.5T, and two sweeps a day is what this module's own
   sizing paragraph describes — or a window in which a thread below it is not disposed of. Neither
   is worth a table, a migration, and an `INSERT/UPDATE` grant in a file whose whole discipline is
   naming what a role may write.

**`ANALYZE checkpoints` runs on the sweep that starts at the top of the table.** What it gives up is
the refresh of the statistics this pass's own deletions invalidate, and that error only ever leaves
the planner believing the table is *larger* than it is — the direction that keeps the conservative
plan — until the next pass analyzes it.

**The row is deleted and this ADR is the record.** Its headline — "a sparse pass still visits every
thread" — remains true and is now argued rather than queued: it is a property of a table that cannot
be indexed, not a missing watermark.

## Consequences

- `durable/retention.py`: `_EXPIRED_THREADS` takes a resume position; `_prune_checkpoints` returns
  one; `_sweep_once` and `_prune_expired_rows` thread it. No new table, no migration, no grant.
- `tests/test_retention.py`: `test_the_next_sweep_of_a_drain_starts_where_the_last_one_stopped`
  plants a checkpoint with an unparseable `ts` *below* the resume position between two sweeps — the
  cast that dates a checkpoint raises on it, so a scan that re-walks the prefix fails and one that
  resumes never reads it. Deterministic, where a wall-clock ratio on a shared runner would pass in
  the direction nobody wants. Watched failing with the position forced to `""`.
  `test_every_pass_starts_at_the_beginning_of_the_table` is the coverage half, and
  `test_a_drain_analyzes_the_table_once_and_not_once_per_sweep` counts through
  `pg_stat_user_tables`. Both watched failing against the unfixed behaviour.
- **A plan test's fixture had to grow thirtyfold, and that is a finding rather than a chore.** The
  resume predicate makes the index path look cheaper to a planner with *no* statistics, so
  `test_the_sweep_gives_the_planner_the_statistics_no_migration_can` stopped reproducing the
  pathological plan at 2,000 threads — measured, 2,000 / 8,000 / 16,000 / 32,000 / 40,000 x 5 all
  plan as a streaming index walk unanalyzed, and 60,000 x 5 (300,000 rows) is where the parallel
  hash aggregate comes back: 328 ms unanalyzed against 0 ms analyzed. Asking at a *larger cap*
  reproduces it at the small fixture and is wrong for a different reason — 500 of 2,000 groups is a
  quarter of the table, so the *analyzed* plan is a seq scan too, and the test would have asserted
  that `ANALYZE` does nothing. The hazard is unchanged where a deployment meets it (at 200,000
  threads / 600,000 rows both statements plan identically badly with no statistics); what moved is
  the size at which a fixture can show it.
