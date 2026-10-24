"""The two per-turn brakes, and the calls that make them observable: runaway cap and spend cap.

Both are `before_model` hooks, which no later hook can skip
(`D-2026-08-15-an-after-model-counter-is-a-counter-that-can-be-skipped`), counted in state channels
(`ChemclawState.model_calls`, `billed_tokens`: `TurnTotal`s, so one budget spans subagents and a
fan-out folds additively). Neither raises, so the turn still answers, marked partial by
`chemclaw.api.runner`, which reads `loop_hit_cap()` / `spend_hit_cap()` because a streaming driver
never gets the final state back (`loop_capped(state)` / `spend_capped(state)` read it where it
does). Each grants its graph one tool-less wrap-up call (`AnswerAtTheCap`, through the one
`loop_wrap_up` flag) and ends the turn on the second arrival: a cap that ends a turn with no answer
has spent the budget for nothing, and the size of one call depends on how many connectors a
deployment binds, so no number makes the budget safe to end on.
`MeterTurnSpend` is the only hook that sees a response's bill. Limits come from `SpendPolicy`.
"""

import logging
from collections.abc import Awaitable, Callable, Mapping
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, cast

from langchain.agents.middleware import AgentMiddleware, ModelRequest, ModelResponse, before_model
from langchain.agents.middleware.types import ExtendedModelResponse
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langgraph.types import Command

from chemclaw.agent.compaction import request_note
from chemclaw.agent.state import ChemclawState
from chemclaw.core.metrics_bridge import degraded
from chemclaw.core.model_prose import ModelProse
from chemclaw.spend.ledger import graph_usage_tokens, metered_turn_tokens
from chemclaw.spend.limits import SpendPolicy

logger = logging.getLogger(__name__)

# --- the runaway cap: model calls per turn ------------------------------------------------------


@dataclass(slots=True)
class _LoopWatch:
    """One turn's cap mark and its live call count, shared by every branch of the turn.

    `capped` is set once the loop was stopped by its cap. `calls` is what the channel cannot be: a
    number every concurrent branch reads and advances. `SubAgentMiddleware` hands every helper in a
    `task` batch the same pre-superstep `model_calls`, so without a shared count each of `W`
    branches would spend the whole remaining allowance. The contextvar is copied into each branch's
    task but the object is shared, as with `ledger.TurnUsage`. Off the request path there is
    no watch and the cap falls back to the channel and the thread.
    """

    capped: bool = False
    calls: int = 0


_loop_watch: ContextVar[_LoopWatch | None] = ContextVar("chemclaw_loop_watch", default=None)


def begin_loop_watch() -> object:
    """Start watching this turn's loop decisions; returns a token for `end_loop_watch`."""
    return _loop_watch.set(_LoopWatch())


def end_loop_watch(token: object) -> None:
    """Tear the turn's watch down (mirrors every other ambient's reset)."""
    _loop_watch.reset(token)  # type: ignore[arg-type]


def loop_hit_cap() -> bool:
    """Whether this turn's model loop was stopped by its iteration cap.

    `False` off the request path (no watch). The state-side answer is `loop_capped`.
    """
    watch = _loop_watch.get()
    return watch is not None and watch.capped


