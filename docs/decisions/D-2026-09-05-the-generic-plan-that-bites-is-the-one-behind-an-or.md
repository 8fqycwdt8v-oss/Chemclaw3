# D-2026-09-05-the-generic-plan-that-bites-is-the-one-behind-an-or — the setting stays; the first draft of this ADR measured three shapes the code does not run

## Status

Accepted, after its own first draft was refuted by a fresh-context review. No behaviour changes;
the justification does, twice.

**The title is kept deliberately and is now wrong.** It names the conclusion of the draft this
document replaces, and renaming it would hide that a merged-looking argument was reversed inside its
own pull request. The statement that bites is the **dense vector** one.

## Context

`core/db.py` adds `-c plan_cache_mode=force_custom_plan` to every connection this module's pools
open. The comment arguing for it made three claims. A review found the first did not reproduce, and
the draft of this ADR concluded all three were false and supplied replacements. **A second review
then refuted the replacements**, and it was right: every one of the draft's probes measured a shape
this repository does not issue.

### What the draft got wrong, and why each error was invisible

**The embedding width.** The draft probed `_dense` at `vector(384)` and found the generic plan was
an HNSW `Index Scan` with no cliff — concluding the original claim was "false, and not how pgvector
behaves". But `infra/sql/012_note_index.sql` declares `vector(1536)` and `core/config` *raises*
unless `embedding_dim` matches it, so 384 is a width no deployment can have. At 1536, on the same
100k rows and the same statement:

```
EXPLAIN (GENERIC_PLAN):  Seq Scan on note_index  ->  Sort
  force_generic_plan   1,762 ms
  force_custom_plan        0.93 ms
```

~1,890x, and `Seq Scan ... Sort` is the literal plan shape the original comment named. **The
sentence the draft deleted was correct.** Width is the deciding variable, and nothing in the draft's
method could see it, because a probe writes its own schema.

**The lexical statement.** The draft reported a 1.81x cliff on `_lexical`'s
`(%(ids)s::text[] IS NULL OR note_id = ANY(...))`. The shipped `_lexical` splices
`core.fulltext.TSQUERY_TERMS` — a `LATERAL` whose tsquery is built from `ARRAY(SELECT ...)`
SubPlans, which can never constant-fold — so the custom and generic plans do not diverge the way the
draft's simplified `websearch_to_tsquery` form does. The draft measured the statement `_lexical`
used to be.

**The execution number.** The draft said the flip happens at execution 6, "not the eleventh".
Measured both ways on one skewed table:

```
prepare=True (forced from execution 1):  38 ms x5, then 87 ms from execution 6
auto-prepare, as the shipped code runs:  38 ms x10, then 83 ms from execution 11
```

Server-side, a plain `PREPARE` serves five custom plans and switches on the prepared statement's
sixth `EXECUTE` (cost 7,450 -> 273). psycopg's `prepare_threshold=5` makes the client's sixth call
the prepared statement's first, so the two compose to **11**. The draft's probe passed
`prepare=True`, which forces preparation from call one — an artefact of the measurement, not of the
system. **The original comment's "execution 11" was correct.**

Three errors, one shape: each probe wrote its own version of the thing it was measuring.

## Decision

**The setting stays, and the comment now carries the claims that survive measurement.**

- The dense vector statement is the one at risk, and the **embedding width** is why — stated
  explicitly, because that is the variable a future probe will otherwise get wrong again.
- What is true *today* is that `auto` does not take the bad plan: at 100k rows the generic plan
  estimates 5,915 against the custom plan's 2,334. That margin is **an estimate that is wrong in
  the fortunate direction** — the plan it declines is three orders of magnitude slower in reality —
  and estimates move with statistics, row counts and a planner upgrade. The setting is what stops
  the outcome depending on that. It is insurance, not the repair of an observed cliff, and saying so
  is the difference between this comment and the one it replaces.
- Four more statements carry the same `IS NULL OR` shape (`science/calc/postgres_store`,
  `ingest/documents/index.py`, `external_index.py`, `science/labels/store.py`). On the calc browse
  statement, `force_generic_plan` measures 59 ms against `force_custom_plan`'s 0.95 ms, and `auto`
  likewise declines it. Same insurance, four more places.
- Cost on the statements that do not need it: a point lookup goes 263 µs to 288 µs (~25 µs on this
  box; the draft recorded ~5 µs from a faster one — direction and magnitude-class hold, the absolute
  does not travel).
- The checkpointer exclusion stands, on the corrected reason: not "primary-key lookups" (false of
  LangGraph's SQL) but that its `IS NULL OR` sits behind `thread_id = %s AND checkpoint_ns = %s`, so
  the generic plan is an `Index Scan Backward using checkpoints_pkey` with both equalities in the
  `Index Cond`. ~0.4 ms either way at 200k rows over 2,000 threads. (`Index Scan`, not `Index Only
  Scan` — the draft's probe projected key columns only; LangGraph's `SELECT_SQL` does not.)

## Consequences

- `tests/test_db.py::test_the_checkpointer_pool_is_not_given_the_plan_mode_and_the_reason_is_structural`
  is rewritten to parse the `AsyncConnectionPool(...)` call with `ast` and check its keywords,
  including a literal `kwargs=` dict. **The draft's version asserted nothing**: it partitioned on
  `")"`, which stops at the `)` of `conninfo=_session_dsn()` — 39 characters of a 34,000-character
  file — so `options=_FORCE_CUSTOM_PLAN` on the pool passed it. Only a bare substring scan for
  `plan_cache_mode` caught the one spelling the mutation check happened to use, and that scan made a
  *comment* naming the exclusion fail the test that exists to explain it. All three cases are now
  mutation-verified: keyword fails, `kwargs=` entry fails, comment passes.
- No test pins the 1,890x. It needs 100k rows at `vector(1536)` and a live server, which is a
  benchmark rather than a unit test.
- **The lesson is not "measure it" — the draft did measure, three times, carefully.** It is that a
  probe you write yourself is a claim about the shape you gave it, and the shape is the part a
  reviewer has to check. Every one of these three defects lives in the fixture, not the method:
  a width from the probe's own `CREATE TABLE`, a query the probe retyped instead of importing, a
  `prepare=True` the probe added for determinism. `D-2026-09-05-a-ratchet-that-binds-no-connectors-measures-a-smaller-system`
  is the same failure in a test fixture; this is it in a benchmark, and both were found only by
  somebody who had not written the probe.

## Alternatives considered

**Remove the setting.** Tempting when the first claim appeared to fall, and the draft's own closing
line congratulated itself for resisting it. That reasoning was doubly wrong: claim 1 had not fallen
(it is the surviving reason), and claim 2 — the one the draft kept the setting *for* — is the one
that does not reproduce. Rejected on the measurement that actually holds: 1,890x on the dense
statement at the shipped width.

**Give the checkpointer the setting too, for symmetry.** Rejected on measurement: ~0.4 ms either way.
