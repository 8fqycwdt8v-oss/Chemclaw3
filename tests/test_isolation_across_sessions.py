"""Schema isolation survives a second pytest session inside one Python process.

`tests/test_suite_isolation.py` guards which DSN a run resolves; this guards that the schema still
holds the tables, and needs a real database. Tools like `mutmut` call `pytest.main()` repeatedly in
one process. `isolated_postgres_schema` drops and recreates `TEST_SCHEMA` per session while the
name stays constant, so a migration memo keyed on the name would skip migrating the fresh schema
and unqualified names would resolve to `public`. Both tests are read-only.
"""

import subprocess
import sys
from pathlib import Path

import pytest

from chemclaw.core.config import settings
from chemclaw.core.db import connect
from tests.conftest import _POSTGRES_SKIP
from tests.pg import TEST_SCHEMA, migrated_db_or_skip

_REPO_ROOT = Path(__file__).resolve().parents[1]

# The probe below, named so the driver runs exactly it and never re-enters the driver itself.
_PROBE = f"tests/{Path(__file__).name}::test_the_isolation_schema_holds_the_migrated_tables"

# Two `pytest.main()` calls in one process, as `mutmut`'s runner does. A subprocess script, because
# the defect is process-lifetime state.
_DRIVER = """
import sys

import pytest

for session in (1, 2):
    code = pytest.main(["-x", "-q", "-p", "no:randomly", sys.argv[1]])
    print(f"### session {session} exited {code}")
    if code != 0:
        raise SystemExit(session)
"""


async def test_the_isolation_schema_holds_the_migrated_tables() -> None:
    """An unqualified table name resolves inside `TEST_SCHEMA`, not through it to `public`.

    `schema_dsn` keeps `public` second on the search_path for the `vector` type, so a missing table
    silently resolves elsewhere; `pg_table_is_visible` answers what a store's query asks. Also fails
    first if a migration is written with a qualified schema.
    """
    await migrated_db_or_skip()

    async with await connect(settings.postgres_dsn) as conn:
        cursor = await conn.execute(
            "SELECT n.nspname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE c.relname = 'audit_events' AND pg_table_is_visible(c.oid)"
        )
        row = await cursor.fetchone()

    assert row is not None, (
        "`audit_events` resolves to no schema at all, so the isolation schema is unmigrated and "
        "every store in this run is reading whatever the search_path reaches next"
    )
    assert row[0] == TEST_SCHEMA, (
        f"an unqualified `audit_events` resolves in {row[0]!r}, not the isolation schema "
        f"{TEST_SCHEMA!r} — this run's truncations and deletes are landing outside it"
    )


def test_a_second_pytest_session_in_one_process_keeps_its_isolation_schema() -> None:
    """A second pytest session in one process keeps its isolation schema.

    Driven as a real pair of sessions, since the state at fault is a module-level memo. A skipped
    probe exits green, so the passed/skipped counts are read, and an absent database is reported as
    a skip under the marker `tests/conftest.py` counts. The subprocess timeout sits under
    pytest-timeout's cap so the driver's own output is what reports a hang.
    """
    result = subprocess.run(
        [sys.executable, "-c", _DRIVER, _PROBE],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    combined = result.stdout + result.stderr

    if combined.count("1 skipped") == 2:
        pytest.skip(f"{_POSTGRES_SKIP}: the probe skipped in both driven sessions")

    assert combined.count("1 passed") == 2, (
        "a second pytest session in one process did not keep its isolation schema, so the suite "
        "ran against whatever the search_path reached next — `public`, on a dev database. "
        f"exit={result.returncode}\n{combined[-4000:]}"
    )
