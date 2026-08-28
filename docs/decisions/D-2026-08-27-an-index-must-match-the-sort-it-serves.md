# D-2026-08-27-an-index-must-match-the-sort-it-serves — an index must match the sort it serves

**Status:** accepted · **Date:** 2026-08-27

## Context

`infra/sql/025_observations.sql` creates one index and says what it is for:

```sql
-- The two reads: the retrieval bucket wants open observations newest-first, and the promotion
-- sweep wants the open ones with enough support. Both filter on status, so it leads.
CREATE INDEX IF NOT EXISTS observations_status_idx ON observations (status, last_seen DESC);
```

The retrieval bucket does not want open observations newest-first, and never has.
`memory/observations.py::_SELECT_OPEN` — the only statement `open_observations` runs, and
`recall_observations` is its only caller — reads:

```sql
SELECT … FROM observations
 WHERE status = 'open' ORDER BY cardinality(evidence_note_ids) DESC, last_seen DESC LIMIT %s
```

So the index covered the `status` equality and nothing else. The sort key it *does* declare,
`last_seen DESC`, is the second key of a two-key sort whose first key is an expression the index
never mentioned, which makes it unusable for ordering: Postgres fetched **every** open row and
top-N sorted it in memory on each call. That call is inside a conversation turn.

Two texts disagreed about what this bucket orders by, and only one of them could be right.

## Which side is authoritative

**The code.** Support-before-recency is not an accident that drifted from the migration; it is a
decision stated in three places and depended on by a fourth:

- `open_observations`'s docstring — "an observation backed by six merged notes is worth reading
  ahead of last night's single-note one, and the tool's page is small enough that the ordering
  decides what is seen at all". `observation_max_results` is **10**, so this is not a tie-break, it
  is what reaches the model.
- `Observation.with_id`'s account of an anchor move: the superseded subset row is "a weaker
  restatement ranked below the current finding" — redundancy that is tolerable only because support
  leads. Under newest-first the subset and the superset are both refreshed to roughly now, and the
  weaker one can rank first.
- `D-2026-08-01-a-cap-that-starves-a-source` cites the same ordering for the same reason.
- `durable.observation_jobs`'s supersession relies on the strictly-greater-support row outranking
  the row it supersedes.

Newest-first would rank last night's single-note finding above a six-note one on a ten-row page.
Nothing argues for it except one comment. So the index moves to the query.

## Measured, before deciding

Postgres 16.15, this migration set, synthetic `observations` rows at ~92% `open` with
`cardinality(evidence_note_ids)` spread 1–9 and `last_seen` spread over 30 days.
`EXPLAIN (ANALYZE, BUFFERS)` on the shipped `_SELECT_OPEN` with `LIMIT 10`, best of three runs,
after `ANALYZE`.

**Before — 924 324 open rows (1 M total), `observations_status_idx` only:**

```
 Limit (actual time=230.347..234.333 rows=10 loops=1)
   ->  Gather Merge (actual time=230.345..234.328 rows=10 loops=1)
         Workers Planned: 2
         Workers Launched: 2
         ->  Sort (actual time=227.012..227.014 rows=8 loops=3)
               Sort Key: (cardinality(evidence_note_ids)) DESC, last_seen DESC
               Sort Method: top-N heapsort  Memory: 33kB
               ->  Parallel Seq Scan on observations (actual time=0.034..129.982 rows=308108 loops=3)
                     Filter: (status = 'open'::text)
 Execution Time: 234.382 ms
```

The declared index is not in the plan at all: not for the filter (a scan of 92% of the table is
cheaper), and not for the order.

**After — same rows, with `observations_open_rank_idx`:**

```
 Limit (actual time=0.030..0.054 rows=10 loops=1)
   Buffers: shared hit=13
   ->  Index Scan using observations_open_rank_idx on observations (actual time=0.029..0.052 rows=10 loops=1)
         Buffers: shared hit=13
 Execution Time: 0.079 ms
```

No sort node, no parallel workers, 12 082 buffers → 13.

**Across scale**, so the answer is not one convenient corpus size:

