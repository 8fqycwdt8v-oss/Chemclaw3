"""The durable ledger of what one turn cost, and who it cost it for.

**The record itself lives in `chemclaw.core.turn_cost`; this module is the machinery.** `TurnCost`
is re-exported here because every writer already imports it from this module and the split is about
layering rather than about call sites: `chemclaw.evals` scores these records and is deliberately
forbidden from importing `chemclaw.agent`, so the shape had to be somewhere both may read. What
stays here is everything with behaviour — the sink protocol, the backend choice, and the
fire-and-forget write below.

**Why a table and not a label.** Spend was already measured — `chemclaw_tokens_total` and its four
siblings, labelled `profile`. That answers "what is this deployment spending" and cannot answer
"what did this team spend last quarter", and the gap is structural rather than an oversight in the
label set: `core/metrics` caps a counter at 64 label series and refuses beyond (D-152), because a
label value is attacker-influenced and an unbounded map keyed on one is the memory leak this
codebase has already fixed three times. An Entra `oid` is exactly such a key — minting tokens for
many oids is the way around any per-principal limit. Attribution needs unbounded cardinality and
quarters of history; a time-series database is the wrong instrument for both, and a relational one
is the right instrument for both.

**Why not the budget tracker.** `api/budget.py` already meters tokens per user. It does so to
*refuse a turn*: in process, reset on restart, LRU-evicted under a cap. That is a guard, and its
docstring says so. A guard that forgets is correct; a ledger that forgets is not.

**Why the write must outlive the turn.** The runner books the cost from a `finally` block that also
runs on the disconnect path, where an `await` re-raises the cancellation and silently skips
everything after it (D-130). So `record_turn_cost` never awaits: it schedules the write as a task on
the loop and keeps a reference to it. A disconnected turn is not an edge case to drop — a client
that hangs up on a runaway turn is precisely the spend this ledger exists to find, and dropping it
would under-report the one case that matters.
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

# Strong references to the in-flight writes. Without them the event loop keeps only a weak one and
# a cost row can be garbage-collected mid-write — the documented `asyncio.create_task` hazard, and
# an invisible one here because the loss is a row nobody is watching for.
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

    Reads the same `session_store == "postgres"` switch as the audit sink and the job record: it is
    the deployment's statement that a database exists and durable records belong in it. The store is
    imported lazily so a memory-store process never pulls psycopg for a store it will not use.
    """
    if settings.session_store != "postgres":
        return NullTurnCostSink()
    from chemclaw.agent.turn_cost_store import PostgresTurnCostSink

    return PostgresTurnCostSink()


def record_turn_cost(cost: TurnCost) -> None:
    """Book one turn's cost, **without awaiting** — see the module docstring.

    Synchronous by contract, because the only caller is a `finally` block in which an `await` would
    re-raise a pending cancellation and skip the five context-var resets after it. The write runs as
    its own task, swallows and logs its own failure (an escaping exception would surface only as an
    unattributed `Task exception was never retrieved`), and is held in `_PENDING` until it finishes.

    A cost that cannot be written is logged at warning level and lost. That is the honest trade for
    telemetry booked off the hot path: failing a turn that already answered, in order to record what
    it cost, would be the tail wagging the dog.
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
