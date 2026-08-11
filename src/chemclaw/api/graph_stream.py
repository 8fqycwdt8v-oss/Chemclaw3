"""Turning a compiled graph's stream into the turn event contract (M8, D-2026-08-10).

`api/events.py` is the conformance boundary of this migration: a LangGraph turn either emits the
same events the previous engine's turn did, or the rebuild is not done. That is what let the two be
scored against each other rather than argued about, and it is why this module exists at all.

**Why a translator and not an update-shaped adapter.** The tempting move was to make the graph
yield objects with `.text` and `.contents` so the old runner loop consumed it unchanged. That would
have been faking one framework's private update shape with another's, and such a shape is not
stable enough to be worth impersonating — `runner_trace` says so in its own docstring, and it is
why that module duck-types rather than importing any concrete content class. Emitting the
*contract* directly is both simpler and the thing that is actually pinned by tests.

**What each stream mode is for**, with `stream_mode=["messages", "updates", "custom"]` and
`subgraphs=True`, which yields `(namespace, mode, payload)` three-tuples (measured against
`langgraph.pregel.main._output`; note the mode list must be a `list` — a tuple falls through to a
different branch and yields two-tuples instead):

- `messages` carries `(chunk, metadata)` per token, which is `TokenEvent`, and it is the only mode
  that arrives *while* the model is producing rather than after the node finishes. Tool calls are
  deliberately **not** read from here even though the chunks carry `tool_call_chunks`: that is the
  streamed, fragmented shape whose reassembly cost the previous engine two live-run defects
  (D-138, and the OpenAI-Responses case that announced ten `tool_call` events for one call).
- `updates` carries `{node: state_update}` once a node completes, so a tool call arrives *whole*.
  That is where calls, results and the todo list are read.
- `custom` carries what a *node* chose to publish about itself. Today that is the evidence
  fan-out's per-branch report (`chemclaw.retrieval.fanout`), which reaches here from inside a tool
  call because a branch's writer surfaces under the `tools:<id>` namespace, and every other
  out-of-band signal a turn raises (`core/turn_signals.py` publishes them through
  `get_stream_writer()`).

**The ordering rule is the runner's, reproduced rather than re-derived**: a signal is drained
before the content of the update it arrived with, because a tool that ran while the model was
producing that update ran *before* the text it then produced. That is the truthful transcript
order (RCH-4/RCH-5) and the two engines must not disagree about it.
"""

import logging
from collections.abc import AsyncIterator
from typing import Any

from langchain_core.messages import AIMessageChunk

