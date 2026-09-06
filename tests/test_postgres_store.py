"""Integration tests for the Postgres calculation store (plan step 1b.3).

Runs against a real database (CI provides a Postgres service; the offline sandbox
has none, so these skip). Proves the durable backend honors the same ResultStore
contract as InMemoryStore: round-trip, upsert on the same key, distinct rows per
version.
"""

import asyncio
from datetime import datetime

from chemclaw.core.chem import require_canonical_smiles
from chemclaw.core.migrate import migrate
from chemclaw.science.calc.postgres_store import PostgresStore, default_store
from chemclaw.science.calc.store import (
    CalculationKey,
    CalculationQuery,
    CorruptCacheRow,
    InMemoryStore,
    ResultStore,
    StoredResult,
    cached_compute,
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


def test_default_store_is_postgres_backed() -> None:
    """The production seam names the durable backend, not the in-memory one.

    `default_store()` is what every calculator resolves its store from, and its in-memory sibling
    satisfies the same `ResultStore` Protocol — so a seam accidentally left pointing at
    `InMemoryStore` type-checks, passes every store test, and silently discards the cache on
    process exit, which is D-011 turned off. `tests/test_audit.py` pins the audit sink the same
    way, and `test_default_artifact_store_is_postgres_backed` its artifact twin; this is the third
    of the trio and the only one that was missing.
    """
    store: ResultStore = default_store()
    assert isinstance(store, PostgresStore)


def test_a_rewrite_without_a_cost_keeps_what_the_original_miss_measured() -> None:
    """`compute_seconds` is written once by the miss that paid it and never erased.

    `StoredResult.compute_seconds` defaults to `None` and only `cached_compute` sets it, so every
    other writer — `record_best_geometry`, a backfill, an admin correction of a payload — re-`put`s
    the key with no cost attached. Without the `COALESCE` those writes `SET compute_seconds =
    NULL`, and `find_calculations` then reports an expensive DFT run as costless: the one number
    that says what the cache has saved, wrong in the direction that argues the cache is worthless.

    Replacing that line with `compute_seconds = EXCLUDED.compute_seconds,` leaves the calculation
    store, browse, artifact and in-memory suites green (measured: 47 passed).
    """

    async def _run() -> tuple[float | None, float | None]:
        store = await _store_or_skip()
        key = CalculationKey.build("pgcost", "v1", inputs={"smiles": "pg-cost-CCO"})
        await store.put(
            StoredResult(key=key, result={"energy": -1.0}, compute_seconds=310.5),
        )
        first = await store.get(key)
        # A second write of the same answer from a path that does not time itself.
        await store.put(StoredResult(key=key, result={"energy": -1.0}, provenance="backfill"))
        second = await store.get(key)
        assert first is not None and second is not None
        return first.compute_seconds, second.compute_seconds

    measured, after_rewrite = asyncio.run(_run())
    assert measured == 310.5
    assert after_rewrite == 310.5, "a costless rewrite erased what the original miss cost"


def test_a_rewrite_keeps_the_date_the_value_was_computed() -> None:
    """`created_at` answers "when was this computed", so a rewrite must not move it.

    The key is content-addressed: a second `put` under it is the same calculation being rewritten
    — a backfill, an `ArrayOffloadingStore` offload, an admin correction — not a new one. The
    upsert nonetheless set `created_at = now()`, so a rewrite restamped the row as freshly
    computed, `find`'s newest-first order and its `since`/`until` window described the last
    *write*, and `find_calculations` promises "results computed at or after it". Measured before
    the fix on this shape: a row computed at 09:33:39 came back reading 09:33:40 after a backfill
    that ran no calculator. `InMemoryStore` keeps whatever date the caller stored, so the two
    backends disagreed as well.
    """

    async def _run() -> tuple[datetime | None, datetime | None]:
        store = await _store_or_skip()
        key = CalculationKey.build("pgdate", "v1", inputs={"smiles": "pg-date-CCO"})
        query = CalculationQuery(calc_type="pgdate", limit=5)
        await store.put(StoredResult(key=key, result={"energy": -1.0}, compute_seconds=42.0))
        [first] = await store.find(query)
        await asyncio.sleep(0.05)
        await store.put(StoredResult(key=key, result={"energy": -1.0}, provenance="backfill"))
        [second] = await store.find(query)
        return first.created_at, second.created_at

    computed_at, after_rewrite = asyncio.run(_run())
    assert computed_at is not None
    assert after_rewrite == computed_at, "a rewrite restamped the row as newly computed"


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


def test_known_answers_existence_in_bulk_and_both_backends_agree() -> None:
    """`known` is the `kg-validate` calc_refs probe: held keys come back, typos do not.

    Checked against both backends in one test because the CLI runs the Postgres one while the
    validate-layer unit tests run the in-memory one — if the two disagreed, the unit tests would
    prove a gate the deployment does not run.
    """

    async def _run() -> None:
        store = await _store_or_skip()
        memory = InMemoryStore()
        key = CalculationKey.build("xtb", "gfn2", inputs={"smiles": "pg-known-CCO"})
        stored = StoredResult(key=key, result={"energy": -1.0}, provenance="computed")
        await store.put(stored)
        await memory.put(stored)

        asked = [key.as_str(), "xtb@gfn2:0000:0000"]
        assert await store.known(asked) == {key.as_str()}
        assert await memory.known(asked) == {key.as_str()}
        assert await store.known([]) == set()

    asyncio.run(_run())


async def _write_raw_result(key: CalculationKey, payload: object) -> None:
    """Put `payload` into `calculation_results.result` without going through the store.

    The column is bare `JSONB NOT NULL`, so every value here is one a restore, an operator, or a
    calculation server returning a shape this repository does not check could leave behind. Written
    with SQL for that reason: the store's own `put` is exactly the path these rows did not take.
    """
    from psycopg.types.json import Jsonb

    from chemclaw.core import db
    from chemclaw.core.config import settings

    async with db.connection(settings.postgres_dsn) as conn, conn.cursor() as cur:
        await cur.execute(
            "INSERT INTO calculation_results (key, calc_type, calc_version, input_hash, "
            "params_hash, result, provenance) VALUES (%s, %s, %s, %s, %s, %s, 'computed') "
            "ON CONFLICT (key) DO UPDATE SET result = EXCLUDED.result",
            (
                key.as_str(),
                key.calc_type,
                key.calc_version,
                key.input_hash,
                key.params_hash,
                Jsonb(payload),
            ),
        )
        await conn.commit()


def test_a_row_that_is_not_a_json_object_is_refused_by_name() -> None:
    """`JSONB NOT NULL` accepts an array, a string, a number and `null`; psycopg parses all four.

    The old read was `result if isinstance(result, dict) else json.loads(result)`, written for a
    driver that returns a string — so each of these reached `json.loads` as a `list`/`str`/`int`
    and produced `TypeError: the JSON object must be str, bytes or bytearray, not list`, which
    names neither the table nor the row an operator has to delete.
    """

    async def _run() -> list[str]:
        store = await _store_or_skip()
        messages = []
        for index, payload in enumerate(["corrupted", [1, 2, 3], 42, None]):
            key = CalculationKey.build("probe.shape", "1", inputs={"n": index})
            await _write_raw_result(key, payload)
            try:
                await store.get(key)
            except CorruptCacheRow as exc:
                messages.append(str(exc))
            else:
                messages.append("")
        return messages

    for message in asyncio.run(_run()):
        assert "calculation_results row" in message, (
            f"a non-object row was accepted or refused without naming itself: {message!r}"
        )


def test_an_empty_result_is_neither_cached_nor_handed_back() -> None:
    """`{}` is what a truncated or failed call to the calculation server degrades into.

    Measured before this: a `{}` row answered `hit=True computes=0` and flowed out as the tool's
    answer, permanently — D-011 never recomputes a persisted result and `calculation_results` is
    never pruned, so one such write poisons that key for the life of the deployment. Both doors are
    asserted: the write gate in `cached_compute`, which is the one path a calculation server's
    answer comes through, and the read, for a row that got in some other way.

    A *wrong value* under a right key is deliberately not covered — catching that needs the
    calculator's schema, which lives in `Chemclaw3-mcp` by
    `D-2026-08-16-the-physics-leaves-the-cache-stays`.
    """

    async def _run() -> tuple[bool, bool, int]:
        store = await _store_or_skip()
        read_key = CalculationKey.build("probe.empty", "1", inputs={"n": "read"})
        await _write_raw_result(read_key, {})
        read_refused = False
        try:
            await store.get(read_key)
        except CorruptCacheRow:
            read_refused = True

        write_key = CalculationKey.build("probe.empty", "1", inputs={"n": "write"})
        computes = 0

        async def _empty() -> dict[str, object]:
            nonlocal computes
            computes += 1
            return {}

        write_refused = False
        try:
            await cached_compute(store, write_key, _empty)
        except CorruptCacheRow:
            write_refused = True
        return read_refused, write_refused, computes

    read_refused, write_refused, computes = asyncio.run(_run())
    assert read_refused, "an empty stored result was handed back as a cache hit"
    assert write_refused, "an empty computed result was persisted, which D-011 makes permanent"
    assert computes == 1, "the write gate ran before the computation instead of after it"


def test_one_corrupt_row_does_not_empty_the_browse() -> None:
    """`find` answers "what do we already have"; one poisoned row used to answer nothing at all.

    The split is deliberate and is the opposite of `get`'s: an exact-key lookup must refuse the row
    the caller asked for, and a listing must not be taken down by a row nobody asked for. The same
    call `retrievers._chunks_from_hits` makes for an index hit whose note no longer loads.
    """

    async def _run() -> list[str]:
        store = await _store_or_skip()
        good = CalculationKey.build("probe.browse", "1", inputs={"n": "good"})
        bad = CalculationKey.build("probe.browse", "1", inputs={"n": "bad"})
        await store.put(StoredResult(key=good, result={"energy": -1.0}))
        await _write_raw_result(bad, [1, 2, 3])
        found = await store.find(CalculationQuery(calc_type="probe.browse"))
        return [stored.key.as_str() for stored in found]

    keys = asyncio.run(_run())
    assert keys, "one corrupt row emptied the whole browse"
    assert all("bad" not in key for key in keys), "the corrupt row was handed back anyway"
