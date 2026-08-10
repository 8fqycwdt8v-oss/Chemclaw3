"""Turning a compiled graph's stream into the turn event contract (M8, D-2026-08-10).

`api/events.py` is the conformance boundary of this migration: a LangGraph turn either emits the
same events a MAF turn does, or the rebuild is not done. This module is the half that makes that
true — the LangGraph twin of the `async for update in stream:` loop in `chemclaw.api.runner`.

**Why a translator and not a MAF-shaped adapter.** The tempting move is to make the graph yield
objects with `.text` and `.contents` so the runner's existing loop consumes it unchanged. That
would be faking one framework's private update shape with another's, and the shape is not stable
enough to be worth impersonating — `runner_trace` says so in its own docstring, and it is why that
module duck-types rather than importing MAF's content classes. Emitting the *contract* directly is
both simpler and the thing that is actually pinned by tests.

**What each stream mode is for**, with `stream_mode=["messages", "updates", "custom"]` and
`subgraphs=True`, which yields `(namespace, mode, payload)` three-tuples (measured against
`langgraph.pregel.main._output`; note the mode list must be a `list` — a tuple falls through to a
different branch and yields two-tuples instead):

- `messages` carries `(chunk, metadata)` per token, which is `TokenEvent`, and it is the only mode
  that arrives *while* the model is producing rather than after the node finishes. Tool calls are
  deliberately **not** read from here even though the chunks carry `tool_call_chunks`: that is the
  streamed, fragmented shape whose reassembly cost MAF two live-run defects (D-138, and the
  OpenAI-Responses case that announced ten `tool_call` events for one call).
- `updates` carries `{node: state_update}` once a node completes, so a tool call arrives *whole*.
  That is where calls, results and the todo list are read.
- `custom` carries what a *node* chose to publish about itself. Today that is the evidence
  fan-out's per-branch report (`chemclaw.retrieval.fanout`), which reaches here from inside a tool
  call because a branch's writer surfaces under the `tools:<id>` namespace. Chemclaw's other
  out-of-band signals still travel by contextvar (`core/turn_signals.py`), which both engines
  drain; M13 is where that becomes a stream write too.

**The ordering rule is the runner's, reproduced rather than re-derived**: a signal is drained
before the content of the update it arrived with, because a tool that ran while the model was
producing that update ran *before* the text it then produced. That is the truthful transcript
order (RCH-4/RCH-5) and the two engines must not disagree about it.
"""

import logging
from collections.abc import AsyncIterator
from typing import Any

from langchain_core.messages import AIMessageChunk

from chemclaw.api.events import Event, EvidenceSourceEvent, PlanEvent, TokenEvent
from chemclaw.api.runner_trace import ToolCallTrace
from chemclaw.api.runner_usage import graph_usage_tokens
from chemclaw.core.turn_signals import Signal, drain

logger = logging.getLogger(__name__)

# The three modes, as a list. `astream` tests `isinstance(stream_mode, list)` literally, so a tuple
# here silently changes the yielded tuple's arity — a bug that would look like a stream shape
# mismatch rather than a type mistake.
_MODES = ["messages", "updates", "custom"]


async def graph_events(
    graph: Any,
    message: str,
    *,
    config: dict[str, Any],
    trace: ToolCallTrace,
    on_signal: Any,
    usage: Any,
) -> AsyncIterator[Event]:
    """Drive one turn on a compiled graph, yielding the turn's events in order.

    Args:
        graph: The compiled graph for this turn (`agent.langgraph_agent.build_langgraph_agent`),
            already holding this turn's connector tools — it is compiled per turn precisely so it
            can.
        message: The chemist's message.
        config: The invocation config, carrying `configurable.thread_id` so a checkpointed session
            continues rather than restarting.
        trace: The turn's `ToolCallTrace`. Shared with the runner rather than created here because
            the answer gate reads `outputs` and `called_tools` off it after the stream ends, and a
            second trace would grade the answer against an empty turn.
        on_signal: Called with each drained `Signal` before its event is yielded, so the runner can
            keep its own ledger (the job ids a mid-turn resume waits on) without this module
            knowing what a session is.
        usage: The turn's token ledger; fed from each message chunk's `usage_metadata`.

    Yields:
        `Event`s in the same order and with the same meanings the MAF loop yields them.
    """
    todos: list[str] = []
    async for namespace, mode, payload in graph.astream(
        {"messages": [("user", message)]}, config, stream_mode=_MODES, subgraphs=True
    ):
        for signal in drain():
            on_signal(signal)
            yield _signal_event(signal)
        if mode == "messages":
            chunk, _metadata = payload
            usage.add(graph_usage_tokens(chunk))
            text = _text_of(chunk)
            if text:
                yield TokenEvent(text=text)
        elif mode == "custom":
            event = _custom_event(payload)
            if event is not None:
                yield event
        elif mode == "updates":
            async for event in _from_update(payload, namespace, trace, todos):
                yield event
    # A signal recorded while producing the *final* update has no next iteration to carry it, so
    # drain once more — otherwise the last job started or note proposed in a turn is dropped. The
    # MAF loop does exactly this, and for exactly this reason.
    for signal in drain():
        on_signal(signal)
        yield _signal_event(signal)