# `can_jump_to` builds the conditional edge: without it the hook's `jump_to: "end"` is read by
# nothing and the loop continues. Only a compiled graph test proves this.
@before_model(can_jump_to=["end"])
def enforce_loop_cap(state: Mapping[str, Any], runtime: Any) -> dict[str, Any] | None:
    """Authorise this turn's next model call, or end the run because the cap is reached.

    The increment counts authorisations, not completions: `before_model` is the one hook no later
    middleware can skip, so a later jumping hook (`enforce_spend_cap`) can leave an
    increment for a call never made.

    First-party rather than `ModelCallLimitMiddleware`, which counts in `after_model` (skippable by
    any `after_model` hook that jumps), fabricates an assistant message on `exit_behavior="end"`,
    keeps a private counter that `SubAgentMiddleware` strips (so helpers would each get a full
    budget), and checkpoints a counter nobody reads. `ModelCallLimitMiddleware` is unsafe to compose
    with any middleware that jumps from `after_model`. Revisit when upstream moves the increment to
    `before_model` and offers a non-message exit.

    The run ends rather than raising, after one tool-less call whose answer goes out and is marked
    partial by `chemclaw.api.runner`. A wrap-up the spend cap already granted counts as this graph's
    one: past the cap too, the next arrival ends the turn.
    """
    # The count has three floors. The channel is untracked, so a resumed turn reads 0; the thread
    # (this turn's assistant messages) is the floor for that. On an ordinary turn the two are equal
    # at the comparison. The turn-wide watch count is the only floor a sibling branch can move, so a
    # fan-out of width `W` does not spend `W` allowances. (The billed-token budget has no thread
    # floor and still resets on resume.)
    watch = _loop_watch.get()
    # Two numbers: `own` (this branch's count) is what the channel advances to, so `TurnTotal` folds
    # one advance per real call; `turn` is what the cap compares against. Writing `turn + 1` would
    # over-count a fan-out.
    own = max(int(state.get("model_calls", 0)), calls_already_made(state.get("messages")))
    turn = max(own, watch.calls if watch is not None else 0)
    calls = turn
    if calls >= SpendPolicy.current().model_calls_per_turn:
        record_loop_cap()
        # `loop_capped` is written only here: a capped turn and one that used its last call and
        # finished both reach exactly `cap`, so only a flag set by the branch that fires
        # distinguishes them.
        if state.get("loop_wrap_up"):
            # The second arrival: the tool-less call already happened, so nothing further is
            # authorised.
            logger.warning("the model loop hit its %d-iteration cap after its wrap-up", calls)
            return {"jump_to": "end", "loop_capped": True}
        logger.warning(
            "the model loop hit its %d-iteration cap; one last call, without tools, writes the "
            "answer from what it has",
            calls,
        )
        # Counted like any authorised call — in the channel and on the watch — so the number that
        # records the turn is still the number of calls it made, one past the cap per graph.
        if watch is not None:
            watch.calls = turn + 1
        return {"loop_capped": True, "loop_wrap_up": True, "model_calls": own + 1}
    # Advanced in the hook that authorises the call, mutated so every branch sees it, and with `max`
    # so two branches reading the same base do not each add one.
    if watch is not None:
        watch.calls = turn + 1
    return {"model_calls": own + 1}


#: What a graph at the step cap is told on its one tool-less call. Added to the request only, never
#: stored. Worded to be obeyed rather than answered, so the reply does not open with an
#: acknowledgement.
WRAP_UP_NOTE = ModelProse(
    "[System note, not from the person you are working for — do not reply to it or mention it.] "
    "This turn has reached its step limit, so no further tools can run. Write your final response "
    "now, from what you have already found: lead with what the evidence gathered so far supports, "
    "with its citations, then say plainly that the work was cut short at the step limit and name "
    "what was left unexamined, so it is not read as absent. Do not describe what you would check "
    "next as if you were about to do it."
)


