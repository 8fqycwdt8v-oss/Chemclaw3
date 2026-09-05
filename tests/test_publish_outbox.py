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
from typing import Any

import psycopg
import pytest
from psycopg.types.json import Jsonb

from chemclaw.core.config import settings
from chemclaw.publish import outbox
from chemclaw.publish.record import (
    Conditions,
    Publication,
    ResultRecord,
    Subject,
    SubjectMember,
    TheoryLevel,
)
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
        async with outbox._connect("test_fixture") as conn:
            await _reset(conn)
        await outbox.enqueue([_record("counted")])

        # Two claims with no `mark_failed` between them — as two overlapping runs would do.
        assert len(await outbox.claim("alpha", 10)) == 1
        assert len(await outbox.claim("alpha", 10)) == 1

        async with outbox._connect("test_fixture") as conn:
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
        # A stored document this release cannot parse parses identically on every retry, which
        # the drain's own comment said while spending one attempt of eight on it. It retires now.
        assert rows["poison"] == ("failed", 1)

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
            # **Retired on the first answer, not on the eighth.** `publish/driver.py` says the
            # split between its two exception classes "decides whether the outbox tries again";
            # until it did, a `SinkRejectedError` — a sink that has *answered* about this content —
            # spent all eight attempts on a fault that fails identically forever, two hours of the
            # shipped fifteen-minute schedule before reaching the state it was destined for from
            # the first pass. `--requeue` is how it comes back once the cause is fixed.
            "poison": "failed",
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
            cursor = await first.execute(outbox._CLAIM, ("alpha", 5, 2))
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
    """

    async def _run() -> None:
        await migrated_db_or_skip()
        _with_sink(monkeypatch, "alpha")
        monkeypatch.setattr(settings, "result_publish_max_attempts", 2)
        async with outbox._connect("test_fixture") as conn:
            await _reset(conn)
        await outbox.enqueue([_record("abandoned")])

        # Two claims, no failure reported — two workers that died mid-delivery.
        assert len(await outbox.claim("alpha", 10)) == 1
        assert len(await outbox.claim("alpha", 10)) == 1
        assert await outbox.claim("alpha", 10) == [], "a row out of attempts was claimed again"

        async with outbox._connect("test_fixture") as conn:
            cursor = await conn.execute(
                "SELECT state, attempts FROM result_publications WHERE calc_ref = 'abandoned'"
            )
            assert await cursor.fetchone() == ("pending", 2), (
                "the row must still be pending — this is the bound doing the work, not the state"
            )

    asyncio.run(_run())


def test_a_second_chemists_publication_is_not_dropped(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two chemists running the same calculation produce one record and **two** publications.

    `record.py` states exactly that, and the shipped sink schema is built for it: the
    `calculation_publication` primary key is `(calc_ref, tenant_id, session_id, job_id)` and
    `calculation_publication_actor_idx` exists so a site can ask who ran what. The outbox's
    `ON CONFLICT (sink, calc_ref, schema_version) DO NOTHING` used to drop the whole second row —
    the document, and with it the second publication — so the sink learned about alice and never
    about bob. Measured before the fix: `alice_rows=1 bob_rows=0`.
    """

    async def _run() -> None:
        await migrated_db_or_skip()
        _with_sink(monkeypatch, "alpha")
        async with outbox._connect("test_fixture") as conn:
            await _reset(conn)

        alice = Publication(actor="alice", session_id="s-alice")
        bob = Publication(actor="bob", session_id="s-bob")
        assert (
            await outbox.enqueue(
                [_record("shared-key").model_copy(update={"publications": [alice]})]
            )
            == 1
        )
        await outbox.enqueue([_record("shared-key").model_copy(update={"publications": [bob]})])

        actors: set[str] = set()
        for _, _, document in await outbox.claim("alpha", 10):
            actors.update(publication["actor"] for publication in document.get("publications", []))
        assert actors == {"alice", "bob"}, "both chemists' provenance must reach the sink"

    asyncio.run(_run())


def test_re_enqueueing_the_same_publication_changes_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The idempotency the `ON CONFLICT` clause exists for, unchanged.

    Three call sites write with no coordination, a retried Temporal activity must not double-queue,
    and the backfill CLI must be re-runnable. Each of those replays the *same* publication, so the
    identity that has to hold is "this calculation, for this sink, on behalf of this requester".
    """

    async def _run() -> None:
        await migrated_db_or_skip()
        _with_sink(monkeypatch, "alpha")
        async with outbox._connect("test_fixture") as conn:
            await _reset(conn)

        record = _record("replayed").model_copy(
            update={"publications": [Publication(actor="alice", session_id="s1", job_id="j1")]}
        )
        assert await outbox.enqueue([record]) == 1
        assert await outbox.enqueue([record]) == 0, "a replay writes nothing"

        async with outbox._connect("test_fixture") as conn:
            cursor = await conn.execute(
                "SELECT count(*), max(state) FROM result_publications WHERE calc_ref = 'replayed'"
            )
            assert await cursor.fetchone() == (1, "pending")

    asyncio.run(_run())


def test_a_payload_this_release_cannot_project_is_counted_and_named(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A shape no projector knows must not be dropped in silence.

    A projector that *raises* increments `chemclaw_result_projection_failures_total`; a payload
    kind that was never registered used to return 0 with a `logger.debug` line, so a new connector
    job publishing nothing left every dashboard, alert and gauge reading normal. Both are the same
    fault — this release cannot turn this payload into a record — which is exactly what that
    counter's own declaration says it counts.
    """
    from chemclaw.core.metrics import METRICS

    _with_sink(monkeypatch, "alpha")
    before = METRICS.value("chemclaw_result_projection_failures_total")
    with caplog.at_level(logging.WARNING, logger="chemclaw.publish.outbox"):
        written = asyncio.run(
            outbox.enqueue_payload(
                calc_ref="new@v1:a:b",
                calc_type="newconnector.newjob",
                payload={"x": 1},
                payload_kind="BrandNewResult",
            )
        )
    assert written == 0
    assert METRICS.value("chemclaw_result_projection_failures_total") == before + 1
    assert any("BrandNewResult" in record.getMessage() for record in caplog.records), (
        "the unpublishable shape has to be named where an operator will see it"
    )


