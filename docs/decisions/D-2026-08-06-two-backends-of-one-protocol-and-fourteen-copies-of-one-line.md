# D-2026-08-06-two-backends-of-one-protocol-and-fourteen-copies-of-one-line — Two backends of one Protocol, and fourteen copies of one line

**Status:** accepted · **Date:** 2026-08-06

## Context

Four rows from the store-seam lane. The Q-A lane's measured verdict stands — the ten store triads
are *not* one abstraction waiting to be extracted — so this takes only what it found: the
divergences, and the plumbing that hid them.

- **[M]** `session_store_dsn` means two databases and `make db-migrate` touches one.
- **[L]** `InMemoryStore.find` raises `TypeError` on a timezone-aware `created_at`; Postgres does not.
- **[L]** Only one of three writers of computed payloads rejects non-finite floats.
- **[L]** The bounded-connection helper is hand-rolled fourteen times, five docstrings
  byte-identical and four claiming *"one place, DRY"*.

The last row is not cosmetic and belongs with the other three: the same six lines written fourteen
times is a place where a rule can be omitted once and never noticed. That is exactly what the
`statement_timeout` mutation survivor (D-2026-08-06-a-refusal-is-an-attempt-worth-recording) was.

## Decision

### `make db-migrate` migrates every database this deployment stores in

Six session-layer stores follow `session_store_dsn` — the message history, the session owners, the
turn claims, the push-back mailbox, the preferences and `turn_costs`. `migrate()` ran against
`migration_dsn()` alone, so a split deployment ran the migration, was told it succeeded, and had no
`session_messages` table at all. The front door then failed on its first turn.

`migration_targets()` resolves the list; `migrate_all()` applies to each; `__main__` prints one line
per database. Targets are compared on **host/port/dbname**, not on the DSN string, because a
split-role deployment writes the same database twice with different credentials — that is one
database, not two.

The whole file set is applied to each target rather than a per-database subset. That leaves unused
tables in both, which is the honest cost of a monolithic migration set and is strictly better than a
hand-maintained "which tables belong where" map that the next migration would silently fall out of.

**One combination is refused rather than half-served**: a split *database* together with a split
*credential*. There is one migrator DSN and it names the calculation database, so nothing here can
own the schema in the session one. Refusing is the point — doing nothing is the bug being fixed, and
guessing the runtime credential would fail obscurely at the first `CREATE TABLE`.

### A naive datetime means UTC, because that is what the durable half does

`InMemoryStore.find` sorted against a naive `datetime.max` and compared a caller's naive
`since`/`until` against a stored value, so a timezone-aware `created_at` — which is what
`datetime.now(UTC)` produces, and what Postgres returns for *every* row — raised `TypeError: can't
compare offset-naive and offset-aware datetimes` on a query the Postgres backend answered without
complaint.

Normalized rather than rejected, because `timestamptz` is what the durable backend actually does: it
converts on the way in and hands back an aware value, so a naive input has always meant UTC on that
side. Rejecting here, or picking any other convention, would make the two backends disagree in a
second and quieter direction. The existing ordering is unchanged — a row with no `created_at` still
sorts to the front, which is the in-memory store's "insertion order stands in for time" behaviour
and is unreachable on Postgres, where the column has a default.

### The non-finite guard moved to where the connection lives

`json.dumps` emits bare `NaN`/`Infinity`. Those are not JSON, `jsonb` rejects them — but only after
the statement reaches the server — and a caller that logs and continues turns that into silent data
loss. The BO campaign store learned this the expensive way: a degenerate GP posterior produced a
`NaN`, the insert was swallowed at WARNING, and the campaign read back with no observations at all.

The calculation cache and the durable job record write exactly the same kind of payload — numbers a
calculator produced — and had the same hole, with no reason to have learned it separately. So
`db.jsonb()` is one function in the module that owns the connection, and all three writers use it.

Deliberately *not* applied to the session stores: they persist MAF messages, which carry text rather
than computed numbers, and a strictness they cannot violate is noise.

### `db.bounded()` is the fourteenth copy, written once

`connection(dsn, statement_timeout_seconds=settings.pg_statement_timeout_seconds)` appeared at
twenty-nine call sites across fifteen modules, wrapped in a private `_connect` in fourteen of them.
`dsn` defaults to `settings.postgres_dsn`, which is the only thing those fourteen helpers actually
varied in — three of them became so thin they were inlined away entirely.

**The sweep caught its own refactor, which is the shape it was written for.** Collapsing the copies
made every one of them stop matching `db.connection(..., statement_timeout_seconds=...)`, and
`tests/test_db.py` failed on the floor assertion rather than quietly sweeping an empty list. That
assertion exists because a guard-by-source test's failure mode is passing over nothing.

## Consequences

- A split-DSN deployment is migrated by `make db-migrate`, or told plainly that its combination
  cannot be. Every existing single-database deployment is unaffected.
- The in-memory and Postgres calculation stores answer a timezone-aware query the same way.
- A `NaN` from any of the three computed-payload writers raises in-process, naming the writer.
- One spelling for a bounded connection, and a sweep that now recognises it.
- `Jsonb` and the non-finite rule are no longer spelled per store, so a new store gets both by using
  the module every store already imports.

## Alternatives rejected

- **Refusing the `session_store_dsn` split at startup** (the row's other option). It is a supported
  configuration with six stores behind it; refusing would delete a feature to avoid migrating it.
- **A per-database subset of the migration files.** A map of which table belongs where, maintained
  by hand, that the next migration falls out of silently.
- **Comparing DSN strings to decide whether two DSNs are one database.** A split-role deployment
  writes the same database twice with different credentials, and this would migrate it twice.
- **Rejecting naive datetimes in `find`.** Makes the two backends disagree in a new direction; the
  durable one has always accepted them and meant UTC.
- **Copying the `allow_nan=False` wrapper into the other two stores.** Three copies of a rule that
  was learned once, in a repository whose `_connect` row is about exactly that.
- **Applying the strict dumper to the session stores too.** They write message text; a rule nothing
  can violate reads as protection where there is no threat.