class AnswerAtTheCap(AgentMiddleware[Any, Any, Any]):
    """Turn the call a cap authorises past its limit into an answer, not another step.

    `enforce_loop_cap` and `enforce_spend_cap` both authorise it, through `loop_wrap_up`.

    On the request and response only, never the thread:

    - **`tool_choice="none"`, with the tools still bound**: a thread with `tool_use` blocks must
      declare tools, so unbinding them is a 400.
    - **`WRAP_UP_NOTE` appended** to the request's messages, for either cap: the chemist is told
      the true reason by the `spend_cap_reached` event that follows the answer.
    - **Any tool call in the reply is dropped**, with a warning, so a provider ignoring
      `tool_choice` cannot buy another round and the thread stays legal; the text is kept exactly.

    Both sync and async hooks, because `create_agent` puts the middleware in both chains.
    """

    @staticmethod
    def _request(request: ModelRequest[Any]) -> ModelRequest[Any]:
        """The wrap-up request, or `request` unchanged off the wrap-up."""
        if not request.state.get("loop_wrap_up"):
            return request
        return request.override(
            tool_choice="none" if request.tools else None,
            # A `request_note`, not a bare `HumanMessage`, so the conversation window does not treat
            # it as the newest turn and cut the chemist's question.
            messages=[*request.messages, request_note(WRAP_UP_NOTE)],
        )

    @staticmethod
    def _response(request: ModelRequest[Any], response: Any) -> Any:
        """The wrap-up's reply with any tool call removed, or `response` unchanged."""
        if not request.state.get("loop_wrap_up") or not isinstance(response, ModelResponse):
            return response
        kept: list[BaseMessage] = []
        for message in response.result:
            calls = isinstance(message, AIMessage) and (
                message.tool_calls
                or message.invalid_tool_calls
                or "tool_calls" in message.additional_kwargs
            )
            if not calls:
                kept.append(message)
                continue
            logger.warning(
                "the wrap-up call at a cap asked for tools despite tool_choice='none'; "
                "the calls are dropped and its text is kept"
            )
            extra = {k: v for k, v in message.additional_kwargs.items() if k != "tool_calls"}
            kept.append(
                message.model_copy(
                    update={"tool_calls": [], "invalid_tool_calls": [], "additional_kwargs": extra}
                )
            )
        return ModelResponse(result=kept, structured_response=response.structured_response)

    def wrap_model_call(
        self, request: ModelRequest[Any], handler: Callable[[ModelRequest[Any]], Any]
    ) -> Any:
        """The wrap-up on the synchronous path."""
        wrapped = self._request(request)
        return self._response(wrapped, handler(wrapped))

    async def awrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], Awaitable[Any]],
    ) -> Any:
        """The wrap-up on the path a turn actually takes."""
        wrapped = self._request(request)
        return self._response(wrapped, await handler(wrapped))


def record_loop_cap() -> None:
    """Mark this turn's watch, so the runner can see a cap it cannot read off the state.

    `chemclaw.api.runner` calls `loop_hit_cap()` to emit `loop_cap_reached` and count
    `chemclaw_turn_loop_caps_total`, since a streaming driver never gets the final state. This
    records only the fact; the count stays in `model_calls`.
    """
    watch = _loop_watch.get()
    if watch is not None:
        # Mutated rather than rebound, for the reason `_LoopWatch` gives: the runner must
        # see it even when the stream is driven from a task of its own.
        watch.capped = True


def loop_capped(state: Mapping[str, Any]) -> bool:
    """Whether this turn's model loop was stopped by its cap — **read, not inferred**.

    Reads the `loop_capped` flag the stopping branch sets; the count alone cannot tell a capped turn
    from one that finished on its last allowed call.

    Args:
        state: The state the finished run **returned**. Not `graph.get_state(config).values`:
            `loop_capped` is untracked, so it is absent from the checkpoint and reads `False` there.

    Returns:
        Whether the run reached the configured iteration cap.
    """
    return bool(state.get("loop_capped", False))


def calls_already_made(messages: Any) -> int:
    """How many model calls this turn has already made, read off the thread itself.

    The cap channels are untracked, so a resumed turn would start from zero; this re-derives the
    count from state that survives a pod death: assistant messages since the last human one (the
    turn, not the conversation). Used as a floor (`max`); on an ordinary turn it equals the channel
    at the comparison, so over-counting by one would cap early. The call in flight at a pod death
    left no message, so a resume may get one extra call.

    Args:
        messages: The thread, oldest first, as the checkpoint holds it.

    Returns:
        Assistant messages since the last human message, or over the whole list when there is none.
    """
    history = list(messages or [])
    start = 0
    for index in range(len(history) - 1, -1, -1):
        if isinstance(history[index], HumanMessage):
            start = index
            break
    return sum(1 for message in history[start:] if isinstance(message, AIMessage))


# --- the spend cap: billed tokens per turn ------------------------------------------------------


@dataclass(slots=True)
class _SpendWatch:
    """One turn's spend mark — what it billed, and whether the cap is what stopped it."""

    capped: bool = False
    billed: int = 0


_spend_watch: ContextVar[_SpendWatch | None] = ContextVar("chemclaw_spend_watch", default=None)


def begin_spend_watch() -> object:
    """Start watching this turn's spend; returns a token for `end_spend_watch`."""
    return _spend_watch.set(_SpendWatch())


def end_spend_watch(token: object) -> None:
    """Tear the turn's watch down (mirrors every other ambient's reset)."""
    _spend_watch.reset(token)  # type: ignore[arg-type]


