"""The outbox delivers at least once, converges on redelivery, and never fails a calculation.

Three properties, and each was a design decision rather than an implementation detail:

- **Enqueue is idempotent**, because three call sites write to it with no coordination and a
  retried Temporal activity must not double-queue.
- **A failed delivery leaves its row pending**, because at-least-once against a content-addressed
  target is safe and losing a result is not.
- **Nothing here can fail the calculation that produced the record**, because by the time any of it
  runs the science is finished and persisted.
"""

import asyncio
from typing import Any

import psycopg
import pytest

from chemclaw.core.config import settings
from chemclaw.publish import outbox
from chemclaw.publish.record import Conditions, ResultRecord, Subject, SubjectMember, TheoryLevel
from tests.pg import migrated_db_or_skip


def _record(ref: str) -> ResultRecord:
    """A minimal but valid record — this file is about the queue, not the chemistry."""
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


async def _reset(conn: psycopg.AsyncConnection[Any]) -> None:
    """Empty the outbox, so each test starts from a known queue."""
    await conn.execute("DELETE FROM result_publications")
    await conn.commit()


def _with_sink(monkeypatch: pytest.MonkeyPatch, *names: str) -> None:
    """Enable sinks without needing a manifest on disk.

    Patched at the registry rather than through settings, because `enabled()` validates names
    against discovered manifests and this file is testing the queue, not discovery.
    """
    monkeypatch.setattr(outbox, "publishing_enabled", lambda: bool(names))
    monkeypatch.setattr(outbox, "enabled_names", lambda: list(names))


def test_publishing_costs_nothing_when_no_sink_is_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no sink enabled, enqueue does no database work at all.

    This is what keeps the subsystem free for a deployment that has not turned it on — and it is
    checked rather than asserted in prose, because the enqueue sits on the calculation path.
    """
    _with_sink(monkeypatch)

    async def _explode() -> None:
        raise AssertionError("enqueue must not open a connection when publishing is disabled")

    monkeypatch.setattr(outbox, "_connect", _explode)
    assert asyncio.run(outbox.enqueue([_record("k")])) == 0


def test_enqueueing_the_same_record_twice_queues_it_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The identity index is the idempotency, so the call sites need no coordination."""

    async def _run() -> None:
        await migrated_db_or_skip()
        _with_sink(monkeypatch, "alpha")
        async with outbox._connect() as conn:
            await _reset(conn)

        assert await outbox.enqueue([_record("dup")]) == 1
        assert await outbox.enqueue([_record("dup")]) == 0, "a second enqueue writes nothing"

        async with outbox._connect() as conn:
            cursor = await conn.execute(
                "SELECT count(*) FROM result_publications WHERE calc_ref = 'dup'"
            )
            row = await cursor.fetchone()
            assert row is not None and row[0] == 1

    asyncio.run(_run())


