"""Integration tests for the Postgres calculation store (plan step 1b.3).

Runs against a real database (CI provides a Postgres service; the offline sandbox
has none, so these skip). Proves the durable backend honors the same ResultStore
contract as InMemoryStore: round-trip, upsert on the same key, distinct rows per
version.
"""

import asyncio

from chemclaw.core.chem import require_canonical_smiles
from chemclaw.core.migrate import migrate
from chemclaw.science.calc.postgres_store import PostgresStore
from chemclaw.science.calc.store import (
    CalculationKey,
    CalculationQuery,
    InMemoryStore,
    StoredResult,
)
from tests.pg import migrated_db_or_skip


def test_migrate_is_idempotent_and_tracked() -> None:
    """A first migrate applies files; a second finds them tracked and applies none."""

    async def _run() -> None:
        await migrated_db_or_skip()  # first pass applies (or reuses an already-migrated db)
        second = await migrate()  # everything now recorded in schema_migrations
        assert second == []  # ledger short-circuits re-application

    asyncio.run(_run())


async def _store_or_skip() -> PostgresStore:
    """Return a migrated Postgres store, or skip if no database is reachable."""
    await migrated_db_or_skip()
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


def test_find_matches_the_in_memory_backend() -> None:
    """The browse query answers the same questions in Postgres as in memory (W2.2).

    `ResultStore` is `@runtime_checkable`, so a method added to one backend and not the other
    still satisfies the Protocol at runtime and fails only where it is called. The two are
    exercised against the same fixtures here for that reason — the SQL expresses the same
    predicate as `_matches`, and nothing but a test makes them stay equal.
    """

    async def _run() -> None:
        store = await _store_or_skip()
        memory = InMemoryStore()
        # `find` reads `created_at`, which Postgres sets itself, so the two stores can only be
        # compared on filters that do not depend on it.
        rows = [
            StoredResult(
                key=CalculationKey.build(
                    "pgfind", "v1", inputs={"smiles": require_canonical_smiles(smiles)}
                ),
                result={"value": value},
            )
            for smiles, value in (("CCO", 1.0), ("CCN", 2.0))
        ]
        rows.append(
            StoredResult(
                key=CalculationKey.build(
                    "pgfind", "v2", inputs={"smiles": require_canonical_smiles("CCO")}
                ),
                result={"value": 3.0},
            )
        )
        for row in rows:
            await store.put(row)
            await memory.put(row)

        for query in (
            CalculationQuery(calc_type="pgfind"),
            CalculationQuery(calc_type="pgfind", smiles="CCO"),
            CalculationQuery(calc_type="pgfind", smiles="OCC"),  # same molecule, other spelling
            CalculationQuery(calc_type="pgfind", calc_version="v2"),
            CalculationQuery(calc_type="pgfind", smiles="CCO", limit=1),
        ):
            durable = await store.find(query)
            in_memory = await memory.find(query)
            assert {r.key.as_str() for r in durable} == {r.key.as_str() for r in in_memory}, query
            # The durable backend is the only one with a real clock, so this is where the
            # timestamp is proven to survive the round trip at all.
            assert all(r.created_at is not None for r in durable)

    asyncio.run(_run())
