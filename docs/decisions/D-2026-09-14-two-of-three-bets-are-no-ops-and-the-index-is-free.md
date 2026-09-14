# D-2026-09-14-two-of-three-bets-are-no-ops-and-the-index-is-free — the `stated`-quote ambient's read

**Status**: accepted

## Context

`agent/session_store._SELECT_RECENT_USER_ROWS` runs once per turn on the answer path
(`api/runner._turn_ambient`). It asks for one session's newest human messages:
`session_id = $1 AND message_shape = $2 AND message_original IS NULL AND message->>'type' = 'human'
ORDER BY id DESC LIMIT $3`.

`BACKLOG.md` measured what that costs on a busy database and **deliberately proposed no fix**,
naming three different bets — `CREATE STATISTICS` on the expression, a partial or expression index,
and hoisting the type test out of SQL — and one objection: *"an index added to force a plan is a
cost every write pays forever."* This ADR is the decision, taken by driving all of them.

## What was measured

A replica of the shipped schema with its real indexes: one 12,000-row session plus 120,000 newer
rows across 300 other sessions, `VACUUM ANALYZE`, three warm repetitions, then `EXPLAIN ANALYZE`.

| arm | plan | rows discarded | time |
| --- | --- | ---: | ---: |
| the shipped statement | `Index Scan` on `session_messages_pkey` | **120,020** | 14.8 ms |
| `CREATE STATISTICS` on `(session_id, (message->>'type'))` | `session_messages_pkey` | 120,020 | 16.1 ms |
| type test hoisted into Python, `LIMIT 40` | `session_messages_pkey` | 120,000 | 16.8 ms |
| bounded inner window, filter outside | `session_messages_pkey` | (same plan) | 16.1 ms |
| **a partial index on the human rows** | **that index** | **0** | **0.036 ms** |

**Two of the three bets are measured no-ops.** Extended statistics on the expression do not move the
plan — the planner's choice here is driven by `ORDER BY id DESC LIMIT k` against an ordered index,
not by that predicate's estimated selectivity — and hoisting the type test out of SQL does not
either, because `session_id = $1` alone still loses to the primary-key walk. A rewritten statement
with a bounded inner window plans identically.

**And the objection does not hold.** Measured at 2,000 inserts: **162 µs/row without the index and
161 µs/row with it** — no measurable cost, because the index is *partial*. It covers only the rows
the ambient can quote (a human turn that was not migrated): **3.6 MB against a 33 MB table** in the
same probe.

The existing `session_messages_session_recent_idx` cannot serve this read: it is
`(session_id, created_at DESC)`, and the statement orders by `id`.

## Decision

Migration `infra/sql/098_session_ambient_human_rows.sql` adds
`session_messages_ambient_human_idx (session_id, message_shape, id) WHERE message_original IS NULL
AND message->>'type' = 'human'`. The column order is the statement's — two equality predicates then
the ordering column as a backward scan — and the predicate repeats the statement's two constant
conditions verbatim, which is what lets Postgres prove the index covers the query.

The read goes from O(table) to O(session): **14.8 ms → 0.036 ms**, 120,020 discarded rows → **0**.

## Consequences

- The degradation this removes was invisible (the answer was always correct, only slow) and was
  bounded by `durable/retention.py`, so a deployment that prunes hard may never have reached it.
  The fix costs it nothing either way.
- This is one more index on a hot table, and the write measurement is what makes that acceptable
  rather than an argument.

## What keeps it true

- `tests/test_stated_quote_ambient.py::test_the_ambient_read_is_bounded_by_the_session_and_not_by_the_table`
  — a **plan** assertion, not a wall clock: two independent measurements of this statement agreed on
  the row counts and disagreed on the milliseconds by 50x, so what is asserted is the index the
  planner picks. Driven: removing the `CREATE INDEX` from migration `098` reddens it.
  (The first mutation attempt dropped the index from the `public` schema and the test stayed green —
  the suite runs migrations into its own isolated schema, so the mutation had not reached the
  database under test. A mutation that fails to apply reports a pass.)
