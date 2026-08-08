# D-2026-08-08-a-borrowed-connection-is-bounded-by-default — the safe bound is the default, and the escape hatch is a different function

**Status:** accepted

## Context

`chemclaw.core.db.connection()` is the one helper every store borrows a Postgres connection from.
Its per-statement bound was opt-in: `statement_timeout_seconds` defaulted to `None`, and `None`
meant *no bound at all*. Thirty call sites across twenty-two modules therefore wrote out

```python
async with db.connection(dsn, statement_timeout_seconds=settings.pg_statement_timeout_seconds):
```

by hand, and a thirty-first that forgot would get an unbounded connection with nothing saying so.
Measured against the live database before the change, on a caller that passes nothing:

```
configured pg_statement_timeout_seconds : 30.0
unpooled, no argument -> SHOW statement_timeout = 0
pooled,   no argument -> SHOW statement_timeout = 0
unpooled, explicit    -> SHOW statement_timeout = 30s
```

`0` is Postgres for "no limit". So the bound was a *convention* the codebase happened to keep,
enforced by nothing, on a helper whose whole purpose is that call sites do not each reinvent the
connect. One runaway query in a store that omitted the keyword would hold a pooled connection for
as long as it ran — and the pool is small by design (`pg_pool_max_size`), so the failure is not one
slow query but a starved process. This is the same class of defect D-119 and
`D-2026-08-05-the-connection-budget-is-a-fleet-number` were about: the reading exists, the guard
exists, and the thing that decides whether either applies is whether a human remembered.

The fpstore omission this replaces had already happened once —
`test_postgres_store_applies_the_configured_statement_timeout` exists because the fingerprint store
was the one store that forgot, and its HNSW scans are the slowest queries in the tree. The response
at the time was to add the keyword and pin it with a test *for that one store*, which fixes the
instance and leaves the mechanism.

## What was established before changing anything

Three questions had to be answered by reading, not by pattern-matching the keyword, because a
default that silently shortens a deliberately-longer bound is a fix that breaks a working path.

**1. Does any `connection()` call site want a different bound, or none?** One wants a different
one: `api/routes/ops.py` bounds the `/readyz` probe's `SELECT 1` at
`service_readiness_db_timeout_seconds` (2 s), deliberately tighter than the stores' 30 s — the
config comment says so, and the route's whole safety argument is that a probe answers quickly or
answers "not ready". It keeps its explicit argument and is pinned by a test that would fail if a
default overrode it. **No `connection()` call site wants an unbounded connection.** The three paths
that genuinely do — `core/migrate.py` (an index build may legitimately run long), `core/grants.py`,
and `tests/pg.py` — all reach past `connection()` to `connect()`, the dedicated unpooled connect,
and always did: the migration runner's comment already explains that it needs a connection nobody
else can be handed, for the transaction-scoped advisory lock.

**2. What the default should be, and whether `None` must stay expressible as "no bound".** It need
not: nothing passes `None` to `connection()`, and the deliberately-unbounded paths are all on
`connect()`. So `None` was free to become "unspecified" — which is what a caller omitting the
argument means anyway. `0` remains "no bound", unchanged, because `_merged_options` already treats
it that way and because `pg_statement_timeout_seconds` is `ge=0` with "0 disables it" documented.

**3. Does any call site pass a computed value?** No. Every one of the thirty passes the settings
field verbatim; the readiness probe passes a different settings field. There is no arithmetic on a
timeout anywhere in the tree.

## Decision

**`connection()` applies `pg_statement_timeout_seconds` when the caller names no bound.**
`connect()` does not, and keeps defaulting to unbounded. The asymmetry between two functions that
look alike is the decision, not an oversight in it: a connection you *own* may run an index build
for an hour; a connection you *borrowed* from a pool the request path shares may not. Both
docstrings now say so, pointing at each other.

Resolution happens per call (`if statement_timeout_seconds is None: … = settings.…`), not as a
default argument value, because a default argument is evaluated once at import and would freeze
whatever the setting was before `tests/conftest.py` — or an operator's env — redirected it.

The thirty explicit arguments are deleted. They are now noise that says the default out loud, and
noise of exactly the kind that makes the one *meaningful* explicit argument (the readiness probe's
2 s) invisible in review.

