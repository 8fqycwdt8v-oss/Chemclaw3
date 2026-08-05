"""Apply the SQL migrations in `infra/sql/` to the configured database.

Ordered `.sql` files applied in filename order, each tracked in a `schema_migrations`
ledger so a file runs exactly once and an already-applied file that later changes is
flagged as drift rather than silently re-run. Run via `make db-migrate` (and in CI
before the integration tests); also imported by the store's integration test so
schema setup lives in one place.

Each file is sent whole (psycopg's simple-query protocol executes all of a file's
semicolon-separated statements in one round trip when there are no placeholders), so
a statement containing a `;` inside a string literal or a `DO $$ … $$` block applies
intact — no fragile client-side splitting.

**Two locks, for two different reasons**, both added after a readiness review found this module
doing DDL against a live database with neither.

`pg_advisory_xact_lock` serializes migrators. The whole run is one transaction (Postgres DDL is
transactional, and there is a single commit at the end), so a transaction-scoped lock covers
exactly the right span and is released by the commit, the rollback, or the connection dropping —
there is no path that leaks it. The second migrator then waits, and finds every file already
recorded in `schema_migrations`. This is the same mechanism `agent/audit_store.py` uses to keep two
appends from forking the hash chain (`infra/sql/011`), which is what made its absence here
conspicuous: the audit writer serialized its *inserts* and the migrator did not serialize its *DDL*.

`lock_timeout` bounds how long a statement waits for a table lock. It is not a nicety and it is not
`statement_timeout`: an `ALTER TABLE` needs `ACCESS EXCLUSIVE`, Postgres's lock queue is FIFO, and a
lock request queued behind one long-running read **blocks every subsequent query on that table
behind it**. So a migration against a live system does not just wait — it takes the table down while
waiting, for as long as the slowest open query lasts. Bounding the wait and leaving the work
unbounded is the correct shape: a `CREATE INDEX` may legitimately build for minutes *after* it has
its lock, which is why this module still connects with no `statement_timeout` at all.

The two budgets are deliberately far apart (5 s for a table lock, 300 s for another migrator),
because waiting for a peer is a normal event and queueing in front of live traffic is not.

**This module has no caller but its own `__main__`.** The docstring used to say migrations "run at
service startup (the front door and each worker migrate before serving)", and nothing has ever done
that — the front door's lifespan does not call `migrate`, and neither does any worker. The claim
mattered here: it made concurrent migration sound routine, when the real concurrency is two deploys
overlapping or an operator running `make db-migrate` during one. The chart runs this as a
`pre-install,pre-upgrade` hook Job that completes before any app container starts (D-034).

**It runs as the migrator, not as the application.** `postgres_migration_dsn` is the credential
that owns the schema; `postgres_dsn` is the runtime one, which under a split deployment cannot
issue DDL at all (D-2026-08-05-append-only-by-grant-not-by-contract). Unset, it falls back to
`postgres_dsn` and everything behaves exactly as it did — a single-principal database is still a
supported deployment, and it is what `make up`, CI and every test use.

**Lives in `core/`** because the schema belongs to the whole application. It sat in
`science/calc/`, which is neither of the two homes `ARCHITECTURE.md` allows for capability code —
an artefact of the QM cache having been the first thing to need a table.
"""

import asyncio
import hashlib
from pathlib import Path

from chemclaw.core.config import settings
from chemclaw.core.db import connect

# The ledger's own DDL. Applied first and not itself tracked — it is the tracker.
_LEDGER_FILE = "000_schema_migrations.sql"

# The advisory-lock key that serializes migrators. Arbitrary but stable, and distinct from the
# audit chain's (`agent/audit_store.py`) — advisory locks share one namespace per database, so two
# subsystems picking the same number would block each other for no reason either could diagnose.
_MIGRATION_LOCK_KEY = 0x43484D4157_00_02  # "CHMAW" + a discriminator for this path

# `SET` takes no parameters in Postgres, so the timeouts go through `set_config`, whose third
# argument (`is_local`) scopes them to this transaction exactly as `SET LOCAL` would.
_SET_LOCAL_TIMEOUT = "SELECT set_config('lock_timeout', %s, true)"


