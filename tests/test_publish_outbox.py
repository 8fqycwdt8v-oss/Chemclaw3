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
import logging
import time
from typing import Any

import psycopg
import pytest
from psycopg.types.json import Jsonb

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


def _with_a_short_lease(monkeypatch: pytest.MonkeyPatch) -> None:
    """Shorten the claim lease so an abandoned claim's expiry is real rather than hand-written.

    A claim is a lease, so a pass that *dies* — a pod eviction, an activity timeout, the per-sink
    ceiling — leaves the row held until that lease expires, and "the next pass picks it up" is only
    true afterwards. Every test below that simulates a dead pass by claiming and never marking has
    to let the lease run out, and letting the real predicate do it (rather than writing `claimed_at`
    back in SQL) is what keeps the simulation the same shape as the failure it stands for.

    The lease is derived from `result_publish_timeout_seconds`, so shortening that is how it is
    shortened; there is no lease knob of its own, deliberately (see the config property).
    """
    monkeypatch.setattr(settings, "result_publish_timeout_seconds", 0.01)


async def _claim_after_the_previous_pass_died(sink: str, limit: int = 10) -> list[Any]:
    """Claim as the next scheduled pass would, once the dead pass's lease has expired."""
    await asyncio.sleep(settings.result_publish_lease_seconds + 0.01)
    return await outbox.claim(sink, limit)


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
        async with outbox._connect("test_fixture") as conn:
            await _reset(conn)

        assert await outbox.enqueue([_record("dup")]) == 1
        assert await outbox.enqueue([_record("dup")]) == 0, "a second enqueue writes nothing"

        async with outbox._connect("test_fixture") as conn:
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
        async with outbox._connect("test_fixture") as conn:
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
        async with outbox._connect("test_fixture") as conn:
            await _reset(conn)
        await outbox.enqueue([_record("flaky")])

        claimed = await outbox.claim("alpha", 10)
        assert len(claimed) == 1
        await outbox.mark_failed([claimed[0][0]], "destination unreachable")
        # Still claimable: one attempt spent of two.
        assert len(await outbox.claim("alpha", 10)) == 1

        await outbox.mark_failed([claimed[0][0]], "destination unreachable")
        assert await outbox.claim("alpha", 10) == [], "out of attempts, no longer claimed"

        async with outbox._connect("test_fixture") as conn:
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
        async with outbox._connect("test_fixture") as conn:
            await _reset(conn)
        await outbox.enqueue([_record("done")])

        claimed = await outbox.claim("alpha", 10)
        await outbox.mark_delivered([row_id for row_id, _, _ in claimed])
        assert await outbox.claim("alpha", 10) == []

        async with outbox._connect("test_fixture") as conn:
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

    The increment has to happen in the claim rather than in `mark_failed`, because a pass that dies
    between the two records no failure at all and must still have cost something — otherwise a row
    whose delivery kills the worker every time is retried forever.

    **This test used to assert the double-claim as the design.** It claimed twice with no mark
    between them "as two overlapping runs would do" and asserted `attempts == 2`, on the argument
    that spending the attempt in the claim is what keeps the budget correct when two runs overlap.
    Spending it there is necessary and was never sufficient: measured with a 1.0 s sink and a
    second drain started 0.3 s in, both drains *delivered* the row and it came to rest at
    `attempts=2` for one delivery. So the second claim is the thing to refuse, and the assertion
    below is now the one this file should always have made — one delivery, one attempt.
    """

    async def _run() -> None:
        await migrated_db_or_skip()
        _with_sink(monkeypatch, "alpha")
        monkeypatch.setattr(settings, "result_publish_max_attempts", 5)
        async with outbox._connect("test_fixture") as conn:
            await _reset(conn)
        await outbox.enqueue([_record("counted")])

        # Two claims with no `mark_failed` between them — as two overlapping runs would do.
        assert len(await outbox.claim("alpha", 10)) == 1
        assert await outbox.claim("alpha", 10) == [], (
            "the row is leased to the first claim; a second overlapping run must not take it"
        )

        async with outbox._connect("test_fixture") as conn:
            cursor = await conn.execute(
                "SELECT attempts FROM result_publications WHERE calc_ref = 'counted'"
            )
            row = await cursor.fetchone()
        assert row is not None and row[0] == 1, "one claim spends one attempt"

        # And the attempt is still the claim's rather than the report's: the failure that follows
        # records the reason without charging a second one. Shortened only here, because the
        # exclusion above is exactly what a full-length lease is for.
        _with_a_short_lease(monkeypatch)
        claimed = await _claim_after_the_previous_pass_died("alpha")
        await outbox.mark_failed([claimed[0][0]], "the destination said no")
        async with outbox._connect("test_fixture") as conn:
            cursor = await conn.execute(
                "SELECT attempts FROM result_publications WHERE calc_ref = 'counted'"
            )
            row = await cursor.fetchone()
        assert row is not None and row[0] == 2

    asyncio.run(_run())


def test_marking_failed_does_not_double_count_the_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A claim followed by its own failure report costs exactly one attempt, not two."""

    async def _run() -> None:
        await migrated_db_or_skip()
        _with_sink(monkeypatch, "alpha")
        monkeypatch.setattr(settings, "result_publish_max_attempts", 5)
        async with outbox._connect("test_fixture") as conn:
            await _reset(conn)
        await outbox.enqueue([_record("once")])

        claimed = await outbox.claim("alpha", 10)
        await outbox.mark_failed([row_id for row_id, _, _ in claimed], "nope")

        async with outbox._connect("test_fixture") as conn:
            cursor = await conn.execute(
                "SELECT attempts, state FROM result_publications WHERE calc_ref = 'once'"
            )
            row = await cursor.fetchone()
        assert row is not None and row == (1, "pending")

    asyncio.run(_run())


