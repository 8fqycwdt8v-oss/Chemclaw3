"""The ELN sync cursor persists and advances.

Integration test against Postgres. An unseen source reads the epoch, a stored cursor round-trips,
and the upsert is a **high-water** mark, so consecutive runs resume and a lagging writer cannot
undo a leading one's advance. The last test asserts the opposite for `ingest/labels/cursor.py`:
its `TEXT` keyset must not use `GREATEST`, which would compare lexicographically.
"""

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime

import pytest

from chemclaw.core import db
from chemclaw.core.config import settings
from chemclaw.ingest.eln.cursor import (
    _EPOCH,
    _OBSERVED,
    _UPSERT,
    _cursor_lags,
    load_cursor,
    store_cursor,
)
from chemclaw.ingest.labels.cursor import _UPSERT as _KEYSET_UPSERT
from tests.pg import migrated_db_or_skip


async def _reset(source: str) -> None:
    """Clear `source`'s row, the only way to rewind a high-water cursor.

    Storing the epoch no longer resets anything under `GREATEST`. Deleting the row is also what an
    operator does to force a re-drain.
    """
    async with db.connection(settings.postgres_dsn) as conn:
        await conn.execute("DELETE FROM sync_cursors WHERE source = %s", (source,))
        await conn.commit()


async def test_unseen_source_reads_epoch() -> None:
    """A source that has never synced reads the epoch (ingest the whole backlog first run)."""
    await migrated_db_or_skip()
    assert await load_cursor("source-never-synced") == _EPOCH


async def test_cursor_round_trips_and_advances() -> None:
    """A stored cursor is read back, and a later store overwrites it (high-water advance)."""
    await migrated_db_or_skip()
    source = "test-cursor-source"
    first = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    await store_cursor(source, first)
    assert await load_cursor(source) == first

    later = datetime(2026, 6, 1, 9, 30, tzinfo=UTC)
    await store_cursor(source, later)
    assert await load_cursor(source) == later  # upsert advanced the mark


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

    Load the cursor once, then per chunk fetch what is newer, ingest, and store the chunk's
    high-water mark. `max_chunks` models the run bound or a worker dying mid-drain. `released` holds
    the drain after its load and before its first store, so the interleaving is pinned.
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


async def test_a_lagging_writer_cannot_pull_the_cursor_backwards() -> None:
    """The row lock serializes two concurrent advances; `GREATEST` also orders them by value.

    Postgres makes the second upsert wait (pinned here so a serialized write is distinguishable),
    but a blind `SET cursor = EXCLUDED.cursor` would then move the mark backwards. `GREATEST` puts
    the monotonicity guarantee in the statement rather than relying on an idempotent ingest.
    """
    await migrated_db_or_skip()
    source = "test-cursor-overlap"
    await _reset(source)
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
    assert await load_cursor(source) == ahead


async def test_the_lag_gauge_reports_the_stored_mark_and_not_the_losing_one() -> None:
    """A store the table refused must not move `chemclaw_ingest_cursor_lag_seconds` backwards.

    `store_cursor` observes what the statement `RETURNING`s, not what it was handed; otherwise the
    gauge would alert on a source that is fine.
    """
    await migrated_db_or_skip()
    source = "test-cursor-gauge"
    await _reset(source)
    ahead = datetime(2026, 6, 1, tzinfo=UTC)
    behind = datetime(2026, 3, 1, tzinfo=UTC)
    await store_cursor(source, ahead)
    await store_cursor(source, behind)
    assert _OBSERVED[source] == ahead
    # And the lag derived from it is the leading mark's, not the losing one's.
    observed_lag = _cursor_lags()[source]
    assert observed_lag == pytest.approx((datetime.now(UTC) - ahead).total_seconds(), abs=60)


async def test_a_drain_that_lost_a_race_neither_regresses_the_mark_nor_skips_an_entry() -> None:
    """Two drains racing on one source: the mark stays at the leader's, and nothing is passed over.

    Drain A drains everything; drain B, which loaded the same epoch, stores a mark behind A's. Under
    `GREATEST` that store is a no-op, so a following drain C has nothing to redo. No entry is
    skipped either way; what this proves is that the duplicate work is gone.
    """
    await migrated_db_or_skip()
    source = "test-cursor-race"
    await _reset(source)
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
    # The mark B stored is behind what A ingested, and the table kept A's.
    assert await load_cursor(source) == _CORPUS[-1].created_at

    third: list[str] = []
    await _drain(source, third)
    assert third == []  # nothing to re-ingest: the mark never went back
    assert set(first) | set(second) == {entry.id for entry in _CORPUS}


async def test_the_keyset_cursor_is_deliberately_not_a_high_water_upsert() -> None:
    """`ingest/labels/cursor.py` must keep its blind upsert, and the reason is measured here.

    `corpus_cursors.after` is `TEXT` holding a source-domain keyset position (bigint, ULID, padded
    string). `GREATEST` on text is lexicographic, so on a bigint feed it would pin the cursor and
    skip rows forwards, losing data, where a blind write only costs a re-drain. The ordering is
    measured against Postgres, since that is the fact the absence rests on.
    """
    await migrated_db_or_skip()
    async with db.connection(settings.postgres_dsn) as conn:
        result = await conn.execute("SELECT GREATEST(%s::text, %s::text)", ("9", "10"))
        row = await result.fetchone()
    assert row is not None and row[0] == "9", (
        "GREATEST on text no longer orders lexicographically, so the reason this cursor "
        "declines the high-water spelling needs re-deriving rather than re-reading"
    )
    assert "GREATEST" not in _KEYSET_UPSERT, (
        "the keyset cursor grew the ELN cursor's high-water upsert; on a bigint feed that "
        "pins the position at the first single-digit id and skips every row past it"
    )
