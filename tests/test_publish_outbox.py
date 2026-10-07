"""The outbox delivers at least once, converges on redelivery, and never fails a calculation.

- **Enqueue is idempotent**: three uncoordinated call sites write to it and a retried activity must
  not double-queue.
- **A failed delivery leaves its row pending**: at-least-once against a content-addressed target is
  safe, losing a result is not.
- **Nothing here can fail the calculation that produced the record**: the science is already
  persisted.
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
    """Shorten the claim lease so an abandoned claim really expires.

    A pass that dies holds its rows until the lease expires; tests simulating a dead pass let the
    real predicate expire it rather than rewriting `claimed_at`. The lease derives from
    `result_publish_timeout_seconds`, which is how it is shortened.
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


async def test_enqueueing_the_same_record_twice_queues_it_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The identity index is the idempotency, so the call sites need no coordination."""
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


async def test_one_record_is_queued_once_per_enabled_sink(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two sinks are two rows, so one destination being down cannot hold up another."""
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


async def test_a_failed_delivery_leaves_the_row_pending_until_it_runs_out_of_attempts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A destination being down must not lose the record, and must not retry forever.

    The row stays `pending` and re-claimable while it has attempts left, then retires to `failed`
    — where it is kept, not deleted, because it is the record that something was never published.
    """
    await migrated_db_or_skip()
    _with_sink(monkeypatch, "alpha")
    monkeypatch.setattr(settings, "result_publish_max_attempts", 2)
    async with outbox._connect("test_fixture") as conn:
        await _reset(conn)
    await outbox.enqueue([_record("flaky")])

    claimed = await outbox.claim("alpha", 10)
    assert len(claimed) == 1
    await outbox.mark_failed([claimed[0].lease], "destination unreachable")
    # Still claimable: one attempt spent of two.
    reclaimed = await outbox.claim("alpha", 10)
    assert len(reclaimed) == 1

    # The second mark carries the second claim's lease: marks are fenced on the claim's attempt, so
    # the first lease would be a superseded pass releasing a live one's row.
    await outbox.mark_failed([reclaimed[0].lease], "destination unreachable")
    assert await outbox.claim("alpha", 10) == [], "out of attempts, no longer claimed"

    async with outbox._connect("test_fixture") as conn:
        cursor = await conn.execute(
            "SELECT state, attempts, last_error FROM result_publications WHERE calc_ref = 'flaky'"
        )
        row = await cursor.fetchone()
        assert row is not None
        state, attempts, last_error = row
    assert (state, attempts) == ("failed", 2)
    assert "unreachable" in last_error, "the reason is kept for an operator to read"


async def test_a_delivered_row_is_not_claimed_again(monkeypatch: pytest.MonkeyPatch) -> None:
    """Marking delivered removes the row from the queue without deleting it."""
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


def test_a_broken_outbox_does_not_raise_into_the_calculation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A broken outbox does not raise into the calculation.

    The calculation already succeeded and is persisted, so an unavailable store or queue is counted
    and logged and the caller never sees it.
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


async def test_claiming_a_row_spends_its_attempt(monkeypatch: pytest.MonkeyPatch) -> None:
    """Claiming a row spends its attempt, and one delivery costs one attempt.

    The claim, not `mark_failed`, spends the attempt, so a worker that dies every time still
    exhausts the budget. A second claim of an in-flight row is refused, so overlapping drains cannot
    both spend and deliver.
    """
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
    await outbox.mark_failed([claimed[0].lease], "the destination said no")
    async with outbox._connect("test_fixture") as conn:
        cursor = await conn.execute(
            "SELECT attempts FROM result_publications WHERE calc_ref = 'counted'"
        )
        row = await cursor.fetchone()
    assert row is not None and row[0] == 2


async def test_marking_failed_does_not_double_count_the_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A claim followed by its own failure report costs exactly one attempt, not two."""
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


