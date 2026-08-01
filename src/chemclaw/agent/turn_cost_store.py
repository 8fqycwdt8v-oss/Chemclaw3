"""Postgres backing for the turn-cost ledger (`infra/sql/033_cost_attribution.sql`).

Kept separate from `chemclaw.agent.turn_cost` for the reason `audit_store` is kept separate from
`audit`: the module the front door imports on every turn carries no database dependency, so a
memory-store process never pulls psycopg for a store it will not use.

The write is an **upsert on `correlation_id`**, not an append. The row is booked from a task that
outlives its turn, and the one arithmetic error a cost ledger must never make is counting a turn
twice — so a retry, or a second write under the same correlation id, replaces rather than adds.
"""

from contextlib import AbstractAsyncContextManager

import psycopg
from psycopg.rows import TupleRow

from chemclaw.agent.turn_cost import TurnCost
from chemclaw.core import db
from chemclaw.core.config import settings

_COLUMNS = (
    "correlation_id, session_id, actor, profile, input_tokens, output_tokens, "
    "cache_read_tokens, cache_write_tokens, duration_seconds, completed"
)

_UPSERT = f"""
    INSERT INTO turn_costs ({_COLUMNS})
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    ON CONFLICT (correlation_id) DO UPDATE SET
        session_id = EXCLUDED.session_id,
        actor = EXCLUDED.actor,
        profile = EXCLUDED.profile,
        input_tokens = EXCLUDED.input_tokens,
        output_tokens = EXCLUDED.output_tokens,
        cache_read_tokens = EXCLUDED.cache_read_tokens,
        cache_write_tokens = EXCLUDED.cache_write_tokens,
        duration_seconds = EXCLUDED.duration_seconds,
        completed = EXCLUDED.completed,
        recorded_at = now()
"""

# Spend for one actor over a window, which is the question the table was added for. Summed in the
# database rather than by pulling rows: a quarter of turns for one team is a large result set and a
# small answer.
_SPEND_BY_ACTOR = """
    SELECT actor,
           count(*),
           coalesce(sum(input_tokens + output_tokens + cache_read_tokens + cache_write_tokens), 0)
    FROM turn_costs
    WHERE recorded_at >= now() - make_interval(days => %s)
      AND (%s = '' OR actor = %s)
    GROUP BY actor
    ORDER BY 3 DESC
"""


def _connect() -> AbstractAsyncContextManager[psycopg.AsyncConnection[TupleRow]]:
    """The configured connection, with the shared statement timeout (one place, DRY)."""
    return db.connection(
        settings.session_store_dsn or settings.postgres_dsn,
        statement_timeout_seconds=settings.pg_statement_timeout_seconds,
    )


class PostgresTurnCostSink:
    """Writes each completed turn's cost to `turn_costs`, one connection per row."""

    async def record(self, cost: TurnCost) -> None:
        """Insert the cost, replacing any existing row for the same correlation id."""
        async with _connect() as conn:
            await conn.execute(
                _UPSERT,
                (
                    cost.correlation_id,
                    cost.session_id,
                    cost.actor,
                    cost.profile,
                    cost.input_tokens,
                    cost.output_tokens,
                    cost.cache_read_tokens,
                    cost.cache_write_tokens,
                    cost.duration_seconds,
                    cost.completed,
                ),
            )
            await conn.commit()


async def read_spend_by_actor(days: int, actor: str = "") -> list[tuple[str, int, int]]:
    """`(actor, turns, tokens)` over the last `days`, biggest spender first.

    The whole point of the table, expressed as the one query that answers it. `actor=""` reports
    every actor, which is the deployment-wide breakdown; naming one reports that one.
    """
    async with _connect() as conn:
        cursor = await conn.execute(_SPEND_BY_ACTOR, (days, actor, actor))
        rows = await cursor.fetchall()
    return [(row[0], int(row[1]), int(row[2])) for row in rows]