def test_one_unreadable_document_does_not_retire_its_whole_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A poison row is one row's problem, and which rows it took must not depend on claim order.

    The drain validated the batch inside a single `try` and, on the first document it could not
    parse, marked **every** claimed id failed. One row written by a future writer whose record shape
    this release cannot read therefore retired up to `batch_size - 1` perfectly deliverable rows
    once they had spent their attempts — silently, since a retired row is kept rather than deleted
    and nothing counts it as lost.
    """
    from chemclaw.durable import publish_results

    delivered: list[str] = []

    class _Sink:
        async def deliver(self, records: Any) -> None:
            delivered.extend(record.calc_ref for record in records)

        async def aclose(self) -> None:
            """Holds nothing; present because `ResultSink` requires it of every sink."""

    async def _run() -> None:
        await migrated_db_or_skip()
        _with_sink(monkeypatch, "alpha")
        async with outbox._connect("test_fixture") as conn:
            await _reset(conn)

        assert await outbox.enqueue([_record("good-1"), _record("good-2")]) == 2
        # A row this release cannot parse, written straight into the queue beside them.
        async with outbox._connect("test_fixture") as conn:
            await conn.execute(
                "INSERT INTO result_publications (sink, calc_ref, document, schema_version) "
                "VALUES ('alpha', 'poison', '{\"calc_ref\": \"poison\"}'::jsonb, '1')"
            )
            await conn.commit()

        outcome = await publish_results._drain_one("alpha", _Sink(), 10)
        assert sorted(delivered) == ["good-1", "good-2"], (
            "the readable rows must still be delivered when a neighbour cannot be parsed"
        )
        assert outcome.delivered == 2
        assert outcome.failed == 1, "exactly the unreadable row is charged an attempt"

        async with outbox._connect("test_fixture") as conn:
            cursor = await conn.execute(
                "SELECT calc_ref, state, attempts FROM result_publications ORDER BY calc_ref"
            )
            rows = {row[0]: (row[1], row[2]) for row in await cursor.fetchall()}
        assert rows["good-1"][0] == "delivered"
        assert rows["good-2"][0] == "delivered"
        assert rows["poison"][0] == "pending", "one failed attempt, not yet retired"

    asyncio.run(_run())


def test_the_drain_closes_every_sink_it_builds(monkeypatch: pytest.MonkeyPatch) -> None:
    """Built per run means closed per run, or a scheduled job leaks a connection per pass.

    `drain_result_publications` builds a sink each run deliberately, so a rotated credential takes
    effect on the next pass rather than the next restart. `SqlResultSink` opens its connection
    lazily and holds it for the sink's life. Neither decision is wrong; together, and with nothing
    closing the sink, they leaked one Postgres connection every `result_publish_schedule_minutes`
    — reaching a stock `max_connections` of 100 inside a day and then failing the whole worker.

    Asserted on a failing batch too, because a sink that could not deliver is holding exactly the
    same connection as one that could.
    """
    from chemclaw.durable import publish_results
    from chemclaw.publish.manifest import ResultSinkManifest

    closed: list[str] = []

    class _Sink:
        def __init__(self, name: str, fail: bool) -> None:
            self._name, self._fail = name, fail

        async def deliver(self, records: Any) -> None:
            if self._fail:
                raise ConnectionError("destination down")

        async def aclose(self) -> None:
            closed.append(self._name)

    manifests = [
        ResultSinkManifest(name="alpha", description="x", driver="m:c"),
        ResultSinkManifest(name="beta", description="x", driver="m:c"),
    ]

    async def _run() -> None:
        await migrated_db_or_skip()
        _with_sink(monkeypatch, "alpha", "beta")
        async with outbox._connect("test_fixture") as conn:
            await _reset(conn)
        await outbox.enqueue([_record("shared")])

        monkeypatch.setattr(publish_results, "enabled", lambda: manifests)
        monkeypatch.setattr(
            publish_results, "build", lambda m: _Sink(m.name, fail=m.name == "beta")
        )
        outcome = await publish_results.drain_result_publications()
        assert outcome.delivered == 1, "alpha delivers; beta is down"
        assert sorted(closed) == ["alpha", "beta"], (
            f"every sink built must be closed, including the one that failed; closed={closed}"
        )

    asyncio.run(_run())


def test_one_refused_record_does_not_retire_its_neighbours(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The delivery side of the poison-row rule the parse side above already holds.

    `_drain_one` protected against an unreadable *document* per row and against a refused *record*
    per batch, so one record the sink would not take marked every id in the claim failed — up to
    `batch_size - 1` neighbours retired once they had spent their attempts, and because `_CLAIM` is
    `ORDER BY enqueued_at` the poison sat at the head of the queue and re-collected the same
    neighbours on every pass. Worse, `SqlResultSink` writes record-by-record on an autocommit
    connection: the records *before* the poison are already durable at the far end while being
    booked `failed`, and the ones after it are never attempted at all.

    Measured on the shipped code with a sink refusing the third of five: `delivered=0 failed=5` on
    every pass, two rows written to the far end three times over and marked failed anyway.
    """
    from chemclaw.durable import publish_results
    from chemclaw.publish.driver import SinkRejectedError

    delivered: list[str] = []

    class _PickySink:
        """Refuses exactly one record, exactly as a `VARCHAR` overflow or a missing FK would."""

        async def deliver(self, records: Any) -> None:
            for record in records:
                if record.calc_ref == "poison":
                    raise SinkRejectedError("value too long for column")
                delivered.append(record.calc_ref)

        async def aclose(self) -> None:
            """Holds nothing; present because `ResultSink` requires it of every sink."""

    async def _run() -> None:
        await migrated_db_or_skip()
        _with_sink(monkeypatch, "alpha")
        async with outbox._connect("test_fixture") as conn:
            await _reset(conn)
        # Enqueued in this order, so the refused one sits in the middle of the claim: the rows
        # before it are the ones the batch-wide handler retired *after* they had been written.
        assert await outbox.enqueue([_record(ref) for ref in ("a-1", "a-2", "poison", "z-1")]) == 4

        outcome = await publish_results._drain_one("alpha", _PickySink(), 10)

        assert sorted(set(delivered)) == ["a-1", "a-2", "z-1"], (
            "every record the sink accepts must be delivered, including the ones queued after "
            f"the refused one; delivered={delivered}"
        )
        assert outcome.delivered == 3
        assert outcome.failed == 1, "exactly the refused record is charged with the failure"

        async with outbox._connect("test_fixture") as conn:
            cursor = await conn.execute(
                "SELECT calc_ref, state FROM result_publications ORDER BY calc_ref"
            )
            rows = {row[0]: row[1] for row in await cursor.fetchall()}
        assert rows == {
            "a-1": "delivered",
            "a-2": "delivered",
            "poison": "pending",
            "z-1": "delivered",
        }, f"a row committed at the far end must never be booked failed; got {rows}"

    asyncio.run(_run())


