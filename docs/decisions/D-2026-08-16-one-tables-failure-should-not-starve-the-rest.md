# D-2026-08-16-one-tables-failure-should-not-starve-the-rest — a database-setup review's six fixes, and three findings left for a decision

**Status:** accepted · **Date:** 2026-08-16

## Context

A deep review of the database layer — schema and migrations, the checkpointer and session store,
connection pooling and grants, and the calculation cache and job-record store — run as four
independent passes so nothing was reviewing its own conclusions. The layer is unusually
self-documented (`core/db.py`, `core/migrate.py`, `agent/checkpointer.py` and `durable/retention.py`
each already carry detailed rationale in their own docstrings), so the passes were briefed to find
what was *not* already written down there or in `docs/planning/BACKLOG.md`, not to restate it.

Six findings were small, verified, and safe to fix without a design decision. Three more name a real
gap but need a human judgement call — a cache-key change that would invalidate every existing cached
row, an unbounded-growth table whose disposal policy nobody has stated, and a migration comment that
disagrees with the query it was written for — and those went to `docs/planning/BACKLOG.md` instead of
being fixed unilaterally, per this file's own rule that a row without an anchor is not ready to be
queued and a decision like that is not one a review gets to make silently.

## The fixes

### 1. Retention's per-table loop had no isolation: one table's failure stopped every table after it

`durable/retention.py::prune_expired_rows` iterates `_PRUNABLE` (`session_events`,
`session_messages`, `tool_result_blobs`, `checkpoints`, in that order) with no `try`/`except` around
any table's block. A persistent problem confined to `session_messages` — a `statement_timeout`, a bad
row — propagated straight out of the function before `tool_result_blobs` or `checkpoints` were even
attempted, and Temporal retries the whole activity from the same starting point every time. Against a
deployment where the first table's problem is not transient, every table after it in iteration order
would never be pruned again until the first was fixed.

Each table's block is now wrapped: on failure it rolls back (Postgres holds a transaction in an
aborted state after an uncaught error, so without the rollback the next table's statement would fail
too — the isolation would not work at all), logs, and lets the loop continue. The first exception is
re-raised once every table has been attempted, so the activity still fails and Temporal still
retries — identical behaviour to before for the table that actually failed, with the isolation as the
only change. `tests/test_retention.py::test_a_failed_table_does_not_starve_the_tables_after_it` seeds
an expired `tool_result_blobs` row alongside the existing fault-injection test's `session_messages`
failure and asserts the later table still gets pruned in the same pass, despite the pass still
raising.

### 2. `memory_store()` repeated the cold-start race `checkpointer()` was built to close

