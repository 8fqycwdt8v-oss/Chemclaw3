"""Make the model loop's runaway cap observable, so a capped turn stops looking finished.

`enforce_loop_cap` is a `before_model` hook over `ChemclawState.model_calls`: it counts each
model call, and at `harness_max_loop_iterations` it ends the graph and marks the turn, so the
number that enforces the limit is the number that records it. The channels are untracked
(`TurnTotal`, `TurnFlag`), so the count is per run with nothing to reset; a resumed turn is
floored by the thread itself (`calls_already_made`), and a fan-out by a shared per-turn watch.

A graph that reaches the cap gets exactly one further call with tools switched off and a note
asking for the answer from what it has (`AnswerAtTheCap`); only a second arrival ends it. The
last iteration of a capped loop is one that wanted a tool, so stopping there would leave
narration or nothing. The cost is one call per graph run that reaches the cap.

Two readers: `loop_capped(state)` reads the flag off the state a finished run returns (tests,
template steps); `loop_hit_cap()` reads a task-local mutable record the hook marks, for
`chemclaw.api.runner`, which never gets the final state back.
"""

import logging
from collections.abc import Awaitable, Callable, Mapping
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from langchain.agents.middleware import AgentMiddleware, ModelRequest, ModelResponse, before_model
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage

from chemclaw.agent.compaction import request_note
from chemclaw.core.config import settings
from chemclaw.core.model_prose import ModelProse

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class _LoopWatch:
    """One turn's cap mark and its live call count, shared by every branch of the turn.

    `capped` is set once the loop was stopped by its cap. `calls` is what the channel cannot be: a
    number every concurrent branch reads and advances. `SubAgentMiddleware` hands every helper in a
    `task` batch the same pre-superstep `model_calls`, so without a shared count each of `W`
    branches
    would spend the whole remaining allowance. The contextvar is copied into each branch's task but
    the object is shared, as with `spend_cap`'s `TurnUsage`. Off the request path there is no watch
    and the cap falls back to the channel and the thread.
    """

    capped: bool = False
    calls: int = 0


_watch: ContextVar[_LoopWatch | None] = ContextVar("chemclaw_loop_watch", default=None)


def begin_loop_watch() -> object:
    """Start watching this turn's loop decisions; returns a token for `end_loop_watch`."""
    return _watch.set(_LoopWatch())


def end_loop_watch(token: object) -> None:
    """Tear the turn's watch down (mirrors every other ambient's reset)."""
    _watch.reset(token)  # type: ignore[arg-type]


def loop_hit_cap() -> bool:
    """Whether this turn's model loop was stopped by its iteration cap.

    `False` off the request path (no watch). The state-side answer is `loop_capped`.
    """
    watch = _watch.get()
    return watch is not None and watch.capped


# `can_jump_to` builds the conditional edge: without it the hook's `jump_to: "end"` is read by
# nothing and the loop continues. Only a compiled graph test proves this.
@before_model(can_jump_to=["end"])
def enforce_loop_cap(state: Mapping[str, Any], runtime: Any) -> dict[str, Any] | None:
    """Authorise this turn's next model call, or end the run because the cap is reached.

    The increment counts authorisations, not completions: `before_model` is the one hook no later
    middleware can skip, so a later jumping hook (`spend_cap.enforce_spend_cap`) can leave an
    increment for a call never made.

    First-party rather than `ModelCallLimitMiddleware`, which counts in `after_model` (skippable by
    any `after_model` hook that jumps), fabricates an assistant message on `exit_behavior="end"`,
    keeps a private counter that `SubAgentMiddleware` strips (so helpers would each get a full
    budget),
    and checkpoints a counter nobody reads. `ModelCallLimitMiddleware` is unsafe to compose with any
    middleware that jumps from `after_model`. Revisit when upstream moves the increment to
    `before_model` and offers a non-message exit.

    The run ends rather than raising, after one tool-less call whose answer goes out and is marked
    partial by `chemclaw.api.runner`.
    """
    # The count has three floors. The channel is untracked, so a resumed turn reads 0; the thread
    # (this turn's assistant messages) is the floor for that. On an ordinary turn the two are equal
    # at
    # the comparison. The turn-wide watch count is the only floor a sibling branch can move, so a
    # fan-out of width `W` does not spend `W` allowances. (The billed-token budget has no thread
    # floor
    # and still resets on resume.)
    watch = _watch.get()
    # Two numbers: `own` (this branch's count) is what the channel advances to, so `TurnTotal` folds
    # one advance per real call; `turn` is what the cap compares against. Writing `turn + 1` would
    # over-count a fan-out.
    own = max(int(state.get("model_calls", 0)), calls_already_made(state.get("messages")))
    turn = max(own, watch.calls if watch is not None else 0)
    calls = turn
    if calls >= settings.harness_max_loop_iterations:
        record_loop_cap()
        # `loop_capped` is written only here: a capped turn and one that used its last call and
        # finished
        # both reach exactly `cap`, so only a flag set by the branch that fires distinguishes them.
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


# What a graph at the cap is told on its one tool-less call. Added to the request only, never
# stored. Worded to be obeyed rather than answered, so the reply does not open with an
# acknowledgement.
WRAP_UP_NOTE = ModelProse(
    "[System note, not from the person you are working for — do not reply to it or mention it.] "
    "This turn has reached its step limit, so no further tools can run. Write your final response "
    "now, from what you have already found: lead with what the evidence gathered so far supports, "
    "with its citations, then say plainly that the work was cut short at the step limit and name "
    "what was left unexamined, so it is not read as absent. Do not describe what you would check "
    "next as if you were about to do it."
)


class AnswerAtTheCap(AgentMiddleware[Any, Any, Any]):
    """Turn the call `enforce_loop_cap` authorises past the cap into an answer, not another step.

    On the request and response only, never the thread:

    - **`tool_choice="none"`, with the tools still bound**: a thread with `tool_use` blocks must
      declare tools, so unbinding them is a 400.
    - **`WRAP_UP_NOTE` appended** to the request's messages.
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
            # it as the
            # newest turn and cut the chemist's question.
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
                "the wrap-up call at the loop cap asked for tools despite tool_choice='none'; "
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
    watch = _watch.get()
    if watch is not None:
        # Mutated rather than rebound, for the reason the module docstring gives: the runner must
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
    at
    the comparison, so over-counting by one would cap early. The call in flight at a pod death left
    no message, so a resume may get one extra call.

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
