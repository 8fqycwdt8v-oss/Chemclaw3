"""Integration tests for the Postgres turn-cost ledger (`infra/sql/033_cost_attribution.sql`, R1.5).

`PostgresTurnCostSink` had no direct test: `test_turn_cost.py` proves the fire-and-forget scheduling
contract entirely against `_RecordingSink`/`_FailingSink` fakes, never the durable sink a deployment
actually writes to. That leaves the one property this table exists to hold — "a retried write is an
upsert, never a double-count" (the module's own docstring) — unproven against a real database.

**The read-back is this file's own SQL, and it used to be production code.**
`turn_cost_store.read_spend_by_actor` called itself "the whole point of the table" and had no caller
in `src/`; its three semantics tests here (window, filter, ordering) were tests of a query nothing
asked, and the three below used it only to see what the write had written. It was deleted in the
2026-08-27 dead-code sweep, and the read-back it provided lives here as `_spend`, where a test
helper belongs. What that costs is one duplicated `SELECT`; what it buys is that the ledger's
surface is what a deployment can reach, and not one function more.

Follows `tests/test_postgres_store.py`'s pattern: `migrated_db_or_skip()` skips cleanly offline and
runs for real in CI; each test is a sync `def` wrapping an inner `async def _run()` driven by
`asyncio.run`; isolation comes from the session-scoped schema redirect in `conftest.py`, with a
distinct `correlation_id`/actor prefix per test on top so tests sharing one schema cannot see each
other's rows.
"""

import asyncio

from chemclaw.agent.turn_cost import TurnCost
from chemclaw.agent.turn_cost_store import PostgresTurnCostSink
from chemclaw.core import db
from chemclaw.core.config import settings
from tests.pg import migrated_db_or_skip

# What one actor's rows sum to. Written here rather than imported, because the production reader
# this replaced had no caller — see the module docstring.
_SPEND = """
    SELECT count(*),
           coalesce(sum(input_tokens + output_tokens + cache_read_tokens + cache_write_tokens), 0)
    FROM turn_costs
    WHERE actor = %s
"""


async def _sink_or_skip() -> PostgresTurnCostSink:
    """Return a migrated Postgres turn-cost sink, or skip if no database is reachable."""
    await migrated_db_or_skip()
    return PostgresTurnCostSink()


def _dsn() -> str:
    """The DSN the sink itself resolves to, so the read-back lands in the same schema."""
    return settings.session_store_dsn or settings.postgres_dsn


async def _spend(actor: str) -> tuple[int, int]:
    """`(turns, tokens)` recorded for `actor` — the read-back these assertions are made through."""
    async with db.connection(_dsn()) as conn:
        cursor = await conn.execute(_SPEND, (actor,))
        row = await cursor.fetchone()
    assert row is not None
    return int(row[0]), int(row[1])


def test_recording_a_cost_is_findable_with_its_own_totals() -> None:
    """The write side, proven by reading the row back: nothing else exposes a single row."""

    async def _run() -> None:
        sink = await _sink_or_skip()
        await sink.record(
            TurnCost(
                correlation_id="pgcost-basic-1",
                actor="pgcost-actor-basic",
                input_tokens=100,
                output_tokens=20,
                cache_read_tokens=5,
                cache_write_tokens=0,
            )
        )

        assert await _spend("pgcost-actor-basic") == (1, 125)  # 100 + 20 + 5 + 0

    asyncio.run(_run())


def test_a_second_write_under_the_same_correlation_id_replaces_never_adds() -> None:
    """The one arithmetic error this ledger must not make (module docstring): no double-count.

    A retry, or a second write for the same turn, must overwrite the row rather than accumulate a
    second one — proven here by asserting both the row count and the summed tokens after the second
    write, not merely that the final value looks plausible.
    """

    async def _run() -> None:
        sink = await _sink_or_skip()
        actor = "pgcost-actor-upsert"
        await sink.record(TurnCost(correlation_id="pgcost-upsert-1", actor=actor, input_tokens=100))
        await sink.record(TurnCost(correlation_id="pgcost-upsert-1", actor=actor, input_tokens=999))

        assert await _spend(actor) == (1, 999), "a retried write was counted as a second turn"

    asyncio.run(_run())


def test_distinct_correlation_ids_both_count() -> None:
    """Two genuinely different turns for one actor both contribute, unlike a retry of one."""

    async def _run() -> None:
        sink = await _sink_or_skip()
        actor = "pgcost-actor-distinct"
        await sink.record(
            TurnCost(correlation_id="pgcost-distinct-1", actor=actor, input_tokens=100)
        )
        await sink.record(
            TurnCost(correlation_id="pgcost-distinct-2", actor=actor, input_tokens=50)
        )

        assert await _spend(actor) == (2, 150)

    asyncio.run(_run())


def test_a_row_written_before_the_knowledge_columns_existed_reads_as_unknown() -> None:
    """The ambiguous zero, in a column — and why these five are nullable and undefaulted.

    `retrieval_calls = 0` is the most interesting value this table can hold: a turn that answered
    without consulting the record. `NOT NULL DEFAULT 0` would assert exactly that about every row
    written before the column existed, so a query for "turns that answered blind" would return the
    whole history of the table, none of which was measured. This drives both halves against a real
    schema: a row inserted without the columns reads NULL, and a row the sink writes carries the
    numbers it was handed.
    """

    async def _run() -> None:
        sink = await _sink_or_skip()
        async with db.connection(_dsn()) as conn:
            await conn.execute(
                "INSERT INTO turn_costs (correlation_id, actor) VALUES (%s, %s) "
                "ON CONFLICT (correlation_id) DO NOTHING",
                ("pgcost-knowledge-legacy", "pgcost-actor-knowledge"),
            )
            cursor = await conn.execute(
                "SELECT retrieval_calls, capture_calls, answer_confidence, review_required, "
                "notes_cited FROM turn_costs WHERE correlation_id = %s",
                ("pgcost-knowledge-legacy",),
            )
            legacy = await cursor.fetchone()
        assert legacy == (None, None, None, None, None), (
            "a row written before the measurement existed reports a measurement"
        )

        await sink.record(
            TurnCost(
                correlation_id="pgcost-knowledge-measured",
                actor="pgcost-actor-knowledge",
                retrieval_calls=3,
                capture_calls=1,
                answer_confidence=0.75,
                review_required=True,
                notes_cited=2,
            )
        )
        async with db.connection(_dsn()) as conn:
            cursor = await conn.execute(
                "SELECT retrieval_calls, capture_calls, answer_confidence, review_required, "
                "notes_cited FROM turn_costs WHERE correlation_id = %s",
                ("pgcost-knowledge-measured",),
            )
            measured = await cursor.fetchone()
        assert measured == (3, 1, 0.75, True, 2)

    asyncio.run(_run())
