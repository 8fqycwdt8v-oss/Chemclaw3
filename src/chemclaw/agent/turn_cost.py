"""The durable ledger of what one turn cost, and who it cost it for.

`TurnCost` lives in `chemclaw.core.turn_cost` (so `chemclaw.evals` can read it without importing
`agent`) and is re-exported; this module holds the sink protocol, backend choice and write.

A table rather than a metric label: per-actor attribution needs unbounded cardinality and long
history, and `core/metrics` caps label series because a label value is attacker-influenced. Not
`api/budget.py` either: that is an in-process guard that may forget; a ledger must not.

`record_turn_cost` never awaits: it is called from teardown on the disconnect path, where an `await`
re-raises the cancellation and skips the rest. It schedules the write as a task, so a disconnected
turn — often the runaway one — is still booked.
"""

import asyncio
import logging
from typing import Protocol

from chemclaw.core.config import settings
from chemclaw.core.metrics_bridge import degraded
from chemclaw.core.turn_cost import TurnCost

logger = logging.getLogger(__name__)

# Re-exported: see the module docstring.
__all__ = [
    "NullTurnCostSink",
    "TurnCost",
    "TurnCostSink",
    "default_turn_cost_sink",
    "record_turn_cost",
]

# Strong references to in-flight writes; the event loop keeps only weak ones, so a write could be
# garbage-collected mid-flight.
_PENDING: set[asyncio.Task[None]] = set()


class TurnCostSink(Protocol):
    """Where a turn's cost goes. One method, so a deployment without Postgres needs no database."""

    async def record(self, cost: TurnCost) -> None:
        """Persist one turn's cost."""
        ...


class NullTurnCostSink:
    """Drops costs — the fallback for a deployment with no database configured."""

    async def record(self, cost: TurnCost) -> None:
        """Log at debug level and keep nothing."""
        logger.debug("turn cost dropped (no durable store): %s", cost.correlation_id)


def default_turn_cost_sink() -> TurnCostSink:
    """The durable sink where a database exists, else the null one.

    Keys on `session_store == "postgres"`, as the audit sink and job record do. The store is
    imported
    lazily so a memory-store process never loads psycopg.
    """
    if settings.session_store != "postgres":
        return NullTurnCostSink()
    from chemclaw.agent.turn_cost_store import PostgresTurnCostSink

    return PostgresTurnCostSink()


def record_turn_cost(cost: TurnCost) -> None:
    """Book one turn's cost without awaiting — see the module docstring.

    Synchronous by contract: both callers (`api/runner._book_turn_spend` and
    `durable/template_activities._book_step_spend`) run in teardown, where an `await` would re-raise
    a
    pending cancellation and skip what follows. The write runs as its own task held in `_PENDING`,
    and a failure is logged at warning level and lost rather than failing a turn that already
    answered.
    """
    sink = default_turn_cost_sink()
    if isinstance(sink, NullTurnCostSink):
        return

    async def _write() -> None:
        try:
            await sink.record(cost)
        except Exception:
            degraded(
                logger,
                "cost_ledger",
                "could not record the cost of turn %s; the spend is lost to the ledger "
                "(the metrics counters still carry it in aggregate)",
                cost.correlation_id,
            )

    try:
        task = asyncio.get_running_loop().create_task(_write())
    except RuntimeError:  # no running loop — a synchronous caller has nowhere to schedule
        logger.debug("no event loop to record the cost of turn %s", cost.correlation_id)
        return
    _PENDING.add(task)
    task.add_done_callback(_PENDING.discard)
