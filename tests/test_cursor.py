"""The ELN sync cursor persists and advances (plan step 4.5, scheduled-run seam).

Integration test against Postgres (CI provides it; the offline sandbox skips). Proves the
self-cursoring contract: an unseen source reads the epoch, a stored cursor round-trips, and the
upsert is a **high-water** mark rather than last-writer-wins — so consecutive scheduled runs resume
without re-doing work, and a lagging writer cannot undo a leading one's advance.

The last test in this file is the other half of that decision, and it asserts an *absence*:
`ingest/labels/cursor.py`'s keyset upsert must **not** grow the same `GREATEST`, because its column
is `TEXT` and the comparison would be lexicographic. That one measures the ordering against the
same Postgres rather than restating it, so the reason stays checkable.
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
    """Clear `source`'s row, which is the only way to rewind a cursor now that it is high-water.

    `store_cursor(source, _EPOCH)` was this file's reset until the upsert became `GREATEST`; it
    then silently stopped resetting anything, which is the shape of failure a test helper hides
    best. Deleting the row is also what an operator does to force a re-drain, so the helper and the
    supported procedure are the same act.
    """
    async with db.connection(settings.postgres_dsn) as conn:
        await conn.execute("DELETE FROM sync_cursors WHERE source = %s", (source,))
        await conn.commit()


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


def test_a_lagging_writer_cannot_pull_the_cursor_backwards() -> None:
    """The row lock serializes two concurrent advances; `GREATEST` also orders them by value.

    Both halves are asserted, because only the first was ever true. Postgres does make the second
    upsert wait — pinned here so the test can tell a serialized write from an unserialized one —
    and `DO UPDATE SET cursor = EXCLUDED.cursor` then applied it on top, leaving the stored mark
    *behind* what the leading drain had already ingested. Measured, at the point this was fixed:

        cursor after worker A stores 2026-06-01: 2026-06-01 00:00:00+00:00
        cursor after worker B stores 2026-03-01: 2026-03-01 00:00:00+00:00

    `D-2026-08-27-what-a-second-background-worker-would-race-on` measured exactly that and left it,
    on the argument that backwards is the harmless direction against an idempotent ingest. It is —
    and the guarantee then belonged to a different module. `GREATEST(sync_cursors.cursor,
    EXCLUDED.cursor)` moves it into the statement, and this is where that is checked.
    """

    async def _run() -> None:
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

    asyncio.run(_run())


def test_the_lag_gauge_reports_the_stored_mark_and_not_the_losing_one() -> None:
    """A store the table refused must not move `chemclaw_ingest_cursor_lag_seconds` backwards.

    `store_cursor` observes what the statement `RETURNING`s rather than what it was handed, and
    those differ exactly when `GREATEST` discards a lagging value. Handed the argument instead, the
    gauge would report a regression the table had just refused — an alert firing on a cursor that
    never moved, which is worse than the silence it replaced because it names a source that is fine.
    """

    async def _run() -> None:
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

    asyncio.run(_run())


def test_a_drain_that_lost_a_race_neither_regresses_the_mark_nor_skips_an_entry() -> None:
    """Two drains racing on one source: the mark stays at the leader's, and nothing is passed over.

    The interleaving: drain A loads the epoch and drains the whole corpus; drain B loaded the same
    epoch first, is released once A is done, ingests one chunk and stops there — storing a mark
    *behind* what A already ingested. Under the blind upsert that store won, and the next scheduled
    fire re-read from it: measured, drain C then re-ingested `e3…e6`. Under `GREATEST` the losing
    store is a no-op, so C has nothing left to do.

    Both halves are asserted, because the second is the one the old spelling also satisfied and the
    first is the one it did not. The union of what the drains ingested is still the whole corpus —
    no entry is skipped either way; that invariant was never the thing at risk. What changed is that
    the duplicate work is gone and the guarantee no longer depends on the ingest being idempotent.
    """

    async def _run() -> None:
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

    asyncio.run(_run())


def test_the_keyset_cursor_is_deliberately_not_a_high_water_upsert() -> None:
    """`ingest/labels/cursor.py` must keep its blind upsert, and the reason is measured here.

    The two modules' statements are the same shape over different column types, which is exactly
    the invitation to copy the fix across. `sync_cursors.cursor` is `TIMESTAMPTZ` and orders the way
    the drain advances; `corpus_cursors.after` is `TEXT` holding a keyset position in the *source's*
    domain, which its own docstring says "may be a bigint, a ULID or a padded string". `GREATEST`
    on text is lexicographic, so on a bigint feed it would pin the cursor at the first single-digit
    id it reached and skip every row past it — forwards, which is the direction that loses data,
    where a blind write can only ever cost a re-drain.

    Asserted as an absence with the ordering *measured* rather than asserted in prose, because the
    claim that makes the absence correct is a fact about Postgres and not about this repository.
    A `ge` comparison here would pass on `'9' >= '10'` being false for the wrong reason; the query
    is what says which value `GREATEST` picks.
    """

    async def _run() -> None:
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

    asyncio.run(_run())
