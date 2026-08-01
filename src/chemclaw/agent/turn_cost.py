"""The durable ledger of what one turn cost, and who it cost it for.

**Why a table and not a label.** Spend was already measured — `chemclaw_tokens_total` and its four
siblings, labelled `profile`. That answers "what is this deployment spending" and cannot answer
"what did this team spend last quarter", and the gap is structural rather than an oversight in the
label set: `api/metrics` caps a counter at 64 label series and refuses beyond (D-152), because a
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
from datetime import datetime
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from chemclaw.core.config import settings

logger = logging.getLogger(__name__)

# Strong references to the in-flight writes. Without them the event loop keeps only a weak one and
# a cost row can be garbage-collected mid-write — the documented `asyncio.create_task` hazard, and
# an invisible one here because the loss is a row nobody is watching for.
_PENDING: set[asyncio.Task[None]] = set()


class TurnCost(BaseModel):
    """What one completed turn spent, and the identity to bill it to.

    `correlation_id` is the key rather than a fresh id because it already identifies the turn
    uniquely, already keys `audit_events`, and is already on every log line — so the ledger joins to
    the trail and the logs with no new correspondence to maintain.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    correlation_id: str = Field(min_length=1)
    session_id: str = ""
    actor: str = ""
    # `default` rather than empty for a session on no profile, matching the metric label exactly, so
    # a sum here and a sum there answer the same question the same way.
    profile: str = "default"
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    cache_read_tokens: int = Field(default=0, ge=0)
    cache_write_tokens: int = Field(default=0, ge=0)
    duration_seconds: float = Field(default=0.0, ge=0)
    # False when the turn was torn down *before it answered* — `chemclaw.api.runner` books
    # `completed=answered`, so a disconnect or wall-clock deadline that lands after the answer is a
    # completed turn that keeps its history, and only one that lands before it is not. Recorded
    # rather than filtered: those turns spent real tokens, and a ledger that kept only the tidy ones
    # would be wrong in the direction that hides a runaway.
    completed: bool = True
    recorded_at: datetime | None = None


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
        except Exception:  # noqa: BLE001 - telemetry must never escalate into the turn's teardown
            logger.warning(
                "could not record the cost of turn %s; the spend is lost to the ledger "
                "(the metrics counters still carry it in aggregate)",
                cost.correlation_id,
                exc_info=True,
            )

    try:
        task = asyncio.get_running_loop().create_task(_write())
    except RuntimeError:  # no running loop — a synchronous caller has nowhere to schedule
        logger.debug("no event loop to record the cost of turn %s", cost.correlation_id)
        return
    _PENDING.add(task)
    task.add_done_callback(_PENDING.discard)