def _custom_event(payload: Any) -> Event | None:
    """One node's self-report as its event, or `None` for a payload nothing renders.

    Matched on shape rather than on a type tag, because a writer payload is whatever the node
    passed and there is no schema between them. Unknown payloads are dropped rather than guessed
    at: a node that publishes something no surface understands is a node ahead of its consumers,
    which is a normal state during a migration and not an error.
    """
    if not isinstance(payload, dict):
        return None
    if "evidence_source" in payload:
        return EvidenceSourceEvent(
            source=str(payload["evidence_source"]), chunks=int(payload.get("chunks", 0))
        )
    return None


async def _from_update(
    payload: Any, namespace: tuple[str, ...], trace: ToolCallTrace, todos: list[str]
) -> AsyncIterator[Event]:
    """The events one completed node produces: its calls, its results, and any new plan.

    `namespace` is the path of node names down to the subgraph that produced this update — `()` at
    the root. It becomes the `agent` attribution on every event a specialist raises (M9), which is
    what stops a team's trace from reading as though one actor did everything.
    """
    agent = _agent_of(namespace)
    for node, update in (payload or {}).items():
        if not isinstance(update, dict):
            continue
        for message in update.get("messages") or []:
            for call in getattr(message, "tool_calls", None) or []:
                yield _attributed(
                    trace.issued(
                        str(call.get("id") or ""), str(call.get("name") or ""), _args(call)
                    ),
                    agent,
                )
            if message.__class__.__name__ == "ToolMessage":
                yield _attributed(
                    await trace.returned(
                        str(getattr(message, "tool_call_id", "")), _content(message)
                    ),
                    agent,
                )
        plan = _todo_titles(update)
        if plan is not None and plan != todos:
            # Only on change and never empty, matching `runner._PlanEmitter`: an unchanged plan
            # re-sent every node would drown the trace, and an empty one is the harness clearing
            # its list rather than a plan worth rendering.
            todos[:] = plan
            if plan:
                yield PlanEvent(todos=plan)
        logger.debug("graph node %r produced %d event source(s)", node, len(update))


def _agent_of(namespace: tuple[str, ...]) -> str:
    """Which agent produced an event, from the subgraph namespace it arrived under.

    The root graph is Chemclaw itself and carries no attribution — an event with an empty `agent`
    means "the agent you are talking to", which is what every event meant before teams existed and
    is what keeps this field additive for an existing consumer. A subgraph's namespace entries are
    `"<node>:<task-id>"`, so the node name is the part before the colon.
    """
    if not namespace:
        return ""
    return namespace[-1].split(":", 1)[0]


def _attributed(event: Event, agent: str) -> Event:
    """Stamp an event with the specialist that raised it, when one did."""
    if not agent or not hasattr(event, "agent"):
        return event
    return event.model_copy(update={"agent": agent})


def _args(call: Any) -> str:
    """A tool call's arguments as text, for the preview `ToolCallEvent` carries."""
    import json

    try:
        return json.dumps(call.get("args") or {})
    except (TypeError, ValueError):
        return str(call.get("args") or {})


def _content(message: Any) -> str:
    """A message's content as text, however the provider shaped it.

    A `ToolMessage`'s content is a string for every tool Chemclaw registers, but LangChain permits
    a list of content blocks and a provider may send one — so the list case is joined rather than
    `str()`-ed, which would put a Python repr in front of a chemist.
    """
    content = getattr(message, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            part.get("text", "") if isinstance(part, dict) else str(part) for part in content
        )
    return str(content)


def _text_of(chunk: Any) -> str:
    """The prose in one streamed chunk, and nothing else.

    An `AIMessageChunk` carries tool-call fragments in the same `content` on some providers, so
    the text is taken from `.text` when the chunk offers it — that accessor already excludes
    non-text blocks, which a `str(content)` would fold in and stream to the chemist as JSON.
    """
    if not isinstance(chunk, AIMessageChunk):
        return ""
    return str(chunk.text or "")


def _todo_titles(update: dict[str, Any]) -> list[str] | None:
    """The plan a node's state update carries, or `None` when it carries none.

    `TodoListMiddleware` keeps `{content, status}` items, so the rendered line is the content —
    the same text `agent/harness_todo.py` renders for the MAF path, so a surface showing a plan
    cannot tell which engine produced it.
    """
    todos = update.get("todos")
    if todos is None:
        return None
    return [str(todo.get("content", "")) for todo in todos if isinstance(todo, dict)]


def _signal_event(signal: Signal) -> Event:
    """One out-of-band signal as its event — deferred to the runner's single map.

    Imported at call time rather than at module load because `chemclaw.api.runner` imports this
    module: the map belongs beside the MAF loop that has always owned it, and duplicating it here
    is precisely the drift `runner._signal_event` exists to prevent ("so the two cannot drift").
    """
    from chemclaw.api.runner import _signal_event as to_event

    return to_event(signal)
