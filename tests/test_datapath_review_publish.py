"""The three publish/ingest readings a reviewer executed and found saying the wrong number.

Each one had a comment asserting the property it did not have: the ingest lag gauge was documented
as a per-source reading and one naive datetime removed the whole family; the dead-letter count was
documented as "exact rather than inferred" and was per call rather than per transition; and the
backlog refresh was documented as excluding the rows a pass is about to deliver, while `_CLAIM`
leaves them `pending`.
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
    """`_cursor_lags` subtracts in a comprehension, so one bad value poisons every source.

    Measured on the unfixed tree: `observe_cursor("naive-source", datetime(2026, 1, 1))` made the
    gauge callable raise `TypeError: can't subtract offset-naive and offset-aware datetimes`, the
    registry's guard dropped `chemclaw_ingest_cursor_lag_seconds` **entirely** from the exposition,
    and `ChemclawIngestCursorStalled` had nothing left to fire on — for every source, permanently,
    with `chemclaw_gauge_read_failures_total` the only trace.

    `store_cursor` is the reachable door: `sync_cursors.cursor` is `TIMESTAMPTZ` so the load path
    cannot produce one, but the store path persists whatever `durable/eln_sync.py` computed from an
    ELN's own timestamps and nothing enforces tz-awareness on the way in.
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
    """`RETURNING state` returns the new state for every *matched* row, changed or not.

    `outbox.py` claims `RETURNING state` "is what makes the dead-letter count exact rather than
    inferred". Measured without the `AND state = 'pending'` guard: `mark_failed(ids)` on the same
    three ids twice booked `chemclaw_results_dead_lettered_total` 0 → 3 → 6 and logged "3 retired
    to dead-letter" both times, for three retirements. Retiring a row is a transition, and a
    transition happens once.
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
        await outbox.mark_failed(ids, "the endpoint refused")
        await outbox.mark_failed(ids, "the endpoint refused")

    asyncio.run(run())

    assert _counter("chemclaw_results_dead_lettered_total") == before + 3


def test_claiming_publishes_no_backlog_reading_because_a_claim_delivers_nothing() -> None:
    """`_CLAIM` only spends the attempt; the row it returns is still `pending`.

    The refresh used to sit inside `claim()`, justified by a comment saying the reading was taken
    after the claim "so the reading excludes the rows this pass is about to deliver". Measured:
    three rows, one `claim()`, and `chemclaw_outbox_pending{sink=...}` read **3.0** with all three
    still pending — the pre-drain depth, published as the current one and held for a whole pass.

    So a claim now publishes nothing, and the reading a scrape sees is the one taken after the
    rows were marked.
    """
    asyncio.run(migrated_db_or_skip())
    sink = "review-claim"
    outbox._PENDING_GAUGE.pop(sink, None)

    async def run() -> list[int]:
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
        return [row[0] for row in claimed]

    ids = asyncio.run(run())

    assert sink not in outbox._PENDING_GAUGE, (
        "claim() published a backlog reading for rows it has not delivered"
    )

    # What a reading taken at that moment would have said, and why it was the wrong one: all three
    # rows are still queued after being claimed, because a claim spends an attempt and nothing else.
    asyncio.run(outbox.refresh_backlog())
    assert _series("chemclaw_outbox_pending", sink=sink) == 3.0

    # And what the pass now publishes instead, once the rows have actually gone.
    asyncio.run(outbox.mark_delivered(ids))
    asyncio.run(outbox.refresh_backlog())
    assert _series("chemclaw_outbox_pending", sink=sink) == 0.0


def test_a_drain_pass_refreshes_the_backlog_once_after_every_sink() -> None:
    """The refresh is a property of the *pass*, not of a claim — and it runs with no sink enabled.

    Both halves matter. Once per pass rather than once per sink, because `refresh_backlog` reads
    every sink in two `GROUP BY sink` statements and one of them is a sequential scan of the whole
    table (`_DEAD_LETTERED`, ~20 ms on 200k rows measured with `EXPLAIN (ANALYZE, BUFFERS)`) — N-1
    of N reads per pass were redundant. And after the marking rather than before it, which is the
    reading `claim()` could not give.
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
