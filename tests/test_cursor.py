"""The ELN sync cursor persists and advances (plan step 4.5, scheduled-run seam).

Integration test against Postgres (CI provides it; the offline sandbox skips). Proves the
self-cursoring contract: an unseen source reads the epoch, and a stored cursor round-trips
and overwrites on upsert — so consecutive scheduled runs resume without re-doing work.
"""

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime

from chemclaw.core import db
from chemclaw.core.config import settings
from chemclaw.ingest.eln.cursor import _EPOCH, _UPSERT, load_cursor, store_cursor
from tests.pg import migrated_db_or_skip


def test_unseen_source_reads_epoch() -> None:
    """A source that has never synced reads the epoch (ingest the whole backlog first run)."""

    async def _run() -> None:
        await migrated_db_or_skip()
        assert await load_cursor("source-never-synced") == _EPOCH

    asyncio.run(_run())


def test_cursor_round_trips_and_advances() -> None:
    """A stored cursor is read back, and a later store overwrites it (high-water advance)."""

    async def _run() -> None:
        await migrated_db_or_skip()
        source = "test-cursor-source"
        first = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
        await store_cursor(source, first)
        assert await load_cursor(source) == first

        later = datetime(2026, 6, 1, 9, 30, tzinfo=UTC)
        await store_cursor(source, later)
        assert await load_cursor(source) == later  # upsert advanced the mark

    asyncio.run(_run())


@dataclass(frozen=True)
class _Entry:
    """One ELN entry, reduced to what the cursor protocol actually reads: an id and its time."""

    id: str
    created_at: datetime


_CORPUS = [_Entry(f"e{n}", datetime(2026, 3, n, tzinfo=UTC)) for n in range(1, 7)]


async def _drain(
    source: str,
    ingested: list[str],
    *,
    batch: int = 6,
    max_chunks: int = 100,
    released: asyncio.Event | None = None,
) -> None:
    """One scheduled-shaped drain over `_CORPUS`, in the shape `ElnSyncWorkflow` runs it.

    Load the cursor once, then per chunk fetch what is strictly newer, ingest it, and store the
    chunk's high-water mark — the exact sequence of `load_sync_cursor`, `sync_eln_entries` and
    `store_sync_cursor`. `max_chunks` models the run bound (`eln_sync_max_iterations`, or a worker
    that dies mid-drain): the drain stops with its last chunk's cursor persisted. `released` holds
    the drain at the one point that matters — after its load, before its first store — so the
    interleaving under test is pinned rather than hoped for.
    """
    cursor = await load_cursor(source)
    if released is not None:
        await released.wait()
    for _ in range(max_chunks):
        chunk = [entry for entry in _CORPUS if entry.created_at > cursor][:batch]
        if not chunk:
            return
        ingested.extend(entry.id for entry in chunk)
        cursor = max(entry.created_at for entry in chunk)
        await store_cursor(source, cursor)


def test_two_overlapping_writers_leave_the_lagging_ones_cursor() -> None:
    """The row lock serializes two concurrent advances; it does not order them by value.

    The module docstring's "this needs no locking" rests on re-fetching being harmless, which is a
    claim about the *reader*. This is the writer, measured rather than reasoned about: two drains
    that both loaded the same mark, upserting inside overlapping transactions. Postgres does make
    the second wait — asserted here, so the test can tell a serialized write from an unserialized
    one — and then applies it on top, because `DO UPDATE SET cursor = EXCLUDED.cursor` is
    last-writer-wins rather than a high-water mark. The stored value ends up behind what the leader
    had already ingested.
    """

    async def _run() -> None:
        await migrated_db_or_skip()
        source = "test-cursor-overlap"
        await store_cursor(source, _EPOCH)
        ahead = datetime(2026, 6, 1, tzinfo=UTC)
        behind = datetime(2026, 3, 1, tzinfo=UTC)
        async with (
            db.connection(settings.postgres_dsn) as leading,
            db.connection(settings.postgres_dsn) as lagging,
        ):
            await leading.execute(_UPSERT, (source, ahead))
            blocked = asyncio.ensure_future(lagging.execute(_UPSERT, (source, behind)))
            await asyncio.sleep(0.2)
            assert not blocked.done(), "the lagging upsert should be waiting on the row lock"
            await leading.commit()
            await blocked
            await lagging.commit()
        assert await load_cursor(source) == behind

    asyncio.run(_run())


def test_a_regressed_cursor_re_ingests_and_never_skips_an_entry() -> None:
    """Two drains racing on one source cost re-ingestion; no entry is ever passed over.

    The interleaving: drain A loads the epoch and drains the whole corpus; drain B loaded the same
    epoch first, is released once A is done, ingests one chunk and stops there — leaving the stored
    cursor *behind* what A already ingested. The next scheduled fire (drain C) therefore re-reads
    from B's stale mark.

    What is asserted is the invariant that decides whether this row needs a lock: the union of what
    the three drains ingested is the whole corpus. A lost update on this cursor can only ever move
    it backwards — every stored value is a mark someone had ingested through — so the cost is
    duplicate work against an idempotent ingest, never an entry nobody read.
    """

    async def _run() -> None:
        await migrated_db_or_skip()
        source = "test-cursor-race"
        await store_cursor(source, _EPOCH)
        released = asyncio.Event()
        first: list[str] = []
        second: list[str] = []

        async def _lagging() -> None:
            await _drain(source, second, batch=2, max_chunks=1, released=released)

        async def _leading() -> None:
            await _drain(source, first)
            released.set()

        # Both load the epoch, then interleave: B's load happens first, its store happens last.
        lagging = asyncio.ensure_future(_lagging())
        await asyncio.sleep(0)  # let B reach its load before A advances the cursor
        await _leading()
        await lagging

        assert first == [entry.id for entry in _CORPUS]
        assert second == ["e1", "e2"]
        # The measured regression: A ingested through e6, the stored mark is back at e2.
        assert await load_cursor(source) == _CORPUS[1].created_at

        third: list[str] = []
        await _drain(source, third)
        assert third == ["e3", "e4", "e5", "e6"]  # re-ingested, not skipped
        assert set(first) | set(second) | set(third) == {entry.id for entry in _CORPUS}
        assert await load_cursor(source) == _CORPUS[-1].created_at

    asyncio.run(_run())
