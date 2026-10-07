"""Apply the SQL migrations in `infra/sql/` to the configured database.

Files apply in filename order, each recorded in `schema_migrations` so it runs once; a recorded file
whose statements later change is refused as drift. Each file is sent whole via the simple-query
protocol, so `;` inside literals or `DO $$ … $$` blocks needs no client-side splitting. Run by `make
db-migrate` and by the chart's `pre-install,pre-upgrade,pre-rollback` hook Job; nothing migrates
at service startup.

Two locks: `pg_advisory_xact_lock` serialises concurrent migrators for the single transaction, and
`lock_timeout` bounds each DDL statement's wait for a table lock, because a queued `ACCESS
EXCLUSIVE` request blocks every later query on that table. The work itself has no
`statement_timeout` (an index build may legitimately take minutes). Waiting for a peer gets a long
budget; queueing in front of live traffic gets a short one.

It reports what it applied rather than certifying the schema: `_warn_if_the_database_is_ahead` warns
about ledger rows this image does not ship (a rollback must still start), and `api/routes/ops.py`'s
readiness probe handles an image ahead of its schema. It runs as the migrator credential
(`migration_dsn`), falling back to the runtime DSN for single-principal deployments.
"""

import asyncio
import hashlib
import logging
import time
from pathlib import Path

import psycopg
from psycopg.rows import TupleRow

from chemclaw.core.config import settings
from chemclaw.core.db import connect
from chemclaw.core.logging import configure_logging, log_event

logger = logging.getLogger(__name__)

# The ledger's own DDL. Applied first and not itself tracked — it is the tracker.
_LEDGER_FILE = "000_schema_migrations.sql"

# Arbitrary but stable advisory-lock key: advisory locks share one namespace per database, so it
# must not collide with any other subsystem's key.
_MIGRATION_LOCK_KEY = 0x43484D4157_00_02  # "CHMAW" + a discriminator for this path

# `SET` takes no parameters in Postgres, so the timeouts go through `set_config`, whose third
# argument (`is_local`) scopes them to this transaction exactly as `SET LOCAL` would.
_SET_LOCAL_TIMEOUT = "SELECT set_config('lock_timeout', %s, true)"


class MigrationError(RuntimeError):
    """A migration cannot be applied safely (e.g. an applied file was edited)."""


def _read_sql_files() -> dict[str, str]:
    """Read every `infra/sql/*.sql` file into `{filename: text}` (the blocking half of `migrate`).

    One function so the directory is read in a single thread hop.
    """
    sql_dir = Path(settings.sql_migrations_dir)
    return {path.name: path.read_text() for path in sorted(sql_dir.glob("*.sql"))}


def newest_shipped_migration() -> str | None:
    """The newest migration filename **this image** ships, or None if it ships none but the ledger.

    Sorted by whole filename, the apply order and the ledger key, independent of Postgres collation.
    Names only, since the readiness probe compares filenames. `_LEDGER_FILE` is excluded because it
    is never recorded in the ledger it creates.
    """
    names = sorted(
        path.name
        for path in Path(settings.sql_migrations_dir).glob("*.sql")
        if path.name != _LEDGER_FILE
    )
    return names[-1] if names else None


def _statements(text: str) -> str:
    """A migration file reduced to the SQL it runs: `--` comments and blank lines removed.

    The drift guard protects an applied migration's effect, and a comment cannot change one, so
    comment edits must not refuse migrations. Line-oriented: only lines starting with `--` are
    dropped, never a trailing comment, so a `--` inside a string literal is never mangled. It
    under-strips, the safe direction.
    """
    return "\n".join(
        line for line in text.splitlines() if line.strip() and not line.lstrip().startswith("--")
    )


def _checksum(text: str) -> str:
    """SHA-256 of a migration file's *statements*, to detect edits after it was applied.

    File integrity rather than `stable_hash`, which keys identities over JSON.
    """
    return hashlib.sha256(_statements(text).encode()).hexdigest()


