"""Shared bootstrap for Postgres-backed integration tests.

`migrated_db_or_skip` turns an unreachable server into a skip (start Docker and `make up` to run
them; `tests/conftest.py`'s epilogue counts the skips). Every table is created in a dedicated
schema, never the running system's, because tests truncate and insert. The schema is carried on
the DSN (`options=-c search_path=...`), so redirecting the DSN settings isolates every store with
no schema parameter in product code; `tests/conftest.py::redirect_dsns_to_test_schema` owns the
list of settings, including the migration DSN.
"""

import re
from collections.abc import Sequence
from typing import Any
from urllib.parse import quote
from uuid import uuid4

import psycopg
import pytest

from chemclaw.core.config import settings
from chemclaw.core.db import connect
from chemclaw.core.migrate import migrate, migration_dsn

# Not a `Settings` field: test-only knobs do not belong on the operator-facing config surface.
#
# A fresh uuid4 suffix per process, so concurrent pytest processes (and xdist workers) against one
# database cannot drop or inherit each other's schemas. A hard kill can leave an orphan
# `chemclaw_test_*` schema; nothing sweeps it. Twelve hex digits because Postgres silently truncates
# identifiers at 63 bytes and derived names such as `f"{TEST_SCHEMA}_no_checkpointer"` need room.
TEST_SCHEMA = f"chemclaw_test_{uuid4().hex[:12]}"


def schema_dsn(dsn: str, schema: str = TEST_SCHEMA) -> str:
    """Return `dsn` with `schema` prepended to the connection's `search_path`.

    `public` stays second because the `vector` extension is installed once per database, so its type
    is only resolvable through `public`.
    """
    separator = "&" if "?" in dsn else "?"
    return f"{dsn}{separator}options={quote(f'-c search_path={schema},public')}"


async def create_test_schema(base_dsn: str, schema: str = TEST_SCHEMA) -> None:
    """Create the isolation schema, using the *unredirected* DSN."""
    async with await connect(base_dsn) as conn:
        await conn.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
        await conn.commit()


async def drop_test_schema(base_dsn: str, schema: str = TEST_SCHEMA) -> None:
    """Drop the isolation schema and everything in it, so a run leaves no residue behind.

    Also forgets the migration memo for that schema, so a later session in the same process
    re-migrates instead of falling through to `public`.
    """
    async with await connect(base_dsn) as conn:
        await conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await conn.commit()
    _forget_migrations_in(schema)


# Which (migration DSN, migrations directory) pairs this process has already migrated. A process
# can outlive a pytest session (`mutmut` runs `pytest.main()` in-process), so `drop_test_schema`
# must clear entries for the schema it drops.
_MIGRATED: set[tuple[str, str]] = set()


def _forget_migrations_in(schema: str) -> None:
    """Discard the memo entries that claimed migrations are applied inside `schema`.

    Matched on the rendered `search_path` option, not a substring, so a derived schema name or a
    schema name inside a password does not match.
    """
    rendered = quote(f"-c search_path={schema},public")
    for entry in list(_MIGRATED):
        if rendered in entry[0]:
            _MIGRATED.discard(entry)


async def migrated_db_or_skip() -> None:
    """Ensure a reachable, migrated Postgres database, or skip if none is available.

    Migrates into `migration_dsn()`, which the session fixture has redirected to `TEST_SCHEMA`; the
    DDL is unqualified, so it lands in the first schema on the search_path. The probe uses
    `postgres_dsn`, the tests' own connection, and runs every time so a database that disappears is
    a skip; the migration is memoised per process because even a no-op run is costly across hundreds
    of tests.
    """
    try:
        conn = await psycopg.AsyncConnection.connect(settings.postgres_dsn)
        await conn.close()
    except psycopg.OperationalError as exc:  # pragma: no cover - env-dependent
        pytest.skip(f"Postgres unavailable (start it: sudo dockerd; make up): {exc}")
    # Keyed on what `migrate()` reads, so a test that repoints the migrations directory or DSN gets
    # a fresh migration.
    applied_to = (migration_dsn(), settings.sql_migrations_dir)
    if applied_to not in _MIGRATED:
        await migrate()
        _MIGRATED.add(applied_to)


async def create_checkpoint_tables() -> None:
    """Create the LangGraph checkpointer's tables in the isolation schema.

    `AsyncPostgresSaver.setup()` creates these, not a migration, so CI's database lacks them while a
    dev database that has run the agent has them in `public` — where an unqualified read would
    resolve. A test that reads them must create them. Runs every saver migration (later ones add
    columns such as `task_path`) except `CREATE INDEX CONCURRENTLY`, which cannot run in a
    transaction and does not affect shape. Uses the statements directly because `setup()` opens its
    own pool outside the isolation schema.
    """
    from langgraph.checkpoint.postgres import base

    async with await connect(settings.postgres_dsn) as conn:
        for statement in base.MIGRATIONS[1:]:
            if "CONCURRENTLY" in statement:
                continue
            await conn.execute(statement)
        await conn.commit()


async def planned_index(
    table: str,
    seed: str,
    statement: str,
    params: Sequence[object] | dict[str, object],
    *,
    without: str | None = None,
) -> str:
    """The index the planner reaches for `statement`, over a copy of `table` this call fills itself.

    A plan depends on the table's size and statistics, and on a shared schema those are whatever
    earlier tests left behind. The copy is a temp table of the same name (so `statement` resolves
    to it unchanged) carrying the same indexes under the same names, filled by `seed` and analysed
    inside the transaction, which is rolled back. Sequential scans are disabled to ask which index
    the schema offers rather than whether a scan is cheaper on a small table.

    Args:
        table: The table in the current schema to copy.
        seed: An `INSERT` into `table`; it runs against the copy, whose columns have the original's
            defaults and `NOT NULL`s.
        statement: The query to plan; its `%s` placeholders are filled from `params`.
        params: The query's parameters.
        without: An index of `table` to leave off the copy, for a control that the plan notices.

    Returns:
        The first index named in the plan, or `""` when it uses none.
    """
    await migrated_db_or_skip()
    async with await connect(settings.postgres_dsn) as conn:
        rows = await (
            await conn.execute(
                "SELECT indexname, indexdef FROM pg_indexes "
                "WHERE schemaname = current_schema() AND tablename = %s",
                (table,),
            )
        ).fetchall()
        await conn.execute(
            f"CREATE TEMP TABLE {table} (LIKE {table} INCLUDING DEFAULTS INCLUDING GENERATED)"
        )
        for name, definition in rows:
            if name != without:
                on_the_copy = re.sub(
                    r" ON \S+ USING ", f" ON pg_temp.{table} USING ", definition, count=1
                )
                await conn.execute(on_the_copy)
        await conn.execute(seed)
        await conn.execute(f"ANALYZE {table}")
        await conn.execute("SET LOCAL enable_seqscan = off")
        cursor = await conn.execute(f"EXPLAIN (FORMAT JSON) {statement}", params)
        row = await cursor.fetchone()
        await conn.rollback()
    nodes: list[Any] = [row[0][0]["Plan"]] if row else []
    while nodes:
        node = nodes.pop()
        if "Index Name" in node:
            return str(node["Index Name"])
        nodes.extend(node.get("Plans", []))
    return ""
