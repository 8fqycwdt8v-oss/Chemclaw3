"""Integration tests for the Postgres turn-cost ledger (`infra/sql/033_cost_attribution.sql`).

Proves against a real database that a retried write is an upsert, never a double-count, and that
two records sharing a correlation id do not merge: the conflict target is `turn_id`
(`D-2026-09-06-an-id-a-caller-chooses-is-not-a-key`). The read-back is this file's own `_spend`;
nothing in `src/` reads the ledger this way. Skips without Postgres; tests use distinct
`correlation_id`/actor prefixes because the schema is shared.
"""

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


async def test_recording_a_cost_is_findable_with_its_own_totals() -> None:
    """The write side, proven by reading the row back: nothing else exposes a single row."""
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


async def test_a_retried_write_of_one_record_replaces_never_adds() -> None:
    """A retried write of one record replaces, never adds.

    One object recorded twice, which is what a retry is; asserted on row count and summed tokens.
    """
    sink = await _sink_or_skip()
    actor = "pgcost-actor-upsert"
    cost = TurnCost(correlation_id="pgcost-upsert-1", actor=actor, input_tokens=999)
    await sink.record(cost)
    await sink.record(cost)

    assert await _spend(actor) == (1, 999), "a retried write was counted as a second turn"


async def test_two_turns_under_one_correlation_id_are_two_rows_and_neither_is_erased() -> None:
    """Two turns under one correlation id are two rows, and neither is erased.

    The front door adopts a caller's `X-Chemclaw-Correlation-Id` for tracing, so keying the ledger
    on it would let a client collapse its own history. The header is asserted too, so a fix that
    stopped adopting it cannot pass by removing the feature.
    """
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


async def test_distinct_correlation_ids_both_count() -> None:
    """Two genuinely different turns for one actor both contribute, unlike a retry of one."""
    sink = await _sink_or_skip()
    actor = "pgcost-actor-distinct"
    await sink.record(TurnCost(correlation_id="pgcost-distinct-1", actor=actor, input_tokens=100))
    await sink.record(TurnCost(correlation_id="pgcost-distinct-2", actor=actor, input_tokens=50))

    assert await _spend(actor) == (2, 150)


async def test_a_row_written_before_the_knowledge_columns_existed_reads_as_unknown() -> None:
    """A row written before the knowledge columns existed reads as unknown (NULL).

    `retrieval_calls = 0` means "answered without consulting the record", so defaulting old rows to
    0 would assert that about history that was never measured.
    """
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


async def test_an_estimate_reaches_the_ledger_and_stays_out_of_the_measured_sum() -> None:
    """A turn's usage estimate reaches the ledger in its own column, outside the measured sum.

    An abandoned stream is billed but never reported, so the budget's estimate is stored. Both
    halves: the estimate arrives, and `_SPEND` is unchanged by it, so an inferred number never
    passes for a provider's.
    """
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
