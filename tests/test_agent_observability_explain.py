"""The trail's own timestamps and ordering, and the plan step `explain` renders.

Decision: `D-2026-08-27-a-refusal-is-not-a-crash`. `PostgresAuditSink.record` buffers, so `ts` is
bound from the middleware's stamp and `chemclaw explain` orders by it rather than by insert
order. `explain` renders `plan_step` (the always-empty `purpose` is gone). Postgres-backed: both
claims are about columns.
"""

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


async def test_the_row_keeps_the_timestamp_the_middleware_stamped() -> None:
    """The INSERT binds `ts` rather than letting the column default to the flush moment.

    A minute in the past, so a defaulted `now()` cannot pass for the stamp.
    """
    await migrated_db_or_skip()
    session = "explain-ts-session"
    started = datetime.now(UTC) - timedelta(minutes=1)
    sink = PostgresAuditSink()
    await sink.record(_event("predict_pka", ts=started, plan_step="", session=session))
    await sink.flush()

    conn = await psycopg.AsyncConnection.connect(settings.postgres_dsn)
    async with conn:
        cursor = await conn.execute("SELECT ts FROM audit_events WHERE session_id = %s", (session,))
        rows = await cursor.fetchall()
    assert rows and abs((rows[0][0] - started).total_seconds()) < 1.0


async def test_explain_orders_by_when_the_tool_ran_and_names_the_plan_step() -> None:
    """Both halves of the reconstruction, over rows written in the *wrong* order on purpose.

    The second call is recorded first, as a batching sink under load can do, so `id ASC` would
    report them backwards; ordering by `ts` tells the turn's story.
    """
    await migrated_db_or_skip()
    session = "explain-order-session"
    first = datetime.now(UTC) - timedelta(minutes=2)
    second = first + timedelta(seconds=30)
    sink = PostgresAuditSink()
    # Recorded out of order, as a drained batch can be.
    await sink.record(_event("record_note", ts=second, plan_step="write it up", session=session))
    await sink.record(
        _event("predict_pka", ts=first, plan_step="measure the amine", session=session)
    )
    await sink.flush()

    lines = await explain(session)

    rendered = [line for line in lines if line.strip().startswith("tool ")]
    assert [line.split()[1] for line in rendered] == ["predict_pka", "record_note"]
    assert "for step: measure the amine" in rendered[0]
    assert "for step: write it up" in rendered[1]