def test_an_unreachable_destination_still_fails_the_whole_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The other half of the same rule, and why it is not one handler.

    A rejection is a statement about one record; an outage is a statement about the destination.
    Re-attempting a batch record-by-record against a sink that cannot be reached would multiply one
    outage into `batch_size` connection attempts per pass and learn nothing, so the unavailable
    case stays batch-wide — one `deliver` call, every row left pending.
    """
    from chemclaw.durable import publish_results
    from chemclaw.publish.driver import SinkUnavailableError

    attempts: list[int] = []

    class _DownSink:
        async def deliver(self, records: Any) -> None:
            attempts.append(len(records))
            raise SinkUnavailableError("destination is down")

        async def aclose(self) -> None:
            """Holds nothing; present because `ResultSink` requires it of every sink."""

    async def _run() -> None:
        await migrated_db_or_skip()
        _with_sink(monkeypatch, "alpha")
        async with outbox._connect("test_fixture") as conn:
            await _reset(conn)
        await outbox.enqueue([_record(ref) for ref in ("d-1", "d-2", "d-3")])

        outcome = await publish_results._drain_one("alpha", _DownSink(), 10)

        assert attempts == [3], (
            f"an outage must cost one delivery attempt, not one per row: {attempts}"
        )
        assert outcome.delivered == 0
        assert outcome.failed == 3

    asyncio.run(_run())


def test_a_projection_that_cannot_succeed_is_not_counted_as_a_publish_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A projector that raises is a code gap, and must not read as a destination's bad day.

    Both were `chemclaw_result_publish_failures_total`, whose declared population is "could not be
    queued or delivered" — so a projector raising on *every* payload of a shape looked exactly like
    a transient publish failure, and the most expensive calculation in the tier reached the result
    store never while the only visible signal was a counter that also rises when a warehouse is
    slow. A projection failure never fixes itself: it is a permanent gap until code changes.
    """
    from chemclaw.core.metrics import METRICS

    _with_sink(monkeypatch, "alpha")
    projection_before = METRICS.value("chemclaw_result_projection_failures_total")
    publish_before = METRICS.value("chemclaw_result_publish_failures_total")

    # A reaction with no products: `_reaction` raises `ProjectionError` deliberately, which is the
    # same escape route an unregistered property takes out of `to_canonical`.
    written = asyncio.run(
        outbox.enqueue_payload(
            calc_ref="broken@v1:a:b",
            calc_type="reaction.energy",
            payload_kind="ReactionEnergyResult",
            payload={"reactants": ["CCO"], "products": []},
        )
    )

    assert written == 0
    assert METRICS.value("chemclaw_result_projection_failures_total") == projection_before + 1, (
        "a payload this release cannot project must be counted as such"
    )
    assert METRICS.value("chemclaw_result_publish_failures_total") == publish_before, (
        "nothing was queued and nothing was delivered, so the publish counter must not move — "
        "it is what an operator reads to decide whether a destination is unhealthy"
    )


