"""Bound what one turn may **bill**, beside the bound on how many times it may think.

`agent/loop_cap.py` caps model calls, but a call is not a unit of cost: within one iteration ceiling
a turn can bill a few thousand tokens or millions. `api/budget.py` meters before and after a turn
and cannot see inside one. This guard caps a turn's billed tokens while it runs, in the same shape
as the loop cap:

- **Enforced in `before_model`**, which no later hook can skip
  (`D-2026-08-15-an-after-model-counter-is-a-counter-that-can-be-skipped`).
- **Counted in a state channel** (`ChemclawState.billed_tokens`, a `TurnTotal`), so one budget spans
  subagents and a fan-out's concurrent writes fold additively.
- **Metered in `wrap_model_call`**, the only hook that sees the response's bill; it writes state
  through `ExtendedModelResponse`'s `Command`, driven end to end by `tests/test_spend_cap.py`.
- **Ends the run rather than raising**, so the last iteration's answer still goes out, marked
  partial. Unlike the loop cap there is no wrap-up call: past the budget there is nothing left to
  spend.
"""

import logging
from collections.abc import Awaitable, Callable, Mapping
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, cast

from langchain.agents.middleware import AgentMiddleware, ModelRequest, before_model
from langchain.agents.middleware.types import ExtendedModelResponse
from langgraph.types import Command

from chemclaw.agent.state import ChemclawState
from chemclaw.agent.turn_usage import graph_usage_tokens, metered_turn_tokens
from chemclaw.core.config import settings
from chemclaw.core.metrics_bridge import degraded

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class _SpendWatch:
    """One turn's spend mark — what it billed, and whether the cap is what stopped it."""

    capped: bool = False
    billed: int = 0


_watch: ContextVar[_SpendWatch | None] = ContextVar("chemclaw_spend_watch", default=None)


def begin_spend_watch() -> object:
    """Start watching this turn's spend; returns a token for `end_spend_watch`."""
    return _watch.set(_SpendWatch())


def end_spend_watch(token: object) -> None:
    """Tear the turn's watch down (mirrors every other ambient's reset)."""
    _watch.reset(token)  # type: ignore[arg-type]


def spend_hit_cap() -> bool:
    """Whether this turn was stopped by its spend cap.

    `False` off the request path. The state-side answer is `spend_capped`; this one is for a
    streaming driver, which is not handed the final state.
    """
    watch = _watch.get()
    return watch is not None and watch.capped


def turn_billed_tokens() -> int:
    """What this turn has billed so far, or 0 off the request path.

    The runner reports it beside the refusal, so a chemist sees how much the turn spent.
    """
    watch = _watch.get()
    return watch.billed if watch is not None else 0


def record_spend_cap(billed: int) -> None:
    """Mark this turn as stopped by its spend cap, having billed `billed`.

    The counterpart to `agent/loop_cap.record_loop_cap`, called by `enforce_spend_cap` and by tests;
    `api/runner._spend_cap_event` reads the result. `billed` is an absolute reading, so it is folded
    with `max` against what `_meter` accumulated rather than added.

    Args:
        billed: What the turn had billed when the cap fired.
    """
    watch = _watch.get()
    if watch is None:
        return
    # Mutated rather than rebound, for the reason `agent/loop_cap.py` gives its own watch: a stream
    # driven from a task of its own must still be able to see it.
    watch.billed = max(watch.billed, billed)
    watch.capped = True


def _meter(billed: int) -> None:
    """Add one model call's own bill to this turn's running total.

    Added, not `max`ed: every branch of a fan-out starts from the same base, so the largest absolute
    total would count only one branch. This is the same fold `state.TurnTotal` performs.
    """
    watch = _watch.get()
    if watch is None:
        return
    watch.billed += billed


