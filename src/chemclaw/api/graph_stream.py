"""Turning a compiled graph's stream into the turn event contract (`api/events.py`).

Emits the contract directly rather than impersonating another framework's update shape. Streams
with `stream_mode=["messages", "updates", "custom", "values"]` and `subgraphs=True`, which yields
`(namespace, mode, payload)` three-tuples:

- `messages` carries `(chunk, metadata)` per token — `TokenEvent`, the only mode that arrives while
  the model is still producing. Tool calls are not read from its fragmented `tool_call_chunks`,
  except by `api/exhibit_drafts.py` for a document preview that decides nothing.
- `updates` carries `{node: state_update}` once a node completes, so a tool call arrives whole:
  calls, results and the todo list are read here.
- `custom` carries what a node published about itself: the evidence fan-out's per-branch report
  and every out-of-band turn signal (`core/turn_signals.py`).
- `values` is read only for the per-turn counters (`_carry_forward`).

A signal is yielded before the content of the update it arrived with, because the tool ran before
the text the model then produced.
"""

import logging
import re
from collections.abc import AsyncIterator
from typing import Any

from langchain_core.messages import AIMessageChunk, ToolMessage

from chemclaw.agent.plan_gate import plan_identity
from chemclaw.agent.plan_scope import declared_scope
from chemclaw.agent.state import PEER_DEPTH_ATTR, turn_input
from chemclaw.agent.tool_result_size import full_result_ref, stored_result_ref, was_cut
from chemclaw.api.events import (
    Event,
    EvidenceSourceEvent,
    ExhibitEvent,
    HandoffEvent,
    JobStartedEvent,
    NoteRecordedEvent,
    PlanEvent,
    QuestionEvent,
    TokenEvent,
    ToolFailedEvent,
    ToolQueuedEvent,
)
from chemclaw.api.exhibit_drafts import DraftStream
from chemclaw.api.runner_trace import ToolCallTrace
from chemclaw.api.runner_usage import graph_usage_tokens
from chemclaw.api.schemas import message_text
from chemclaw.core.result_handle import without_handle_line
from chemclaw.core.turn_signals import _KEY as _SIGNAL_KEY
from chemclaw.core.turn_signals import (
    ExhibitSignal,
    HandoffSignal,
    JobSignal,
    QuestionSignal,
    Signal,
    SkillLoadedSignal,
    ToolFailureSignal,
    ToolQueuedSignal,
)

logger = logging.getLogger(__name__)

# The modes, as a list: `astream` tests `isinstance(stream_mode, list)` and a tuple silently changes
# the yielded tuple's arity. `values` is only for the carry: the carried channels are
# `UntrackedValue`s, absent from `aget_state` and not reconstructible from `updates`. Its payload
# references the channels' objects, so the cost does not grow with the thread.
_MODES = ["messages", "updates", "custom", "values"]

# The node `create_agent` runs tools in. A model call made inside a tool body inherits the graph's
# callbacks and streams under the same empty namespace as the answer, so the node name is what tells
# them apart. `tests/test_langgraph_stream.py` drives a real tool that calls a model, so an upstream
# rename fails loudly.
_TOOL_NODE = "tools"


def root_depth(graph: Any) -> int:
    """How many namespace frames a turn's own agent sits behind on this graph.

    Attribution here is a depth test: events deeper than this come from below the turn's agent. A
    single agent is the stream's root (0); in a turn graph (`agent/turn_graph.py`) every peer is a
    node
    one frame down (1), and treating it as root-depth 0 would mark the chemist's agent as a subagent
    and
    answer the turn empty. Read from a stamp the builder sets, not derived: every compiled agent has
    an
    `active_agent` channel, and node names move with upstream.

    Args:
        graph: The compiled graph a turn runs on.

    Returns:
        0 for a single agent (the shipped default and anything without the stamp), 1 for a turn
        graph whose peers are its nodes.
    """
    return int(getattr(graph, PEER_DEPTH_ATTR, 0))


