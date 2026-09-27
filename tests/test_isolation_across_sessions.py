"""Schema isolation survives a second pytest session inside one Python process.

`tests/test_suite_isolation.py` guards *which DSN* a run resolves and is deliberately
database-free. This file guards the other half — that the schema those DSNs name still holds the
tables — and needs a real database to do it, which is why it is a separate file rather than a
fourth test there.

**The gap was real and `make mutants` fell into it.** `mutmut` does not shell out: its runner calls
`pytest.main()` *in-process*, once to collect stats, once for the clean-test baseline, then once per
mutant. `tests/conftest.py::isolated_postgres_schema` is session-scoped, so it drops `TEST_SCHEMA`
when a session ends and recreates it — empty — when the next begins, while `tests.pg.TEST_SCHEMA` is
a module constant and stays the same name. `tests.pg._MIGRATED` keyed on that unchanged name and
so reported the schema migrated when its tables had just been dropped, `migrated_db_or_skip` applied
nothing, and every unqualified name resolved through the search_path's second entry to `public`.

Measured before the fix, two sessions on one test in one process: session 1 isolated and green,
session 2 appending **24 rows to `public.audit_events`** and then failing its own count assertion.
The mutation run's own failure read "48 of 24"; the reduced reproduction read 96 of 24, because by
then earlier measurements had left 72 rows there — the arithmetic is the same and the two numbers
belong to different runs.

The failing assertion is the harmless end of it. What a mutation run could destroy this way is
narrower than it looks and is worth stating rather than gesturing at: a mutant run executes
`pytest_add_cli_args_test_selection`, not the suite, and of the files it names **2** contain a
`TRUNCATE` or a `DELETE FROM` — only one of which runs anything, a delete scoped to its own
fixture row. Repo-wide the
figure is 47 of 418 test files, re-measured 2026-09-22, and the `note_index` truncations live
in three files a mutation run never selects. So what leaked here was audit rows. The reason to
fix it is not this harness's blast
radius but that the suite's isolation silently stops holding for *any* caller that reuses the
process.

Both tests below are read-only.
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

# Two `pytest.main()` calls in one process, which is `mutmut.__main__.PytestRunner.execute_pytest`
# reduced to the one property that matters here. Kept as a script rather than a helper in this file
# because the defect is about process-lifetime state, and a subprocess is the only way to assert it
# from inside a session that has already built some.
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

    Asserted on the *resolution* rather than on the schema existing, because the escape is silent
    exactly when the schema is there and empty: `schema_dsn` keeps `public` second on the
    search_path so the `vector` type stays reachable, so a missing table is not an error but a
    different table. `pg_table_is_visible` answers the question a store's own query asks.

    This is the probe the driver below runs twice. It is also worth its own place in the suite: it
    is the first thing that fails if a migration is ever written with a qualified schema.
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
    """Two `pytest.main()` calls in one process both stay inside the isolation schema.

    Driven as a real pair of sessions rather than by simulating the teardown, because the state at
    fault is a module-level memo and the thing under test is what one process remembers across
    them. A simulation would have to assert the fix by performing it.

    A run with no database makes the probe skip, and a skipped probe exits pytest green — so the
    subprocess's exit code alone would pass this test on no evidence. The passed/skipped counts in
    its output are what distinguishes the two, and an absent database is reported as a skip here
    under the same marker `tests/conftest.py` counts, imported rather than restated.

    The subprocess timeout sits **under** pytest-timeout's own 180 s cap on this test deliberately.
    At 600 s — its first value — the outer cap always fires first, so the inner bound was dead and
    the failure would have been a bare "Timeout" rather than the driver's own output. Measured at
    ~10.6 s for the pair, so 120 s is ~11x headroom on a loaded box and still says what happened.
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