@before_model(can_jump_to=["end"], state_schema=ChemclawState)
def enforce_spend_cap(state: Mapping[str, Any], runtime: Any) -> dict[str, Any] | None:
    """End the turn before a model call that would put it past its billed-token budget.

    Checked before the call, the only placement that bounds anything. The cap therefore bounds spend
    before the next call; the last allowed call may overshoot by at most one call's bill, since a
    call's cost cannot be predicted reliably beforehand. `can_jump_to` is what connects the jump to
    an edge.

    Args:
        state: The graph state, carrying `billed_tokens` as this turn's calls have folded it.
        runtime: LangGraph's runtime, unused — the budget is a deployment setting rather than a
        per-run one.

    Returns:
        `{"jump_to": "end", "spend_capped": True}` when the turn is over budget, else `None`.
    """
    budget = settings.agent_max_turn_billed_tokens
    if not budget:
        return None
    # The larger of two readings, because each sees calls the other cannot. `billed_tokens` sees
    # what this middleware metered; a model call made inside a tool body (e.g. `agent/condense.py`)
    # is not a graph node and never reaches it, but it does reach the turn's own ledger
    # (`agent/turn_usage.metered_turn_tokens`). Off the request path the ledger is 0 and the channel
    # is the only reading.
    billed = max(int(state.get("billed_tokens", 0)), metered_turn_tokens())
    if billed < budget:
        return None
    logger.warning("the turn hit its %d billed-token cap after %d tokens", budget, billed)
    record_spend_cap(billed)
    return {"jump_to": "end", "spend_capped": True}


def spend_capped(state: Mapping[str, Any]) -> bool:
    """Whether this turn was stopped by its spend cap — read, not inferred.

    Args:
        state: The state the finished run **returned**. Not `graph.get_state(config).values`: the
        channel is untracked, so it is absent from a restored checkpoint and would read `False`.

    Returns:
        Whether the run reached its billed-token budget.
    """
    return bool(state.get("spend_capped", False))


class MeterTurnSpend(AgentMiddleware[Any, Any, Any]):
    """Add each model call's bill to the turn's running total, so `before_model` can read it.

    The bill exists only on the response, so the write is a state update returned from
    `wrap_model_call` (via `ExtendedModelResponse`'s `Command`), applied through `TurnTotal`'s
    additive fold so fan-out branches sum.

    Both sync and async hooks, because `create_agent` puts the middleware in both chains and an
    async-only one would fail `graph.invoke()`.

    Never fails a turn: a response with no usage meters 0, so the cap cannot bind on a provider that
    reports nothing; `turn_usage.graph_usage_tokens` counts unreadable usage separately.
    """

    #: Declared so `billed_tokens` exists on a graph compiled around this middleware alone.
    #: `build_langgraph_agent` already passes `state_schema=ChemclawState`, so this is a safeguard
    #: for other graphs.
    state_schema = ChemclawState

    def _update(self, request: ModelRequest[Any], response: Any) -> Any:
        """The response, plus a command carrying this turn's new absolute billed total.

        Absolute rather than a delta, because `TurnTotal` folds `base + max(value - base, 0)`. The
        ambient watch accumulates instead, so it gets this call's own bill. Guarded end to end:
        metering is an observation and must never end a turn.
        """
        try:
            message = response.result[0] if getattr(response, "result", None) else response
            billed = int(graph_usage_tokens(message).total)
            if billed <= 0:
                return response
            prior = cast(int, request.state.get("billed_tokens", 0) or 0)
            total = int(prior) + billed
            # The channel gets the absolute total and the watch gets this call's own bill, each what
            # its fold is defined against (see `TurnTotal` and `_meter`).
            _meter(billed)
            return ExtendedModelResponse(
                model_response=response, command=Command(update={"billed_tokens": total})
            )
        except Exception:
            degraded(logger, "spend_cap", "could not meter this model call's bill")
            return response

    def wrap_model_call(
        self, request: ModelRequest[Any], handler: Callable[[ModelRequest[Any]], Any]
    ) -> Any:
        """Run the call, then book what it billed (sync path)."""
        return self._update(request, handler(request))

    async def awrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], Awaitable[Any]],
    ) -> Any:
        """The path a turn actually takes."""
        return self._update(request, await handler(request))
