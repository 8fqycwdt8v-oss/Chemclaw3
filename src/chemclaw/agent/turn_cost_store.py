"""Postgres backing for the turn-cost ledger (`infra/sql/033_cost_attribution.sql`).

Separate from `chemclaw.agent.turn_cost` so a memory-store process never loads psycopg.

The write upserts on `turn_id`, minted per record and never crossing a wire, so a retried write
replaces rather than double-counts. Not on `correlation_id`: a caller may choose that header, and
repeating it would overwrite its own history. Write-only here; the single reader is
`chemclaw.operations.activity.spend`.
"""

from contextlib import AbstractAsyncContextManager

import psycopg
from psycopg.rows import TupleRow

from chemclaw.agent.turn_cost import TurnCost
from chemclaw.core import db
from chemclaw.core.config import settings

# Every column the writer sets; the INSERT list, placeholder count and `DO UPDATE` list are derived
# from it.
_COLUMNS = (
    "turn_id",
    "correlation_id",
    "session_id",
    "actor",
    "profile",
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "estimated_tokens",
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
    "compacted",
    "context_unreducible",
    "retrieval_calls",
    "capture_calls",
    "answer_confidence",
    "review_required",
    "notes_cited",
    "skills_loaded",
)

# `turn_id` is the conflict target, so it is the one column the update must not re-set — and it is
# deliberately first in `_COLUMNS`, so the slice below cannot drift from the `ON CONFLICT` clause.
_UPDATED = ",\n        ".join(f"{name} = EXCLUDED.{name}" for name in _COLUMNS[1:])

_UPSERT = f"""
    INSERT INTO turn_costs ({", ".join(_COLUMNS)})
    VALUES ({", ".join(["%s"] * len(_COLUMNS))})
    ON CONFLICT ({_COLUMNS[0]}) DO UPDATE SET
        {_UPDATED},
        recorded_at = now()
"""


def _connect() -> AbstractAsyncContextManager[psycopg.AsyncConnection[TupleRow]]:
    """The configured connection, with the shared statement timeout (one place, DRY)."""
    return db.connection(settings.session_store_dsn or settings.postgres_dsn)


class PostgresTurnCostSink:
    """Writes each completed turn's cost to `turn_costs`, one connection per row."""

    async def record(self, cost: TurnCost) -> None:
        """Insert the cost, replacing any existing row for the *same record* (see the module)."""
        async with _connect() as conn:
            await conn.execute(
                _UPSERT,
                tuple(getattr(cost, name) for name in _COLUMNS),
            )
            await conn.commit()