| open rows | without | with | plan without |
| --- | --- | --- | --- |
| 185 | 0.125 ms | 0.038 ms | seq scan + top-N heapsort |
| 924 | 0.598 ms | 0.099 ms | seq scan + top-N heapsort |
| 9 243 | 6.12 ms | 0.063 ms | seq scan + top-N heapsort |
| 92 433 | 29.9 ms | 0.118 ms | parallel seq scan, 2 workers, 12 082 buffers |
| 924 324 | 234 ms | 0.076 ms | parallel seq scan, 2 workers |

**Is it ever a dead index?** No — that was the question worth asking, because an index the planner
never chooses is not a neutral cost, it is a claim that something is optimised. Sweeping the row
count down: at **20 rows** Postgres correctly prefers the sequential scan and the sort; from
**50 rows** upward it chooses `observations_open_rank_idx`, at every size tested to a million. A
tier whose promotion threshold is three merged notes across two projects, mined nightly from an ELN
corpus this system has already run to ~700 k entries, does not live at twenty rows.

**And the write cost, stated rather than waved at.** Upserting 5 000 findings into a 100 000-row
table — one mining pass — goes **155 ms → 185 ms**, about +6 µs per row, +20%. The index is ~39 MB
at a million rows. That write is a daily Temporal activity; the read it buys is in the turn.

## Decision

**Both halves of the backlog row, because they are one repair.**

1. **Migration `062_observations_open_index.sql` adds
   `observations_open_rank_idx ON observations (status, cardinality(evidence_note_ids) DESC,
   last_seen DESC)`** — the sort `_SELECT_OPEN` performs, exactly, with the filter column leading.
2. **The stated rationale is corrected**, in `062`'s own prose and in a comment above `_SELECT_OPEN`
   naming the index that covers it. `025` is *not* edited: an applied migration is checksummed by
   the ledger and an edit reads as drift (`D-2026-08-04-the-schema-only-goes-forward`). The
   correction lives in the next file, which is where the forward-only rule puts every correction.

**`observations_status_idx` stays, and not merely because nothing may be dropped.** It is the right
index for the other read that names it: `_RETIRE_STALE` (`status = 'open' AND last_seen < now() -
N days`) is a genuine `(status, last_seen)` range, and it still plans onto it — measured, index
scan, 120 rows, 0.65 ms over a million. Two reads, two indexes; what was wrong was one index
claiming both.

## What holds it

`tests/test_observations.py` gains two checks, deliberately split by what each can see:

- **`test_the_open_index_declares_the_sort_the_open_read_performs`** parses the `ORDER BY` out of
  `_SELECT_OPEN` itself — never a restatement, since a restatement is the second copy of the answer
  that let this drift in the first place — and asserts `062`'s `CREATE INDEX` carries that exact key
  list after `status`. Pure, so the offline sandbox catches a drift too.
- **`test_the_open_read_is_served_by_the_index_rather_than_by_a_sort`** runs the shipped statement
  through `EXPLAIN` against a populated table and asserts the plan *names the index* and contains
  **no** `Sort` node. Asking for the plan rather than for rows is the point: the previous state
  returned correct rows too.

Both were shown to fail before being trusted. Rewriting the `ORDER BY` to the `last_seen DESC` that
`025`'s comment claims fails the first on the text and the second on the index name; rewriting it to
`cardinality(evidence_note_ids) DESC, first_seen DESC` — a sort no index covers — leaves the name
assertion silent and fails the plan assertion on an `Incremental Sort`, which is what makes the two
assertions worth having separately.

## Consequences

**A general rule this repository can carry.** An index's comment names a *query*, and a query moves.
Where a migration says which read an index is for, something has to hold the two together, and the
only thing that can is a check that reads both texts. This is the second time a declaration in this
tree was believed for months while the thing it described had changed — the same shape as an
attribution nothing writes and a policy nobody reads.

**What this does not fix.** `_SELECT_PROMOTABLE` (`status = 'open'` plus two `cardinality(…) >=`
predicates, ordered by `id`) still plans as a scan on a synthetic corpus where half the rows match;
on a real one the sweep's matching set is small and short-lived, because a row over both thresholds
is promoted out of the open set on the next pass. It is a different read with a different shape and
is out of this row's scope — noted here so the next reader does not have to re-measure to find that
out.
