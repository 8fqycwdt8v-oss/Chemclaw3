"""Bound what one turn may **bill**, beside the bound on how many times it may think.

`agent/loop_cap.py` caps a turn's model *calls*. That is a real guard and it is not a cost guard,
because a call is not a unit of cost: inside one 25-iteration ceiling, a prose turn bills a few
thousand tokens and a turn that fans out over large tool results against a long context bills
millions. The iteration cap cannot tell those apart, and nothing else was watching.

**It is easy to believe something was.** `api/budget.py` meters tokens per session and per user and
refuses a turn that would breach a cap — but `check()` runs *before* a turn against usage already
booked, and `record()` books a turn *after* it ended. Both halves sit outside the turn, so the one
thing neither can observe is a turn spending without a bound while it runs. That module's own
docstring states the belief that leaves the hole: "A single agent turn is already iteration-capped
(`harness_max_loop_iterations`), so one turn cannot loop forever." One turn cannot *loop* forever.
One turn can *spend* without a bound, and the session budget learns about it one turn too late —
which is exactly the "$400 in twenty minutes" failure that module was written against, arriving
through the door it left open.

So this is the same guard shape in the other unit, and it is deliberately the same shape rather
than a new one:

**Enforced in `before_model`.** The whole argument is
`D-2026-08-15-an-after-model-counter-is-a-counter-that-can-be-skipped`:
`after_model` hooks run in reverse list order, so any middleware jumping from
`after_model` short-circuits the rest of the chain — measured there at a cap of 2 letting 4 model
calls through. `before_model` runs before the model regardless of what any later hook decides.

**Counted in a state channel, not in an ambient.** The count has to cross the subagent boundary or a
turn that delegates gets one budget per branch — regression 3 in `agent/loop_cap.py`'s list of why
its own counter is first-party. `ChemclawState.billed_tokens` is a `TurnTotal`, which folds a
superstep's concurrent writes additively; an ambient contextvar would also have made the cap
inert wherever no caller had remembered to start a watch, which is the "per-turn is a property of
every call site" mistake `agent/state.py` records and moved away from.

**Metered in `wrap_model_call`, because that is the only hook that can see the bill.** The number
lives on the response of the call being wrapped, and `wrap_model_call` is where the response is.
That it can also *write* state was measured on a compiled graph before this module was written
rather than read off the documentation — `ExtendedModelResponse` carries a `Command` that LangGraph
applies through the channel's own reducer — and `tests/test_spend_cap.py` drives the whole path the
same way, because a hook returning the right dict proves nothing about whether the channel exists
(`tests/test_state_channels.py` is that lesson as a file).

**Ending the run rather than raising**, for the reason `agent/loop_cap.py` gives and this module
inherits without restating it: the answer the last iteration managed still goes out, and a surface
marks it partial. A raised error would discard work a chemist is entitled to see, and would discard
it *after* the tokens were already spent, which is the worst of both.
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
from chemclaw.agent.turn_usage import graph_usage_tokens
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

    `False` off the request path, which is what makes this safe to ask unconditionally. The
    state-side answer is `spend_capped`; this is the one a streaming driver can reach, for the
    reason `agent/loop_cap.py::loop_hit_cap` gives — a compiled graph's final state is not
    something the streaming driver is handed back.
    """
    watch = _watch.get()
    return watch is not None and watch.capped


def turn_billed_tokens() -> int:
    """What this turn has billed so far, or 0 off the request path.

    The runner reports the number beside the refusal, because "the turn stopped" and "the turn
    stopped after 1.2 million tokens" are different messages to a chemist and only the second one
    says what to do about it.
    """
    watch = _watch.get()
    return watch.billed if watch is not None else 0


def _mark(billed: int, *, capped: bool = False) -> None:
    """Record this turn's running spend on the watch, and whether the cap has now fired.

    Mutated rather than rebound, for the reason `agent/loop_cap.py` gives its own watch: a stream
    driven from a task of its own must still be able to see it.
    """
    watch = _watch.get()
    if watch is None:
        return
    watch.billed = max(watch.billed, billed)
    if capped:
        watch.capped = True