async def test_one_unreadable_document_does_not_retire_its_whole_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One unreadable document does not retire its whole batch.

    A row from a future writer this release cannot parse is that row's problem; marking every
    claimed id failed would silently retire deliverable neighbours.
    """
    from chemclaw.durable import publish_results

    delivered: list[str] = []

    class _Sink:
        async def deliver(self, records: Any) -> None:
            delivered.extend(record.calc_ref for record in records)

        async def aclose(self) -> None:
            """Holds nothing; present because `ResultSink` requires it of every sink."""

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


async def test_the_drain_closes_every_sink_it_builds(monkeypatch: pytest.MonkeyPatch) -> None:
    """The drain closes every sink it builds, on success and on failure.

    A sink is built per run so rotated credentials apply next pass, and `SqlResultSink` holds its
    connection for its life, so an unclosed sink leaks a connection per scheduled pass.
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

    await migrated_db_or_skip()
    _with_sink(monkeypatch, "alpha", "beta")
    async with outbox._connect("test_fixture") as conn:
        await _reset(conn)
    await outbox.enqueue([_record("shared")])

    monkeypatch.setattr(publish_results, "enabled", lambda: manifests)
    monkeypatch.setattr(publish_results, "build", lambda m: _Sink(m.name, fail=m.name == "beta"))
    outcome = await publish_results.drain_result_publications()
    assert outcome.delivered == 1, "alpha delivers; beta is down"
    assert sorted(closed) == ["alpha", "beta"], (
        f"every sink built must be closed, including the one that failed; closed={closed}"
    )


