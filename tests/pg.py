"""Shared bootstrap for Postgres-backed integration tests.

CI provides a real (pgvector-enabled) database; the offline sandbox has none, so
`migrated_db_or_skip` turns an unreachable server into a skip. Kept in one place
so every Postgres-backed test uses the same connect-check + migration (DRY); each
test file only constructs its own store on top of the migrated database.

**Isolation.** Every table here is created in a dedicated schema, never the one the running
system uses. Without that, the suite operates on live data: `test_vector_index` truncates
`note_index`, the audit suites append rows nothing cleans up, and the rest leak fixture rows
permanently — a
stray `id='CCO'` row from real ELN ingestion once tied with this suite's own `pg-ethanol`
fixture and broke a similarity assertion. CI never noticed because its database is a
throwaway container; a shared dev database is where this bites.

The schema is carried on the DSN itself (`options=-c search_path=...`) rather than threaded
through the stores, because every store already resolves its own connection from
`settings.postgres_dsn` — so redirecting that one value isolates all of them with no schema
parameter anywhere in product code. `tests/conftest.py` owns that redirect.
"""

import os
from urllib.parse import quote

import psycopg
import pytest

from chemclaw.core.config import settings
from chemclaw.core.db import connect
from chemclaw.core.migrate import migrate

# Not a `Settings` field on purpose: `core/config/` is the operator-facing deployment
# surface, and its parity tests (DA-1) require every field to be documented in `.env.example`.
# A test-only knob does not belong there.
#
# Suffixed with the pid so two pytest runs against one database cannot collide: the session
# fixture *drops* its schema on the way out, so a fixed name means a second run deletes the first
# run's tables mid-flight. Found the hard way — running a single test file while the full suite
# was going did exactly that. A hard kill can leave an orphan schema behind; it is inert, named
# unmistakably, and dropped by the next run that happens to reuse the pid.
TEST_SCHEMA = f"chemclaw_test_{os.getpid()}"


def schema_dsn(dsn: str, schema: str = TEST_SCHEMA) -> str:
    """Return `dsn` with `schema` prepended to the connection's `search_path`.

    `public` stays second because the `vector` extension is installed once per *database*
    (migrations 002/003/012 use `CREATE EXTENSION IF NOT EXISTS`), so the `vector` type is only
    resolvable while `public` remains reachable.
    """
    separator = "&" if "?" in dsn else "?"
    return f"{dsn}{separator}options={quote(f'-c search_path={schema},public')}"


async def create_test_schema(base_dsn: str, schema: str = TEST_SCHEMA) -> None:
    """Create the isolation schema, using the *unredirected* DSN."""
    async with await connect(base_dsn) as conn:
        await conn.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
        await conn.commit()


async def drop_test_schema(base_dsn: str, schema: str = TEST_SCHEMA) -> None:
    """Drop the isolation schema and everything in it, so a run leaves no residue behind."""
    async with await connect(base_dsn) as conn:
        await conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await conn.commit()


async def migrated_db_or_skip() -> None:
    """Ensure a reachable, migrated Postgres database, or skip if none is available.

    Migrates into whatever `settings.postgres_dsn` currently points at — which the session
    fixture in `conftest.py` has already redirected to `TEST_SCHEMA`. Every DDL statement in
    `infra/sql` is unqualified, so they land in the first schema on the search_path.
    """
    try:
        conn = await psycopg.AsyncConnection.connect(settings.postgres_dsn)
        await conn.close()
    except psycopg.OperationalError as exc:  # pragma: no cover - env-dependent
        pytest.skip(f"Postgres unavailable (offline sandbox): {exc}")
    await migrate()
