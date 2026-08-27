"""Postgres backing for the turn-cost ledger (`infra/sql/033_cost_attribution.sql`).

Kept separate from `chemclaw.agent.turn_cost` for the reason `audit_store` is kept separate from
`audit`: the module the front door imports on every turn carries no database dependency, so a
memory-store process never pulls psycopg for a store it will not use.

The write is an **upsert on `correlation_id`**, not an append. The row is booked from a task that
outlives its turn, and the one arithmetic error a cost ledger must never make is counting a turn
twice — so a retry, or a second write under the same correlation id, replaces rather than adds.

**Write-only from this process, and that is the honest state rather than an oversight.** There was
a `read_spend_by_actor` whose docstring called itself "the whole point of the table"; it had no
caller in `src/` — no route, no CLI, no ops endpoint — and the only other reader of `turn_costs`,
`evals/live.session_tokens`, had none either. Both went in the 2026-08-27 sweep. What reads the
ledger today is an operator with `psql`, and `tests/test_turn_cost.py` pins that absence so a
reader cannot come back without a surface to reach it through.
"""

from contextlib import AbstractAsyncContextManager

import psycopg
from psycopg.rows import TupleRow

from chemclaw.agent.turn_cost import TurnCost
from chemclaw.core import db
from chemclaw.core.config import settings

# Every column the writer sets, in one tuple so the INSERT list, the placeholder count and the
# `DO UPDATE` list below are derived from it rather than being three hand-kept copies — the shape
# in which the previous ten-column version was already one edit away from a mismatch.
_COLUMNS = (
    "correlation_id",
    "session_id",
    "actor",
    "profile",
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "duration_seconds",
    "completed",
    "outcome",
    "error_code",
    "model",
    "tool_calls",
    "tool_failures",
    "tool_refusals",
    "jobs_started",
    "ttft_seconds",
)

# `correlation_id` is the conflict target, so it is the one column the update must not re-set.
_UPDATED = ",\n        ".join(f"{name} = EXCLUDED.{name}" for name in _COLUMNS[1:])

_UPSERT = f"""
    INSERT INTO turn_costs ({", ".join(_COLUMNS)})
    VALUES ({", ".join(["%s"] * len(_COLUMNS))})
    ON CONFLICT (correlation_id) DO UPDATE SET
        {_UPDATED},
        recorded_at = now()
"""


def _connect() -> AbstractAsyncContextManager[psycopg.AsyncConnection[TupleRow]]:
    """The configured connection, with the shared statement timeout (one place, DRY)."""
    return db.connection(settings.session_store_dsn or settings.postgres_dsn)


class PostgresTurnCostSink:
    """Writes each completed turn's cost to `turn_costs`, one connection per row."""

    async def record(self, cost: TurnCost) -> None:
        """Insert the cost, replacing any existing row for the same correlation id."""
        async with _connect() as conn:
            await conn.execute(
                _UPSERT,
                tuple(getattr(cost, name) for name in _COLUMNS),
            )
            await conn.commit()
