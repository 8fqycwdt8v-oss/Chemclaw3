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
recorded in `schema_migrations`. This is the same mechanism `agent/audit_store.py` used to keep two
appends from forking the audit hash chain, which is what made its absence here conspicuous: the
audit writer serialized its *inserts* and the migrator did not serialize its *DDL*. That chain and
its lock are gone (D-2026-08-14); this lock is not, because DDL still races.

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

**It reports what it applied; it does not certify that the database matches this image.** The apply
loop iterates the *image's* files, so before this ran a ledger row with no corresponding file was
never looked at and an image behind its database reported "already up to date" — see
`_warn_if_the_database_is_ahead`, which is a WARNING rather than a refusal because a rollback must
still be able to start. The forward mismatch (an image *ahead* of its schema, which cannot serve a
turn at all) is answered where a rollout can act on it, by `api/routes/ops.py`'s readiness probe
over `newest_shipped_migration`.

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


def newest_shipped_migration() -> str | None:
    """The newest migration filename **this image** ships, or None if it ships none but the ledger.

    Sorted by the whole filename, which is the order `migrate` applies in and the key
    `schema_migrations` records — so "newest shipped" and "last applied by a full run" are the same
    string, and neither depends on a Postgres collation.

    Names only: `Path.glob` is one directory read and no file is opened, because the one caller
    outside this module (`api/routes/ops.py`'s readiness probe) wants a filename to compare against
    the ledger and has no use for a megabyte of SQL.

    `_LEDGER_FILE` is excluded because it is the one file that is never recorded in the ledger it
    creates. Including it would make an image shipping *only* the bootstrap file report a newest
    migration that no database can ever have applied — a readiness check that can only fail.
    """
    names = sorted(
        path.name
        for path in Path(settings.sql_migrations_dir).glob("*.sql")
        if path.name != _LEDGER_FILE
    )
    return names[-1] if names else None


def _statements(text: str) -> str:
    """A migration file reduced to the SQL it runs: `--` comments and blank lines removed.

    What the drift guard is protecting is that an applied migration's *effect* never changes. A
    comment cannot change an effect, and hashing the whole file made every comment edit a
    deployment outage — `make db-migrate` refuses on any database that already applied the file,
    while CI, which always starts from an empty one, stays green. It happened twice from two
    different sessions: `006_audit_events.sql` was corrected on 2026-08-01 to name the module that
    the D-148 package move had renamed, and `031_bo_campaigns.sql` on 2026-08-05 to say that two
    columns hold the lead objective only. Both edits were *right*, and both broke migrations for
    four days and an hour respectively.

    Line-oriented rather than SQL-aware on purpose: a `--` inside a string literal would be
    mangled by a naive strip, so only lines whose first non-space characters are `--` are dropped,
    never a trailing comment on a statement line. That under-strips and never over-strips, which is
    the safe direction — the worst case is that a trailing-comment edit still trips the guard,
    which is the behaviour we had.
    """
    return "\n".join(
        line for line in text.splitlines() if line.strip() and not line.lstrip().startswith("--")
    )


def _checksum(text: str) -> str:
    """SHA-256 of a migration file's *statements*, to detect edits after it was applied.

    File integrity, deliberately not `chemclaw.core.ids.stable_hash` (which is for
    content-addressed *identity* keys over JSON) — here the bytes are what matter, minus the ones
    that cannot run (see `_statements`).
    """
    return hashlib.sha256(_statements(text).encode()).hexdigest()