from chemclaw.api.events import (
    ApprovalRequestEvent,
    Event,
    EvidenceSourceEvent,
    HandoffEvent,
    JobStartedEvent,
    NoteProposedEvent,
    PlanEvent,
    QuestionEvent,
    TokenEvent,
    ToolFailedEvent,
)
from chemclaw.api.runner_trace import ToolCallTrace
from chemclaw.api.runner_usage import graph_usage_tokens
from chemclaw.api.schemas import message_text
from chemclaw.core.turn_signals import _KEY as _SIGNAL_KEY
from chemclaw.core.turn_signals import (
    ApprovalSignal,
    HandoffSignal,
    JobSignal,
    QuestionSignal,
    Signal,
    ToolFailureSignal,
)

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
        `Event`s in the order and with the meanings `api/events.py` declares.
    """
    todos: list[str] = []
    # Who is currently answering, tracked across the stream by the handoff pair rather than
    # inferred per event. `""` is the main agent, which is every turn without a team.
    agent = ""
    async for _namespace, mode, payload in graph.astream(
        {"messages": [("user", message)]}, config, stream_mode=_MODES, subgraphs=True
    ):
        if mode == "messages":
            chunk, _metadata = payload
            usage.add(graph_usage_tokens(chunk))
            text = _text_of(chunk)
            if text:
                yield TokenEvent(text=text)
        elif mode == "custom":
            event = _custom_event(payload, on_signal)
            if isinstance(event, HandoffEvent):
                # The enter names the specialist, the hand back clears it. Safe to read as state
                # because the pair brackets the specialist's execution in stream order, which
                # `tests/test_agent_team.py` pins by asserting its output lands between them.
                agent = event.to
            if event is not None:
                yield event
        elif mode == "updates":
            async for event in _from_update(payload, agent, trace, todos):
                yield event


def _custom_event(payload: Any, on_signal: Any) -> Event | None:
    """One node's self-report as its event, or `None` for a payload nothing renders.

    Matched on shape rather than on a type tag, because a writer payload is whatever the node
    passed and there is no schema between them. Unknown payloads are dropped rather than guessed
    at: a node that publishes something no surface understands is a node ahead of its consumers,
    which is a normal state during a migration and not an error.

    `on_signal` fires here rather than in the caller because a signal *is* a custom payload now,
    and the runner's ledger of launched job ids has to see it before the event is yielded.
    """
    if not isinstance(payload, dict):
        return None
    signal = payload.get(_SIGNAL_KEY)
    if isinstance(signal, Signal):
        on_signal(signal)
        return _signal_event(signal)
    if "evidence_source" in payload:
        return EvidenceSourceEvent(
            source=str(payload["evidence_source"]), chunks=int(payload.get("chunks", 0))
        )
    return None


async def _from_update(
    payload: Any, agent: str, trace: ToolCallTrace, todos: list[str]
) -> AsyncIterator[Event]:
    """The events one completed node produces: its calls, its results, and any new plan.

    `agent` is the specialist currently running, tracked by the caller from the handoff pair, and
    it becomes the `agent` attribution on every event that specialist raises (M9) — which is what
    stops a team's trace from reading as though one actor did everything.

    **It used to be derived from the subgraph namespace, and that was wrong on every real turn.**
    `_agent_of(namespace)` took the node name before the colon, on the assumption that a
    specialist's updates arrive under `("<specialist>:<task-id>",)`. They do not:
    `SubAgentMiddleware` invokes the compiled specialist as an ordinary runnable *inside* the
    `task` tool, so the only frame on the namespace is the parent's tool node and every specialist
    event was attributed to the literal agent `"tools"`. The specialist's name is not in the
    namespace at all, under any dispatch that routes through the task tool — so this was not a
    formatting slip but a name that was never there to read.

    Measured on the live lane before it was believed: a sonnet-5 routing arm scored its one
    delegation as `expected evidence → tools`, i.e. reported as a supervisor mis-route what was
    actually the harness reading the wrong field. The unit test that should have caught it passed
    because it parametrized hand-written namespaces (`("evidence:7f3a",)`) the engine never emits —
    the repo's recurring failure of asserting against an invented shape.

    The handoff pair is the reader that *can* be right: `agent/team.running_specialist` raises it
    with the name it was constructed with, rather than reconstructing one from a graph path.
    """
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
                        str(getattr(message, "tool_call_id", "")), message_text(message)
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
    the same text `agent/plan_gate.py` hashes, which is what makes the plan a surface displays and
    the plan the gate binds an approval to literally the same strings.
    """
    todos = update.get("todos")
    if todos is None:
        return None
    return [str(todo.get("content", "")) for todo in todos if isinstance(todo, dict)]


def _signal_event(signal: Signal) -> Event:
    """Map one out-of-band turn signal to its stream event (one place, so the two cannot drift).

    It used to live in `chemclaw.api.runner` and be imported here at call time, because the runner
    owned the loop that drained the signal buffer and this module could not import it back without
    a cycle. Both reasons are gone: there is one loop, it is this one, and a signal reaches it as a
    stream payload rather than out of a contextvar.
    """
    if isinstance(signal, JobSignal):
        return JobStartedEvent(job_id=signal.job_id, kind=signal.kind)
    if isinstance(signal, QuestionSignal):
        return QuestionEvent(question=signal.question, options=signal.options)
    if isinstance(signal, ApprovalSignal):
        # Carries the durable hold's handle, so a surface can answer it via
        # POST /approvals/{id}/decision. Plan approval is *not* this: that is
        # `chemclaw.agent.plan_gate`, and it never reaches this stream.
        return ApprovalRequestEvent(prompt=signal.prompt, approval_id=signal.approval_id)
    if isinstance(signal, ToolFailureSignal):
        return ToolFailedEvent(tool=signal.tool, message=signal.message)
    if isinstance(signal, HandoffSignal):
        # Raised by `agent/team.running_specialist`, so the pair brackets exactly the interval the
        # audit trail attributes to the specialist. `to=""` is the hand back, not a missing field.
        return HandoffEvent(to=signal.to, reason=signal.reason)
    return NoteProposedEvent(note_id=signal.note_id, reference=signal.reference)