def test_two_workers_claiming_at_once_split_the_queue(monkeypatch: pytest.MonkeyPatch) -> None:
    """`FOR UPDATE SKIP LOCKED` is the whole of "two publisher replicas drain one queue".

    Dropping it from `_CLAIM` passed all 35 tests in the outbox suite, `test_concurrency_claims.py`
    included — that file races the *session-turn* claim with 32 claimants and gives this one only
    sequential calls, and sequential calls cannot tell the two implementations apart: the claim
    commits before delivery, so a second call afterwards legitimately sees the same rows again
    (`test_claiming_a_row_spends_its_attempt` asserts exactly that).

    What tells them apart is a second claimant arriving **while the first still holds its locks**,
    which is the window `claim` occupies between its `UPDATE` and its commit. So the first worker
    here runs the real statement on its own connection and does not commit until the second has
    answered. With `SKIP LOCKED` the second steps over those rows and takes the rest; without it,
    it blocks on them and this fails as a timeout rather than passing quietly — which is the
    difference between two replicas splitting a queue and two replicas serializing on it, a drain
    that takes twice as long and, under `result_publish_max_attempts` plus a statement timeout,
    retires rows that were only ever blocked.
    """

    async def _run() -> None:
        await migrated_db_or_skip()
        _with_sink(monkeypatch, "alpha")
        monkeypatch.setattr(settings, "result_publish_max_attempts", 5)
        async with outbox._connect("test_fixture") as conn:
            await _reset(conn)
        await outbox.enqueue([_record(f"race-{index}") for index in range(4)])

        async with outbox._connect("test_fixture") as first:
            # Worker A, mid-claim: rows updated, transaction still open, locks still held.
            cursor = await first.execute(
                outbox._CLAIM, ("alpha", 5, settings.result_publish_lease_seconds, 2)
            )
            mine = {str(row[1]) for row in await cursor.fetchall()}
            # Worker B, on its own connection, against that live lock. Bounded well under the
            # statement timeout so a blocked claim is reported as a blocked claim.
            theirs = {ref for _, ref, _ in await asyncio.wait_for(outbox.claim("alpha", 2), 10)}
            await first.commit()

        assert len(mine) == 2 and len(theirs) == 2
        assert mine.isdisjoint(theirs), "two concurrent workers delivered the same rows"
        assert mine | theirs == {f"race-{index}" for index in range(4)}, (
            "the two claims together did not cover the queue"
        )

    asyncio.run(_run())


