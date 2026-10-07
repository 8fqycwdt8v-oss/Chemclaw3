"""Integration tests for the Postgres artifact store (D-124).

Proves the durable backend's round trip, content-addressed dedup and overwrite semantics against a
real database. Skips without Postgres (`migrated_db_or_skip()`); each test uses its own `calc_key`
prefix because the session schema is shared.
"""

import asyncio

import pytest

from chemclaw.core import db
from chemclaw.core.config import settings
from chemclaw.science.calc.artifacts import ArtifactStore, content_address
from chemclaw.science.calc.postgres_artifacts import PostgresArtifactStore, default_artifact_store
from tests.pg import migrated_db_or_skip


async def _store_or_skip() -> PostgresArtifactStore:
    """Return a migrated Postgres artifact store, or skip if no database is reachable."""
    await migrated_db_or_skip()
    return PostgresArtifactStore()


def test_default_artifact_store_is_postgres_backed() -> None:
    """The production seam names the durable backend, mirroring `default_store` (calc results)."""
    store: ArtifactStore = default_artifact_store()
    assert isinstance(store, PostgresArtifactStore)


async def test_round_trip_returns_exactly_what_was_put() -> None:
    """`open(content_hash)` must hand back the original bytes, not a codec's idea of them."""
    store = await _store_or_skip()
    data = b"3\nwater\nO 0.0 0.0 0.0\nH 0.0 0.0 0.96\nH 0.93 0.0 -0.24\n" * 50
    ref = await store.put("pgart-roundtrip:1", "xtbopt.xyz", data, media_type="chemical/x-xyz")

    assert ref is not None
    assert ref.content_hash == content_address(data)
    assert ref.byte_size == len(data)

    got = await store.open(ref.content_hash)
    assert got == data


async def test_a_miss_returns_none() -> None:
    """An address nothing ever stored answers `None`, matching `InMemoryArtifactStore`."""
    store = await _store_or_skip()
    assert await store.open("no-such-hash-was-ever-stored") is None


async def test_two_different_payloads_do_not_collide() -> None:
    """Content addressing must not fold distinct bytes onto the same hash or the same bytes back."""
    store = await _store_or_skip()
    calc_key = "pgart-distinct:1"
    first = await store.put(calc_key, "hessian", b"first payload" * 10)
    second = await store.put(calc_key, "vibspectrum", b"second, different payload" * 10)

    assert first is not None and second is not None
    assert first.content_hash != second.content_hash

    assert await store.open(first.content_hash) == b"first payload" * 10
    assert await store.open(second.content_hash) == b"second, different payload" * 10


async def test_identical_bytes_dedupe_to_one_blob_but_keep_both_links() -> None:
    """Two calculations producing the same geometry store one blob, addressed identically.

    The whole point of content addressing (module docstring): `list_for` still reports both names
    against the calculation that produced them, and both resolve to the same stored bytes.
    """
    store = await _store_or_skip()
    calc_key = "pgart-dedupe:1"
    payload = b"identical geometry\n" * 20
    a = await store.put(calc_key, "xtbopt.xyz", payload)
    b = await store.put(calc_key, "crest_conformers.xyz", payload)

    assert a is not None and b is not None
    assert a.content_hash == b.content_hash == content_address(payload)

    refs = await store.list_for(calc_key)
    assert {ref.name for ref in refs} == {"xtbopt.xyz", "crest_conformers.xyz"}
    for ref in refs:
        assert await store.open(ref.content_hash) == payload


async def test_overwriting_a_name_repoints_the_link_without_losing_the_old_blob() -> None:
    """A second `put` under the same `(calc_key, name)` repoints the link (D-124's upsert).

    The link resolves to the new content, while the old blob stays retrievable by its own hash until
    eviction reclaims it.
    """
    store = await _store_or_skip()
    calc_key = "pgart-overwrite:1"
    first = await store.put(calc_key, "hessian", b"stale hessian" * 5)
    second = await store.put(calc_key, "hessian", b"refreshed hessian" * 5)

    assert first is not None and second is not None
    assert first.content_hash != second.content_hash

    [ref] = await store.list_for(calc_key)
    assert ref.content_hash == second.content_hash

    assert await store.open(first.content_hash) == b"stale hessian" * 5
    assert await store.open(second.content_hash) == b"refreshed hessian" * 5


def test_relinking_an_artifact_without_a_cost_keeps_what_the_original_run_measured() -> None:
    """Relinking without a cost keeps the `compute_seconds` the original run measured.

    `put` defaults `compute_seconds` to `None`, so most re-puts carry no cost. `_EVICT_TO_FIT` ranks
    a missing cost as 0, so erasing it would evict the most expensive artifacts first and force
    their recomputation.
    """

    async def _run() -> tuple[float | None, float | None]:
        store = await _store_or_skip()
        calc_key = "pgart-cost:1"
        await store.put(calc_key, "hessian", b"expensive hessian" * 5, compute_seconds=240.0)
        after_first = await _recorded_cost(calc_key)
        # The same bytes again from a path that does not time itself — a backfill, a re-index.
        await store.put(calc_key, "hessian", b"expensive hessian" * 5)
        return after_first, await _recorded_cost(calc_key)

    recorded, after_rewrite = asyncio.run(_run())
    assert recorded == 240.0
    assert after_rewrite == 240.0, (
        "a costless rewrite erased the measured cost; the blob now ranks at the bottom of the "
        "eviction order and the expensive run it stands for will be repeated"
    )


async def _recorded_cost(calc_key: str) -> float | None:
    """The `compute_seconds` stored against a calculation's `hessian` link row."""
    async with db.connection(settings.postgres_dsn) as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT compute_seconds FROM calculation_artifacts "
            "WHERE calc_key = %s AND name = 'hessian'",
            (calc_key,),
        )
        row = await cur.fetchone()
    return None if row is None or row[0] is None else float(row[0])


async def test_list_for_orders_by_name_and_is_scoped_to_its_own_calculation() -> None:
    """The reader a note cites by `calc_key` must not see another calculation's by-products."""
    store = await _store_or_skip()
    mine, other = "pgart-listing:mine", "pgart-listing:other"
    await store.put(mine, "vibspectrum", b"v" * 8)
    await store.put(mine, "hessian", b"h" * 8)
    await store.put(other, "xtbopt.xyz", b"x" * 8)

    refs = await store.list_for(mine)
    assert [ref.name for ref in refs] == ["hessian", "vibspectrum"]  # alphabetical
    assert {ref.calc_key for ref in refs} == {mine}


async def test_a_payload_over_the_cap_is_refused_and_never_reaches_the_table(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`too_large` refuses on the write side; nothing durable should exist for the refused bytes."""
    store = await _store_or_skip()
    monkeypatch.setattr(settings, "artifact_max_bytes", 16)
    data = b"far more than sixteen bytes of payload"

    ref = await store.put("pgart-oversize:1", "hessian", data)

    assert ref is None
    assert await store.list_for("pgart-oversize:1") == []
    assert await store.open(content_address(data)) is None


async def test_a_disabled_store_refuses_every_write(monkeypatch: pytest.MonkeyPatch) -> None:
    """`artifact_store_enabled = False` is the other refusal path, checked before the size cap."""
    store = await _store_or_skip()
    monkeypatch.setattr(settings, "artifact_store_enabled", False)

    assert await store.put("pgart-disabled:1", "hessian", b"anything") is None
    assert await store.list_for("pgart-disabled:1") == []
