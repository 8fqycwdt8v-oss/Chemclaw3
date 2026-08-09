"""Integration tests for the Postgres artifact store (D-124, R1.5).

`PostgresArtifactStore` had no direct test anywhere: `test_calc_artifacts.py` and
`test_artifact_eviction.py` both exercise the contract through `InMemoryArtifactStore` or the raw
SQL strings, never the durable backend that a deployment actually runs. That leaves the one thing
this module exists for — bytes that survive a process restart, addressed by content — with no
proof the round trip, the dedup, or the overwrite semantics work against a real database.

Follows `tests/test_postgres_store.py`'s pattern: `migrated_db_or_skip()` skips cleanly with no
Postgres reachable (this sandbox), runs for real in CI; each test is a sync `def` wrapping an inner
`async def _run()` driven by `asyncio.run` (no pytest-asyncio); isolation comes from
`tests.pg`/`conftest.py`'s per-session schema redirect, so each test also uses its own `calc_key`
prefix to stay independent of any other test run against the same schema.
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


def test_round_trip_returns_exactly_what_was_put() -> None:
    """`open(content_hash)` must hand back the original bytes, not a codec's idea of them."""

    async def _run() -> None:
        store = await _store_or_skip()
        data = b"3\nwater\nO 0.0 0.0 0.0\nH 0.0 0.0 0.96\nH 0.93 0.0 -0.24\n" * 50
        ref = await store.put("pgart-roundtrip:1", "xtbopt.xyz", data, media_type="chemical/x-xyz")

        assert ref is not None
        assert ref.content_hash == content_address(data)
        assert ref.byte_size == len(data)

        got = await store.open(ref.content_hash)
        assert got == data

    asyncio.run(_run())


def test_a_miss_returns_none() -> None:
    """An address nothing ever stored answers `None`, matching `InMemoryArtifactStore`."""

    async def _run() -> None:
        store = await _store_or_skip()
        assert await store.open("no-such-hash-was-ever-stored") is None

    asyncio.run(_run())


def test_two_different_payloads_do_not_collide() -> None:
    """Content addressing must not fold distinct bytes onto the same hash or the same bytes back."""

    async def _run() -> None:
        store = await _store_or_skip()
        calc_key = "pgart-distinct:1"
        first = await store.put(calc_key, "hessian", b"first payload" * 10)
        second = await store.put(calc_key, "vibspectrum", b"second, different payload" * 10)

        assert first is not None and second is not None
        assert first.content_hash != second.content_hash

        assert await store.open(first.content_hash) == b"first payload" * 10
        assert await store.open(second.content_hash) == b"second, different payload" * 10

    asyncio.run(_run())


def test_identical_bytes_dedupe_to_one_blob_but_keep_both_links() -> None:
    """Two calculations producing the same geometry store one blob, addressed identically.

    The whole point of content addressing (module docstring): `list_for` still reports both names
    against the calculation that produced them, and both resolve to the same stored bytes.
    """

    async def _run() -> None:
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

    asyncio.run(_run())


def test_overwriting_a_name_repoints_the_link_without_losing_the_old_blob() -> None:
    """A second `put` under the same `(calc_key, name)` updates the link (D-124's upsert).

    `_UPSERT_LINK` is `ON CONFLICT (calc_key, name) DO UPDATE`, never a second row — so the link
    must resolve to the *new* content afterwards, while the old blob (addressed by its own hash)
    stays retrievable on its own hash, since eviction — not an overwrite — is what reclaims it.
    """

    async def _run() -> None:
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

    asyncio.run(_run())


def test_relinking_an_artifact_without_a_cost_keeps_what_the_original_run_measured() -> None:
    """`compute_seconds` is written once and never erased — the eviction ranking depends on it.

    `_UPSERT_LINK` is `ON CONFLICT DO UPDATE`, so any later write to the same `(calc_key, name)`
    that does not carry a cost would otherwise `SET compute_seconds = NULL`. `put`'s signature
    makes that the *default* — `compute_seconds` is keyword-only with a `None` default and only
    `run_cached_with_artifacts` passes one — so a re-`put` from any other path is the ordinary
    case, not an exotic one.

    A nulled cost is not a cosmetic loss: `_EVICT_TO_FIT` ranks by
    `COALESCE(MAX(a.compute_seconds) / …, 0)`, and 0 is the bottom of the order. The four-minute
    Hessian the ranking exists to protect would be evicted *first*, and the next question about
    that molecule pays for the run again — D-011's cost guarantee inverted by an upsert clause.

    Replacing that line with `compute_seconds = EXCLUDED.compute_seconds,` leaves the whole
    artifact and eviction suite green (measured: 42 passed).
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


def test_list_for_orders_by_name_and_is_scoped_to_its_own_calculation() -> None:
    """The reader a note cites by `calc_key` must not see another calculation's by-products."""

    async def _run() -> None:
        store = await _store_or_skip()
        mine, other = "pgart-listing:mine", "pgart-listing:other"
        await store.put(mine, "vibspectrum", b"v" * 8)
        await store.put(mine, "hessian", b"h" * 8)
        await store.put(other, "xtbopt.xyz", b"x" * 8)

        refs = await store.list_for(mine)
        assert [ref.name for ref in refs] == ["hessian", "vibspectrum"]  # alphabetical
        assert {ref.calc_key for ref in refs} == {mine}

    asyncio.run(_run())


def test_a_payload_over_the_cap_is_refused_and_never_reaches_the_table(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`too_large` refuses on the write side; nothing durable should exist for the refused bytes."""

    async def _run() -> None:
        store = await _store_or_skip()
        monkeypatch.setattr(settings, "artifact_max_bytes", 16)
        data = b"far more than sixteen bytes of payload"

        ref = await store.put("pgart-oversize:1", "hessian", data)

        assert ref is None
        assert await store.list_for("pgart-oversize:1") == []
        assert await store.open(content_address(data)) is None

    asyncio.run(_run())


def test_a_disabled_store_refuses_every_write(monkeypatch: pytest.MonkeyPatch) -> None:
    """`artifact_store_enabled = False` is the other refusal path, checked before the size cap."""

    async def _run() -> None:
        store = await _store_or_skip()
        monkeypatch.setattr(settings, "artifact_store_enabled", False)

        assert await store.put("pgart-disabled:1", "hessian", b"anything") is None
        assert await store.list_for("pgart-disabled:1") == []

    asyncio.run(_run())