def _legacy_checksum(text: str) -> str:
    """The whole-file hash this ledger recorded before the guard became statement-only.

    Kept so an existing database is not declared drifted by the fix itself: a recorded checksum
    that matches this is accepted once and rewritten to the statement hash, after which the file's
    comments are free to change. Without it, every row in every deployed `schema_migrations` would
    mismatch on the first run after this change — turning a fix for an outage into a bigger one.
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


async def _warn_if_the_database_is_ahead(
    conn: psycopg.AsyncConnection[TupleRow], sources: dict[str, str]
) -> list[str]:
    """Log the ledger rows this image ships no file for; return their names.

    **The apply loop iterates the *image's* files, so a ledger row with no corresponding file is
    never looked at.** Measured: an image shipping through `080` against a database at `091`
    applied nothing, returned `[]`, and printed "(none — already up to date)" over eleven
    migrations it has never heard of. That is precisely the state an operator is in immediately
    after a rollback, and it is precisely when they run the documented recovery command to find out
    whether the schema matches the image.

    **A warning, not a refusal, and that direction is the decision.** A rollback has to be able to
    start — the release that is rolling back is the one that cannot serve — and this schema only
    ever goes forward, by a merged decision, so there is nothing here for the migrator to undo. The
    whole defect is that the mismatch was *silent*; one WARNING line naming the count and the
    newest unknown filename is what turns it into something an operator can act on.

    Set difference rather than "newer than the newest file I ship", so a *deleted* migration file
    is caught by the same line. Both are the same claim — the ledger records work this image has no
    record of — and `infra/sql` is append-only, so on any tree where a file was not removed the two
    formulations agree.

    Computed in Python against `sorted(sources)`'s own comparator rather than by a `WHERE filename
    > …` predicate, because Postgres would compare under the database's collation and this module's
    apply order is a Python string sort. A ledger of ~90 short filenames is one small round trip.
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

    Idempotent: files recorded in `schema_migrations` are skipped, so re-running applies
    nothing and returns `[]`. Raises `MigrationError` if a previously applied file's
    checksum no longer matches — an edited migration must become a new file, never a
    silent in-place change.
    """
    target = dsn if dsn is not None else migration_dsn()
    applied: list[str] = []
    started = time.perf_counter()
    # Every file read up front, in one worker thread rather than ~30 hops. `connect`, not
    # `connection`: a migration wants its own connection with no statement timeout (an index build
    # may run long), which is precisely what the pool must not hand out to a request path — and it
    # must be a connection nobody else can be handed, because the advisory lock below is scoped to
    # it.
    sources = await asyncio.to_thread(_read_sql_files)
    # **Before the connect, so a mis-set path fails as a configuration error rather than as a
    # `KeyError` deep inside a transaction.** The ledger bootstrap below subscripts `sources`
    # directly, and an empty or wrong `sql_migrations_dir` made that a bare
    # `KeyError: '000_schema_migrations.sql'` in a `pre-install` hook Job's log — naming neither
    # the directory searched nor the setting that points at it. `core/grants.py::apply_grants`
    # already handles the identical misconfiguration by name ("An empty directory is an error, not
    # a successful no-op"); this is the same rule for the same directory.
    if _LEDGER_FILE not in sources:
        raise MigrationError(
            f"no {_LEDGER_FILE} in {settings.sql_migrations_dir!r} — that file "
            f"bootstraps the ledger every migration is tracked against, and {len(sources)} other "
            "file(s) were found beside it. Check sql_migrations_dir "
            "(CHEMCLAW_SQL_MIGRATIONS_DIR) and that the migrations directory ships in this image."
        )
    async with await connect(target) as conn:
        # Wait generously for a peer migrator, then tightly for every table lock after it. The
        # order matters: taking the advisory lock under the 5 s DDL budget would make an ordinary
        # concurrent deploy fail, and doing the DDL under the 300 s budget would let one ALTER
        # TABLE queue in front of live traffic for five minutes.
        await conn.execute(_SET_LOCAL_TIMEOUT, (_ms(settings.pg_migration_lock_wait_seconds),))
        # Announced *before* the wait, which is the whole point: this runs as a
        # `pre-install,pre-upgrade` hook Job, and a deploy blocked on a peer migrator for the full
        # 300 s budget produced byte-identical output to one running normally — nothing at all,
        # until a single `print` after everything had already completed. "Which migration is it
        # stuck on?" is the first question an operator asks of a stalled release, and until this
        # the answer was not in the logs at any level.
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
            # The *normal* concurrency case — two overlapping deploys, or `make db-migrate` run
            # during one — surfaced as a raw `psycopg.errors.LockNotAvailable: canceling statement
            # due to lock timeout` traceback naming this line. The hook Job's `backoffLimit: 3`
            # means it self-heals, so the only thing the traceback ever bought was an operator
            # reading a crash where the system was working as designed.
            #
            # Narrow, and only around this statement: the same `lock_timeout` bounds every DDL
            # statement below, where `LockNotAvailable` means a *table* lock queued behind live
            # traffic — a different event with a different budget, and one that must keep its own
            # error rather than be reported as a peer migrator.
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
                    # Recorded before the guard became statement-only. The file is unchanged in
                    # every way that matters *and* in every way that does not, so upgrade the row
                    # in place rather than making the reader prove it again next time.
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
            # Named before it runs, not after: a `CREATE INDEX` or an `ALTER TABLE` queued behind
            # a long read holds `ACCESS EXCLUSIVE` and takes the table down while it waits, so the
            # file whose name is missing from the tail of the log is exactly the one to look at.
            # An "applied" line printed afterwards can only ever name the files that finished.
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
    # Not "(none — already up to date)": this line is the documented recovery command's whole
    # output, and it asserted a match the run had never checked. What it can honestly say is
    # what it did; `migrate.database_ahead` above says when that is not the same thing.
    print(f"applied migrations: {', '.join(names) or '(none)'}")
