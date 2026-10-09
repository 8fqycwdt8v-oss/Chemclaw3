"""The conversation graph's typed state, and the one function that starts a turn in it.

Extends `PlanningState`, so `todos` (owned by `TodoListMiddleware`) is the one plan. A launched job
is a `job_records` row and a `session_events` push-back, never a todo, so the plan gate needs no
filter to ignore job bookkeeping. A field is declared only once something reads it.

**Per-turn versus per-thread is a property of the channel.** The checkpointer persists state under
`thread_id` (the session id), so a plain field is checkpointed and per-thread. The runaway guards'
fields are `UntrackedValue` channels, which are never checkpointed and so start empty on every run;
otherwise a count would accumulate across turns and brick the session. This shape follows upstream's
`ModelCallLimitMiddleware` counter, but the counting itself is first-party
(`D-2026-08-15-an-after-model-counter-is-a-counter-that-can-be-skipped`).

The counters cross the subagent boundary on purpose, so a superstep with several `task` calls
delivers several values; bare `UntrackedValue` would raise `InvalidUpdateError`. `TurnTotal` and
`TurnFlag` define what a concurrent write means instead (see their docstrings for why `guard=False`
is wrong).
"""

from collections.abc import Sequence
from typing import Annotated, Any, NotRequired

from langchain.agents.middleware.todo import PlanningState
from langchain.agents.middleware.types import PrivateStateAttr
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.channels.last_value import LastValue
from langgraph.channels.untracked_value import UntrackedValue

from chemclaw.core.config import settings

#: The attribute `agent/turn_graph.py` stamps on a compiled mesh, naming how many namespace frames a
#: turn's own agent sits behind; `api/graph_stream.root_depth` reads it. Defined here because
#: `agent` may not import `api`, and both already import this module.
PEER_DEPTH_ATTR = "chemclaw_peer_depth"


class TurnTotal(UntrackedValue[int]):
    """An untracked counter that **folds** a superstep's writes instead of refusing them.

    `SubAgentMiddleware`'s `task` returns each helper's whole final state, and `model_calls`
    deliberately crosses that boundary so one budget spans the team; two `task` calls in one message
    therefore deliver two values in one superstep (`tests/test_subagents.py`). `guard=False` would
    keep only the last branch's count and under-count the shared budget.

    The fold is additive over each writer's advance: every branch starts from the same base, so
    `value - base` is what that branch spent. With one writer it equals `UntrackedValue`. A negative
    advance contributes 0, so no write can walk the count back.
    """

    def update(self, values: Sequence[int]) -> bool:
        """Store the base plus every writer's advance on it."""
        if not values:
            return False
        # `is_available()` rather than a comparison against `MISSING`: that sentinel lives in
        # langgraph's `_internal` package, and this is the public question with the same answer.
        base = int(self.value) if self.is_available() else 0
        self.value = base + sum(max(int(value) - base, 0) for value in values)
        return True


class TurnFlag(UntrackedValue[bool]):
    """An untracked flag that stays set once any writer in the turn has set it.

    `loop_capped` crosses the subagent boundary beside `model_calls`, and last-writer-wins could let
    an uncapped helper's `False` hide a capped one's `True`. So the fold is `or`, including the
    stored value: a cap that fired stays a fact about the turn.
    """

    def update(self, values: Sequence[bool]) -> bool:
        """Set the flag if anything in this superstep set it, and never clear it."""
        if not values:
            return False
        self.value = any(values) or (self.is_available() and bool(self.value))
        return True


class LastPeer(LastValue[str]):
    """A checkpointed name that takes the **first** writer in a superstep instead of refusing.

    A defensive fallback: today ToolNode applies only the first `Command(graph=PARENT)` and
    `handoff.refuse_a_later_handoff` refuses later handoffs in the same message
    (`tests/test_turn_graph.py::test_two_handoffs_in_one_message_hand_over_once`). If that changes
    upstream, `LastValue` would raise `InvalidUpdateError` and kill the turn; taking the first
    agrees with the refusal about which peer wins. Checkpointed, unlike the other channels here (see
    `active_agent`).
    """

    def update(self, values: Sequence[str]) -> bool:
        """Store the first name written this superstep, keeping what is there when none is."""
        if not values:
            return False
        self.value = values[0]
        return True