def test_a_row_out_of_attempts_is_not_claimed_again_even_while_it_is_pending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The attempt bound in the claim predicate, with `mark_failed` kept out of the way.

    Dropping `attempts < %s` from `_CLAIM` also passed the whole suite, and the reason is that the
    test which looks like it covers this reports each failure through `mark_failed` — which retires
    the row to `failed`, so the `state = 'pending'` predicate excludes it whether or not the bound
    is there. The bound's own job is the other case: a worker that claimed and then *died*, leaving
    the row pending with its attempts spent. Without the predicate that row is claimed forever, and
    a destination that is genuinely rejecting it is retried without limit.

    **The second assertion used to read `("pending", 2)`, and that was this file asserting the
    defect.** "Still pending" was a *proxy* for "the bound did the work, not the state" — but a row
    left pending with its budget spent is unclaimable, uncounted as a dead letter, ageing forever
    in the gauge the stuck-outbox alert reads, and unreachable by `--requeue`. `_REAP_EXHAUSTED`
    now names that transition, so the row comes to rest in `'failed'`, which is where every one of
    those readers can see it. The proxy is gone and the invariant it stood for is asserted directly
    instead: the reap and the claim *partition* the pending set on the same bound, so a row one
    attempt short is still handed out and a row at the bound is retired.
    """

    async def _run() -> None:
        await migrated_db_or_skip()
        _with_sink(monkeypatch, "alpha")
        monkeypatch.setattr(settings, "result_publish_max_attempts", 2)
        _with_a_short_lease(monkeypatch)
        async with outbox._connect("test_fixture") as conn:
            await _reset(conn)
        await outbox.enqueue([_record("abandoned")])

        # One claim short of the bound: still pending, still claimable, not reaped.
        assert len(await outbox.claim("alpha", 10)) == 1
        async with outbox._connect("test_fixture") as conn:
            cursor = await conn.execute(
                "SELECT state, attempts FROM result_publications WHERE calc_ref = 'abandoned'"
            )
            assert await cursor.fetchone() == ("pending", 1), (
                "a row with attempts left must not be retired — the reap and the claim partition "
                "the pending set on the bound, and this is the claim's side of it"
            )

        # The second claim spends the last attempt and reports no failure — a worker that died
        # mid-delivery, which is a lease nobody comes back for.
        assert len(await _claim_after_the_previous_pass_died("alpha")) == 1
        assert await _claim_after_the_previous_pass_died("alpha") == [], (
            "a row out of attempts was claimed again"
        )

        async with outbox._connect("test_fixture") as conn:
            cursor = await conn.execute(
                "SELECT state, attempts FROM result_publications WHERE calc_ref = 'abandoned'"
            )
            assert await cursor.fetchone() == ("failed", 2), (
                "a spent row must come to rest where the dead-letter gauge and --requeue can see "
                "it, not in a fourth state nothing names"
            )

    asyncio.run(_run())


def test_a_document_this_system_already_queued_stays_readable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A queued row is data, not a claim: the release that reads it may not refuse it.

    `_drain_one` re-validates every stored `document` with `ResultRecord.model_validate` before
    delivering it, so any check added to the *write* model becomes a filter on the *read* path —
    over bytes that were written before it existed and cannot be rewritten. The scope check is the
    one that showed it: `relative_energy` is registered per conformer, every species distribution
    published it as a calculation scalar, and a validator on `PropertyFact` therefore made those
    already-enqueued rows unparseable. Each then spends an attempt per pass until it dead-letters,
    and the backfill CLI cannot help — the stored bytes are still the same bytes.

    The document below is exactly what this system wrote at contract version 2. A projection bug is
    caught where the projection happens (`project`), which is the only place it can be caused.
    """

    async def _run() -> None:
        await migrated_db_or_skip()
        _with_sink(monkeypatch, "alpha")
        async with outbox._connect("test_fixture") as conn:
            await _reset(conn)
            document = _record("species_ranking@1:abc:def").model_dump(mode="json")
            document["contract_version"] = 2
            document["properties"] = [
                {
                    "property": "relative_energy",
                    "value": 0.0,
                    "unit": "kcal/mol",
                    "reported_value": 0.0,
                    "scope": "calculation",
                }
            ]
            await conn.execute(
                "INSERT INTO result_publications (sink, calc_ref, document, schema_version) "
                "VALUES (%s, %s, %s, %s)",
                ("alpha", document["calc_ref"], Jsonb(document), 2),
            )
            await conn.commit()

        claimed = await outbox.claim("alpha", 10)
        assert len(claimed) == 1
        stored = claimed[0][2]
        record = ResultRecord.model_validate(stored)
        assert [fact.property for fact in record.properties] == ["relative_energy"]

    asyncio.run(_run())


