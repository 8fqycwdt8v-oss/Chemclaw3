"""Three publish/ingest readings that must report the right number.

The ingest lag gauge must survive one naive datetime, the dead-letter count must be per
transition, and the backlog must be read after a pass marks its rows (a claim leaves them
`pending`).
"""

import asyncio
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest

from chemclaw.core import db
from chemclaw.core.config import settings
from chemclaw.durable import publish_results
from chemclaw.ingest.eln import cursor as eln_cursor
from chemclaw.publish import outbox
from chemclaw.publish.manifest import ResultSinkManifest
from chemclaw.publish.record import Conditions, ResultRecord, Subject, SubjectMember, TheoryLevel
from tests.pg import migrated_db_or_skip
from tests.test_datapath_observability import _counter, _rendered, _series


def _record(ref: str) -> ResultRecord:
    """A minimal but valid record — these tests are about the queue, not the chemistry."""
    return ResultRecord(
        calc_ref=ref,
        calc_type="pka",
        subject=Subject(
            kind="molecule",
            members=[SubjectMember(ordinal=0, role="subject", smiles="CCO")],
            label="CCO",
        ),
        conditions=Conditions(),
        level=TheoryLevel(method="GFN2-xTB"),
    )


@pytest.fixture
def _clean_cursor_observations() -> Iterator[None]:
    """`_OBSERVED` is module state a whole process shares; put back what was there."""
    saved = dict(eln_cursor._OBSERVED)
    eln_cursor._OBSERVED.clear()
    yield
    eln_cursor._OBSERVED.clear()
    eln_cursor._OBSERVED.update(saved)


def test_one_naive_cursor_does_not_take_the_whole_lag_family_off_the_scrape(
    _clean_cursor_observations: None,
) -> None:
    """One naive cursor does not take the whole lag family off the scrape.

    `_cursor_lags` subtracts in a comprehension, so one naive datetime would raise and the registry
    would drop `chemclaw_ingest_cursor_lag_seconds` for every source. `store_cursor` is the
    reachable
    door: it persists what `durable/eln_sync.py` computed from an ELN's own timestamps.
    """
    eln_cursor.observe_cursor("review-aware", datetime.now(UTC))
    eln_cursor.observe_cursor("review-naive", datetime(2026, 1, 1))

    lags = eln_cursor._cursor_lags()
    assert set(lags) == {"review-aware", "review-naive"}
    # Read as UTC rather than rejected: this is telemetry, and refusing the observation would lose
    # the very reading the caller came to give.
    assert lags["review-naive"] > 0.0

    rendered = _rendered("chemclaw_ingest_cursor_lag_seconds")
    assert any('source="review-aware"' in line for line in rendered)
    assert any('source="review-naive"' in line for line in rendered)


def test_the_dead_letter_count_is_per_transition_not_per_call() -> None:
    """The dead-letter count is per transition, not per call.

    `RETURNING state` returns every *matched* row, changed or not, so without `AND state =
    'pending'` a repeated `mark_failed` would count the same retirements again.
    """
    asyncio.run(migrated_db_or_skip())
    before = _counter("chemclaw_results_dead_lettered_total")

    async def run() -> None:
        async with db.connection(settings.postgres_dsn) as conn:
            await conn.execute("DELETE FROM result_publications WHERE sink = 'review-dead'")
            cursor = await conn.execute(
                "INSERT INTO result_publications (sink, calc_ref, document, schema_version, "
                "attempts) VALUES ('review-dead', 'calc-a', '{}'::jsonb, 1, %(n)s), "
                "('review-dead', 'calc-b', '{}'::jsonb, 1, %(n)s), "
                "('review-dead', 'calc-c', '{}'::jsonb, 1, %(n)s) RETURNING id",
                {"n": settings.result_publish_max_attempts},
            )
            ids = [int(row[0]) for row in await cursor.fetchall()]
            await conn.commit()
        # Inserted at the budget rather than claimed, so the fence value is the attempts on the row.
        leases = [outbox.Lease(row_id, settings.result_publish_max_attempts) for row_id in ids]
        await outbox.mark_failed(leases, "the endpoint refused")
        await outbox.mark_failed(leases, "the endpoint refused")

    asyncio.run(run())

    assert _counter("chemclaw_results_dead_lettered_total") == before + 3