class MigrationError(RuntimeError):
    """A migration cannot be applied safely (e.g. an applied file was edited)."""


def _read_sql_files() -> dict[str, str]:
    """Read every `infra/sql/*.sql` file into `{filename: text}` (the blocking half of `migrate`).

    One function so the whole directory is read in a single thread hop rather than one per file.
    """
    sql_dir = Path(settings.sql_migrations_dir)
    return {path.name: path.read_text() for path in sorted(sql_dir.glob("*.sql"))}


def _checksum(text: str) -> str:
    """SHA-256 of a migration file's text, to detect edits after it was applied.

    File integrity, deliberately not `chemclaw.core.ids.stable_hash` (which is for
    content-addressed *identity* keys over JSON) — here the raw bytes are what matter.
    """
    return hashlib.sha256(text.encode()).hexdigest()


def _ms(seconds: float) -> str:
    """Render a seconds budget as the millisecond string `lock_timeout` expects."""
    return f"{int(seconds * 1000)}ms"


def migration_dsn() -> str:
    """The credential that owns the schema: `postgres_migration_dsn`, else the runtime one.

    One resolver so the migration runner and the grant reconciliation cannot end up pointing at
    different databases — a grant applied to one server and DDL to another is the failure mode a
    second `or` expression invites. Falling back keeps every single-principal deployment (dev, CI,
    `make up`, the whole test suite) working with nothing configured.
    """
    return settings.postgres_migration_dsn or settings.postgres_dsn


async def migrate(dsn: str | None = None) -> list[str]:
    """Apply every not-yet-applied `infra/sql/*.sql` file in order; return the names applied.

    Idempotent: files recorded in `schema_migrations` are skipped, so re-running applies
    nothing and returns `[]`. Raises `MigrationError` if a previously applied file's
    checksum no longer matches — an edited migration must become a new file, never a
    silent in-place change.
    """
    target = dsn if dsn is not None else migration_dsn()
    applied: list[str] = []
    # Every file read up front, in one worker thread rather than ~30 hops. `connect`, not
    # `connection`: a migration wants its own connection with no statement timeout (an index build
    # may run long), which is precisely what the pool must not hand out to a request path — and it
    # must be a connection nobody else can be handed, because the advisory lock below is scoped to
    # it.
    sources = await asyncio.to_thread(_read_sql_files)
    async with await connect(target) as conn:
        # Wait generously for a peer migrator, then tightly for every table lock after it. The
        # order matters: taking the advisory lock under the 5 s DDL budget would make an ordinary
        # concurrent deploy fail, and doing the DDL under the 300 s budget would let one ALTER
        # TABLE queue in front of live traffic for five minutes.
        await conn.execute(_SET_LOCAL_TIMEOUT, (_ms(settings.pg_migration_lock_wait_seconds),))
        await conn.execute("SELECT pg_advisory_xact_lock(%s)", (_MIGRATION_LOCK_KEY,))
        await conn.execute(_SET_LOCAL_TIMEOUT, (_ms(settings.pg_migration_lock_timeout_seconds),))
        # Bootstrap the ledger before anything can be tracked against it.
        await conn.execute(sources[_LEDGER_FILE])
        for name in sorted(sources):
            if name == _LEDGER_FILE:
                continue
            text = sources[name]
            checksum = _checksum(text)
            cursor = await conn.execute(
                "SELECT checksum FROM schema_migrations WHERE filename = %s", (name,)
            )
            row = await cursor.fetchone()
            if row is not None:
                if row[0] != checksum:
                    raise MigrationError(
                        f"migration {name} was edited after being applied "
                        f"(recorded checksum differs); add a new migration file instead"
                    )
                continue
            await conn.execute(text)
            await conn.execute(
                "INSERT INTO schema_migrations (filename, checksum) VALUES (%s, %s)",
                (name, checksum),
            )
            applied.append(name)
        await conn.commit()
    return applied


if __name__ == "__main__":
    names = asyncio.run(migrate())
    print(f"applied migrations: {', '.join(names) or '(none — already up to date)'}")