def test_a_row_that_spends_its_budget_without_an_outcome_is_retired_not_stranded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pass that dies between the claim and the mark must not strand the row forever.

    `_CLAIM` spends the attempt and commits before the delivery, deliberately — so a pod eviction,
    an activity timeout or the per-sink delivery ceiling leaves the row `pending` with an attempt
    spent and no `last_error`. That is fine until the *last* attempt, at which point the row was in
    a fourth state the three-state contract does not name: excluded from `_CLAIM` by
    `attempts < max` so never delivered again, not `'failed'` so never counted as a dead letter,
    still `'pending'` so counted and ageing forever in the two gauges `ChemclawResultOutboxStuck`
    reads, and unmatched by `requeue_failed` so the documented remedy reset nothing.

    Measured on the unfixed outbox: eight interrupted passes left `('alpha','stranded','pending',8,
    '')`, `claim()` returned `[]`, and `requeue_failed()` reset **0** rows.

    The interruption is simulated by claiming and never marking, which is exactly what every one of
    those failures leaves behind — the accounting is identical whether the pass died in Temporal,
    in the pod, or at the ceiling. Since a claim is now a *lease*, that also means each simulated
    pass has to let the dead one's lease expire before it can claim, which is the real recovery
    path rather than a fixture convenience: nothing else runs, and the row comes back inside the
    next ordinary claim.
    """
    from chemclaw.publish import backfill

    async def _run() -> None:
        await migrated_db_or_skip()
        _with_sink(monkeypatch, "alpha")
        _with_a_short_lease(monkeypatch)
        async with outbox._connect("test_fixture") as conn:
            await _reset(conn)
        assert await outbox.enqueue([_record("stranded")]) == 1

        for _ in range(settings.result_publish_max_attempts):
            assert len(await _claim_after_the_previous_pass_died("alpha")) == 1, (
                "the row must come back once the dead pass's lease expires"
            )
        # The pass that finds the budget spent is the one that has to say so.
        assert await _claim_after_the_previous_pass_died("alpha") == []

        async with outbox._connect("test_fixture") as conn:
            cursor = await conn.execute(
                "SELECT state, attempts, last_error FROM result_publications WHERE calc_ref = %s",
                ("stranded",),
            )
            row = await cursor.fetchone()
        assert row is not None
        state, attempts, last_error = row
        assert state == "failed", (
            "a row whose budget is spent with no outcome recorded is a dead letter; leaving it "
            "'pending' hides it from the dead-letter gauge and from --requeue while it pages "
            "forever on the age gauge"
        )
        assert attempts == settings.result_publish_max_attempts
        assert "without an outcome" in last_error, (
            "the retirement must say why, because this cause is not the destination's failure and "
            "an operator reading `last_error` would otherwise see an empty string"
        )

        # The documented remedy now reaches it, which is the whole point of the state it is in.
        assert await backfill.requeue_failed(dry_run=True) == 1
        assert await backfill.requeue_failed() == 1
        async with outbox._connect("test_fixture") as conn:
            cursor = await conn.execute(
                "SELECT state, attempts FROM result_publications WHERE calc_ref = %s",
                ("stranded",),
            )
            assert await cursor.fetchone() == ("pending", 0)

    asyncio.run(_run())


def test_the_real_failure_reason_outranks_the_reaper_s_generic_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A row that *did* record why it failed keeps that reason when it is retired.

    The reaper writes `last_error` only when it is empty. An operator opening a dead letter needs
    the destination's own account of the failure — "connection refused", "no such column" — not
    this system's account of its own bookkeeping.
    """

    async def _run() -> None:
        await migrated_db_or_skip()
        _with_sink(monkeypatch, "alpha")
        _with_a_short_lease(monkeypatch)
        async with outbox._connect("test_fixture") as conn:
            await _reset(conn)
        await outbox.enqueue([_record("has-a-reason")])
        claimed = await outbox.claim("alpha", 10)
        await outbox.mark_failed([claimed[0][0]], "connection refused by the results warehouse")
        # A reported failure releases the lease as it records the reason, so the retry is the next
        # pass rather than the next lease period — claimed straight away, with no wait.
        assert len(await outbox.claim("alpha", 10)) == 1
        # Spend the rest of the budget the silent way, then let the next claim retire it.
        for _ in range(settings.result_publish_max_attempts - 2):
            await _claim_after_the_previous_pass_died("alpha")
        await _claim_after_the_previous_pass_died("alpha")

        async with outbox._connect("test_fixture") as conn:
            cursor = await conn.execute(
                "SELECT state, last_error FROM result_publications WHERE calc_ref = %s",
                ("has-a-reason",),
            )
            row = await cursor.fetchone()
        assert row == ("failed", "connection refused by the results warehouse")

    asyncio.run(_run())


