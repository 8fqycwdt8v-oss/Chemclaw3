"""The shape of one turn's spend — the record, not the machinery that writes it.

**In `core/` because two layers read it and only one writes it.** `agent/turn_cost.py` owns the
sink, the fire-and-forget scheduling and the `finally`-block discipline that makes a disconnected
turn still book its cost; those are behaviours and they belong beside the turn. This is the *record*
those behaviours move, and `chemclaw.evals` scores it — `turn_cost_ratio` reads a list of these out
of a case file.

Splitting them is what keeps `tests/test_layering.py` honest rather than what works around it. The
eval layer is deliberately not allowed to import `chemclaw.agent`: an eval scores output, the agent
is the thing under test, and a dependency from the scorer to the scored is the one edge that would
let a change to the agent silently change what "correct" means. `core` is the shared kernel both may
read, and a data shape is exactly the kind of thing it exists to hold.

The alternative — a second model in `evals/` describing the same five counters — was rejected for
the reason the reuse is worth having: a case file's shape would then be free to drift from what the
ledger actually produces, and nothing would say so.
"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

__all__ = ["TurnCost"]


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