class ChemclawState(PlanningState):
    """The graph state Chemclaw adds on top of the plan the todo middleware maintains.

    Fields arrive with the phase that reads one — a declared field nothing consults is the same
    stub as a function nothing calls, and reads as coverage while proving nothing.

    **The field does not carry `PrivateStateAttr`**, and that is deliberate rather than an omission
    from the upstream declaration it otherwise copies. `PrivateStateAttr` is
    `OmitFromSchema(input=True, output=True)`, so it would strip the field from what `ainvoke`
    *returns* — and once the value is out of the checkpoint, the return is the only place left to
    read it. `loop_cap.loop_capped(state)` is that reader: it takes "the turn's final graph state",
    which callers get from `ainvoke`, and hiding the field from the output would leave it with
    nothing to read — a capped turn unreportable again, which is the defect `agent/loop_cap.py`
    exists to fix. Upstream's own `run_model_call_count` does carry `PrivateStateAttr` and is
    therefore unreadable by the time anyone asks, which is why neither field below delegates to it.
    """

    # How many model calls this turn has *authorised* — the runaway guard's counter
    # (`agent/loop_cap.py`). The increment is written in `before_model`, so a later `before_model`
    # hook that ends the run (`spend_cap.enforce_spend_cap`) leaves one increment for a call never
    # made; the cap can bind one call early, never late (pinned by `tests/test_spend_cap.py`).
    #
    # Counted in `before_model` because an `after_model` count can be skipped by any middleware
    # jumping from `after_model`. Untracked, so a new run on the same thread starts at 0. Not
    # private, so one budget spans a team turn (`SubAgentMiddleware` strips private keys), which is
    # what makes `TurnTotal` necessary.
    model_calls: NotRequired[Annotated[int, TurnTotal(int)]]

    # Whether the runaway guard stopped this turn — the fact beside the count, written by
    # `loop_cap.enforce_loop_cap` on the branch that fires, so the two cannot disagree. Untracked,
    # so a capped turn does not mark every later turn partial; the value lives only in what the run
    # returns.
    loop_capped: NotRequired[Annotated[bool, TurnFlag(bool)]]

    # What this turn has billed so far across every model call — the spend guard's counter
    # (`agent/spend_cap.py`). Untracked (the turn's, not the session's), not private (one budget
    # across delegation), and a `TurnTotal` (fan-out writes fold additively). Written from
    # `wrap_model_call` as an absolute total, which is what `TurnTotal`'s fold is defined against.
    billed_tokens: NotRequired[Annotated[int, TurnTotal(int)]]

    # Whether the spend guard stopped this turn, written on the stopping branch. The count alone
    # cannot answer it: the stopping branch bills nothing, so a capped turn and one that finished at
    # its last allowed token end at the same number.
    spend_capped: NotRequired[Annotated[bool, TurnFlag(bool)]]

    # Whether *this graph* has spent its one tool-less call at the loop cap — the per-branch half of
    # the cap, where `loop_capped` is per-turn. Private, so `SubAgentMiddleware` strips it in both
    # directions and a caller never reads its helper's cap as its own and ends without answering. A
    # `TurnFlag` only so a second writer would fold rather than raise.
    loop_wrap_up: NotRequired[Annotated[bool, TurnFlag(bool), PrivateStateAttr]]

    # Which peer agent holds the conversation — the one deliberately **per-thread** field here, and
    # the reason `agent/turn_graph.py` needs a state channel. A follow-up question goes to whoever
    # the chemist was handed to, which is what distinguishes a handoff from re-routing every turn.
    #
    # It carries no authority: it only names a node the turn graph compiled
    # (`turn_graph.entry_peer_or_root` falls back to the root for an unknown name), and every peer's
    # surface is already intersected with the root's. `LastPeer` rather than a plain field, as a
    # fallback (see its docstring).
    active_agent: NotRequired[Annotated[str, LastPeer(str)]]

    # How many times this turn has handed between agents — the bound on a chain. Per-turn: a
    # conversation moving between agents over many turns is working; many hops in one turn is a
    # loop. A `TurnTotal`, so the handoff tool writes the running total rather than `1` (a constant
    # contributes 0 after the first hop). Untracked, so earlier turns' handoffs do not count against
    # this one.
    handoffs: NotRequired[Annotated[int, TurnTotal(int)]]