async def test_one_refused_record_does_not_retire_its_neighbours(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One refused record does not retire its neighbours.

    `_CLAIM` orders by `enqueued_at`, so a poison record at the head would re-collect the same
    neighbours every pass, and `SqlResultSink` autocommits per record, so earlier records are
    durable at the far end even when the batch is booked failed.
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


async def test_an_unreachable_destination_still_fails_the_whole_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unreachable destination still fails the whole batch.

    An outage is about the destination, not one record; replaying per record would multiply one
    outage into `batch_size` connection attempts per pass.
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

    await migrated_db_or_skip()
    _with_sink(monkeypatch, "alpha")
    async with outbox._connect("test_fixture") as conn:
        await _reset(conn)
    await outbox.enqueue([_record(ref) for ref in ("d-1", "d-2", "d-3")])

    outcome = await publish_results._drain_one("alpha", _DownSink(), 10)

    assert attempts == [3], f"an outage must cost one delivery attempt, not one per row: {attempts}"
    assert outcome.delivered == 0
    assert outcome.failed == 3


def test_a_projection_that_cannot_succeed_is_not_counted_as_a_publish_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A projection that cannot succeed is not counted as a publish failure.

    `chemclaw_result_publish_failures_total` means "could not be queued or delivered"; a projector
    raising is a permanent code gap and gets its own series.
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


async def test_two_workers_claiming_at_once_split_the_queue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two workers claiming at once split the queue (`FOR UPDATE SKIP LOCKED`).

    Sequential claims cannot distinguish the implementations, so the first worker holds its locks
    uncommitted while the second claims on its own connection. With `SKIP LOCKED` the second takes
    the rest; without it, it blocks and this fails as a timeout.
    """
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
        # Worker B, on its own connection, against that live lock. An unblocked claim takes about a
        # millisecond and a blocked one waits for the statement timeout, so the bound sits near
        # `pg_statement_timeout_seconds` to avoid failing on merely slow connection acquisition.
        theirs = {ref for _, ref, _ in await asyncio.wait_for(outbox.claim("alpha", 2), 25)}
        await first.commit()

    assert len(mine) == 2 and len(theirs) == 2
    assert mine.isdisjoint(theirs), "two concurrent workers delivered the same rows"
    assert mine | theirs == {f"race-{index}" for index in range(4)}, (
        "the two claims together did not cover the queue"
    )


async def test_a_row_out_of_attempts_is_not_claimed_again_even_while_it_is_pending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A row out of attempts is not claimed again, even while pending.

    `mark_failed` retires rows, so the claim predicate's `attempts < %s` bound matters for a worker
    that claimed and died. The reap and the claim partition the pending set on the same bound: one
    attempt short is still handed out, at the bound it is retired to `'failed'`.
    """
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


async def test_a_document_this_system_already_queued_stays_readable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A document this system already queued stays readable.

    `_drain_one` re-validates stored documents, so a check added to the write model would filter
    bytes written before it and dead-letter them. This document is exactly what was written at
    contract version 2; projection bugs are caught in `project`.
    """
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
    stored = claimed[0].document
    record = ResultRecord.model_validate(stored)
    assert [fact.property for fact in record.properties] == ["relative_energy"]


async def test_a_row_that_spends_its_budget_without_an_outcome_is_retired_not_stranded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A row that spends its budget without an outcome is retired, not stranded.

    A pass that dies after the claim leaves the row pending with an attempt spent. On the last
    attempt it would be unclaimable, uncounted as a dead letter, ageing in the stuck-outbox gauges
    and missed by `requeue_failed`; the reap moves it to `'failed'`. Simulated by claiming and never
    marking, and each pass lets the dead lease expire, the real recovery path.
    """
    from chemclaw.publish import backfill

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


async def test_the_real_failure_reason_outranks_the_reaper_s_generic_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real failure reason outranks the reaper's generic one: the reaper writes `last_error`
    only when empty.
    """
    await migrated_db_or_skip()
    _with_sink(monkeypatch, "alpha")
    _with_a_short_lease(monkeypatch)
    async with outbox._connect("test_fixture") as conn:
        await _reset(conn)
    await outbox.enqueue([_record("has-a-reason")])
    claimed = await outbox.claim("alpha", 10)
    await outbox.mark_failed([claimed[0].lease], "connection refused by the results warehouse")
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


async def test_an_emptied_queue_reads_as_zero_seconds_behind_not_as_fifty_six_years(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An emptied queue reads as zero seconds behind.

    A sink that drains to zero stays in the gauge family with a zero epoch, so the age computation
    must not subtract that epoch from the clock.
    """
    await migrated_db_or_skip()
    _with_sink(monkeypatch, "alpha")
    async with outbox._connect("test_fixture") as conn:
        await _reset(conn)
    await outbox.enqueue([_record("drains-to-empty")])
    await outbox.refresh_backlog()
    assert outbox._oldest_pending_seconds()["alpha"] < 60.0

    claimed = await outbox.claim("alpha", 10)
    await outbox.mark_delivered([claimed[0].lease])
    await outbox.refresh_backlog()

    assert outbox._PENDING_GAUGE["alpha"] == 0.0, "the series must stay, reading zero"
    assert outbox._oldest_pending_seconds()["alpha"] == 0.0, (
        "an empty queue is zero seconds behind; anything else pages when nothing is wrong"
    )


def test_all_three_backlog_gauge_families_are_actually_bound() -> None:
    """All three backlog gauge families are actually bound.

    `record_metric` swallows a `None` callable by design, so an unbound family is never exported and
    `ChemclawResultOutboxStuck` could never fire. Asserted without a database because the Postgres-
    backed reading can skip.
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
    """A row enqueued by a pod whose clock runs ahead reads as zero, not one.

    Clock skew gives a negative age; the floor must be exactly zero, or a healthy drain shows a
    fabricated backlog.
    """
    probe = "clock-skew-probe"
    outbox._OLDEST_ENQUEUED[probe] = time.time() + 300.0
    try:
        assert outbox._oldest_pending_seconds()[probe] == 0.0, (
            "a clock-skewed row read as a backlog; an alert cannot interpret a fabricated age"
        )
    finally:
        outbox._OLDEST_ENQUEUED.pop(probe, None)


async def test_rows_for_a_disabled_sink_stop_paging_and_are_reported_instead(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Rows for a disabled sink stop paging and are reported instead.

    Nothing drains, prunes or requeues them, so counting them in the backlog gauge would fire
    `ChemclawResultOutboxStuck` forever. They are reported per pass on the degradation series.
    """
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


def test_two_overlapping_drains_do_not_both_deliver_one_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two overlapping drains do not both deliver one row.

    Drains overlap during delivery, which lasts seconds, after the claim has committed, so `SKIP
    LOCKED` alone cannot exclude them. The lease moves the row out of `pending`, so the second run
    skips it by predicate.
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


async def test_a_lease_its_claimer_died_holding_returns_to_the_queue_on_the_next_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A lease its claimer died holding returns to the queue on the next claim.

    The lease's own predicate, evaluated at the next ordinary claim, recovers it, not a separate
    timer. The test claims, shows no other drain can take it while the lease holds, lets it expire
    and claims again.
    """
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


def test_one_unqueueable_record_costs_one_document_and_not_the_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One unqueueable record costs one document and not the batch.

    `records_for` decomposes one calculation into several records. A savepoint per record is
    required: psycopg refuses a NUL client-side (transaction healthy), while Postgres refuses an
    out-of-range `schema_version` and aborts the transaction, and only a savepoint contains both.
    """

    async def _run() -> list[str]:
        await migrated_db_or_skip()
        _with_sink(monkeypatch, "alpha")
        async with outbox._connect("test_fixture") as conn:
            await _reset(conn)
        server_side = _record("poison@v1:2:x").model_copy(update={"contract_version": 2**40})
        written = await outbox.enqueue(
            [
                _record("good@v1:1:x"),
                _record("poison@v1:\x00:x"),
                server_side,
                _record("good@v1:2:x"),
            ]
        )
        async with outbox._connect("test_fixture") as conn:
            rows = await (
                await conn.execute("SELECT calc_ref FROM result_publications ORDER BY calc_ref")
            ).fetchall()
        assert written == 2, f"the return value does not count what was actually queued: {written}"
        return [row[0] for row in rows]

    assert asyncio.run(_run()) == ["good@v1:1:x", "good@v1:2:x"], (
        "one refused document took the good records queued beside it"
    )


def test_a_calculation_that_produced_a_non_finite_number_is_refused_at_projection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A calculation that produced a non-finite number is refused at projection.

    `NaN` is not JSON and fails identically on every retry, so it belongs in the projection-failure
    series, leaving `chemclaw_result_publish_failures_total` for destination or database trouble.
    """
    from chemclaw.core.metrics import METRICS

    _with_sink(monkeypatch, "alpha")
    projection_before = METRICS.value("chemclaw_result_projection_failures_total")
    publish_before = METRICS.value("chemclaw_result_publish_failures_total")

    written = asyncio.run(
        outbox.enqueue_payload(
            calc_ref="nonfinite@v1:a:b",
            calc_type="reaction.energy",
            payload_kind="ReactionEnergyResult",
            payload={
                "reactants": ["CCO"],
                "products": ["CC=O"],
                "method": "GFN2-xTB",
                "delta_e_kcal": float("nan"),
            },
        )
    )

    assert written == 0, "a document carrying a NaN was queued, and no column will take it"
    assert METRICS.value("chemclaw_result_projection_failures_total") == projection_before + 1, (
        "a value this release cannot publish at all must be counted as the permanent gap it is"
    )
    assert METRICS.value("chemclaw_result_publish_failures_total") == publish_before, (
        "refused before the queue, so the destination-health counter must not move"
    )


def test_project_payload_separates_a_projector_that_raised_from_a_payload_with_nothing_to_queue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`project_payload` separates a projector that raised from a payload with nothing to queue.

    `enqueue_payload` returns 0 for all three cases, fine for best-effort hooks; `backfill.py`
    reports counts and needs `None` for the failure. Asserted through both entry points so they
    agree.
    """
    _with_sink(monkeypatch, "alpha")
    monkeypatch.setattr(outbox, "enqueue", _counting_enqueue)

    # `points[].energy_hartree` missing is the real legacy shape: `xtb.scan` rows written before
    # the field was renamed carry `energy`, and `_scan` subscripts rather than `.get()`s it.
    legacy_scan = {
        "smiles": "CCO",
        "coordinate": "dihedral",
        "points": [{"value": 0.0, "energy": -1.0}],
    }
    assert (
        outbox.project_payload(calc_ref="s1", calc_type="xtb.scan", payload=legacy_scan) is None
    ), "a projector that raised must be distinguishable from one with nothing to queue"
    counted = asyncio.run(
        outbox.enqueue_payload(calc_ref="s1", calc_type="xtb.scan", payload=legacy_scan)
    )
    assert counted == 0, "the count-only entry point keeps its contract: never raises, answers 0"

    good = {"smiles": "CCO", "pka": 4.2, "method": "empirical"}
    records = outbox.project_payload(calc_ref="p1", calc_type="pka", payload=good)
    assert records is not None and len(records) == 1, (
        "a readable payload must come back as its records, not as the absence"
    )


async def _counting_enqueue(records: Any) -> int:
    """Stand in for the queue write: this pair of assertions is about the projection."""
    return len(records)