`agent/scratchpad.py::memory_store` published its `_store` global before awaiting `setup()`, and
called `_checkpoint_pool()` — a function whose own docstring says it takes no lock because its *one*
caller (`checkpointer()`) already holds `_init_lock` — with no lock held at all. Two coroutines
racing this function unguarded (a memory-enabled turn's first call, concurrent with another) could
each pass `_checkpoint_pool`'s `if _pool is None` check and construct a distinct
`AsyncConnectionPool` against the same DSN. Only one survives in the module global; the other is
opened and never closed — a real connection-pool leak on a busy cold start, not just a race on which
store object wins.

The fix takes the same `_initialization_lock()` `checkpointer()` uses — imported from
`agent/checkpointer.py`, the same lock object, since `_checkpoint_pool()` is the resource both
modules share — and publishes `_store` only once `setup()` has completed under it, mirroring
`checkpointer()`'s own shape exactly.
`tests/test_scratchpad.py::test_concurrent_first_turns_get_one_migrated_store` mirrors
`test_checkpointer_schema.py::test_concurrent_first_turns_get_one_migrated_saver`: it slows
`AsyncPostgresStore.setup` to widen the race window and asserts four concurrent first callers all get
a fully-migrated store and the *same* store object (proving one `setup()` run and one pool).

### 3. Neither pool was ever closed on shutdown, and the store's own close function had no caller

`agent/api/app.py`'s lifespan opened `db.pooling()` (the shared request-path pool) and closed it on
exit, but never called `close_checkpointer()` or `close_memory_store()` — both existed, both were
exercised only by tests, and the front door built the checkpointer's separate pool
(`agent/checkpointer.py`) on first turn and then let it die with the process on every shutdown,
instead of closing it the way `db.pooling()` already closes its own. `close_memory_store()` clears
the store reference first (it holds no connections of its own, only a reference into the
checkpointer's pool), then `close_checkpointer()` closes the pool — the same order the module
docstrings already describe, now actually wired into the one process that builds either.

### 4. The connection pool never health-checked a borrowed connection

`core/db.py::_pool_for` constructed `AsyncConnectionPool` with no `check=` callback.
`pg_pool_max_idle_seconds` only governs when the pool itself decides to close a connection it thinks
has sat idle too long — it says nothing about one already killed out from under the pool by something
it cannot see: a managed-Postgres idle limit, a stateful load balancer's NAT timeout,
`idle_in_transaction_session_timeout`. Without a check, the first query on such a connection reaches a
caller and fails with a raw connection-reset error instead of the pool's background health-check loop
quietly replacing it first. `check=AsyncConnectionPool.check_connection` closes the gap; it is
psycopg_pool's own built-in `SELECT 1` probe, not new code.

### 5. Two live-lane CLIs bypassed the redaction filter and could print a raw DSN

`cli/live_jobs.py` and `cli/live_storm.py` are the only two CLI entrypoints in the tree that call bare
`logging.basicConfig(...)` instead of `core/logging.py::configure_logging()` — every other one does,
per that module's own docstring ("every CLI has its own `main`... each of them calls
`configure_logging()`"), which is also what installs `SecretRedactingFilter`. Both also built their
report's Postgres line as `settings.postgres_dsn.rsplit('@', 1)[-1]`, printed to stdout and written to
a markdown report file — a construction that assumes URL-form DSNs. A libpq keyword-form DSN
(`host=... password=...`, which `core/db.py` explicitly anticipates operators using) has no `@`, so
`rsplit` returns the entire string, cleartext password included, bypassing both the logging-layer
filter and `core/db._redact`. Both scripts now call `configure_logging()` and use `core.db._redact`
for the DSN they print.

### 6. `artifact_eviction.py`'s docstring overstated what "never recomputed" covers

Its module docstring claimed "the answer itself stays cached forever" once an artifact blob is
evicted — true for a result whose row carries its answer inline, and *not* true for
`science.calc.artifacts.ArrayOffloadingStore` (built for `hessian()`, D-124), whose row stores only a
content hash for each packed array and whose `get()` returns `None` — a full cache miss, not a
partial result — when the blob is gone. Reclaiming that blob evicts the answer along with it, not
merely a by-product of it. This is a deliberate, accepted trade from
`D-2026-08-16-the-physics-leaves-the-cache-stays`, not an oversight in this job — the docstring now
names the exception explicitly instead of stating an absolute the code does not keep.

## Left as `BACKLOG.md` rows, not fixed here

- **`CalculationKey.as_str()`'s unescaped concatenation as the literal cache primary key.** Two
  different `(calc_type, calc_version)` pairs can serialise to the same string (`calc_version` is not
  guaranteed free of `@`/`:` — real examples exist in `D-2026-08-16-the-physics-leaves-the-cache-stays`),
  which could let one calculator's `ON CONFLICT DO UPDATE` overwrite another's row. The correct fix —
  hashing the four components as a mapping — changes every existing row's key, which under D-011
  ("never recomputed") is a full-cache invalidation on deploy. That trade needs its own ADR and a
  migration plan, not a quiet change inside this one.
- **`session_owners`/`session_turns` grow with no age-based disposal.** Neither is in
  `durable/retention.py`'s `_PRUNABLE` set, and `infra/sql/README.md`'s own `session_owners` row
  already flagged this with a dangling "BACKLOG" cross-reference that named no row — closed by adding
  one, not by inventing a disposal policy this review was not asked to design.
- **`observations_status_idx` does not cover the query it names as its reason.** The migration's
  comment says the index exists to serve "open observations newest-first" by `last_seen`; the code
  that ships sorts by `cardinality(evidence_note_ids) DESC, last_seen DESC` instead. Whether the fix
  is an expression index matching the real sort or a correction to which field is authoritative is a
  product call, not a schema call.

## Verification

Every mutable code fix (1, 2) was mutation-checked — the fix reverted from a `cp` backup (never
`git checkout`, per this repository's own recorded lesson about losing uncommitted work that way),
the specific test observed red, the fix restored and the test observed green again:

| mutation | test that caught it |
|---|---|
| `except Exception as exc: ... raise exc` (no isolation, immediate re-raise) | `test_a_failed_table_does_not_starve_the_tables_after_it` |
| `memory_store()` back to publish-before-`setup()`, unlocked `_checkpoint_pool()` call | `test_concurrent_first_turns_get_one_migrated_store` |

The schema migration (`infra/sql/046_review_hardening_indexes.sql`) was applied against a real,
`make up`-started Postgres via `make db-migrate`, including the `NOT VALID` `CHECK` constraint on
`session_messages.message_shape`; `infra/sql/README.md`'s table inventory was updated to name it,
which `tests/test_schema_inventory.py` verifies bidirectionally. `make lint type test` is green.