def test_an_emptied_queue_reads_as_zero_seconds_behind_not_as_fifty_six_years(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The healthiest state the drain has must not be its worst gauge reading.

    `_replace` keeps a sink that has fallen to zero in the gauge family rather than dropping it,
    which is right — a disappearing series silently stops an alert evaluating. But the family it
    zeroes holds an *epoch*, and `_oldest_pending_seconds` subtracted it from the clock. Measured
    on a sink whose queue had just drained:
    `chemclaw_outbox_oldest_pending_seconds{sink="alpha"} = 1788721651` — about 56 years —
    so `ChemclawResultOutboxStuck` fires at its maximum reading on every deployment the first time
    a sink clears its backlog.

    `refresh_backlog`'s own docstring already claimed the fixed behaviour ("which reads as '0
    seconds behind', the honest answer for an empty queue"); the arithmetic said the opposite.
    """

    async def _run() -> None:
        await migrated_db_or_skip()
        _with_sink(monkeypatch, "alpha")
        async with outbox._connect("test_fixture") as conn:
            await _reset(conn)
        await outbox.enqueue([_record("drains-to-empty")])
        await outbox.refresh_backlog()
        assert outbox._oldest_pending_seconds()["alpha"] < 60.0

        claimed = await outbox.claim("alpha", 10)
        await outbox.mark_delivered([claimed[0][0]])
        await outbox.refresh_backlog()

        assert outbox._PENDING_GAUGE["alpha"] == 0.0, "the series must stay, reading zero"
        assert outbox._oldest_pending_seconds()["alpha"] == 0.0, (
            "an empty queue is zero seconds behind; anything else pages when nothing is wrong"
        )

    asyncio.run(_run())


def test_all_three_backlog_gauge_families_are_actually_bound() -> None:
    """The declaration is not the binding, and only the binding puts a series on `/metrics`.

    `record_metric` swallows a `None` callable by design — a metrics failure may not take a request
    down — so a `bind_gauge_family` call that stops happening is a silent no-op: the family is
    declared, never registered, never exported, and `ChemclawResultOutboxStuck` can never fire
    because the series it alerts on does not exist. Replacing the whole
    `chemclaw_outbox_oldest_pending_seconds` binding with `None` left the repository green under
    every test whose tracing named this function, which is the same shape as the counter
    `D-2026-08-08` found declared and never incremented.

    Off the database on purpose. The Postgres-backed reading in
    `tests/test_datapath_observability.py` does exercise all three, and it skips wherever Postgres
    does — so the claim "the alert's series exists" would rest on a lane that can go quiet. This
    asserts the registration itself, which needs no queue.
    """
    from chemclaw.core.metrics import METRICS

    probe = "gauge-binding-probe"
    outbox._PENDING_GAUGE[probe] = 2.0
    outbox._DEAD_GAUGE[probe] = 1.0
    outbox._OLDEST_ENQUEUED[probe] = time.time() - 30.0
    try:
        outbox.bind_backlog_gauges()
        rendered = METRICS.render()
        for family in (
            "chemclaw_outbox_pending",
            "chemclaw_outbox_oldest_pending_seconds",
            "chemclaw_outbox_dead_lettered",
        ):
            assert f'{family}{{sink="{probe}"}}' in rendered, (
                f"{family} is declared but nothing bound it, so it is never exported and any rule "
                "written against it evaluates on an absent series"
            )
    finally:
        outbox._PENDING_GAUGE.pop(probe, None)
        outbox._DEAD_GAUGE.pop(probe, None)
        outbox._OLDEST_ENQUEUED.pop(probe, None)


def test_a_row_enqueued_by_a_pod_whose_clock_runs_ahead_reads_as_zero_not_as_one() -> None:
    """The other end of the same clamp, and the end no fixture reached.

    The test above pins the drained-queue case, where the stored epoch is the zero placeholder.
    This is the skew case the clamp's own docstring names: a row enqueued by a pod whose clock runs
    ahead of this one subtracts to a negative age. Zero is the honest reading — the row is not
    behind — and a floor of anything else is a fabricated backlog that grows no matter how healthy
    the drain is. `max(0.0, ...)` could become `max(1.0, ...)` with 63 tests green, because every
    one of them had a row genuinely in the past.
    """
    probe = "clock-skew-probe"
    outbox._OLDEST_ENQUEUED[probe] = time.time() + 300.0
    try:
        assert outbox._oldest_pending_seconds()[probe] == 0.0, (
            "a clock-skewed row read as a backlog; an alert cannot interpret a fabricated age"
        )
    finally:
        outbox._OLDEST_ENQUEUED.pop(probe, None)


def test_rows_for_a_disabled_sink_stop_paging_and_are_reported_instead(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Turning a destination off must not leave an alert nobody can silence.

    `enqueue` writes one row per *currently enabled* sink and the drain iterates *currently
    enabled* manifests, while the backlog read took every row regardless. Measured: with `beta`
    removed from the enable list its row was drained by nobody, pruned by nobody (retention sweeps
    `delivered` only), requeued by nobody, and read `chemclaw_outbox_pending{sink="beta"} 1.0`
    forever — so `ChemclawResultOutboxStuck` fired permanently for a destination the operator had
    deliberately turned off.

    The rows are not forgotten: they are reported once per pass on the degradation series, which is
    a different fact wanting a different, non-paging rule.
    """

    async def _run() -> None:
        await migrated_db_or_skip()
        _with_sink(monkeypatch, "alpha", "beta")
        async with outbox._connect("test_fixture") as conn:
            await _reset(conn)
        assert await outbox.enqueue([_record("orphaned")]) == 2

        _with_sink(monkeypatch, "alpha")
        with caplog.at_level(logging.WARNING):
            await outbox.refresh_backlog()

        assert "beta" not in outbox._PENDING_GAUGE or outbox._PENDING_GAUGE["beta"] == 0.0, (
            "a sink nothing drains must not be counted as a backlog the drain is behind on"
        )
        assert any("no longer enabled" in record.getMessage() for record in caplog.records), (
            "the stranded rows must still be reported — silence is how they are forgotten"
        )

    asyncio.run(_run())


def test_two_overlapping_drains_do_not_both_deliver_one_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The case `_CLAIM`'s comment was written for, driven rather than reasoned about.

    A scheduled drain and an operator's manual one overlap over the **delivery**, which takes
    seconds to a minute — not over the claim, which takes milliseconds. `FOR UPDATE SKIP LOCKED`
    excludes only overlapping *transactions*, and the claim commits immediately by design so no row
    lock is held across a delivery, so the second run's claim happens after that commit and sees a
    row that is still `pending`. Measured on the unfixed outbox with a 1.0 s sink and a second
    drain started 0.3 s in: **both** drains delivered the row and it came to rest at `attempts=2`
    for one delivery — an attempt budget of 8 that empties after 4 real attempts against one
    destination's outage.

    What closes it is the lease: the claim moves the row out of `pending`, so the second run skips
    it by predicate rather than by lock duration.
    """
    from chemclaw.durable import publish_results

    delivered: list[str] = []

    class _SlowSink:
        """A destination that takes a second, which is what every real one does."""

        async def deliver(self, records: Any) -> None:
            await asyncio.sleep(1.0)
            delivered.extend(record.calc_ref for record in records)

        async def aclose(self) -> None:
            """Holds nothing; present because `ResultSink` requires it of every sink."""

    async def _run() -> None:
        await migrated_db_or_skip()
        _with_sink(monkeypatch, "alpha")
        async with outbox._connect("test_fixture") as conn:
            await _reset(conn)
        assert await outbox.enqueue([_record("overlap")]) == 1

        async def _manual_drain() -> Any:
            # The operator, 0.3 s into the scheduled run's delivery.
            await asyncio.sleep(0.3)
            return await publish_results._drain_one("alpha", _SlowSink(), 10)

        scheduled, manual = await asyncio.gather(
            publish_results._drain_one("alpha", _SlowSink(), 10), _manual_drain()
        )

        assert delivered == ["overlap"], (
            "two overlapping drains delivered one row twice; the second must skip it by predicate"
        )
        assert (scheduled.delivered, manual.delivered) == (1, 0)
        async with outbox._connect("test_fixture") as conn:
            cursor = await conn.execute(
                "SELECT state, attempts FROM result_publications WHERE calc_ref = 'overlap'"
            )
            assert await cursor.fetchone() == ("delivered", 1), (
                "one delivery must spend one attempt, not one per overlapping drain"
            )

    asyncio.run(_run())


def test_a_lease_its_claimer_died_holding_returns_to_the_queue_on_the_next_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The crashed claimer, which is what a lease costs and has to pay for itself.

    A claim that moves a row out of `pending` is a claim that can be abandoned: a pod eviction, an
    activity `start_to_close` expiry or the per-sink ceiling leaves the row `in_flight` with nobody
    coming back for it. Without a way out that row is worse than the doubled attempt it replaced —
    it is invisible to the claim *and* to `--requeue`.

    The way out is the lease's own predicate, evaluated at the head of the next ordinary claim for
    that sink — not a second timer nobody runs. So this test never marks the row: it claims it,
    proves no other drain can take it while the lease holds, lets the lease expire, and claims
    again with nothing else having happened in between.
    """

    async def _run() -> None:
        await migrated_db_or_skip()
        _with_sink(monkeypatch, "alpha")
        monkeypatch.setattr(settings, "result_publish_max_attempts", 5)
        # The lease is the drain activity's own ceiling; shortened here so the expiry is real
        # rather than hand-written into `claimed_at`.
        monkeypatch.setattr(settings, "result_publish_timeout_seconds", 0.5)
        async with outbox._connect("test_fixture") as conn:
            await _reset(conn)
        assert await outbox.enqueue([_record("abandoned")]) == 1

        assert len(await outbox.claim("alpha", 10)) == 1
        assert await outbox.claim("alpha", 10) == [], (
            "a second drain must not take a row the first is still delivering"
        )
        async with outbox._connect("test_fixture") as conn:
            cursor = await conn.execute(
                "SELECT state, attempts, claimed_at IS NOT NULL "
                "FROM result_publications WHERE calc_ref = 'abandoned'"
            )
            # Still `pending`, which is the truth — it has not been delivered — and held by a
            # lease, which is what the second claim above was refused by.
            assert await cursor.fetchone() == ("pending", 1, True)

        # The claimer died here: no `mark_delivered`, no `mark_failed`, ever.
        await asyncio.sleep(settings.result_publish_lease_seconds + 0.1)

        assert len(await outbox.claim("alpha", 10)) == 1, (
            "an expired lease must return its row to the queue on the next ordinary claim"
        )
        async with outbox._connect("test_fixture") as conn:
            cursor = await conn.execute(
                "SELECT state, attempts FROM result_publications WHERE calc_ref = 'abandoned'"
            )
            assert await cursor.fetchone() == ("pending", 2), (
                "the recovered row spends the second claim's attempt and no more"
            )

    asyncio.run(_run())