@before_model(can_jump_to=["end"], state_schema=ChemclawState)
def enforce_spend_cap(state: Mapping[str, Any], runtime: Any) -> dict[str, Any] | None:
    """End the turn before a model call that would put it past its billed-token budget.

    Checked *before* the call rather than after the one that crossed the line, which is the only
    placement that bounds anything: a turn already over its budget is one whose next call is the
    expensive one, and the request about to go out is the largest the turn has assembled. Asking
    afterwards would report the overrun and pay for it.

    So the cap is a ceiling on what a turn may spend **before** its next call, not a ceiling on
    what it ends up having spent — the last allowed call may carry it past the number, by at most
    one call's bill. Bounding the overshoot exactly would mean predicting a call's cost before
    making it, and the estimator that could is measured at 0.45x on this payload class
    (`agent/context_budget.py`). A guard that is one call loose and honest about it beats one that
    is exact against a number it made up.

    `can_jump_to` is the edge rather than decoration — see `agent/loop_cap.py`, where omitting it
    made the cap run, decide correctly, and be connected to nothing.

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
    billed = int(state.get("billed_tokens", 0))
    if billed < budget:
        return None
    logger.warning("the turn hit its %d billed-token cap after %d tokens", budget, billed)
    _mark(billed, capped=True)
    return {"jump_to": "end", "spend_capped": True}


def spend_capped(state: Mapping[str, Any]) -> bool:
    """Whether this turn was stopped by its spend cap — read, not inferred.

    Args:
        state: The state the finished run **returned**. Not `graph.get_state(config).values`:
            the channel is untracked, so it is deliberately absent from a restored checkpoint and
            asking there gets a silent `False`.

    Returns:
        Whether the run reached its billed-token budget.
    """
    return bool(state.get("spend_capped", False))


class MeterTurnSpend(AgentMiddleware[Any, Any, Any]):
    """Add each model call's bill to the turn's running total, so `before_model` can read it.

    **The write is a state update returned from `wrap_model_call`**, which is not the obvious shape
    and is the only correct one here. The bill exists on the response, so `before_model` cannot
    read it and `after_model` can be skipped by any middleware that jumps from there. LangChain's
    `ExtendedModelResponse` carries a `Command` alongside the response that LangGraph applies
    through the channel's own reducer — so `TurnTotal`'s additive fold does the accumulating, and a
    fan-out's branches sum instead of overwriting one another.

    **Both hooks, because `create_agent` puts a middleware declaring either into both chains** — an
    async-only middleware fails every synchronous `graph.invoke()`, which is what
    `tests/test_spend_cap.py` drives. `RecordContextCompaction` carries the same pair for the same
    reason, and states it at length.

    **Never fails a turn.** A response shape carrying no usage meters 0, exactly as
    `graph_usage_tokens` does everywhere else: a provider that reports nothing must not fail a
    turn. The cost of that is a cap that cannot bind on such a provider, which is the honest
    failure — `turn_usage.graph_usage_tokens` counts an unreadable usage block separately so the
    difference between "reported nothing" and "we could not read it" stays visible.
    """

    #: Declared so LangGraph creates `billed_tokens` on any graph this middleware is attached to.
    #: Without it the update below is dropped in silence — the defect `tests/test_state_channels.py`
    #: exists to catch, and the one the first probe of this design walked straight into.
    state_schema = ChemclawState

    def _update(self, request: ModelRequest[Any], response: Any) -> Any:
        """The response, plus a command carrying this turn's new absolute billed total.

        Absolute rather than a delta, because that is what `TurnTotal`'s fold is defined against:
        it stores `base + max(value - base, 0)`, so a delta would read as a walk backwards and
        contribute nothing.

        Guarded end to end. Metering is an observation, and an observation that ended a turn would
        invert this module's entire purpose — the guard exists to stop a turn *cheaply*, not to be
        one more thing that can lose one.
        """
        try:
            message = response.result[0] if getattr(response, "result", None) else response
            billed = int(graph_usage_tokens(message).total)
            if billed <= 0:
                return response
            prior = cast(int, request.state.get("billed_tokens", 0) or 0)
            total = int(prior) + billed
            _mark(total)
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