async def graph_events(
    graph: Any,
    message: str,
    *,
    config: dict[str, Any],
    trace: ToolCallTrace,
    on_signal: Any,
    usage: Any,
    exchanges: list[Any] | None = None,
    carry: dict[str, Any] | None = None,
) -> AsyncIterator[Event]:
    """Drive one turn on a compiled graph, yielding the turn's events in order.

    Args:
        graph: The compiled graph for this turn, already holding this turn's connector tools.
        message: The chemist's message.
        config: The invocation config, carrying `configurable.thread_id` so a checkpointed session
            continues rather than restarting.
        trace: The turn's `ToolCallTrace`, shared with the runner, whose answer gate reads `outputs`
            and `called_tools` after the stream ends.
        on_signal: Called with each drained `Signal` before its event is yielded, so the runner can
            keep its own ledger (job ids a mid-turn resume waits on).
        usage: The turn's token ledger; fed from each message chunk's `usage_metadata`.
        exchanges: Appended with the tool-bearing messages the graph produced, in order, for the
            transcript projection (`api/runner._record_transcript`); events carry no call id, so
            they could not pair a result with its call. `None` collects nothing.
        carry: The turn's per-turn counters (`model_calls`, `billed_tokens`, `handoffs`), seeded
        into
            this run's input and updated from it, so the caps span a mid-turn resume — the one case
            where a turn is two graph invocations and untracked channels would restart at 0. `None`
            for one invocation per turn.

    Yields:
        `Event`s in the order and with the meanings `api/events.py` declares.
    """
    todos: list[str] = []
    # Calls already reported as failed, by call id. Not read from the `ToolMessage` status, which
    # `agent/tool_authz.answered_failure` rewrites to `"success"`; without this a failed call would
    # also
    # emit `tool_result` and its error would enter the grounding corpus. By id, since two calls to
    # one
    # tool may differ.
    failed_calls: set[str] = set()
    # Artefact announcements waiting for the result of the call that wrote them: the signal arrives
    # before the tools node's update, and the contract orders `tool_call` → `tool_result` →
    # `exhibit`.
    # Released when the root's tools node completes, not on a helper's update during it.
    held_exhibits: list[Event] = []
    # Previews a document artefact the root agent is still writing. Closed on every root update, so
    # a
    # call's last frame precedes its `tool_call`.
    drafts = DraftStream()
    # Read once per turn rather than per event: it is a property of the compiled object, and
    # re-deriving it 400 times a turn would be the same answer 400 times.
    depth = root_depth(graph)
    failure: list[Exception] = []
    async for namespace, mode, payload in _until_failure(
        graph.astream(
            {**turn_input(message), **(carry or {})}, config, stream_mode=_MODES, subgraphs=True
        ),
        failure,
    ):
        if mode == "messages":
            chunk, metadata = payload
            usage.add(graph_usage_tokens(chunk))
            # A model call made inside a tool is the tool's working, never the answer: its usage is
            # metered
            # above, but its text is not a token (the runner concatenates tokens into the answer).
            # Filtered here
            # rather than tagging calls `nostream`, which would also drop their usage.
            if (metadata or {}).get("langgraph_node") == _TOOL_NODE:
                continue
            if len(namespace) <= depth:
                for draft in drafts.feed(chunk):
                    yield draft
            text = _text_of(chunk)
            # Only the turn's own agent's tokens are the answer, since the runner concatenates
            # unattributed
            # `TokenEvent`s. Below that depth a chunk is marked `"subagent"` — the namespace carries
            # no name.
            # Usage is counted either way.
            if text:
                yield TokenEvent(text=text, agent="subagent" if len(namespace) > depth else "")
        elif mode == "custom":
            if isinstance(signal := (payload or {}).get(_SIGNAL_KEY), ToolFailureSignal):
                # Every id, the empty one included: a refusal is deliberately `status="success"`, so
                # this set is the
                # only thing keeping it out of `tool_result` and the grounding corpus. A signal's id
                # is its own
                # call's id, so an empty one matches only a `ToolMessage` with the same empty id.
                failed_calls.add(signal.call_id)
            event = _custom_event(payload, on_signal)
            if isinstance(event, ExhibitEvent):
                held_exhibits.append(event)
            elif event is not None:
                yield event
        elif mode == "updates":
            # Deeper than `depth` means below the turn's agent (on a turn graph a peer is at depth
            # 1, a helper
            # inside it at 2; see `root_depth`). Such events are attributed `"subagent"` and a
            # helper's plan is
            # withheld, since `PlanEvent` has no `agent` field and must not replace the turn's plan.
            # Not separated: `trace` and `exchanges` still receive a helper's results, so they enter
            # the parent's
            # `ToolCallTrace.outputs`, full-result store and transcript (which records no agent).
            below_root = len(namespace) > depth
            if not below_root:
                for draft in drafts.close():
                    yield draft
            async for event in _from_update(
                payload,
                "subagent" if below_root else "",
                trace,
                todos,
                exchanges,
                failed_calls,
                emit_plan=not below_root,
            ):
                yield event
            if held_exhibits and not below_root and _TOOL_NODE in (payload or {}):
                for exhibit in held_exhibits:
                    yield _with_call_id(exhibit, payload[_TOOL_NODE])
                held_exhibits.clear()
        elif mode == "values":
            # Only the outermost graph's channels: reducers have already folded deeper frames into
            # them, so the
            # shallowest frame holds the turn's total.
            if carry is not None and not namespace:
                _carry_forward(carry, payload)
    # A write the stream never saw a root tools update for — a run cut off between the tool body
    # and its node completing, or a graph that raised — is still a write, and the store holds it.
    for exhibit in held_exhibits:
        yield exhibit
    if failure:
        raise failure[0]