def spend_hit_cap() -> bool:
    """Whether this turn was stopped by its spend cap.

    `False` off the request path. The state-side answer is `spend_capped`; this one is for a
    streaming driver, which is not handed the final state.
    """
    watch = _spend_watch.get()
    return watch is not None and watch.capped


def turn_billed_tokens() -> int:
    """What this turn has billed so far, or 0 off the request path.

    The runner reports it beside the refusal, so a chemist sees how much the turn spent.
    """
    watch = _spend_watch.get()
    return watch.billed if watch is not None else 0


def record_spend_cap(billed: int) -> None:
    """Mark this turn as stopped by its spend cap, having billed `billed`.

    The counterpart to `record_loop_cap`, called by `enforce_spend_cap` and by tests;
    `api/runner._spend_cap_event` reads the result. `billed` is an absolute reading, so it is folded
    with `max` against what `_meter` accumulated rather than added.

    Args:
        billed: What the turn had billed when the cap fired.
    """
    watch = _spend_watch.get()
    if watch is None:
        return
    # Mutated rather than rebound, for the reason `_LoopWatch` gives: a stream
    # driven from a task of its own must still be able to see it.
    watch.billed = max(watch.billed, billed)
    watch.capped = True


def _meter(billed: int) -> None:
    """Add one model call's own bill to this turn's running total.

    Added, not `max`ed: every branch of a fan-out starts from the same base, so the largest absolute
    total would count only one branch. This is the same fold `state.TurnTotal` performs.
    """
    watch = _spend_watch.get()
    if watch is None:
        return
    watch.billed += billed


@before_model(can_jump_to=["end"], state_schema=ChemclawState)
def enforce_spend_cap(state: Mapping[str, Any], runtime: Any) -> dict[str, Any] | None:
    """Grant one tool-less call, then end, once the turn is over its billed-token budget.

    Checked before the call, the only placement that bounds anything. The first arrival over budget
    does not end the turn: it sets `loop_wrap_up`, so `AnswerAtTheCap` makes the call it is about to
    authorise an answer, and that call may overshoot by its own bill. The second arrival ends the
    run. A turn ended with no answer spent its whole budget for nothing, and no number prevents it:
    the size of one call depends on how many connectors a deployment binds. `can_jump_to` is what
    connects the jump to an edge.

    The hook runs after `enforce_loop_cap`, so a `loop_wrap_up` it finds may be that cap's pending
    wrap-up (`spend_capped` unset): that call is the answer and the loop cap ends the turn after it.

    Args:
        state: The graph state, carrying `billed_tokens` as this turn's calls have folded it.
        runtime: LangGraph's runtime, unused — the budget is a deployment setting rather than a
        per-run one.

    Returns:
        `{"spend_capped": True, "loop_wrap_up": True}` on the first arrival over budget,
        `{"jump_to": "end", "spend_capped": True}` on the second, else `None`.
    """
    budget = SpendPolicy.current().billed_tokens_per_turn
    if not budget:
        return None
    # The larger of two readings, because each sees calls the other cannot. `billed_tokens` sees
    # what this middleware metered; a model call made inside a tool body (e.g. `agent/condense.py`)
    # is not a graph node and never reaches it, but it does reach the turn's own ledger
    # (`ledger.metered_turn_tokens`). Off the request path the ledger is 0 and the channel
    # is the only reading.
    billed = max(int(state.get("billed_tokens", 0)), metered_turn_tokens())
    if billed < budget:
        return None
    if state.get("loop_wrap_up"):
        if not state.get("spend_capped"):
            # The step cap's wrap-up, authorised a moment ago in this same pass: it is the answer.
            return None
        logger.warning("the turn hit its %d billed-token cap after its wrap-up", budget)
        return {"jump_to": "end", "spend_capped": True}
    logger.warning(
        "the turn hit its %d billed-token cap after %d tokens; one last call, without tools, "
        "writes the answer from what it has",
        budget,
        billed,
    )
    record_spend_cap(billed)
    return {"spend_capped": True, "loop_wrap_up": True}


def spend_capped(state: Mapping[str, Any]) -> bool:
    """Whether this turn was cut short by its spend cap — read, not inferred.

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
    reports nothing; `ledger.graph_usage_tokens` counts unreadable usage separately.
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
