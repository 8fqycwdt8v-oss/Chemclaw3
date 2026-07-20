"""Integration tests for the Postgres calculation store (plan step 1b.3).

Runs against a real database (CI provides a Postgres service; the offline sandbox
has none, so these skip). Proves the durable backend honors the same ResultStore
contract as InMemoryStore: round-trip, upsert on the same key, distinct rows per
version.
"""

import asyncio

import psycopg
import pytest

from calc.migrate import _statements, migrate
from calc.postgres_store import PostgresStore
from calc.store import CalculationKey, StoredResult
from chemclaw.config import settings


def test_statements_split_ignores_comment_semicolons() -> None:
    """Multi-statement SQL splits into individual commands, even with `;` in comments."""
    sql = "-- a; comment with semicolon\nCREATE TABLE t (id int);\nCREATE INDEX i ON t (id);\n"
    statements = _statements(sql)
    assert len(statements) == 2
    assert statements[0].startswith("CREATE TABLE")
    assert statements[1].startswith("CREATE INDEX")


async def _store_or_skip() -> PostgresStore:
    """Return a migrated Postgres store, or skip if no database is reachable."""
    try:
        conn = await psycopg.AsyncConnection.connect(settings.postgres_dsn)
        await conn.close()
    except psycopg.OperationalError as exc:  # pragma: no cover - env-dependent
        pytest.skip(f"Postgres unavailable (offline sandbox): {exc}")
    await migrate()
    return PostgresStore()


def test_round_trip_and_upsert() -> None:
    """Put then get returns the payload; a second put on the same key overwrites."""

    async def _run() -> None:
        store = await _store_or_skip()
        key = CalculationKey.build("xtb", "gfn2", inputs={"smiles": "pg-CCO"})

        await store.put(StoredResult(key=key, result={"energy": -1.5}, provenance="computed"))
        got = await store.get(key)
        assert got is not None
        assert got.result == {"energy": -1.5}
        assert got.provenance == "computed"

        await store.put(StoredResult(key=key, result={"energy": -2.0}, provenance="measured"))
        got2 = await store.get(key)
        assert got2 is not None
        assert got2.result == {"energy": -2.0}
        assert got2.provenance == "measured"

    asyncio.run(_run())


def test_version_bump_is_a_distinct_row() -> None:
    """Different calc_version keys coexist independently in the table."""

    async def _run() -> None:
        store = await _store_or_skip()
        inputs = {"smiles": "pg-benzene"}
        k1 = CalculationKey.build("solub", "v1", inputs=inputs)
        k2 = CalculationKey.build("solub", "v2", inputs=inputs)

        await store.put(StoredResult(key=k1, result={"logS": -1.0}))
        await store.put(StoredResult(key=k2, result={"logS": -2.0}))

        got1 = await store.get(k1)
        got2 = await store.get(k2)
        assert got1 is not None and got1.result == {"logS": -1.0}
        assert got2 is not None and got2.result == {"logS": -2.0}

    asyncio.run(_run())


def test_get_miss_returns_none() -> None:
    """An absent key returns None from the durable backend too."""

    async def _run() -> None:
        store = await _store_or_skip()
        key = CalculationKey.build("xtb", "gfn2", inputs={"smiles": "pg-absent-xyz"})
        # Ensure absence regardless of prior runs by using a version that won't collide.
        missing = key.model_copy(update={"calc_version": "never-written"})
        assert await store.get(missing) is None

    asyncio.run(_run())
