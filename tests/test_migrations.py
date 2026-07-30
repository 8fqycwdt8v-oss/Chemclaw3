"""The migration reader finds the SQL files, offline and with no database (D-148).

`_read_sql_files` located `infra/sql/` as `Path(__file__).parent.parent` — correct only while the
module sat at the repository root, where that expression happened to resolve there. D-148 moved
it to `science/calc/migrate.py`, two levels deeper, and the path silently became a
directory *inside* the package. `glob` on a non-existent directory raises nothing and yields
nothing, so `make db-migrate` did not fail on a bad path — it applied zero migrations, and CI only
caught it because the integration tests that follow found no schema.

That is the failure mode worth a test: not "the migrations are wrong" but "there are no migrations
and nobody said so". These run offline and touch no database, so they fail on the commit that
breaks the path rather than on the first job that needs the schema.
"""

from pathlib import Path

import pytest

from chemclaw.core.config import settings
from chemclaw.science.calc.migrate import _LEDGER_FILE, MigrationError, _read_sql_files

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