#: The two tools whose result names the artefact revision they wrote.
_EXHIBIT_WRITERS = frozenset({"create_exhibit", "revise_exhibit"})


def _with_call_id(event: Event, update: Any) -> Event:
    """`event` carrying the id of the tool call that wrote it, read off the tools node's results.

    The signal is raised inside the tool body, which does not know the call id; the `ToolMessage`
    knows
    the id and the `exhibit_id`/`revision` the tool returned, so matching on the pair lets a surface
    settle a draft by `call_id`. Unmatched keeps `""`.
    """
    if not isinstance(event, ExhibitEvent) or not isinstance(update, dict):
        return event
    revision = re.compile(rf'"revision":\s*{event.revision}\b')
    for message in update.get("messages") or []:
        if (
            isinstance(message, ToolMessage)
            and message.name in _EXHIBIT_WRITERS
            and event.exhibit_id in (text := message_text(message))
            and revision.search(text)
        ):
            return event.model_copy(update={"call_id": str(message.tool_call_id or "")})
    return event


async def _until_failure(
    stream: AsyncIterator[Any], failure: list[Exception]
) -> AsyncIterator[Any]:
    """`stream`'s items until it raises, with the `Exception` recorded in `failure` instead.

    A tool that wrote an artefact has committed it whatever the graph does next, so held
    announcements
    must still be yielded before failing. A `finally` cannot yield during close or cancellation, so
    only an `Exception` is caught here; the caller re-raises it after releasing what it held.
    """
    try:
        async for item in stream:
            yield item
    except Exception as exc:
        failure.append(exc)


# The channels a mid-turn resume continues rather than restarts, named explicitly because the carry
# is fed into the graph's input. `handoffs` included so a resume cannot reset `agent_max_handoffs`.
# `active_agent` is checkpointed and needs no entry.
_CARRIED_CHANNELS = ("model_calls", "billed_tokens", "handoffs")