def _legacy_checksum(text: str) -> str:
    """The whole-file hash this ledger recorded before the guard became statement-only.

    A recorded checksum matching this is accepted once and rewritten to the statement hash, so
    existing databases are not declared drifted.
    """
    return hashlib.sha256(text.encode()).hexdigest()


def _ms(seconds: float) -> str:
    """Render a seconds budget as the millisecond string `lock_timeout` expects."""
    return f"{int(seconds * 1000)}ms"


def migration_dsn() -> str:
    """The credential that owns the schema: `postgres_migration_dsn`, else the runtime one.

    One resolver so migrations and grant reconciliation always target the same database; the
    fallback keeps single-principal deployments (dev, CI, tests) working unconfigured.
    """
    return settings.postgres_migration_dsn or settings.postgres_dsn


def _log_server_warning(diagnostic: psycopg.errors.Diagnostic) -> None:
    """Log a `RAISE WARNING` a migration emitted; ignore the lower-severity notices.

    psycopg drops notices without a handler, and a migration may need to tell its operator something
    (for example a constraint deliberately left `NOT VALID`). WARNING and above only, so replayed
    `IF NOT EXISTS` notices do not bury it.
    """
    if diagnostic.severity_nonlocalized not in {"WARNING", "ERROR", "FATAL", "PANIC"}:
        return
    log_event(
        logger,
        "migrate.server_warning",
        "the database reported: %s",
        diagnostic.message_primary,
        level=logging.WARNING,
        severity=diagnostic.severity_nonlocalized,
    )


async def _warn_if_the_database_is_ahead(
    conn: psycopg.AsyncConnection[TupleRow], sources: dict[str, str]
) -> list[str]:
    """Log the ledger rows this image ships no file for; return their names.

    The apply loop iterates the image's files, so without this an image behind its database (for
    example after a rollback) would report itself up to date. A warning rather than a refusal: a
    rollback must be able to start, and the schema only moves forward. Set difference, so a deleted
    file is caught too, computed in Python because the apply order is a Python string sort, not the
    database's collation.
    """
    cursor = await conn.execute("SELECT filename FROM schema_migrations")
    unknown = sorted({str(row[0]) for row in await cursor.fetchall()} - set(sources))
    if unknown:
        log_event(
            logger,
            "migrate.database_ahead",
            "this database records %d migration(s) this image does not ship (newest: %s) — "
            "the schema is ahead of this image, which is expected after a rollback and means "
            "'already up to date' would be a false reading",
            len(unknown),
            unknown[-1],
            level=logging.WARNING,
            unknown=len(unknown),
            newest_unknown=unknown[-1],
        )
    return unknown


