"""The durable Postgres audit sink persists a tool-audit row (INV-3).

`PostgresAuditSink` is the "who ran what" record; it had no direct test. This proves an append-only
round trip against a real database (CI provides one; the offline sandbox skips): a recorded
`AuditEvent` lands in `audit_events` with every field intact, and concurrent appenders lose nothing.

The concurrency test came from the deleted concurrency-chain suite, which drove twenty-four
writers at the sink to prove the hash chain could not fork under a lost advisory lock.
The chain is gone and so is the lock, but the other half of what that test measured is not about
the chain at all: an insert that races another insert must still arrive. Rows are independent now,
so there is no ordering to get wrong — only the arrival, which is what is asserted here.
"""

import asyncio

import psycopg
import pytest

from chemclaw.agent.audit import AuditEvent
from chemclaw.agent.audit_store import PostgresAuditSink
from chemclaw.core.config import settings
from chemclaw.core.db import connect
from tests.pg import migrated_db_or_skip

# Enough writers that a sink dropping rows under contention shows up as a count, not a coin flip.
# Each is its own sink and therefore its own connection, which is what a second pod is; sharing one
# would serialize them in the client and prove nothing.
_WRITERS = 24


def test_postgres_audit_sink_persists_an_event() -> None:
    """Recording an event writes one append-only row with all fields preserved."""

    async def _run() -> None:
        await migrated_db_or_skip()
        correlation_id = "conv-audit-roundtrip"
        event = AuditEvent(
            correlation_id=correlation_id,
            actor="u-oid-1",
            tool="sample_conformers",
            arguments='{"smiles": "CCO"}',
            outcome="ok",
            detail="job qm-1 started",
            latency_ms=12.5,
            revision="orchestrator-abc123",
            # The two provenance fields are read back together, because the mistake they guard
            # against is one being written into the other's column: the `_INSERT` column list and
            # the value tuple are positional, so a field appended to `AuditEvent` and forgotten in
            # one of the two lands every later value one column to the left — silently, since both
            # are `TEXT NOT NULL DEFAULT ''`.
            tool_revision="calc@server-9f3c1d",
        )
        sink = PostgresAuditSink()
        await sink.record(event)
        # `record` buffers and returns — the write is off the tool-call path — so the drain seam
        # is what a reader awaits before asserting rows. Production's reader is the runner's
        # turn-end flush; this is the test-side use of the same seam.
        await sink.flush()

        # Read the row back and assert every field survived the insert.
        conn = await psycopg.AsyncConnection.connect(settings.postgres_dsn)
        try:
            cursor = await conn.execute(
                "SELECT actor, tool, arguments, outcome, detail, latency_ms, revision, "
                "tool_revision "
                "FROM audit_events WHERE correlation_id = %s ORDER BY id DESC LIMIT 1",
                (correlation_id,),
            )
            row = await cursor.fetchone()
        finally:
            await conn.close()

        assert row is not None
        assert row[0] == "u-oid-1"
        assert row[1] == "sample_conformers"
        assert row[2] == '{"smiles": "CCO"}'
        assert row[3] == "ok"
        assert row[4] == "job qm-1 started"
        assert float(row[5]) == 12.5
        assert row[6] == "orchestrator-abc123"
        assert row[7] == "calc@server-9f3c1d"

    asyncio.run(_run())


def _race_event(index: int) -> AuditEvent:
    """One distinguishable audit event — distinct fields so a lost row is a detectable loss."""
    return AuditEvent(
        correlation_id=f"c-race-{index}",
        actor="u-race",
        tool="find_notes",
        arguments=f'{{"text": "race {index}"}}',
        outcome="ok",
        detail=f"concurrent append {index}",
        latency_ms=float(index),
    )


def test_concurrent_appends_all_arrive() -> None:
    """Twenty-four sinks append at once and every event is in the trail afterwards.

    Counted by `correlation_id` rather than over the whole table, so this needs no `TRUNCATE` and
    cannot be perturbed by another test's rows — which is also why it is safe to run beside the
    round-trip test above against one shared schema.
    """

    async def _run() -> None:
        await migrated_db_or_skip()

        sinks = [PostgresAuditSink() for _ in range(_WRITERS)]
        await asyncio.gather(*(sink.record(_race_event(index)) for index, sink in enumerate(sinks)))
        await asyncio.gather(*(sink.flush() for sink in sinks))

        async with await connect(settings.postgres_dsn) as conn:
            cursor = await conn.execute(
                "SELECT count(*) FROM audit_events WHERE correlation_id LIKE 'c-race-%'"
            )
            row = await cursor.fetchone()
        assert row is not None
        assert row[0] == _WRITERS, f"{row[0]} of {_WRITERS} concurrent appends survived"

    asyncio.run(_run())


def test_the_write_buffer_sheds_the_oldest_rather_than_growing_without_bound(
    monkeypatch: "pytest.MonkeyPatch",
) -> None:
    """A slow database must not be able to grow this buffer until the pod dies.

    `_flush_all` already refuses to re-queue a failed batch, which bounds the buffer against a
    database that is **down**. Nothing bounded it against one that is merely **slow** — `record`
    appends and returns, so at the ~90 rows a turn this module measures, a drain taking seconds
    loses the race on the producer side inside a 1 GiB pod.

    Driven without a database on purpose: no flusher can make progress here, which is exactly the
    shape being bounded. The assertions are that the buffer stops at the bound and that the events
    it kept are the *newest* — an operator reading this trail is asking what just happened.
    """
    monkeypatch.setattr(settings, "agent_audit_buffer_max_events", 10)

    async def _run() -> None:
        sink = PostgresAuditSink(dsn="postgresql://nobody@127.0.0.1:1/none")
        for index in range(25):
            await sink.record(_race_event(index))
        buffered = list(sink._buffer)
        assert len(buffered) <= 10, f"buffer grew past its bound: {len(buffered)}"
        assert [event.latency_ms for event in buffered] == [float(i) for i in range(15, 25)], (
            "the buffer shed the newest events; the oldest are the ones that may go"
        )

    asyncio.run(_run())


def test_a_zero_bound_restores_the_unbounded_buffer(
    monkeypatch: "pytest.MonkeyPatch",
) -> None:
    """0 is the escape hatch for a deployment that would rather have the OOM than the gap.

    Asserted because a bound whose disable path is untested is a bound nobody can opt out of, and
    the setting's own comment promises this.
    """
    monkeypatch.setattr(settings, "agent_audit_buffer_max_events", 0)

    async def _run() -> None:
        sink = PostgresAuditSink(dsn="postgresql://nobody@127.0.0.1:1/none")
        for index in range(25):
            await sink.record(_race_event(index))
        assert len(sink._buffer) == 25

    asyncio.run(_run())
