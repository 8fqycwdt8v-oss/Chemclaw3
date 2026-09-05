# D-2026-09-05-the-generic-plan-that-bites-is-the-one-behind-an-or — the setting stays, all three of its reasons were wrong

## Status

Accepted. No behaviour changes; the justification does.

## Context

`core/db.py` adds `-c plan_cache_mode=force_custom_plan` to every connection this module's pools
open. The comment that argues for it made three claims, in the present tense, and a review found the
first did not reproduce. All three were measured against a live Postgres, at the shapes this system
actually issues.

**Claim 1 — the dense vector query is the one at risk.** As written: psycopg auto-prepares, Postgres
switches to a generic plan, "and for the shape this system's retrieval is built on that plan is a
sequential scan, because an `ORDER BY embedding <=> $1` cannot use an HNSW index when `$1` is not yet
a value", measured at "9 ms → 1,280 ms on execution 11", with `EXPLAIN (GENERIC_PLAN)` naming
`Seq Scan on note_index` under a `Sort`.

**False, and not how pgvector behaves.** Driven at 100k rows on `_dense`'s own shape — three
predicates and an `ORDER BY` on the same distance expression — `EXPLAIN (GENERIC_PLAN)` prints:

```
Index Scan using note_index_embedding_idx on note_index
  Order By: (embedding <=> ($1)::vector(384))
  Filter: ((embedding IS NOT NULL) AND (embedding_key = $2) AND ...)
```

An HNSW index orders on a parameterised operand perfectly well. Twenty executions under `auto`
measure 1.10 ms (1–5) → 0.69 ms (11–20): no cliff, in the improving direction. The execution number
was wrong too — psycopg's `prepare_threshold` is 5, so the sixth execution is the first prepared one,
not the eleventh.

**Claim 2 — two other statements have the same shape.** One of them turns out to be the *only* one
that has the problem at all, and its mechanism is a cost estimate rather than an unusable index.

`retrieval/vector_index.py::_lexical` carries `(%(ids)s::text[] IS NULL OR note_id = ANY(...))`, and
one prepared statement serves both parameterisations of it — which want structurally different
plans. Measured at 100k notes:

| parameters | custom plan | cost |
| --- | --- | --- |
| `ids` NULL | the OR constant-folds away; parallel seq scan + top-N heapsort | 5,194 |
| 20 ids | `Index Scan using note_index_pkey`, `Index Cond: note_id = ANY(...)` | 118 |
| generic | bitmap heap scan, OR as a `Filter`, **estimating rows=3** | 1,190 |

The generic plan's estimate is wrong by four orders of magnitude, which makes it *look* cheaper than
the correct plan. `auto` compares estimates, so it keeps it. The unscoped query goes **36.8 ms →
66.7 ms at execution 6 and stays there for the life of the connection**; `force_custom_plan` holds
37.0 ms flat. That is 1.81x — real, and a fifth of the 142x the comment claimed for a different
statement.

**Claim 3 — the checkpointer is excluded because its statements are primary-key lookups.**
LangGraph's own SQL carries `(%s::text IS NULL OR checkpoint_id < %s)` and two `= ANY(%s)` clauses:
the same family. The exclusion is nevertheless correct, for a reason nobody had stated. That OR sits
*behind* `thread_id = %s AND checkpoint_ns = %s`, so the generic plan is `Index Only Scan Backward
using checkpoints_pkey` with both equalities in the `Index Cond` and the OR filtering one thread's
checkpoints rather than a corpus. Measured at 200k rows over 2,000 threads: 0.35 ms under `auto`
against 0.37 ms forced — nothing to buy.

## Decision

**The setting stays and the comment is rewritten from the measurements.**

Keeping it is not inertia: the benefit is real, measured on a statement every scoped search issues,
and the cost is the 10 µs a point lookup pays (135.7 → 140.5 µs). What changes is that the argument
now names the statement that has the problem, the mechanism that causes it, and the execution at
which it starts.

The checkpointer exclusion also changes what it rests on: not "primary-key lookups", which is false,
but "the risky clause sits behind an equality on the index's leading columns". That is the property
to preserve when a statement is added there, and it is the one a reader can check.

## Consequences

- `tests/test_db.py::test_the_checkpointer_pool_is_not_given_the_plan_mode_and_the_reason_is_structural`
  pins the exclusion as an *absence*: the pool passes no `options` and names no `plan_cache_mode`.
  Mutation-verified by adding the option to `agent/checkpointer.py`. A pool that sets options at all
  is one whose exclusion needs re-deciding, which is what the failure message says.
- No test pins the 1.81x itself. It needs 100k rows and a live server to reproduce, which is a
  benchmark rather than a unit test; what the suite holds is that the option is on every connection
  this module opens, and the measurement is in the comment beside it with its date.
- **The general lesson is about which claim was checkable and which was not.** "An HNSW index cannot
  order on a parameter" is a statement about Postgres, one `EXPLAIN (GENERIC_PLAN)` away from being
  checked, and it survived a review, a commit and a merge because it was *plausible* and the setting
  it justified was harmless. The repository's own rule covers this exactly: prose is evidence about
  what its author believed, never about what the code does — and that holds when the code is
  somebody else's.

## Alternatives considered

**Remove the setting.** Tempting the moment the first claim fell: a setting justified by a
measurement that does not reproduce is the shape this repository deletes. Rejected because measuring
the *second* claim found a real 1.81x regression — which is the whole argument for measuring before
deleting rather than after.

**Give the checkpointer the setting too, for symmetry.** Rejected on measurement: 0.35 ms against
0.37 ms. Symmetry is not a reason to spend a re-plan on every turn's state read.