def _carry_forward(carry: dict[str, Any], payload: Any) -> None:
    """Copy the turn's per-turn counters off the graph's own channels, so a resume continues them.

    Read from the `values` stream: with `subgraphs=True` each `updates` payload holds one node, so
    no
    fold over updates can recover a fan-out's total, and an undercount here would give a resumed
    turn
    fresh allowance. `aget_state` does not carry `UntrackedValue` channels. Taken as `max` with the
    existing carry, so the count never walks back.

    Args:
        carry: The turn's carry, updated in place.
        payload: One `values` payload from the outermost namespace — the whole state, keyed by
            channel.
    """
    if not isinstance(payload, dict):
        return
    for channel in _CARRIED_CHANNELS:
        value = payload.get(channel)
        if isinstance(value, int) and not isinstance(value, bool):
            carry[channel] = max(value, int(carry.get(channel, 0)))


def _custom_event(payload: Any, on_signal: Any) -> Event | None:
    """One node's self-report as its event, or `None` for a payload nothing renders.

    Matched on shape, since writer payloads have no schema; unknown payloads are dropped.
    `on_signal`
    fires here so the runner's job-id ledger sees a signal before its event is yielded.
    """
    if not isinstance(payload, dict):
        return None
    signal = payload.get(_SIGNAL_KEY)
    if isinstance(signal, Signal):
        on_signal(signal)
        return _signal_event(signal)
    if "evidence_source" in payload:
        return EvidenceSourceEvent(
            source=str(payload["evidence_source"]),
            chunks=int(payload.get("chunks", 0)),
            # Defaulted on read too: an older writer publishing only a count means "answered", never
            # "broken".
            failed=bool(payload.get("failed", False)),
        )
    return None