def test_claiming_publishes_no_backlog_reading_because_a_claim_delivers_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Claiming publishes no backlog reading, because a claim delivers nothing.

    `_CLAIM` only spends the attempt; its rows are still `pending`, so a reading taken there would
    be the pre-drain depth held for a whole pass. The reading is taken after the rows are marked.
    """
    asyncio.run(migrated_db_or_skip())
    sink = "review-claim"
    outbox._PENDING_GAUGE.pop(sink, None)
    # The backlog gauges read only *enabled* sinks (a removed sink's rows would page forever), so
    # the probe sink is enabled, patched at the module the reader uses as
    # `tests/test_publish_outbox.py::_with_sink` does, since this tests the reading, not discovery.
    monkeypatch.setattr(outbox, "enabled_names", lambda: [sink])

    async def run() -> list[outbox.Lease]:
        async with db.connection(settings.postgres_dsn) as conn:
            await conn.execute("DELETE FROM result_publications WHERE sink = %s", (sink,))
            await conn.execute(
                "INSERT INTO result_publications (sink, calc_ref, document, schema_version) "
                "VALUES (%(s)s, 'calc-1', '{}'::jsonb, 1), (%(s)s, 'calc-2', '{}'::jsonb, 1), "
                "(%(s)s, 'calc-3', '{}'::jsonb, 1)",
                {"s": sink},
            )
            await conn.commit()
        claimed = await outbox.claim(sink, 10)
        assert len(claimed) == 3
        return [row.lease for row in claimed]

    leases = asyncio.run(run())

    assert sink not in outbox._PENDING_GAUGE, (
        "claim() published a backlog reading for rows it has not delivered"
    )

    # What a reading taken at that moment would have said, and why it was the wrong one: all three
    # rows are still queued after being claimed, because a claim spends an attempt and nothing else.
    asyncio.run(outbox.refresh_backlog())
    assert _series("chemclaw_outbox_pending", sink=sink) == 3.0

    # And what the pass now publishes instead, once the rows have actually gone.
    asyncio.run(outbox.mark_delivered(leases))
    asyncio.run(outbox.refresh_backlog())
    assert _series("chemclaw_outbox_pending", sink=sink) == 0.0


def test_a_drain_pass_refreshes_the_backlog_once_after_every_sink() -> None:
    """The refresh is a property of the *pass*, not of a claim — and it runs with no sink enabled.

    Once per pass rather than per sink, because `refresh_backlog` reads every sink and one of its
    statements is a sequential scan; and after the marking, which `claim()` could not give.
    """
    asyncio.run(migrated_db_or_skip())
    calls: list[int] = []
    real_refresh = outbox.refresh_backlog
    delivered: list[str] = []
    for name in ("review-alpha", "review-beta"):
        outbox._PENDING_GAUGE.pop(name, None)

    async def counting_refresh(dsn: str | None = None) -> None:
        calls.append(1)
        await real_refresh(dsn)

    class _Sink:
        """Accepts everything, so the pass reaches `mark_delivered` for both sinks."""

        def __init__(self, name: str) -> None:
            self._name = name

        async def deliver(self, records: list[Any]) -> None:
            delivered.extend(f"{self._name}:{record.calc_ref}" for record in records)

        async def aclose(self) -> None:
            return None

    manifests = [
        ResultSinkManifest(name="review-alpha", description="x", driver="m:c"),
        ResultSinkManifest(name="review-beta", description="x", driver="m:c"),
    ]

    async def run() -> None:
        async with db.connection(settings.postgres_dsn) as conn:
            await conn.execute("DELETE FROM result_publications")
            await conn.commit()
        assert await outbox.enqueue([_record("calc-pass")]) == 2
        await publish_results._drain_result_publications()

    monkey = pytest.MonkeyPatch()
    monkey.setattr(outbox, "publishing_enabled", lambda: True)
    monkey.setattr(outbox, "enabled_names", lambda: [m.name for m in manifests])
    monkey.setattr(publish_results, "enabled", lambda: manifests)
    monkey.setattr(publish_results, "build", lambda m: _Sink(m.name))
    monkey.setattr(outbox, "refresh_backlog", counting_refresh)
    try:
        asyncio.run(run())
    finally:
        monkey.undo()

    assert len(delivered) == 2, "both sinks took the record"
    assert calls == [1], "the backlog was read once for the pass, not once per sink"
    # Post-drain, which is the reading the gauge exists to give. With the refresh inside `claim()`
    # both of these read 1.0 — the row each sink was about to deliver, published as its backlog.
    assert outbox._PENDING_GAUGE.get("review-alpha", 0.0) == 0.0
    assert outbox._PENDING_GAUGE.get("review-beta", 0.0) == 0.0


async def _row(sink: str, calc_ref: str = "calc-fence") -> int:
    """One fresh pending row for `sink`, with anything already there removed."""
    async with db.connection(settings.postgres_dsn) as conn:
        await conn.execute("DELETE FROM result_publications WHERE sink = %s", (sink,))
        cursor = await conn.execute(
            "INSERT INTO result_publications (sink, calc_ref, document, schema_version) "
            "VALUES (%s, %s, '{}'::jsonb, 1) RETURNING id",
            (sink, calc_ref),
        )
        row = await cursor.fetchone()
        await conn.commit()
    assert row is not None
    return int(row[0])


async def _row_state(row_id: int) -> tuple[str, int, bool]:
    """`(state, attempts, lease_released)` for one row."""
    async with db.connection(settings.postgres_dsn) as conn:
        cursor = await conn.execute(
            "SELECT state, attempts, claimed_at IS NULL FROM result_publications WHERE id = %s",
            (row_id,),
        )
        row = await cursor.fetchone()
    assert row is not None
    return str(row[0]), int(row[1]), bool(row[2])


def test_a_superseded_pass_cannot_release_the_lease_the_live_pass_holds() -> None:
    """A superseded pass cannot release the lease the live pass holds.

    `claimed_at = NULL` is a release, and keyed on id alone an expired pass's `mark_failed` would
    requeue a row a live pass is delivering, spending attempts on nobody's failure. The fence is
    `attempts`, which `_CLAIM` increments in the statement that takes the lease, so the number a
    pass holds names its own claim.
    """
    asyncio.run(migrated_db_or_skip())

    async def run() -> tuple[tuple[str, int, bool], int]:
        row_id = await _row("review-fence")
        claimed = await outbox.claim("review-fence", 10)
        assert [row.lease.row_id for row in claimed] == [row_id]
        held = claimed[0].lease

        # A superseded pass: same row, a lease value it no longer holds.
        await outbox.mark_failed([outbox.Lease(row_id, held.attempt - 1)], "a stale pass's outage")
        after_stale = await _row_state(row_id)
        # Nothing may be claimable while the live pass still holds it.
        again = await outbox.claim("review-fence", 10)
        # The pass that does hold the lease is still able to record its outcome.
        await outbox.mark_failed([held], "the destination refused")
        return after_stale, len(again)

    after_stale, reclaimed = asyncio.run(run())

    state, attempts, released = after_stale
    assert (state, attempts) == ("pending", 1)
    assert released is False, (
        "the stale mark released the live pass's lease — the row goes back in the queue and the "
        "next claim spends another attempt for no delivery"
    )
    assert reclaimed == 0, "so no second drain could take the row while it was still leased"


def test_a_superseded_pass_cannot_mark_delivered_what_the_live_pass_still_holds() -> None:
    """A superseded pass cannot mark delivered what the live pass still holds.

    The state guard cannot cover this: a live pass's row is `pending` too. Without the
    `AND p.attempts = lease.attempt` fence a stale delivery report would retire the row, count a
    publication that did not happen, and make the live pass's real outcome a no-op. Worse than the
    `mark_failed` twin, because a wrongly delivered row is never looked at again.
    """
    asyncio.run(migrated_db_or_skip())
    before = _counter("chemclaw_results_published_total")

    async def run() -> tuple[tuple[str, int, bool], int]:
        row_id = await _row("review-fence-delivered")
        claimed = await outbox.claim("review-fence-delivered", 10)
        assert [row.lease.row_id for row in claimed] == [row_id]
        held = claimed[0].lease

        # A superseded pass claiming it delivered, under a lease it no longer holds.
        await outbox.mark_delivered([outbox.Lease(row_id, held.attempt - 1)])
        after_stale = await _row_state(row_id)
        # And the pass that does hold it can still record what really happened.
        await outbox.mark_delivered([held])
        return after_stale, len(await outbox.claim("review-fence-delivered", 10))

    after_stale, reclaimed = asyncio.run(run())

    state, attempts, released = after_stale
    assert (state, attempts) == ("pending", 1), (
        f"a stale mark_delivered moved the row to {state!r}: the queue now reports a publication "
        "that never happened, and the live pass's own outcome will be dropped by its pending guard"
    )
    assert released is False, "and it released the live pass's lease on the way"
    assert _counter("chemclaw_results_published_total") == before + 1.0, (
        "exactly one transition was published — the live pass's. `RETURNING id` is what makes that "
        "counter a count of transitions rather than of call arguments"
    )
    assert reclaimed == 0


def test_a_stale_mark_delivered_cannot_walk_a_dead_lettered_row_back() -> None:
    """A stale `mark_delivered` cannot walk a dead-lettered row back.

    A claimed row is `pending` by construction, so the guard is the transition's own precondition;
    without it a `failed` row would become `delivered` and the dead-letter count and table disagree.
    """
    asyncio.run(migrated_db_or_skip())
    before = _counter("chemclaw_results_published_total")

    async def run() -> tuple[str, int, bool]:
        row_id = await _row("review-fence-dead")
        async with db.connection(settings.postgres_dsn) as conn:
            await conn.execute(
                "UPDATE result_publications SET state = 'failed', attempts = %s, "
                "last_error = 'the destination refused' WHERE id = %s",
                (settings.result_publish_max_attempts, row_id),
            )
            await conn.commit()
        await outbox.mark_delivered([outbox.Lease(row_id, settings.result_publish_max_attempts)])
        return await _row_state(row_id)

    state, _, _ = asyncio.run(run())

    assert state == "failed", "a dead-lettered row is the record that something was not published"
    assert _counter("chemclaw_results_published_total") == before, (
        "and nothing may be booked as published, or the counter stops counting transitions"
    )


def test_the_pass_that_holds_the_lease_delivers_and_marks_normally() -> None:
    """The fence must not cost the ordinary path.

    A guard that also blocked the legitimate mark would be a queue that never drains, so the happy
    path is pinned beside the two refusals.
    """
    asyncio.run(migrated_db_or_skip())
    before = _counter("chemclaw_results_published_total")

    async def run() -> tuple[str, int, bool]:
        row_id = await _row("review-fence-ok")
        claimed = await outbox.claim("review-fence-ok", 10)
        await outbox.mark_delivered([row.lease for row in claimed])
        return await _row_state(row_id)

    state, attempts, released = asyncio.run(run())

    assert (state, attempts, released) == ("delivered", 1, True)
    assert _counter("chemclaw_results_published_total") == before + 1