def turn_input(message: str, message_id: str | None = None) -> dict[str, Any]:
    """The graph input that starts one turn: the user's message.

    Per-turn reset comes from the untracked channels, not from here. Kept as a function so the
    turn's input shape (the `("user", message)` tuple the graph coerces) is written once for its
    callers.

    Args:
        message: The user's message for this turn.
        message_id: The id to give the message in the thread, so the turn's start can be found
            again (`agent/turn_resume.py`); the graph mints one when omitted.

    Returns:
        The mapping to pass to `ainvoke`/`astream`.
    """
    if message_id is None:
        return {"messages": [("user", message)]}
    return {"messages": [HumanMessage(content=message, id=message_id)]}


def turn_config(thread_id: str | None = None) -> dict[str, Any]:
    """The invocation config one turn runs under: its thread, step ceiling, and fan-out bound.

    Upstream bakes `recursion_limit=9999`, and hitting a recursion limit raises
    `GraphRecursionError`, discarding the turn's work. The loop cap is the graceful stop; this
    ceiling is the backstop under it, sized so the cap always fires first. One function so the
    number is chosen once.

    Args:
        thread_id: The checkpointed session to continue, or `None` for a graph built without a
        checkpointer (a template step, one bounded turn with no thread).

    Returns:
        The config to pass to `ainvoke`/`astream`.
    """
    config: dict[str, Any] = {"recursion_limit": settings.agent_recursion_limit}
    # `ToolNode` gathers a parallel batch with no limit of its own, so `max_concurrency` bounds how
    # many tool calls (and pool connections) run at once. 0 means unbounded, expressed by omitting
    # the key (upstream's default).
    if settings.agent_max_parallel_tool_calls:
        config["max_concurrency"] = settings.agent_max_parallel_tool_calls
    if thread_id is not None:
        config["configurable"] = {"thread_id": thread_id}
    return config


def _text_of(message: AIMessage) -> str:
    """One assistant message's content as text, joined across blocks and coerced for any shape.

    Shared with `answer_text` so "did this message say anything" and "what did it say" use the same
    flattening.
    """
    content = message.content
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            str(part.get("text", "")) if isinstance(part, dict) else str(part) for part in content
        )
    return str(content)


def answer_text(result: Any) -> str:
    """The final assistant text out of a graph turn — the output side of `turn_input`.

    The last `AIMessage` with non-empty text, walking back no further than the turn's own user
    message. Not the last message: a turn stopped by a cap in `before_model` ends on a
    `ToolMessage`, which is never the agent's answer. Not merely the last `AIMessage`: a
    tool-calling message usually has empty content, and the prose an earlier iteration wrote must
    not be lost (upstream's `SubAgentMiddleware` reports the same way). Stopping at the user message
    keeps a previous turn's answer from being presented as this one's; with no text the answer is
    `""`.

    Shared by `cli/chat.py` and `durable/template_activities.py`. Blocks are joined and coerced with
    `str` so no content shape fails a caller.
    """
    for message in reversed(result.get("messages") or []):
        if isinstance(message, HumanMessage):
            break
        if isinstance(message, AIMessage) and (text := _text_of(message)):
            return text
    return ""
