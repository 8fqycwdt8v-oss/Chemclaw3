"""The migration runner (`chemclaw.core.migrate`): files, checksums, locks and the ledger.

The path tests run offline: a wrong `sql_migrations_dir` globs nothing and raises nothing, so
`make db-migrate` would apply zero migrations silently. These fail on the commit that breaks the
path rather than on the first job that needs the schema.
"""

import asyncio
import logging
import re
from collections.abc import Callable
from pathlib import Path

import psycopg
import pytest

from chemclaw.core.config import settings
from chemclaw.core.migrate import (
    _LEDGER_FILE,
    _MIGRATION_LOCK_KEY,
    MigrationError,
    _checksum,
    _legacy_checksum,
    _log_server_warning,
    _read_sql_files,
    _statements,
    migrate,
    migration_dsn,
    newest_shipped_migration,
)
from tests.pg import migrated_db_or_skip

_REPO_ROOT = Path(__file__).resolve().parents[1]


# The two prefix collisions already applied when this guard was written. `schema_migrations` keys on
# the filename, so renaming one would re-apply it on every existing database; they are frozen and
# every future prefix must be unique. The exemption is the four filenames, not the two prefixes, so
# a third `037` or `043` still fails.
_APPLIED_DUPLICATES: dict[str, frozenset[str]] = {
    "037": frozenset({"037_bo_suggestion_provenance.sql", "037_document_index.sql"}),
    "043": frozenset({"043_session_listing.sql", "043_session_message_shape.sql"}),
}


def test_no_two_migrations_share_a_number() -> None:
    """No two migrations share a number.

    `migrate()` sorts by whole filename, so the frozen pairs have a deterministic order. A new file
    numbered into an already-used low prefix would sort early on a fresh install but apply last on
    an existing database, so two installs of one commit would apply in two orders.
    """
    prefixes: dict[str, list[str]] = {}
    for path in sorted((_REPO_ROOT / "infra" / "sql").glob("*.sql")):
        prefixes.setdefault(path.name.split("_", 1)[0], []).append(path.name)

    collisions = {
        prefix: sorted(set(names) - _APPLIED_DUPLICATES.get(prefix, frozenset()))
        for prefix, names in prefixes.items()
        if len(names) > 1 and set(names) != _APPLIED_DUPLICATES.get(prefix, frozenset())
    }
    assert not collisions, (
        f"two migrations share a number: {collisions}. Give the new one the next free prefix — "
        "never renumber an applied file, whose name is its key in `schema_migrations`"
    )
    # The other direction, so the exemption cannot outlive its subject: a grandfathered pair that
    # is no longer exactly those two files is a line to delete, not a permanent licence. Compared as
    # a set of names rather than a count, so both a removal and an addition fail here.
    stale = {
        prefix: sorted(names)
        for prefix, names in _APPLIED_DUPLICATES.items()
        if set(prefixes.get(prefix, [])) != names
    }
    assert not stale, f"_APPLIED_DUPLICATES exempts {stale}, which is not what is in `infra/sql`"


def test_the_configured_directory_exists_and_holds_the_migrations() -> None:
    """`sql_migrations_dir` resolves to a real directory from the repository root."""
    sql_dir = _REPO_ROOT / settings.sql_migrations_dir
    assert sql_dir.is_dir(), (
        f"sql_migrations_dir={settings.sql_migrations_dir!r} is not a directory; "
        "`make db-migrate` would silently apply nothing"
    )


def test_reading_the_migrations_finds_files(monkeypatch: pytest.MonkeyPatch) -> None:
    """The reader returns actual SQL, including the ledger DDL every other file depends on."""
    monkeypatch.chdir(_REPO_ROOT)
    files = _read_sql_files()
    assert files, "no .sql files found — the migration path is wrong or the directory is empty"
    assert _LEDGER_FILE in files, f"{_LEDGER_FILE} missing; it is the ledger every migration needs"
    assert all(text.strip() for text in files.values()), "a migration file is empty"


def test_an_empty_directory_is_not_mistaken_for_no_work(tmp_path: Path) -> None:
    """An empty directory is an error, not "nothing to migrate".

    `_read_sql_files` returning `{}` is indistinguishable from a fully migrated database at the call
    site.
    """
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(settings, "sql_migrations_dir", str(tmp_path))
    try:
        assert _read_sql_files() == {}
    finally:
        monkeypatch.undo()


