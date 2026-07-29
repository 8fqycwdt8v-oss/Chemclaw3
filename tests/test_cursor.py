"""The ELN sync cursor persists and advances (plan step 4.5, scheduled-run seam).

Integration test against Postgres (CI provides it; the offline sandbox skips). Proves the
self-cursoring contract: an unseen source reads the epoch, and a stored cursor round-trips
and overwrites on upsert — so consecutive scheduled runs resume without re-doing work.
"""

import asyncio
from datetime import UTC, datetime

from eln.cursor import _EPOCH, load_cursor, store_cursor
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