**The escape hatch is enumerated, not trusted.** `tests/test_db.py` AST-walks `src/chemclaw` for
callers of `db.connect` — resolving through the imports, since `connect` is also
`temporal_client.connect` in a dozen modules — and asserts the set is exactly
`{core/migrate.py, core/grants.py}`. Without it, defaulting the bound closes the hole a forgotten
keyword opened and leaves one a *different* import reopens, which is the "a fix can open the hole it
closes elsewhere" failure this campaign has hit repeatedly. Two modules had to move to make the set
true: `cli/live_jobs` and `cli/live_storm` each read one scalar from the live database through
`connect()`, wanting neither a dedicated connection nor an unbounded query — only the shortest way
to a connection at the time they were written. They now use `connection()` like everything else.

## Consequences

- A new store gets a bounded connection by writing nothing. Forgetting is no longer a failure mode.
- The libpq `options` string is unchanged for every existing call site, so **pool keying is
  unchanged** — no store starts or stops sharing a pool with another. The `/readyz` probe still
  keys its own pool, as it did before, because its options differ.
- Bare `db.connection(dsn)` calls in tests (retention, session store, review fixtures) are now
  bounded at 30 s where they were unbounded. All are fixture inserts and reads; none approaches it.
- `SHOW statement_timeout` after the change reads `7500ms` under a monkeypatched 7.5 s setting,
  pooled and unpooled, and an explicit `2.5` still yields `2500ms`.

## Alternatives rejected

**A sentinel so `None` keeps meaning "no bound" on `connection()`.** It costs a `Literal[enum]`
union in a public signature under `mypy --strict`, and it buys the ability to express something no
call site expresses and that `connect()` already provides. An abstraction with no caller.

**Making `statement_timeout_seconds` required.** It would force every call site to name a value,
which is the current state with a compiler behind it — it makes forgetting loud rather than making
the safe thing automatic, and it puts a settings lookup back into thirty modules.

**Leaving the wrappers alone but inlining them.** Most modules reach `connection()` through a small
`_connection()` helper that is now little more than "which DSN". They are kept: the DSN choice is
real logic in several of them (`session_store_dsn or postgres_dsn`), and each is the module's one
connection seam, which several tests patch. Their docstrings ("borrow a connection with the
configured per-statement timeout") remain true — the timeout arrives, it is simply no longer this
function that names it.

## Verification

- `tests/test_db_pool.py::test_a_caller_that_asks_for_no_timeout_still_gets_the_configured_one` —
  live Postgres, pooled and unpooled; fails on the unfixed code with `assert '0' == '7500ms'`.
- `tests/test_db_pool.py::test_an_explicit_timeout_still_overrides_the_default` — passes before and
  after, which is the point: it is the guard on the working path, not evidence of the fix.
- `tests/test_db.py::test_connection_defaults_the_statement_timeout_onto_the_connect` — the offline
  half, pinning the `options` string and the `0`-means-no-bound escape.
- `tests/test_db.py::test_only_the_migration_paths_open_an_unbounded_postgres_connection` — proven
  to catch a violation by planting a `connect` caller in `src/chemclaw/core/` and watching it fail.
- `tests/test_molfp.py::test_postgres_store_applies_the_configured_statement_timeout` — rewritten.
  It asserted the *keyword* the store forwards, which after this change would prove only that the
  store still repeats itself. It now asserts the libpq `options` the connect receives, and was
  confirmed to fail with the default removed.
- Gate: `ruff format`, `ruff check`, `mypy src examples tests` clean; 1 513 tests across every
  touched module and every Postgres-backed suite green (6 skipped — no `xtb`/`crest` binary).

Measured delta across `src/` and `tests/`: 30 files, +237/−116. The mechanical part — deleting the
argument at its thirty call sites — is 24 files and −61 net lines (+33/−94, the additions being the
calls collapsing back onto one line). An earlier prototype estimated 23 files and ~−185 lines; the
file count is close and the line count does not reproduce. It predates six merged lanes, and it
counted a deletion-only diff — the tests, the AST pin and the two `connect()` call sites that had to
move are what this actually cost.