async def migrate(dsn: str | None = None) -> list[str]:
    """Apply every not-yet-applied `infra/sql/*.sql` file in order; return the names applied.

    Idempotent: files recorded in `schema_migrations` are skipped, so re-running applies nothing and
    returns `[]`. Raises `MigrationError` if a previously applied file's checksum no longer matches
    — an edited migration must become a new file, never a silent in-place change.
    """
    target = dsn if dsn is not None else migration_dsn()
    applied: list[str] = []
    started = time.perf_counter()
    # All files read up front in one thread hop. `connect`, not the pool: a migration needs its own
    # connection with no statement timeout, and the advisory lock below is scoped to it.
    sources = await asyncio.to_thread(_read_sql_files)
    # Checked before connecting, so a mis-set `sql_migrations_dir` fails as a named configuration
    # error rather than a `KeyError` inside the transaction.
    if _LEDGER_FILE not in sources:
        raise MigrationError(
            f"no {_LEDGER_FILE} in {settings.sql_migrations_dir!r} — that file "
            f"bootstraps the ledger every migration is tracked against, and {len(sources)} other "
            "file(s) were found beside it. Check sql_migrations_dir "
            "(CHEMCLAW_SQL_MIGRATIONS_DIR) and that the migrations directory ships in this image."
        )
    async with await connect(target) as conn:
        # Wait generously for a peer migrator, then tightly for each table lock. Order matters: the
        # short DDL budget on the advisory lock would fail ordinary concurrent deploys, and the long
        # one on DDL would let an `ALTER TABLE` block live traffic for minutes.
        conn.add_notice_handler(_log_server_warning)
        await conn.execute(_SET_LOCAL_TIMEOUT, (_ms(settings.pg_migration_lock_wait_seconds),))
        # Announced before the wait, so a hook Job blocked on a peer migrator says so in its log.
        log_event(
            logger,
            "migrate.waiting",
            "waiting for the migration advisory lock (up to %.0fs)",
            settings.pg_migration_lock_wait_seconds,
            lock_wait_budget_s=settings.pg_migration_lock_wait_seconds,
            files=len(sources),
        )
        try:
            await conn.execute("SELECT pg_advisory_xact_lock(%s)", (_MIGRATION_LOCK_KEY,))
        except psycopg.errors.LockNotAvailable as exc:
            # A peer migrator holding the lock past its budget is normal concurrency (the hook Job
            # retries), so it gets a named error rather than a raw `LockNotAvailable` traceback.
            # Only around this statement: the same error on a DDL statement below means a table lock
            # queued behind live traffic and keeps its own error.
            raise MigrationError(
                f"another migrator held the migration lock for the whole "
                f"{settings.pg_migration_lock_wait_seconds:.0f}s budget "
                "(CHEMCLAW_PG_MIGRATION_LOCK_WAIT_SECONDS) — an overlapping deploy or a concurrent "
                "`make db-migrate`. Nothing was applied and nothing is half-applied; re-run once "
                "the other migrator finishes."
            ) from exc
        log_event(
            logger,
            "migrate.locked",
            "hold the migration lock after %.3fs; applying up to %d file(s)",
            time.perf_counter() - started,
            len(sources),
            waited_s=round(time.perf_counter() - started, 3),
            files=len(sources),
        )
        await conn.execute(_SET_LOCAL_TIMEOUT, (_ms(settings.pg_migration_lock_timeout_seconds),))
        # Bootstrap the ledger before anything can be tracked against it.
        await conn.execute(sources[_LEDGER_FILE])
        await _warn_if_the_database_is_ahead(conn, sources)
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
                if row[0] == _legacy_checksum(text):
                    # Recorded under the legacy whole-file hash: upgrade the row to the statement
                    # hash in place.
                    await conn.execute(
                        "UPDATE schema_migrations SET checksum = %s WHERE filename = %s",
                        (checksum, name),
                    )
                elif row[0] != checksum:
                    raise MigrationError(
                        f"migration {name} was edited after being applied "
                        f"(its statements differ, not just its comments); "
                        f"add a new migration file instead"
                    )
                continue
            # Logged before it runs: a statement waiting on `ACCESS EXCLUSIVE` blocks the table, so
            # the last "applying" line names the file to look at.
            log_event(logger, "migrate.applying", "applying %s", name, file=name)
            file_started = time.perf_counter()
            await conn.execute(text)
            await conn.execute(
                "INSERT INTO schema_migrations (filename, checksum) VALUES (%s, %s)",
                (name, checksum),
            )
            applied.append(name)
            log_event(
                logger,
                "migrate.applied",
                "applied %s in %.0fms",
                name,
                (time.perf_counter() - file_started) * 1000,
                file=name,
                duration_ms=round((time.perf_counter() - file_started) * 1000, 1),
            )
        await conn.commit()
    log_event(
        logger,
        "migrate.finished",
        "applied %d migration(s) in %.0fms",
        len(applied),
        (time.perf_counter() - started) * 1000,
        applied=len(applied),
        duration_ms=round((time.perf_counter() - started) * 1000, 1),
    )
    return applied


if __name__ == "__main__":
    # The hook Job's only reader is a log collector, so configure logging before anything can be
    # logged — without this the module's own records go nowhere and the run is as silent as it was.
    configure_logging()
    names = asyncio.run(migrate())
    # Reports only what was applied; `migrate.database_ahead` warns when the database has more.
    print(f"applied migrations: {', '.join(names) or '(none)'}")