async def _from_update(
    payload: Any,
    agent: str,
    trace: ToolCallTrace,
    todos: list[str],
    exchanges: list[Any] | None = None,
    failed_calls: frozenset[str] | set[str] = frozenset(),
    emit_plan: bool = True,
) -> AsyncIterator[Event]:
    """The events one completed node produces: its calls, its results, and any new plan.

    `exchanges`, when given, collects the tool-bearing messages for the transcript projection; this
    is
    where they still exist as messages with call ids. `agent` is the caller's attribution — `""` for
    the turn's own agent, `"subagent"` below it — set on every event the node raises. The namespace
    holds no helper name (a `task` helper runs inside the tool node), so non-emptiness is the only
    fact available.
    """
    for node, update in (payload or {}).items():
        # Note for whoever adds the first `interrupt()`: LangGraph delivers it as
        # `{"__interrupt__": (Interrupt(...),)}`, a tuple, which this `continue` drops, so the turn
        # would end
        # as `empty_answer`. Nothing raises one today.
        if not isinstance(update, dict):
            continue
        for message in update.get("messages") or []:
            if exchanges is not None and (
                getattr(message, "tool_calls", None) or isinstance(message, ToolMessage)
            ):
                exchanges.append(message)
            for call in getattr(message, "tool_calls", None) or []:
                yield _attributed(
                    trace.issued(
                        str(call.get("id") or ""), str(call.get("name") or ""), _args(call)
                    ),
                    agent,
                )
            # `isinstance`, not a class-name test, so `ToolMessageChunk` is traced too.
            if isinstance(message, ToolMessage):
                # A failed call is not a result and must not become evidence (`trace.returned` feeds
                # the grounding
                # check); `announce_tool_failures` already raised `tool_failed`. The status test is
                # a fallback for a
                # `ToolMessage` from a path that raised no signal (a middleware short-circuit).
                call_id = str(getattr(message, "tool_call_id", ""))
                if call_id in failed_calls or getattr(message, "status", "success") == "error":
                    logger.debug("tool call %s failed; already reported as tool_failed", call_id)
                else:
                    # `cut`/`full_ref` come from `response_metadata`, set by the tool chain, the
                    # only place that saw
                    # both texts.
                    yield _attributed(
                        await trace.returned(
                            str(getattr(message, "tool_call_id", "")),
                            without_handle_line(message_text(message)),
                            cut=was_cut(message),
                            full_ref=full_result_ref(message),
                            stored_ref=stored_result_ref(message),
                        ),
                        agent,
                    )
        plan = _todo_titles(update) if emit_plan else None
        if plan is not None and plan != todos:
            # Only on change and never empty, like `runner._PlanEmitter`: an empty list is the
            # harness clearing
            # its plan.
            todos[:] = plan
            if plan:
                # Hash the steps (`content` plus `tools` declaration, as `plan_state.session_plan`
                # answers them), not
                # the rendered `plan`, or no decision could ever match the hash. Non-empty here, so
                # `plan_identity` returns a value. One read feeds both `plan_hash` and `scope`, in
                # the order
                # `routes/plan._read_plan` uses, so the hash and the scope it authorizes describe
                # the same plan.
                steps = _plan_steps(update)
                yield PlanEvent(
                    todos=plan,
                    plan_hash=plan_identity(steps) or "",
                    scope=sorted(declared_scope(steps)),
                )
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

    Uses `.text` when available, which excludes the tool-call fragments some providers put in
    `content`.
    """
    if not isinstance(chunk, AIMessageChunk):
        return ""
    return str(chunk.text or "")


def _todo_titles(update: dict[str, Any]) -> list[str] | None:
    """The plan a node's state update carries as a checklist, or `None` when it carries none.

    Each `{content, status}` item is rendered with its status checkbox so a surface need not infer
    progress. The checkbox is rendering only: `agent/plan_gate.plan_identity` ignores `status`, so
    an
    approval stays valid as steps complete.
    """
    todos = update.get("todos")
    if todos is None:
        return None
    return [
        f"[{'x' if todo.get('status') == 'completed' else ' '}] {todo.get('content', '')}"
        for todo in todos
        if isinstance(todo, dict)
    ]


def _plan_steps(update: dict[str, Any]) -> list[dict[str, Any]]:
    """The plan's steps as the identity reads them — not what is displayed.

    Returns the steps whole, as `agent/plan_state.session_plan` does, rather than stripping the
    checkbox off `_todo_titles`, which would silently break when the rendering changes.
    """
    return [
        todo for todo in update.get("todos") or [] if isinstance(todo, dict) and "content" in todo
    ]


def _signal_event(signal: Signal) -> Event | None:
    """Map one out-of-band turn signal to its stream event, in one place."""
    if isinstance(signal, JobSignal):
        return JobStartedEvent(job_id=signal.job_id, kind=signal.kind, plan_step=signal.plan_step)
    if isinstance(signal, ToolQueuedSignal):
        return ToolQueuedEvent(
            tool=signal.tool, job_id=signal.job_id, state=signal.state, waiting=signal.waiting
        )
    if isinstance(signal, QuestionSignal):
        return QuestionEvent(question=signal.question, options=signal.options)
    if isinstance(signal, HandoffSignal):
        # Raised by the transfer tool itself, once per call, with the peer's real name. Kept above
        # the tail:
        # the chain ends in an unguarded `NoteRecordedEvent` default.
        return HandoffEvent(
            from_agent=signal.from_agent, to_agent=signal.to_agent, reason=signal.reason
        )
    if isinstance(signal, ToolFailureSignal):
        # The classification rides on the signal, made by `agent/audit.refusal_reason` from the
        # exception,
        # so the event and the audit row carry the same verdict.
        return ToolFailedEvent(
            tool=signal.tool, message=signal.message, reason=signal.reason, call_id=signal.call_id
        )
    if isinstance(signal, ExhibitSignal):
        return ExhibitEvent(**signal.model_dump())
    if isinstance(signal, SkillLoadedSignal):
        # Deliberately no event: skill loading is bookkeeping for the cost row
        # (`turn_costs.skills_loaded`),
        # not something to show the chemist. Returns explicitly so it cannot fall through to the
        # `NoteRecordedEvent` default.
        return None
    return NoteRecordedEvent(note_id=signal.note_id, reference=signal.reference)
