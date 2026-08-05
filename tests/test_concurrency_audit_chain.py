"""Does the GxP hash chain still verify when many appenders write at once?

`PostgresAuditSink.record` reads the chain tip, hashes the new event onto it, and inserts — three
steps that are only atomic because of one line: `SELECT pg_advisory_xact_lock(...)` before the read.
Its comment states the hazard exactly — "two concurrent inserts cannot read the same tip and fork
the chain" — and every existing test of the sink appends sequentially, where that lock could be
deleted with no test noticing.

That is a worse gap here than elsewhere. A forked chain is not a crash and not a wrong answer; it
is a trail that fails `make audit-verify` **later**, with no way to tell tampering from a race, and
under GxP the trail's whole value is that a failure means something. So this puts real concurrent
writers behind the claim and then asks the production verifier for its verdict.

**Both tests were shown to kill the mutant they exist for**, which is the only evidence that a
passing concurrency test is testing anything: with the `pg_advisory_xact_lock` line replaced by
`pass`, both fail immediately — the first on `count(DISTINCT prev_hash)` (twenty-four rows sharing
far fewer predecessors) and the second on the number of rows claiming to be the chain's genesis.
Restored, both pass. A green concurrency test whose guard can be deleted without it noticing is
worse than none, because it is *cited*.

Postgres-backed and therefore skipped offline, like the rest of the sink's tests. It truncates
`audit_events` for the same reason `test_audit_chain.py` does — `verify_chain` reads the whole
table — and `tests/pg.py`'s per-run schema is what makes that safe.
"""

from __future__ import annotations

import asyncio

from chemclaw.agent.audit import AuditEvent
from chemclaw.agent.audit_store import PostgresAuditSink
from chemclaw.core.config import settings
from chemclaw.core.db import connect
from chemclaw.durable.audit_chain import verify_chain
from tests.pg import migrated_db_or_skip

# Enough writers that a lost lock is near-certain to fork the chain rather than merely likely.
# Each is its own sink and therefore its own connection, which is what a second pod is; sharing one
# would serialize them in the client and prove nothing.
_WRITERS = 24


def _event(index: int) -> AuditEvent:
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


async def _truncate() -> None:
    """Empty the trail so `verify_chain`'s verdict is about this test's rows and nothing else."""
    async with await connect(settings.postgres_dsn) as conn:
        await conn.execute("TRUNCATE audit_events RESTART IDENTITY")
        await conn.commit()


def test_concurrent_appends_produce_one_unforked_verifiable_chain() -> None:
    """Twenty-four sinks append at once; the trail must verify and hold every event.

    Two assertions, because a forked chain and a lost event are different failures and only one of
    them is visible to the verifier. `verify_chain() == []` catches a fork — two rows claiming the
    same `prev_hash`, which is what dropping the advisory lock produces. The row count catches the
    other outcome, where a racing writer's insert is simply gone; a chain of one row verifies
    perfectly, so "it verifies" alone would pass a trail that had thrown away twenty-three events.
    """

    async def _run() -> None:
        await migrated_db_or_skip()
        await _truncate()

        await asyncio.gather(
            *(PostgresAuditSink().record(_event(index)) for index in range(_WRITERS))
        )

        problems = await verify_chain()
        assert problems == [], f"concurrent appends forked the chain: {problems[:3]}"

        async with await connect(settings.postgres_dsn) as conn:
            cursor = await conn.execute(
                "SELECT count(*), count(DISTINCT prev_hash) FROM audit_events"
            )
            row = await cursor.fetchone()
        assert row is not None
        written, distinct_prev = row
        assert written == _WRITERS, f"{written} of {_WRITERS} concurrent appends survived"
        # Every row links to a different predecessor: that *is* what an unforked chain means, and
        # it is the property the advisory lock exists to give. Stated separately from the
        # verifier's verdict so a change to the verifier cannot quietly stop checking it.
        assert distinct_prev == _WRITERS, (
            f"{_WRITERS} rows share only {distinct_prev} distinct prev_hash values — "
            "two writers read the same tip"
        )

        await _truncate()

    asyncio.run(_run())


def test_a_second_wave_chains_onto_the_first_rather_than_restarting_it() -> None:
    """Concurrency must not manufacture a second genesis row.

    The failure this rules out is subtle and the worst one available: a racing writer that read no
    tip at all would insert `prev_hash = ''`, which is the genesis marker. The chain would then
    verify from that row forward while everything before it hung off the trail unreferenced —
    tamper-evidence silently reduced to whatever was written after the race.
    """

    async def _run() -> None:
        await migrated_db_or_skip()
        await _truncate()

        await asyncio.gather(*(PostgresAuditSink().record(_event(i)) for i in range(_WRITERS // 2)))
        await asyncio.gather(
            *(PostgresAuditSink().record(_event(i)) for i in range(_WRITERS // 2, _WRITERS))
        )

        assert await verify_chain() == []
        async with await connect(settings.postgres_dsn) as conn:
            cursor = await conn.execute("SELECT count(*) FROM audit_events WHERE prev_hash = ''")
            row = await cursor.fetchone()
        assert row is not None
        assert row[0] == 1, f"{row[0]} rows claim to be the start of the chain; exactly one may"

        await _truncate()

    asyncio.run(_run())
