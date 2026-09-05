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

import asyncio
import re
from pathlib import Path

import pytest

from chemclaw.core.config import settings
from chemclaw.core.migrate import (
    _LEDGER_FILE,
    MigrationError,
    _checksum,
    _legacy_checksum,
    _read_sql_files,
    _statements,
    migrate,
)

_REPO_ROOT = Path(__file__).resolve().parents[1]


# The two collisions that were already applied when this guard was written, and the reason it is a
# ratchet rather than a flat rule. Both are applied everywhere this schema runs, and
# `schema_migrations` keys on the **filename**: renaming one would re-apply it on every existing
# database and fail on the objects it already created. So they are frozen here, and every future
# prefix has to be unique.
#
# **The exemption is the four filenames, not the two prefixes**, and it shipped as the prefixes —
# which exempted exactly the case the test below exists for. `037` and `043` are the only already-
# used low prefixes in the tree, so "a third file numbered into an already-used low prefix" could
# only ever be a third `037` or a third `043`, and a prefix-keyed exemption waves both through. The
# symmetric staleness check had the same hole: `< 2` fires when a grandfathered pair loses a member
# and never when it gains one.
_APPLIED_DUPLICATES: dict[str, frozenset[str]] = {
    "037": frozenset({"037_bo_suggestion_provenance.sql", "037_document_index.sql"}),
    "043": frozenset({"043_session_listing.sql", "043_session_message_shape.sql"}),
}


def test_no_two_migrations_share_a_number() -> None:
    """A duplicate prefix means one file set applied in two different orders, both reported as fine.

    `migrate()` sorts by the whole filename, so today's two pairs each have a deterministic order
    and are independent of each other (columns on `bo_suggestions` against new document tables;
    indexes against a new column) — the run is one transaction, so nothing can half-apply, and
    measured on a fresh database all 79 files apply cleanly in that order. Harmless, and nothing
    kept it so.

    What it is latent *for* is the next file numbered into an already-used low prefix: on a fresh
    install it sorts before everything from `038` on, and on an existing database it applies after
    everything, because the ledger has already recorded the rest. Two installs of the same commit,
    two orders, success reported both times — and a migration that depends on a later one is a
    failure only the fresh install sees.
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


def test_a_directory_with_no_ledger_file_fails_by_name(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A mis-set `sql_migrations_dir` must name itself, the way the grant applier already does.

    `migrate` bootstraps the ledger with `sources[_LEDGER_FILE]` and nothing guards the subscript,
    so an empty or wrong directory failed a `pre-install` hook Job with a bare
    `KeyError: '000_schema_migrations.sql'` — naming neither the directory that was searched nor
    the setting that points at it, and leaving an operator nothing to act on. `apply_grants`
    handles the identical misconfiguration explicitly ("An empty directory is an error, not a
    successful no-op") and names the path; this is the same rule at the same layer.

    Checked before the connect, so a mis-set path fails as the configuration error it is rather
    than as a `KeyError` inside an open transaction — and so this test needs no server.
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
    """The whole-file hash made a corrected comment an outage on every existing database.

    Twice, from two different sessions, and both edits were right: `006_audit_events.sql` renamed a
    module the D-148 package move had moved, `031_bo_campaigns.sql` recorded that two columns hold
    the lead objective only. Each made `make db-migrate` refuse on any database that had already
    applied the file, while CI stayed green because CI starts from an empty one. A comment cannot
    change what a migration did, so it cannot be drift.
    """
    recommented = _APPLIED.replace("-- what this migration is for", "-- what this migration does")
    assert _checksum(recommented) == _checksum(_APPLIED)


def test_changing_a_statement_still_counts_as_drift() -> None:
    """The guard has to keep the power the fix above could have cost it."""
    altered = _APPLIED.replace("id BIGSERIAL PRIMARY KEY", "id TEXT PRIMARY KEY")
    assert _checksum(altered) != _checksum(_APPLIED)


def test_a_trailing_comment_is_left_alone_rather_than_stripped_unsafely() -> None:
    """Under-strip, never over-strip: a `--` inside a string literal must survive.

    Only lines whose first non-space characters are `--` are dropped. The cost is that editing a
    *trailing* comment still reads as drift, which is the behaviour we already had and is the safe
    direction to be wrong in.
    """
    assert "-- the key" in _statements(_APPLIED)
    assert "INSERT INTO t VALUES ('a -- b')" in _statements("INSERT INTO t VALUES ('a -- b')\n")


def test_a_ledger_row_written_before_the_fix_is_upgraded_rather_than_rejected() -> None:
    """The fix must not declare every deployed database drifted on its first run.

    Every existing `schema_migrations` row holds a whole-file hash. Without recognising it, the
    change that fixes an outage would cause a bigger one, so the runner accepts the legacy value
    once and rewrites the row — verified against the live database, where `036`'s row went from the
    file hash to the statement hash and the run reported "already up to date".
    """
    assert _legacy_checksum(_APPLIED) != _checksum(_APPLIED)
    assert _legacy_checksum(_APPLIED) == _legacy_checksum(_APPLIED)


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

    `agent/audit_store.py` took exactly this lock to keep two appends from forking its hash chain
    (both are gone as of D-2026-08-14; DDL still races, so this one stays)
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
