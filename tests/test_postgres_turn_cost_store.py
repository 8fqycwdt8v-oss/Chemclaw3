"""Integration tests for the Postgres turn-cost ledger (`infra/sql/033_cost_attribution.sql`, R1.5).

`PostgresTurnCostSink` had no direct test: `test_turn_cost.py` proves the fire-and-forget scheduling
contract entirely against `_RecordingSink`/`_FailingSink` fakes, never the durable sink a deployment
actually writes to. That leaves the one property this table exists to hold — "a retried write is an
upsert, never a double-count" (the module's own docstring) — unproven against a real database, and
`read_spend_by_actor` (the whole reason the table exists) with no reader test at all.

Follows `tests/test_postgres_store.py`'s pattern: `migrated_db_or_skip()` skips cleanly offline and
runs for real in CI; each test is a sync `def` wrapping an inner `async def _run()` driven by
`asyncio.run`; isolation comes from the session-scoped schema redirect in `conftest.py`, with a
distinct `correlation_id`/actor prefix per test on top so tests sharing one schema cannot see each
other's rows — `read_spend_by_actor` with `actor=""` sums *everyone*, so cross-test leakage would
silently inflate a deployment-wide total.
"""

import asyncio
from datetime import UTC, datetime, timedelta

from chemclaw.agent.turn_cost import TurnCost
from chemclaw.agent.turn_cost_store import PostgresTurnCostSink, read_spend_by_actor
from chemclaw.core import db
from chemclaw.core.config import settings
from tests.pg import migrated_db_or_skip


async def _sink_or_skip() -> PostgresTurnCostSink:
    """Return a migrated Postgres turn-cost sink, or skip if no database is reachable."""
    await migrated_db_or_skip()
    return PostgresTurnCostSink()


def _dsn() -> str:
    """The DSN the sink itself resolves to, so a backdoor insert lands in the same schema."""
    return settings.session_store_dsn or settings.postgres_dsn


async def _insert_with_recorded_at(correlation_id: str, actor: str, recorded_at: datetime) -> None:
    """Insert a row with an explicit `recorded_at`, to exercise the reader's time window.

    `record()` always stamps `now()`, so backdating a row to test `read_spend_by_actor`'s window
    needs a direct insert — the one place this file reaches past the class under test, and only to
    set up a fixture the public API has no way to construct.
    """
    async with db.connection(
        _dsn(), statement_timeout_seconds=settings.pg_statement_timeout_seconds
    ) as conn:
        await conn.execute(
            """
            INSERT INTO turn_costs (correlation_id, actor, input_tokens, output_tokens, recorded_at)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (correlation_id, actor, 10, 5, recorded_at),
        )
        await conn.commit()


def test_recording_a_cost_is_findable_with_its_own_totals() -> None:
    """The write side, proven through the read side: nothing else exposes a single row."""

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

        rows = await read_spend_by_actor(days=1, actor="pgcost-actor-basic")
        assert rows == [("pgcost-actor-basic", 1, 125)]  # 100 + 20 + 5 + 0

    asyncio.run(_run())


def test_a_second_write_under_the_same_correlation_id_replaces_never_adds() -> None:
    """The one arithmetic error this ledger must not make (module docstring): no double-count.

    A retry, or a second write for the same turn, must overwrite the row rather than accumulate a
    second one — proven here by asserting both the row count (`turns`) and the summed tokens after
    the second write, not merely that the final value looks plausible.
    """

    async def _run() -> None:
        sink = await _sink_or_skip()
        actor = "pgcost-actor-upsert"
        await sink.record(TurnCost(correlation_id="pgcost-upsert-1", actor=actor, input_tokens=100))
        await sink.record(TurnCost(correlation_id="pgcost-upsert-1", actor=actor, input_tokens=999))

        rows = await read_spend_by_actor(days=1, actor=actor)
        assert rows == [(actor, 1, 999)], "a retried write was counted as a second turn"

    asyncio.run(_run())


def test_distinct_correlation_ids_both_count() -> None:
    """Two genuinely different turns for one actor both contribute, unlike a retry of one."""

    async def _run() -> None:
        sink = await _sink_or_skip()
        actor = "pgcost-actor-distinct"
        first = TurnCost(correlation_id="pgcost-distinct-1", actor=actor, input_tokens=100)
        second = TurnCost(correlation_id="pgcost-distinct-2", actor=actor, input_tokens=50)
        await sink.record(first)
        await sink.record(second)

        rows = await read_spend_by_actor(days=1, actor=actor)
        assert rows == [(actor, 2, 150)]

    asyncio.run(_run())


def test_read_spend_by_actor_with_no_name_reports_every_actor_biggest_first() -> None:
    """`actor=""` is the deployment-wide breakdown, ordered by spend descending (`ORDER BY 3`)."""

    async def _run() -> None:
        sink = await _sink_or_skip()
        big, small = "pgcost-actor-big", "pgcost-actor-small"
        await sink.record(TurnCost(correlation_id="pgcost-mix-1", actor=big, input_tokens=1000))
        await sink.record(TurnCost(correlation_id="pgcost-mix-2", actor=small, input_tokens=10))

        rows = await read_spend_by_actor(days=1, actor="")
        by_actor = {row[0]: row for row in rows}
        assert by_actor[big][2] > by_actor[small][2]
        big_index = next(i for i, row in enumerate(rows) if row[0] == big)
        small_index = next(i for i, row in enumerate(rows) if row[0] == small)
        assert big_index < small_index, "the biggest spender must sort first"

    asyncio.run(_run())


def test_naming_one_actor_excludes_every_other_actor() -> None:
    """`actor="X"` must not also report Y's spend — the filter half of the same query."""

    async def _run() -> None:
        sink = await _sink_or_skip()
        wanted, other = "pgcost-actor-wanted", "pgcost-actor-unwanted"
        await sink.record(TurnCost(correlation_id="pgcost-filter-1", actor=wanted, input_tokens=42))
        await sink.record(TurnCost(correlation_id="pgcost-filter-2", actor=other, input_tokens=42))

        rows = await read_spend_by_actor(days=1, actor=wanted)
        assert [row[0] for row in rows] == [wanted]

    asyncio.run(_run())


def test_a_row_outside_the_window_is_excluded_until_the_window_widens() -> None:
    """`recorded_at >= now() - days` must actually bound the query, not just accept an argument."""

    async def _run() -> None:
        await migrated_db_or_skip()
        actor = "pgcost-actor-stale"
        stale_at = datetime.now(UTC) - timedelta(days=10)
        await _insert_with_recorded_at("pgcost-stale-1", actor, stale_at)

        narrow = await read_spend_by_actor(days=1, actor=actor)
        assert narrow == [], "a ten-day-old row was counted inside a one-day window"

        wide = await read_spend_by_actor(days=30, actor=actor)
        assert wide == [(actor, 1, 15)]  # 10 + 5, the fixture's input/output tokens

    asyncio.run(_run())