def test_an_aggregate_and_its_parts_are_claimed_in_the_order_they_were_written(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`enqueue` writes an aggregate and its parts in one transaction, so they share a timestamp.

    `ORDER BY enqueued_at` alone is then not a total order, and Postgres does not promise a stable
    relative order for tied rows across the separate statements that fetch consecutive batches —
    the exact tie `publish/backfill.py` fixed with a `key` tiebreaker on its own walk. A part can
    therefore be claimed in one pass and its aggregate in the next, which is a read-consistency gap
    at the destination for as long as the two passes are apart.
    """

    async def _run() -> None:
        await migrated_db_or_skip()
        _with_sink(monkeypatch, "alpha")
        async with outbox._connect("test_fixture") as conn:
            await _reset(conn)
        await outbox.enqueue([_record("screen"), _record("screen#0"), _record("screen#1")])

        async with outbox._connect("test_fixture") as conn:
            cursor = await conn.execute(
                "SELECT count(DISTINCT enqueued_at), count(*) FROM result_publications"
            )
            assert await cursor.fetchone() == (1, 3), "one transaction, one timestamp"

        # One row per pass, settled before the next claim, which is what a `batch_size` smaller
        # than the burst does. Which row each pass takes must be decided by the statement rather
        # than by whatever the plan happens to return for tied keys.
        seen: list[str] = []
        for _ in range(3):
            claimed = await outbox.claim("alpha", 1)
            seen.extend(ref for _, ref, _ in claimed)
            await outbox.mark_delivered([row_id for row_id, _, _ in claimed])
        assert seen == ["screen", "screen#0", "screen#1"]
        assert "ORDER BY enqueued_at, id" in outbox._CLAIM, (
            "the order has to be total in the statement: with ties this common, a run that "
            "happens to come back in insertion order is not evidence that the next one will"
        )

    asyncio.run(_run())


def test_a_drain_pass_empties_the_queue_rather_than_taking_one_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pass is bounded by time, not by one batch — because one batch is a rate, not a bound.

    At the shipped `result_publish_batch_size=100` and `result_publish_schedule_minutes=15`, one
    batch per pass is **400 records/hour/sink**. Nothing on the production side holds a deployment
    under that: `enqueue` never blocks, refuses or samples, and one solvent screen emits 1+N
    records — so a backlog that grows faster than 400/hour never drains, and the only signal is an
    oldest-pending age that rises exactly as it would if the drain had stopped altogether.

    Measured at 250 queued rows with a batch size of 100: one pass delivered **100** before this
    change and **250** after, in the same activity budget.
    """
    from chemclaw.durable import publish_results

    class _Sink:
        async def deliver(self, records: Any) -> None:
            """Accepts everything; this test is about how much a pass claims."""

        async def aclose(self) -> None:
            """Holds nothing; present because `ResultSink` requires it of every sink."""

    async def _run() -> None:
        await migrated_db_or_skip()
        _with_sink(monkeypatch, "alpha")
        async with outbox._connect("test_fixture") as conn:
            await _reset(conn)
        await outbox.enqueue([_record(f"bulk-{index:03d}") for index in range(250)])

        outcome = await publish_results._drain_one("alpha", _Sink(), 100)
        assert outcome.delivered == 250, (
            f"a pass must drain what is queued rather than 100 of it; delivered={outcome.delivered}"
        )
        assert await outbox.claim("alpha", 10) == []

    asyncio.run(_run())


def test_a_publication_merged_mid_delivery_is_not_marked_delivered_unsent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The race the publication merge would otherwise open, closed by `revision`.

    `claim` commits before anything is delivered — a delivery may take the better part of a minute
    and must not hold a row lock across it — so a second chemist's enqueue can merge a publication
    into a row that is already in flight. Settling that row would drop the new publication
    permanently and silently, which is the very defect the merge exists to fix arriving through the
    back door.
    """

    async def _run() -> None:
        await migrated_db_or_skip()
        _with_sink(monkeypatch, "alpha")
        async with outbox._connect("test_fixture") as conn:
            await _reset(conn)
        await outbox.enqueue(
            [_record("inflight").model_copy(update={"publications": [Publication(actor="alice")]})]
        )

        claimed = await outbox.claim("alpha", 10)
        # Bob arrives while the pass is delivering what it claimed.
        await outbox.enqueue(
            [_record("inflight").model_copy(update={"publications": [Publication(actor="bob")]})]
        )
        assert await outbox.mark_delivered([row_id for row_id, _, _ in claimed]) == 0

        again = await outbox.claim("alpha", 10)
        assert len(again) == 1, "the row stays queued until the merged document has gone out"
        actors = {row["actor"] for row in again[0][2]["publications"]}
        assert actors == {"alice", "bob"}
        assert await outbox.mark_delivered([row_id for row_id, _, _ in again]) == 1

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
