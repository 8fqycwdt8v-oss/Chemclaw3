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


def test_overwriting_a_name_repoints_the_link_and_deliberately_leaks_its_predecessor() -> None:
    """A second `put` under the same `(calc_key, name)` updates the link and **keeps** the old blob.

    `_UPSERT_LINK` is `ON CONFLICT (calc_key, name) DO UPDATE`, never a second row — so the link
    must resolve to the *new* content afterwards. Nothing then references the old blob: it keeps no
    link, no `calculation_results` payload names it (the row is rewritten by the same `put`), no
    note can cite it (`artifact_refs` are `<calc_key>#<name>`, resolved through the link table), and
    `durable/artifact_eviction.py` does not collect it in any shipped configuration, because
    `artifact_store_max_bytes` and `artifact_evict_idle_days` are both 0. Measured, one rewrite
    leaves `blobs=2 links=1 unlinked_blobs=1`, and that is real growth a deployment cannot see.

    **It is asserted as intended anyway, because the obvious fix destroyed data.** A reclaiming
    `DELETE ... WHERE NOT EXISTS (a link)` was written here, guarded by a `FOR UPDATE` on the link
    row, and a review reproduced it deleting *another calculation's committed artifact* against a
    live database: `NOT EXISTS` is evaluated on the deleting transaction's snapshot, the `DELETE`
    blocks on the `KEY SHARE` lock a concurrent inserter holds on that blob, and PostgreSQL then
    proceeds without re-evaluating the subquery. Content addressing makes that collision ordinary
    rather than exotic — migration 019 exists so two runs producing an identical geometry store one
    copy — so a rewrite of one key racing a first write of another onto the same bytes cascaded the
    second key's link away after its transaction had committed.

    So this asserts the leak on purpose: a leaked blob is wasted bytes, and the reclaim deleted
    science. Whoever re-adds one must order it against a concurrent *link insert*, not against a
    concurrent rewrite of the same link, and must decide what the losing writer sees.
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

        assert await store.open(first.content_hash) == b"stale hessian" * 5, (
            "the predecessor is leaked deliberately; see this test's docstring before reclaiming it"
        )
        assert await store.open(second.content_hash) == b"refreshed hessian" * 5

    asyncio.run(_run())


def test_a_rewrite_keeps_a_blob_another_calculation_still_links() -> None:
    """The invariant any future reclaim must not break, kept as a standing guard.

    Identical bytes are one blob shared by every calculation that produced them, so a rewrite may
    only ever delete what *no* link still names. This is the **serial** case and it passes with no
    reclaim at all, which is precisely why it is not evidence that a reclaim would be safe — the
    failure a reclaim produces is a race, and a review had to drive the interleaving against a live
    database to see it. Left here as the floor a reimplementation has to clear before it starts
    thinking about concurrency.
    """

    async def _run() -> None:
        store = await _store_or_skip()
        shared = b"a shared hessian" * 5
        first = await store.put("pgart-shared-a:1", "hessian", shared)
        second = await store.put("pgart-shared-b:1", "hessian", shared)
        assert first is not None and second is not None
        assert first.content_hash == second.content_hash

        # Rewrite one of the two links; the other still names the blob.
        await store.put("pgart-shared-a:1", "hessian", b"a different hessian" * 5)
        assert await store.open(first.content_hash) == shared

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
