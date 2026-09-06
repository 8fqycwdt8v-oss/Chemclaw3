"""The trail's own timestamps and ordering, and the "why" column that was structurally blank.

The decision is `D-2026-08-27-a-refusal-is-not-a-crash`. `PostgresAuditSink.record` buffers and
returns, `audit_events.ts` defaulted to `now()` at INSERT and `id` is a `BIGSERIAL` assigned at the
same moment — so under load a turn's rows were both dated and *ordered* by whenever a batch happened
to drain, and `chemclaw explain` reconstructs a turn from that order.

Beside it, `explain` rendered `purpose`, which has been empty on every row ever written and cannot
be authored honestly (`agent/audit.AuditEvent.purpose`). It now renders `plan_step`, which is a
narrower question with an exact answer.

Postgres-backed, because both claims are about columns: what the INSERT binds and what the SELECT
orders by. Skipped where no database is configured — the run's own epilogue says so.
"""

import asyncio
from datetime import UTC, datetime, timedelta

import psycopg

from chemclaw.agent.audit import AuditEvent
from chemclaw.agent.audit_store import PostgresAuditSink
from chemclaw.cli.explain import explain
from chemclaw.core.config import settings
from tests.pg import migrated_db_or_skip


def _event(tool: str, *, ts: datetime, plan_step: str, session: str) -> AuditEvent:
    """One audited call, dated when it started rather than when it will be flushed."""
    return AuditEvent(
        correlation_id="turn-1",
        session_id=session,
        plan_step=plan_step,
        actor="u-oid-1",
        tool=tool,
        arguments="{}",
        outcome="ok",
        detail="",
        latency_ms=1.0,
        ts=ts,
    )


def test_the_row_keeps_the_timestamp_the_middleware_stamped() -> None:
    """The INSERT binds `ts` rather than letting the column default to the flush moment.

    A minute in the past is used deliberately: `now()` would be indistinguishable from a correct
    stamp taken a millisecond earlier, so the test would pass against the defect it exists to
    catch.
    """

    async def _run() -> None:
        await migrated_db_or_skip()
        session = "explain-ts-session"
        started = datetime.now(UTC) - timedelta(minutes=1)
        sink = PostgresAuditSink()
        await sink.record(_event("predict_pka", ts=started, plan_step="", session=session))
        await sink.flush()

        conn = await psycopg.AsyncConnection.connect(settings.postgres_dsn)
        async with conn:
            cursor = await conn.execute(
                "SELECT ts FROM audit_events WHERE session_id = %s", (session,)
            )
            rows = await cursor.fetchall()
        assert rows and abs((rows[0][0] - started).total_seconds()) < 1.0

    asyncio.run(_run())


def test_explain_orders_by_when_the_tool_ran_and_names_the_plan_step() -> None:
    """Both halves of the reconstruction, over rows written in the *wrong* order on purpose.

    The second call is recorded first, so `id ASC` alone would report them backwards — which is
    exactly what a batching sink under load produces. Ordering by `ts` is what makes the
    reconstruction the turn's story rather than the flusher's.
    """

    async def _run() -> None:
        await migrated_db_or_skip()
        session = "explain-order-session"
        first = datetime.now(UTC) - timedelta(minutes=2)
        second = first + timedelta(seconds=30)
        sink = PostgresAuditSink()
        # Recorded out of order, as a drained batch can be.
        await sink.record(
            _event("record_note", ts=second, plan_step="write it up", session=session)
        )
        await sink.record(
            _event("predict_pka", ts=first, plan_step="measure the amine", session=session)
        )
        await sink.flush()

        lines = await explain(session)

        rendered = [line for line in lines if line.strip().startswith("tool ")]
        assert [line.split()[1] for line in rendered] == ["predict_pka", "record_note"]
        assert "for step: measure the amine" in rendered[0]
        assert "for step: write it up" in rendered[1]

    asyncio.run(_run())
