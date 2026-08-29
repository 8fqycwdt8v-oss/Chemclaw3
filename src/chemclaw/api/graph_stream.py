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

from langchain_core.messages import AIMessageChunk, ToolMessage

from chemclaw.agent.plan_gate import plan_identity
from chemclaw.agent.state import turn_input
from chemclaw.api.events import (
    Event,
    EvidenceSourceEvent,
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
    exchanges: list[Any] | None = None,
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
        exchanges: Appended with the tool-bearing messages the graph produced, in order, for the
            transcript projection (`api/runner._record_transcript`). Collected here because this is
            the only place they exist as messages — the events carry no call id, so a projection
            rebuilt from them could not pair a result with its call. `None` collects nothing, which
            is what a caller that only wants the event stream passes.

    Yields:
        `Event`s in the order and with the meanings `api/events.py` declares.
    """
    todos: list[str] = []
    # The calls this turn has already reported as failed, by call id. **Not read off the
    # `ToolMessage`'s status**, which is the mistake this replaced:
    # `agent/tool_authz.answered_failure` rewrites that status to `"success"` before the stream ever
    # sees the message — deliberately, so a provider does not read `is_error` as "retry this" — and
    # its own docstring names this module as the reader that therefore needs a status-independent
    # test. It never got one, so every failed call emitted `tool_failed` *and* `tool_result`, and
    # the error sentence joined `ToolCallTrace.outputs`, which is the corpus `score_answer` grades
    # an answer's grounding against. Measured across four failure shapes on a real compiled graph:
    # the signal fired every time and the status read `'success'` every time.
    #
    # By call id rather than tool name, because a model may issue two calls to one tool in a single
    # batch and only one of them fail.
    failed_calls: set[str] = set()
    async for namespace, mode, payload in graph.astream(
        turn_input(message), config, stream_mode=_MODES, subgraphs=True
    ):
        if mode == "messages":
            chunk, _metadata = payload
            usage.add(graph_usage_tokens(chunk))
            text = _text_of(chunk)
            # **Only the root's tokens are the answer, and the attribution is what says so.**
            # The runner concatenates unattributed `TokenEvent`s into the turn's final answer, so
            # a specialist's working prose has to arrive marked or it is spliced into another
            # agent's answer, interleaved with the supervisor's own text in whatever order the two
            # happened to produce it.
            #
            # The *namespace* is the whole attribution: non-empty means "below the root". The
            # specialist's *name* is not in it (D-2026-08-11-the-specialists-name-is-not-in-the-
            # namespace) and there is no second carrier for it — the handoff pair that used to
            # supply one was deleted with the producer
            # (D-2026-08-26-an-attribution-nothing-can-write-is-not-an-attribution). A chunk from
            # below the root therefore arrives marked `"subagent"` rather than named, which is the
            # direction that fails safe.
            #
            # The usage is counted either way: a specialist's tokens cost the same money.
            if text:
                yield TokenEvent(text=text, agent="subagent" if namespace else "")
        elif mode == "custom":
            if isinstance(signal := (payload or {}).get(_SIGNAL_KEY), ToolFailureSignal):
                # **Only an attributed id.** `ToolFailureSignal.call_id` documents `""` as "not
                # attributed", never "the call with no id" — and a failure that never reached
                # the tool chain has nothing to be matched *to*: `agent/model_calls` announces the
                # calls a turn will not run, and no `tool_call` event is ever emitted for them.
                # (The upstream entries do carry an id; `BrokenCall` drops it as unusable
                # here.) Adding the empty
                # string here would put it in the suppression set, so any `ToolMessage` arriving
                # with an empty `tool_call_id` would have its result silently dropped for a
                # failure that was not its own.
                if signal.call_id:
                    failed_calls.add(signal.call_id)
            event = _custom_event(payload, on_signal)
            if event is not None:
                yield event
        elif mode == "updates":
            # **A non-empty namespace means "below the root", and that is the only attribution
            # available here.** `events.py` states that `""` *means* the main agent, and every turn
            # this repository runs is one. Without the namespace test, every helper the `task` tool
            # runs had its tool calls, its results and its plan emitted as the supervisor's: its
            # output joined `ToolCallTrace.outputs` and the parent session's fetchable `result_ref`
            # indistinguishably, and its `write_todos` surfaced as a root `PlanEvent` that
            # *replaced* the supervisor's — so under `plan_only` the checklist a chemist approved
            # could be the helper's rather than the turn's.
            #
            # The same rule the token branch above already applies, for the same reason: the
            # specialist's *name* is not in the namespace under any dispatch that routes through the
            # task tool (D-2026-08-11-the-specialists-name-is-not-in-the-namespace), but its
            # non-emptiness is a fact, and marking work as a subagent's is what fails safe.
            #
            # The plan is not merely relabelled but withheld: `PlanEvent` carries no `agent` field,
            # so a helper's todo list has nowhere to say whose it is, and a surface showing it as
            # the
            # turn's plan is worse than a surface not showing it.
            below_root = bool(namespace)
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
            source=str(payload["evidence_source"]),
            chunks=int(payload.get("chunks", 0)),
            # Defaulted on the read side as well as on the wire, because this payload has no schema
            # between the branch and here: an older writer that publishes only a count must keep
            # meaning "asked and answered", never "broken".
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

    `exchanges`, when given, collects the tool-bearing messages for the transcript projection. Here
    rather than in the caller because this is where they exist as *messages*: the events carry no
    call id, so a projection rebuilt from them could not pair a result with the call it answers.

    `agent` is the attribution the caller derived from the namespace's non-emptiness — `""` for the
    turn's own agent, `"subagent"` for anything below the root — and it becomes the `agent` field on
    every event that node raises (M9), which is what stops a team's trace from reading as though one
    actor did everything.

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

    The handoff pair was the reader that *could* be right — it was raised with the name the
    specialist was constructed with, rather than reconstructing one from a graph path. Its producer
    went with the specialist team (D-2026-08-15) and its plumbing went in
    `D-2026-08-26-an-attribution-nothing-can-write-is-not-an-attribution`, so no name reaches this
    stream at all. The caller marks work from below the root by the one fact the namespace does
    carry: that it is non-empty.
    """
    for node, update in (payload or {}).items():
        # **Whoever adds the first interrupt: this line drops it.** LangGraph delivers a suspended
        # turn as `{"__interrupt__": (Interrupt(...),)}` — a *tuple*, not a dict — so it takes this
        # `continue` and the turn ends with no answer text, which the runner then classifies as
        # `empty_answer`. Nothing raises `interrupt()` today (D-2026-08-15 kept the plan gate a
        # refusal, and the durable hold went with the challenge panel), so this is latent rather
        # than live, and writing the branch now would be a path no test could reach.
        #
        # Recorded here rather than in `BACKLOG.md` deliberately: a note addressed to whoever adds
        # a producer belongs where they will be reading, not in a queue of forty things capped on
        # what a person can hold.
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
            # `isinstance`, not a class-name test: `ToolMessageChunk` is a real subclass, and a
            # name comparison misses it — silently, since the branch simply does not run. The
            # consequence would be a result never traced: no `result_ref` stored, and a transcript
            # showing a call with no answer.
            if isinstance(message, ToolMessage):
                # **A failed call is not a result, and must not become evidence.** `trace.returned`
                # appends to `outputs`, which the answer gate reads to decide whether a claim was in
                # front of the model — so emitting a failure here fed an error string to the
                # grounding check as though it were retrieved data, and reported the call as
                # `tool_result` while `announce_tool_failures` had already raised `tool_failed` for
                # it. Two events for one outcome, one of them wrong, and the pair is documented as
                # exhaustive.
                #
                # The turn's own failure signals are the reader that can be right; the status is
                # kept as a second test only because a `ToolMessage` can reach here from a path that
                # raised no signal (a middleware short-circuit), and an unreported failure is worse
                # than a redundant check.
                call_id = str(getattr(message, "tool_call_id", ""))
                if call_id in failed_calls or getattr(message, "status", "success") == "error":
                    logger.debug("tool call %s failed; already reported as tool_failed", call_id)
                else:
                    yield _attributed(
                        await trace.returned(
                            str(getattr(message, "tool_call_id", "")), message_text(message)
                        ),
                        agent,
                    )
        plan = _todo_titles(update) if emit_plan else None
        if plan is not None and plan != todos:
            # Only on change and never empty, matching `runner._PlanEmitter`: an unchanged plan
            # re-sent every node would drown the trace, and an empty one is the harness clearing
            # its list rather than a plan worth rendering.
            todos[:] = plan
            if plan:
                # **Hashed over the bare contents, not over `plan` — the two are different
                # strings and only one of them is the identity.** `plan` carries `_todo_titles`'s
                # checkbox rendering, while `plan_identity` is fed `plan_state.session_todos`,
                # which returns `content` alone. Hashing what is displayed would emit a
                # `plan_hash` that no decision could ever match, and it would look authoritative
                # while being wrong on every plan — worse than the missing field it replaces.
                # Non-empty by construction: `plan_identity` returns `None` only for an empty
                # plan, which this branch has already excluded.
                yield PlanEvent(todos=plan, plan_hash=plan_identity(_todo_contents(update)) or "")
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
    """The plan a node's state update carries as a checklist, or `None` when it carries none.

    `TodoListMiddleware` keeps `{content, status}` items and **both halves reach the surface**. The
    status was being dropped, so every step rendered identically whether it was done, in progress or
    untouched — against an event whose own contract says "rendered as a checklist" and a plan that
    is re-emitted on every change, giving a client churn it could not interpret. Completion state is
    the one thing a surface must not have to infer.

    The checkbox is a *rendering*; `agent/plan_gate.plan_identity` hashes the bare `content`, and
    `evals/autonomy._plan_steps` strips the prefix before scoring. So the approval a chemist gives
    is bound to the work, not to how far along it was when they looked — which is what lets a plan
    stay approved while its steps tick over.
    """
    todos = update.get("todos")
    if todos is None:
        return None
    return [
        f"[{'x' if todo.get('status') == 'completed' else ' '}] {todo.get('content', '')}"
        for todo in todos
        if isinstance(todo, dict)
    ]


def _todo_contents(update: dict[str, Any]) -> list[str]:
    """The plan's bare step text — what a decision is hashed against, not what is displayed.

    The sibling of `_todo_titles`, and the pair exists because the two answers differ by exactly the
    checkbox. `agent/plan_state.session_todos` — which is what the gate and the decision route feed
    to `plan_identity` — returns `content` alone, so an identity derived from the rendered lines
    would agree with nothing. Written as its own function rather than by stripping the prefix off
    `_todo_titles`, because a strip is a second, weaker copy of the rendering rule: it goes wrong
    silently the day the rendering changes, where reading the field cannot.
    """
    return [
        str(todo["content"])
        for todo in update.get("todos") or []
        if isinstance(todo, dict) and "content" in todo
    ]


def _signal_event(signal: Signal) -> Event:
    """Map one out-of-band turn signal to its stream event (one place, so the two cannot drift).

    It used to live in `chemclaw.api.runner` and be imported here at call time, because the runner
    owned the loop that drained the signal buffer and this module could not import it back without
    a cycle. Both reasons are gone: there is one loop, it is this one, and a signal reaches it as a
    stream payload rather than out of a contextvar.
    """
    if isinstance(signal, JobSignal):
        return JobStartedEvent(job_id=signal.job_id, kind=signal.kind, plan_step=signal.plan_step)
    if isinstance(signal, QuestionSignal):
        return QuestionEvent(question=signal.question, options=signal.options)
    if isinstance(signal, ToolFailureSignal):
        # The classification rides on the signal, made from the exception by
        # `agent/audit.refusal_reason` where the exception still existed. This used to re-derive it
        # here by testing whether `signal.message` started with `"PlanNotApprovedError:"` — which
        # recovered one of five gates, from a string `failure_detail` truncates, in a module that
        # cannot see the classes. Downstream reads a field either way; what changed is that the
        # field is now the same verdict the audit row records, rather than a second opinion.
        #
        return ToolFailedEvent(tool=signal.tool, message=signal.message, reason=signal.reason)
    return NoteProposedEvent(note_id=signal.note_id, reference=signal.reference)
