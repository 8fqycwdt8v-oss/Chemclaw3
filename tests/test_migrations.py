"""The migration reader finds the SQL files, offline and with no database (D-148).

`_read_sql_files` located `infra/sql/` as `Path(__file__).parent.parent` — correct only while the
module sat at the repository root, where that expression happened to resolve there. D-148 moved it
two levels deeper, into the calc package, and the path silently became a directory *inside* the
package. `glob` on a non-existent directory raises nothing and yields nothing, so `make db-migrate`
did not fail on a bad path — it applied zero migrations, and CI only caught it because the
integration tests that follow found no schema.

The module has since moved again, to `chemclaw.core.migrate`, where the schema's runner belongs
(D-2026-08-05-append-only-by-grant-not-by-contract). That move needed no change to
`sql_migrations_dir`, which is the property these tests exist to keep.

That is the failure mode worth a test: not "the migrations are wrong" but "there are no migrations
and nobody said so". These run offline and touch no database, so they fail on the commit that
breaks the path rather than on the first job that needs the schema.
"""

from pathlib import Path

import pytest

from chemclaw.core.config import settings
from chemclaw.core.migrate import _LEDGER_FILE, MigrationError, _read_sql_files

_REPO_ROOT = Path(__file__).resolve().parents[1]


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
    """Pointing at the wrong directory must be visible, not read as "nothing to migrate".

    This is the guard the original bug walked straight through. `_read_sql_files` returning `{}` is
    indistinguishable from a fully-migrated database at the call site, so the emptiness has to be
    caught here rather than shrugged off downstream.
    """
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(settings, "sql_migrations_dir", str(tmp_path))
    try:
        assert _read_sql_files() == {}
    finally:
        monkeypatch.undo()


def test_the_error_type_exists_for_a_drifted_migration() -> None:
    """`MigrationError` is what an edited-after-applied file raises; keep it importable."""
    assert issubclass(MigrationError, RuntimeError)


# --- the two locks (a readiness-review finding) ---------------------------------------------


class _RecordingCursor:
    """Enough of a psycopg cursor for `migrate`'s one query: "has this file been applied?"."""

    async def fetchone(self) -> None:
        """Nothing is applied yet, so every file is new."""
        return None


class _RecordingConnection:
    """Records the statements `migrate` issues, in order, without a database.

    A recording double rather than a real connection because the property under test is the
    *sequence* — which budget is in force when the advisory lock is taken, and which when the DDL
    runs. Getting that backwards is silently wrong against a live database and invisible against an
    idle one, which is exactly the kind of thing an integration test that only ever runs on an empty
    CI database would never catch.
    """

    def __init__(self) -> None:
        """Start with an empty log; `statements` is what the assertions read."""
        self.statements: list[tuple[str, tuple[object, ...] | None]] = []
        self.committed = False

    async def execute(self, sql: str, params: tuple[object, ...] | None = None) -> _RecordingCursor:
        """Log the statement and hand back a cursor that reports nothing applied."""
        self.statements.append((sql, params))
        return _RecordingCursor()

    async def commit(self) -> None:
        """The single commit that ends the one transaction the whole run happens in."""
        self.committed = True

    async def __aenter__(self) -> "_RecordingConnection":
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None


def _run_against_recorder(monkeypatch: pytest.MonkeyPatch) -> _RecordingConnection:
    """Drive `migrate` against a recording connection and return what it issued."""
    import asyncio

    from chemclaw.core import migrate as module

    conn = _RecordingConnection()

    async def _connect(_dsn: str) -> _RecordingConnection:
        return conn

    monkeypatch.chdir(_REPO_ROOT)
    monkeypatch.setattr(module, "connect", _connect)
    asyncio.run(module.migrate("postgresql://recorder/none"))
    return conn


def test_migrators_are_serialized_by_an_advisory_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two migrators running at once had nothing stopping them from interleaving DDL.

    `agent/audit_store.py` takes exactly this lock to keep two appends from forking the hash chain
    (`infra/sql/011`), which is what made the absence conspicuous: the audit writer serialized its
    *inserts* and the migrator did not serialize its *DDL*. Transaction-scoped, so the single commit
    at the end of the run releases it and there is no path that leaks it.
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
    """The whole reason there are two settings, and the order they have to be applied in.

    Taking the advisory lock under the 5 s DDL budget would fail an ordinary concurrent deploy —
    waiting for a peer migrator is a legitimate event. Running the DDL under the 300 s peer budget
    would let one `ALTER TABLE` sit in the lock queue for five minutes, and because Postgres's lock
    queue is FIFO, *every* later query on that table queues behind it: the migration takes the table
    down while waiting rather than merely being slow.
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
    """`lock_timeout` bounds the *wait*; the work stays unbounded, and that is the point.

    A `CREATE INDEX` may legitimately build for minutes once it has its lock. Capping that with a
    `statement_timeout` would break migrations that are behaving correctly, which is why this module
    connects without one and why the new setting is a different knob rather than a smaller value of
    the old one.
    """
    import inspect

    from chemclaw.core import migrate as module

    source = inspect.getsource(module.migrate)
    assert "statement_timeout" not in source, (
        "the migration connection took a statement timeout, which bounds an index build rather "
        "than the lock wait it was meant to bound"
    )