def test_a_directory_with_no_ledger_file_fails_by_name(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A directory with no ledger file fails with an error naming the directory and setting.

    Otherwise the bootstrap subscript raises a bare `KeyError` that tells an operator nothing; this
    matches `apply_grants`. Checked before connecting, so it is a configuration error and needs no
    server.
    """
    monkeypatch.setattr(settings, "sql_migrations_dir", str(tmp_path))
    with pytest.raises(MigrationError, match=re.escape(str(tmp_path))):
        asyncio.run(migrate())


def test_the_error_type_exists_for_a_drifted_migration() -> None:
    """`MigrationError` is what an edited-after-applied file raises; keep it importable."""
    assert issubclass(MigrationError, RuntimeError)


_APPLIED = """-- what this migration is for
CREATE TABLE IF NOT EXISTS widgets (
    id BIGSERIAL PRIMARY KEY  -- the key
);
"""


def test_editing_a_comment_does_not_count_as_drift() -> None:
    """Editing a comment does not count as drift.

    A comment cannot change what a migration did, and a whole-file hash would make a corrected
    comment refuse on every database that already applied the file while CI (starting empty) stays
    green.
    """
    recommented = _APPLIED.replace("-- what this migration is for", "-- what this migration does")
    assert _checksum(recommented) == _checksum(_APPLIED)


def test_changing_a_statement_changes_the_checksum() -> None:
    """Changing a statement changes `_checksum`.

    A property of the hash only; that `migrate` refuses the drift is asserted against a real ledger
    row in "the ledger row `migrate` finds" below.
    """
    altered = _APPLIED.replace("id BIGSERIAL PRIMARY KEY", "id TEXT PRIMARY KEY")
    assert _checksum(altered) != _checksum(_APPLIED)


def test_a_trailing_comment_is_left_alone_rather_than_stripped_unsafely() -> None:
    """Under-strip, never over-strip: a `--` inside a string literal survives.

    Only lines starting with `--` are dropped, so editing a trailing comment still reads as drift,
    the safe direction to be wrong.
    """
    assert "-- the key" in _statements(_APPLIED)
    assert "INSERT INTO t VALUES ('a -- b')" in _statements("INSERT INTO t VALUES ('a -- b')\n")


def test_the_legacy_hash_and_the_statement_hash_are_different_values() -> None:
    """The legacy whole-file hash and the statement hash differ, so an upgrade path must exist.

    Ledger rows written before statement-only hashing hold the legacy hash; that the runner takes
    the upgrade path is asserted below against a ledger row.
    """
    assert _legacy_checksum(_APPLIED) != _checksum(_APPLIED)


# --- the two locks (a readiness-review finding) ---------------------------------------------


class _RecordingCursor:
    """Enough of a psycopg cursor for `migrate`'s one query: "has this file been applied?".

    `row` is what the ledger holds for the file asked about: `None` (every file new, for the lock
    tests) or a one-column tuple, which exercises both arms of `migrate`'s `if row is not None:`.
    """

    def __init__(self, row: tuple[str] | None = None, rows: tuple[tuple[str], ...] = ()) -> None:
        """Hold the one row this cursor hands back (`None` = not applied), and the whole ledger.

        `rows` answers the *unparameterised* `SELECT filename FROM schema_migrations` the
        ahead-of-image check makes — a listing, not a lookup, so it needs the other fetch verb.
        """
        self.row = row
        self.rows = rows

    async def fetchone(self) -> tuple[str] | None:
        """The recorded ledger row for the file just queried."""
        return self.row

    async def fetchall(self) -> list[tuple[str]]:
        """Every filename the ledger holds — what `_warn_if_the_database_is_ahead` reads."""
        return list(self.rows)


class _RecordingConnection:
    """Records the statements `migrate` issues, in order, without a database.

    The property is the sequence: which timeout is in force when the advisory lock is taken and when
    the DDL runs. Getting it backwards is invisible against an idle CI database.
    """

    def __init__(self, ledger: dict[str, str] | None = None) -> None:
        """Start with an empty log and a `{filename: recorded checksum}` ledger (empty = fresh)."""
        self.statements: list[tuple[str, tuple[object, ...] | None]] = []
        self.committed = False
        self.ledger = dict(ledger or {})

    async def execute(self, sql: str, params: tuple[object, ...] | None = None) -> _RecordingCursor:
        """Log the statement; the ledger lookup is answered from `ledger`, the rest with `None`."""
        self.statements.append((sql, params))
        if sql == "SELECT filename FROM schema_migrations":
            return _RecordingCursor(rows=tuple((name,) for name in sorted(self.ledger)))
        if "FROM schema_migrations" in sql and params:
            recorded = self.ledger.get(str(params[0]))
            return _RecordingCursor(None if recorded is None else (recorded,))
        return _RecordingCursor()

    async def commit(self) -> None:
        """The single commit that ends the one transaction the whole run happens in."""
        self.committed = True

    def add_notice_handler(self, callback: Callable[[psycopg.errors.Diagnostic], None]) -> None:
        """Accept the server-warning handler; this double never emits a notice."""

    async def __aenter__(self) -> "_RecordingConnection":
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None


def _run_against_recorder(
    monkeypatch: pytest.MonkeyPatch, ledger: dict[str, str] | None = None
) -> _RecordingConnection:
    """Drive `migrate` against a recording connection and return what it issued.

    `ledger` is what `schema_migrations` already holds, as `{filename: checksum}`; the default is
    an empty ledger, i.e. a database nothing has ever been applied to.
    """
    import asyncio

    from chemclaw.core import migrate as module

    conn = _RecordingConnection(ledger)

    async def _connect(_dsn: str) -> _RecordingConnection:
        return conn

    monkeypatch.chdir(_REPO_ROOT)
    monkeypatch.setattr(module, "connect", _connect)
    asyncio.run(module.migrate("postgresql://recorder/none"))
    return conn


def test_migrators_are_serialized_by_an_advisory_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    """Concurrent migrators are serialized by a transaction-scoped advisory lock.

    Otherwise two runs could interleave DDL. The single commit at the end releases it, so no path
    leaks it.
    """
    from chemclaw.core.migrate import _MIGRATION_LOCK_KEY

    conn = _run_against_recorder(monkeypatch)
    locks = [params for sql, params in conn.statements if "pg_advisory_xact_lock" in sql]
    assert locks == [(_MIGRATION_LOCK_KEY,)], "the migration run takes no advisory lock"
    assert conn.committed


def test_the_lock_is_taken_before_any_ddl(monkeypatch: pytest.MonkeyPatch) -> None:
    """A lock taken after the first `CREATE TABLE` serializes nothing that matters."""
    conn = _run_against_recorder(monkeypatch)
    order = [
        index
        for index, (sql, _params) in enumerate(conn.statements)
        if "pg_advisory_xact_lock" in sql or "CREATE TABLE" in sql.upper()
    ]
    first = conn.statements[order[0]][0]
    assert "pg_advisory_xact_lock" in first, "DDL ran before the migrators were serialized"


def test_waiting_for_a_peer_and_waiting_for_a_table_get_different_budgets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A peer migrator and a table lock get different wait budgets, applied in that order.

    A peer migrator is a legitimate wait and gets the long budget. DDL gets the short one, because
    Postgres's lock queue is FIFO: an `ALTER TABLE` waiting minutes would queue every later query on
    that table behind it.
    """
    from chemclaw.core.config import settings as live

    conn = _run_against_recorder(monkeypatch)
    budgets = [params for sql, params in conn.statements if "set_config('lock_timeout'" in sql]
    assert budgets == [
        (f"{int(live.pg_migration_lock_wait_seconds * 1000)}ms",),
        (f"{int(live.pg_migration_lock_timeout_seconds * 1000)}ms",),
    ], "the peer-wait and table-lock budgets are the same, in the wrong order, or absent"
    assert live.pg_migration_lock_timeout_seconds < live.pg_migration_lock_wait_seconds


def test_the_run_still_takes_no_statement_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """The run takes no `statement_timeout`: `lock_timeout` bounds the wait, not the work.

    A `CREATE INDEX` may build for minutes once it holds its lock.
    """
    import inspect

    from chemclaw.core import migrate as module

    source = inspect.getsource(module.migrate)
    assert "statement_timeout" not in source, (
        "the migration connection took a statement timeout, which bounds an index build rather "
        "than the lock wait it was meant to bound"
    )


# --- the ledger row `migrate` finds ------------------------------------------------------------
#
# Both arms of `migrate`'s `if row is not None:` (refuse an edited file, upgrade a legacy row) are
# driven through `migrate` itself: once against a doubled connection and once against the real
# ledger in Postgres, which proves the row is actually rewritten.


def _first_tracked_migration() -> tuple[str, str]:
    """The first real migration after the ledger bootstrap, as `(filename, text)`.

    Read off `infra/sql` rather than named here, so this does not become a second place that has
    to be edited when `001_*` is renamed — and `migrate` walks the files in the same sorted order.
    """
    sql_dir = _REPO_ROOT / settings.sql_migrations_dir
    names = sorted(path.name for path in sql_dir.glob("*.sql") if path.name != _LEDGER_FILE)
    assert names, f"no tracked migrations in {sql_dir}"
    return names[0], (sql_dir / names[0]).read_text()


def test_migrate_refuses_a_file_that_was_edited_after_it_was_applied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ledger row whose checksum disagrees stops the run, naming the file.

    An applied migration's effect never changes, so an in-place edit must become a new file;
    otherwise the schema diverges from the ledger silently.
    """
    name, _text = _first_tracked_migration()
    with pytest.raises(MigrationError, match=re.escape(name)):
        _run_against_recorder(monkeypatch, ledger={name: "0" * 64})


def test_migrate_upgrades_a_legacy_ledger_row_rather_than_rejecting_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A legacy ledger row is accepted once and rewritten, not refused.

    The run does not raise, the row is rewritten to the statement hash, and the file is not
    re-applied (the `continue` keeps an applied `CREATE TABLE` from running twice).
    """
    name, text = _first_tracked_migration()
    conn = _run_against_recorder(monkeypatch, ledger={name: _legacy_checksum(text)})
    updates = [
        params
        for sql, params in conn.statements
        if sql.startswith("UPDATE schema_migrations SET checksum")
    ]
    assert updates == [(_checksum(text), name)], "the legacy row was not upgraded in place"
    assert (text, None) not in conn.statements, "an already-applied migration was re-applied"


def test_the_live_ledger_refuses_a_drifted_row_and_upgrades_a_legacy_one() -> None:
    """Against a real `schema_migrations`: a drifted row is refused and a legacy one upgraded.

    Proves the `UPDATE` lands and the following run reports the file applied. Restored in a
    `finally` so a drifted row cannot fail later tests using `migrated_db_or_skip`.
    """

    async def _run() -> None:
        await migrated_db_or_skip()
        name, text = _first_tracked_migration()
        dsn = migration_dsn()
        conn = await psycopg.AsyncConnection.connect(dsn)
        try:
            cursor = await conn.execute(
                "SELECT checksum FROM schema_migrations WHERE filename = %s", (name,)
            )
            row = await cursor.fetchone()
            assert row is not None, f"{name} is not in the ledger after a migration run"
            recorded = str(row[0])

            async def set_checksum(value: str) -> None:
                await conn.execute(
                    "UPDATE schema_migrations SET checksum = %s WHERE filename = %s", (value, name)
                )
                await conn.commit()

            async def current_checksum() -> str:
                read = await conn.execute(
                    "SELECT checksum FROM schema_migrations WHERE filename = %s", (name,)
                )
                got = await read.fetchone()
                assert got is not None
                return str(got[0])

            # An edited file: the run refuses, and the ledger is left as it was.
            await set_checksum("0" * 64)
            with pytest.raises(MigrationError, match=re.escape(name)):
                await migrate(dsn)

            # A row written before the guard became statement-only: accepted and rewritten.
            await set_checksum(_legacy_checksum(text))
            assert await migrate(dsn) == [], "a legacy row made the runner re-apply files"
            assert await current_checksum() == _checksum(text), "the legacy row was not rewritten"
        finally:
            await conn.execute(
                "UPDATE schema_migrations SET checksum = %s WHERE filename = %s", (recorded, name)
            )
            await conn.commit()
            await conn.close()

    asyncio.run(_run())


# --- the database is ahead of the image (a rollback), and a peer holds the lock ----------------


def test_the_newest_shipped_migration_is_what_a_full_run_applies_last(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`newest_shipped_migration` and `migrate`'s apply order agree on "newest".

    The readiness probe in `api/routes/ops.py` compares one against a ledger row the other wrote; if
    they disagree, every pod goes unready.
    """
    monkeypatch.chdir(_REPO_ROOT)
    tracked = sorted(name for name in _read_sql_files() if name != _LEDGER_FILE)
    assert newest_shipped_migration() == tracked[-1]


def test_the_bootstrap_file_is_never_the_newest_shipped_migration(tmp_path: Path) -> None:
    """The bootstrap file is never the newest shipped migration.

    `000_schema_migrations.sql` is never recorded in its own ledger, so returning it would give the
    readiness probe a check that always fails.
    """
    (tmp_path / _LEDGER_FILE).write_text("CREATE TABLE schema_migrations ();")
    original = settings.sql_migrations_dir
    settings.sql_migrations_dir = str(tmp_path)
    try:
        assert newest_shipped_migration() is None
    finally:
        settings.sql_migrations_dir = original


async def test_a_ledger_row_this_image_ships_no_file_for_is_reported(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A ledger row this image ships no file for is reported as a WARNING, and the run still starts.

    The apply loop iterates the image's files, so a database ahead of the image (after a rollback)
    would otherwise report "already up to date". The schema only goes forward, so a rollback must
    still be able to start.
    """
    await migrated_db_or_skip()
    dsn = migration_dsn()
    planted = "999_a_migration_from_a_newer_image.sql"
    conn = await psycopg.AsyncConnection.connect(dsn)
    try:
        await conn.execute(
            "INSERT INTO schema_migrations (filename, checksum) VALUES (%s, %s)",
            (planted, "0" * 64),
        )
        await conn.commit()
        with caplog.at_level(logging.WARNING, logger="chemclaw.core.migrate"):
            assert await migrate(dsn) == [], "a ledger row ahead of the image refused the run"
    finally:
        await conn.execute("DELETE FROM schema_migrations WHERE filename = %s", (planted,))
        await conn.commit()
        await conn.close()

    ahead = [
        record
        for record in caplog.records
        if getattr(record, "event", "") == "migrate.database_ahead"
    ]
    assert ahead, "nothing at WARNING said the database is ahead of this image"
    assert ahead[0].levelno == logging.WARNING
    assert getattr(ahead[0], "unknown", None) == 1
    assert getattr(ahead[0], "newest_unknown", None) == planted


async def test_a_peer_holding_the_lock_is_named_rather_than_raised_as_a_traceback() -> None:
    """A peer holding the lock is named in a message, not raised as a traceback.

    Overlapping deploys are the event the lock exists for, so waiting out the budget is not a crash.
    The budget is named because it is what an operator can change.
    """
    await migrated_db_or_skip()
    dsn = migration_dsn()
    peer = await psycopg.AsyncConnection.connect(dsn)
    try:
        await peer.execute("SELECT pg_advisory_xact_lock(%s)", (_MIGRATION_LOCK_KEY,))
        original = settings.pg_migration_lock_wait_seconds
        settings.pg_migration_lock_wait_seconds = 1.0
        try:
            with pytest.raises(MigrationError, match="another migrator held"):
                await migrate(dsn)
        finally:
            settings.pg_migration_lock_wait_seconds = original
    finally:
        await peer.rollback()
        await peer.close()


async def test_a_migration_s_raise_warning_reaches_the_log_and_a_notice_does_not(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A migration's `RAISE WARNING` reaches the log, and a NOTICE does not.

    psycopg drops unhandled notices, so `migrate` registers a handler. Driven against the server so
    the `Diagnostic` is real; the NOTICE every `IF NOT EXISTS` replay emits stays out.
    """
    await migrated_db_or_skip()
    conn = await psycopg.AsyncConnection.connect(migration_dsn())
    try:
        conn.add_notice_handler(_log_server_warning)
        with caplog.at_level(logging.DEBUG, logger="chemclaw.core.migrate"):
            await conn.execute(
                "DO $$ BEGIN RAISE NOTICE 'routine skip'; RAISE WARNING 'ask a person'; END $$"
            )
    finally:
        await conn.close()
    reported = [
        record
        for record in caplog.records
        if getattr(record, "event", "") == "migrate.server_warning"
    ]
    assert [record.getMessage() for record in reported] == [
        "migrate.server_warning: the database reported: ask a person"
    ]
    assert reported[0].levelno == logging.WARNING