def test_one_record_is_queued_once_per_enabled_sink(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two sinks are two rows, so one destination being down cannot hold up another."""

    async def _run() -> None:
        await migrated_db_or_skip()
        _with_sink(monkeypatch, "alpha", "beta")
        async with outbox._connect() as conn:
            await _reset(conn)

        assert await outbox.enqueue([_record("fan")]) == 2
        alpha = await outbox.claim("alpha", 10)
        beta = await outbox.claim("beta", 10)
        assert [ref for _, ref, _ in alpha] == ["fan"]
        assert [ref for _, ref, _ in beta] == ["fan"]
        # Claiming for one sink must not consume the other's row.
        assert {row[0] for row in alpha}.isdisjoint({row[0] for row in beta})

    asyncio.run(_run())


def test_a_failed_delivery_leaves_the_row_pending_until_it_runs_out_of_attempts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A destination being down must not lose the record, and must not retry forever.

    The row stays `pending` and re-claimable while it has attempts left, then retires to `failed`
    — where it is kept, not deleted, because it is the record that something was never published.
    """

    async def _run() -> None:
        await migrated_db_or_skip()
        _with_sink(monkeypatch, "alpha")
        monkeypatch.setattr(settings, "result_publish_max_attempts", 2)
        async with outbox._connect() as conn:
            await _reset(conn)
        await outbox.enqueue([_record("flaky")])

        claimed = await outbox.claim("alpha", 10)
        assert len(claimed) == 1
        await outbox.mark_failed([claimed[0][0]], "destination unreachable")
        # Still claimable: one attempt spent of two.
        assert len(await outbox.claim("alpha", 10)) == 1

        await outbox.mark_failed([claimed[0][0]], "destination unreachable")
        assert await outbox.claim("alpha", 10) == [], "out of attempts, no longer claimed"

        async with outbox._connect() as conn:
            cursor = await conn.execute(
                "SELECT state, attempts, last_error FROM result_publications "
                "WHERE calc_ref = 'flaky'"
            )
            row = await cursor.fetchone()
            assert row is not None
            state, attempts, last_error = row
        assert (state, attempts) == ("failed", 2)
        assert "unreachable" in last_error, "the reason is kept for an operator to read"

    asyncio.run(_run())


def test_a_delivered_row_is_not_claimed_again(monkeypatch: pytest.MonkeyPatch) -> None:
    """Marking delivered removes the row from the queue without deleting it."""

    async def _run() -> None:
        await migrated_db_or_skip()
        _with_sink(monkeypatch, "alpha")
        async with outbox._connect() as conn:
            await _reset(conn)
        await outbox.enqueue([_record("done")])

        claimed = await outbox.claim("alpha", 10)
        await outbox.mark_delivered([row_id for row_id, _, _ in claimed])
        assert await outbox.claim("alpha", 10) == []

        async with outbox._connect() as conn:
            cursor = await conn.execute(
                "SELECT state, delivered_at IS NOT NULL FROM result_publications "
                "WHERE calc_ref = 'done'"
            )
            assert await cursor.fetchone() == ("delivered", True)

    asyncio.run(_run())


def test_a_broken_outbox_does_not_raise_into_the_calculation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole polarity of this subsystem, in one assertion.

    By the time enqueue runs the calculation has succeeded and is already persisted. A results
    store — or the local queue — being unavailable is strictly less important than returning the
    science, so the failure is counted and logged and the caller never sees it.
    """
    _with_sink(monkeypatch, "alpha")

    def _explode() -> Any:
        raise ConnectionError("postgres is gone")

    monkeypatch.setattr(outbox, "_connect", _explode)
    assert asyncio.run(outbox.enqueue([_record("k")])) == 0


def test_an_unprojectable_payload_does_not_raise_into_the_calculation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A calculator this release has no projector for is skipped, not failed.

    `calculation_results` is never pruned, so a deployment legitimately holds rows from calculators
    that no longer ship. A backfill must walk past those rather than abort on the first one.
    """
    _with_sink(monkeypatch, "alpha")
    written = asyncio.run(
        outbox.enqueue_payload(
            calc_ref="mystery@v1:a:b", calc_type="nothing.we.know", payload={"x": 1}
        )
    )
    assert written == 0


def test_claiming_a_row_spends_its_attempt(monkeypatch: pytest.MonkeyPatch) -> None:
    """The attempt is spent by the claim, not by the failure report.

    **This is what makes the budget correct when two runs overlap** — a scheduled drain and an
    operator's manual one. The claim commits before anything is delivered, because a delivery can
    take the better part of a minute and must not hold a row lock across it; so if the increment
    happened in `mark_failed` instead, both runs would see the same pending rows, deliver them
    twice and each record a failure. Duplicate delivery is safe (every key on the far side is a
    content hash); an attempt budget emptying twice as fast is not, because it retires rows a
    working destination would have accepted.
    """

    async def _run() -> None:
        await migrated_db_or_skip()
        _with_sink(monkeypatch, "alpha")
        monkeypatch.setattr(settings, "result_publish_max_attempts", 5)
        async with outbox._connect() as conn:
            await _reset(conn)
        await outbox.enqueue([_record("counted")])

        # Two claims with no `mark_failed` between them — as two overlapping runs would do.
        assert len(await outbox.claim("alpha", 10)) == 1
        assert len(await outbox.claim("alpha", 10)) == 1

        async with outbox._connect() as conn:
            cursor = await conn.execute(
                "SELECT attempts FROM result_publications WHERE calc_ref = 'counted'"
            )
            row = await cursor.fetchone()
        assert row is not None and row[0] == 2, "each claim spends one attempt"

    asyncio.run(_run())


def test_marking_failed_does_not_double_count_the_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A claim followed by its own failure report costs exactly one attempt, not two."""

    async def _run() -> None:
        await migrated_db_or_skip()
        _with_sink(monkeypatch, "alpha")
        monkeypatch.setattr(settings, "result_publish_max_attempts", 5)
        async with outbox._connect() as conn:
            await _reset(conn)
        await outbox.enqueue([_record("once")])

        claimed = await outbox.claim("alpha", 10)
        await outbox.mark_failed([row_id for row_id, _, _ in claimed], "nope")

        async with outbox._connect() as conn:
            cursor = await conn.execute(
                "SELECT attempts, state FROM result_publications WHERE calc_ref = 'once'"
            )
            row = await cursor.fetchone()
        assert row is not None and row == (1, "pending")

    asyncio.run(_run())
