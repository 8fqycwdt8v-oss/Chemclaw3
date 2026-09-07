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

The upsert's conflict target is `turn_id` since migration 088, not `correlation_id`
(`D-2026-09-06-an-id-a-caller-chooses-is-not-a-key`), and the two tests around that distinction are
deliberately a pair: one asserts a retried write of a record does not double, the other that two
records do not merge.
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


def test_a_retried_write_of_one_record_replaces_never_adds() -> None:
    """The one arithmetic error this ledger must not make (module docstring): no double-count.

    A retry — the *same record*, written twice — must overwrite the row rather than accumulate a
    second one, proven by asserting both the row count and the summed tokens, not merely that the
    final value looks plausible.

    **Written as one object recorded twice, which is what a retry is.** It used to be two different
    `TurnCost`s sharing a correlation id, and that is a different claim: it asserted that the row's
    identity was the correlation id, which is the id the front door *adopts* off the request
    (`D-2026-09-06-an-id-a-caller-chooses-is-not-a-key`). The test below is the other half.
    """

    async def _run() -> None:
        sink = await _sink_or_skip()
        actor = "pgcost-actor-upsert"
        cost = TurnCost(correlation_id="pgcost-upsert-1", actor=actor, input_tokens=999)
        await sink.record(cost)
        await sink.record(cost)

        assert await _spend(actor) == (1, 999), "a retried write was counted as a second turn"

    asyncio.run(_run())


def test_two_turns_under_one_correlation_id_are_two_rows_and_neither_is_erased() -> None:
    """The ledger is not erasable by the party it bills.

    `api/middleware._request_correlation_id` adopts an inbound `X-Chemclaw-Correlation-Id` whenever
    it matches `[A-Za-z0-9_-]{8,64}` — deliberately, so a chemist's click is traceable from the
    browser inwards — and `run_turn` keys the turn on it. While that id was also this table's
    primary key under `ON CONFLICT … DO UPDATE`, a client that repeated one header collapsed its own
    history to the *last* turn's numbers: measured 2026-09-06, 900,000 and 1,000 input tokens for
    one actor left one row reading 1,000, with `chemclaw_tokens_total` and `api/budget.py` still
    seeing both turns.

    So the header is asserted here too, not just the two rows: the reason this is reachable at all
    is that the filter accepts a caller's string, and a fix that quietly stopped adopting one would
    make this pass while removing a feature the tracing depends on.
    """

    async def _run() -> None:
        sink = await _sink_or_skip()
        from chemclaw.api.middleware import _CORRELATION_ID

        forged = "client-chosen-id-0001"
        assert _CORRELATION_ID.match(forged), "the premise: this id is adopted off the request"
        actor = "pgcost-actor-forged"
        for tokens in (900_000, 1_000):
            await sink.record(
                TurnCost(correlation_id=forged, actor=actor, input_tokens=tokens, session_id="s")
            )

        assert await _spend(actor) == (2, 901_000), (
            "two turns sent under one correlation id collapsed into one row — the cost ledger is "
            "erasable by the party being billed"
        )

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
                # `turn_id` is spelled out because it is the primary key since migration 088 and
                # a legacy row is one migration 088 backfilled from its own correlation id — which
                # is exactly what this INSERT reproduces.
                "INSERT INTO turn_costs (turn_id, correlation_id, actor) VALUES (%s, %s, %s) "
                "ON CONFLICT (turn_id) DO NOTHING",
                (
                    "pgcost-knowledge-legacy",
                    "pgcost-knowledge-legacy",
                    "pgcost-actor-knowledge",
                ),
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


def test_an_estimate_reaches_the_ledger_and_stays_out_of_the_measured_sum() -> None:
    """A turn the provider never reported writes a real number, in its own column.

    `stream_options.include_usage` puts a request's usage on the terminal chunk, so a turn the
    client abandons mid-message is billed by the gateway and reported by nobody. Wave 4 measured
    that end to end: `input_tokens=0, output_tokens=0` on a row for a turn that really spent, while
    the budget — which meters the measured tokens *plus* the estimate — had the number all along
    and had nowhere durable to put it (migration 087).

    Both halves are asserted, because either alone would pass a wrong implementation. That the
    estimate **arrives** catches a column the writer never sets; that `_SPEND` is **unchanged by
    it** catches the tempting fix of adding it to `input_tokens`, which would let an inferred
    number pass for a provider's in every existing dashboard and eval that reads this table.
    """

    async def _run() -> None:
        sink = await _sink_or_skip()
        actor = "pgcost-actor-estimated"
        await sink.record(
            TurnCost(
                correlation_id="pgcost-estimated-1",
                actor=actor,
                input_tokens=0,
                output_tokens=0,
                estimated_tokens=43438,
                outcome="abandoned",
                completed=False,
            )
        )

        async with db.connection(settings.session_store_dsn or settings.postgres_dsn) as conn:
            cursor = await conn.execute(
                "SELECT estimated_tokens FROM turn_costs WHERE correlation_id = %s",
                ("pgcost-estimated-1",),
            )
            row = await cursor.fetchone()
        assert row is not None, "the row the sink just wrote is not there"
        assert int(row[0]) == 43438, "the estimate did not reach the ledger"

        assert await _spend(actor) == (1, 0), "an estimate was summed into the measured tokens"

    asyncio.run(_run())
