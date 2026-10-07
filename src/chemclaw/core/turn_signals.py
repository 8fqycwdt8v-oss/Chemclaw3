"""Side-channel for things a tool learns that the turn's event stream must surface.

A tool that launches a durable job or records a note returns its result into the model's context,
but the chemist must also see that it happened. Signals are published on LangGraph's custom stream
(`get_stream_writer()`), the same stream the tokens ride, so their order relative to everything else
is the stream's own. The information stays out of the model-facing tool signature, so the model
cannot fabricate "a job started" or "a note was recorded".

In `core` because both ends are outside the conversation layer (connector jobs and template steps
record; the front-door stream renders), so the LangGraph import here is declared in
`tests/test_third_party_layering.py`. The event types live in `api/events.py` and are not imported
here. One sink for all kinds, so the relative order of a launched job and a recorded note is
defined.
"""

from typing import Any, Literal

from langgraph.config import get_stream_writer
from pydantic import BaseModel

from chemclaw.core.plan_context import get_current_plan_link


class JobSignal(BaseModel):
    """A durable job a tool started during this turn."""

    job_id: str
    kind: str
    # The plan step the launch served, read ambiently at emit time (D-2026-08-27). Empty when the
    # launch was not made from a plan step — a template step, the CLI, a turn with no plan.
    plan_step: str = ""


class ToolQueuedSignal(BaseModel):
    """A queued tool call is waiting for a slot, or has just got one (`connectors/queued.py`).

    The tool-call card otherwise says "running" for the whole wait, which is false while the call
    sits in the queue and is the one thing a chemist watching a busy deployment wants to know.
    """

    tool: str
    job_id: str
    state: Literal["queued", "running"]
    # Calls waiting on the connector's interactive queue when read: the broker's approximate
    # backlog, never a strict rank. `None` where the broker could not say.
    waiting: int | None = None


class QuestionSignal(BaseModel):
    """A disambiguation the agent asked for during this turn."""

    question: str
    options: list[str]


class NoteRecordedSignal(BaseModel):
    """A note a tool wrote into the knowledge graph during this turn."""

    note_id: str
    reference: str


#: The kinds of deliberate refusal a failing tool call can carry. One definition shared by
#: `agent/audit.refusal_reason` (produces it), `ToolFailureSignal` (carries it) and
#: `api/events.ToolFailedEvent` (puts it on the wire for `Chemclaw3_ui` and `Chemclaw3_mock`). In
#: `core` because both the agent and the API may import it. A new gate adds its reason here.
RefusalReason = Literal["dry_run", "undeclared_write", "plan_gate", "repeat", "authz"]


class ToolFailureSignal(BaseModel):
    """A tool that raised during this turn, so the chemist can see why an answer went thin.

    Until this existed a failing tool was visible in three places — the model's context, the
    server log, and the audit trail — and in none of them to the person who asked. A live run
    caught the shape that makes it matter: a job launcher raised on every attempt, MAF stopped
    the tool loop after three consecutive errors, and the turn ended on the model's last words
    before the final failure — "Let me try the carboxylic acid acetylation:" — with no answer,
    no error, and nothing to say why (D-138). The turn had not crashed, so `ErrorEvent` was
    right to stay silent; what was missing was the *trace* being honest about a step that did
    not work.

    A signal rather than a return value, for the reason every other member of this union is one:
    it must come from the failing call itself, never from anything the model can author.
    """

    tool: str
    message: str
    # The call this failure belongs to, so consumers match on the call rather than the tool name
    # (two calls to one tool in a batch must not both be suppressed). Defaulted because the signal
    # is a cross-repository contract; empty means "not attributed".
    call_id: str = ""
    # Which gate refused this call, or `None` for a genuine fault, classified from the exception by
    # `agent/audit.refusal_reason` where the signal is recorded
    # (`agent/tool_authz.announce_tool_failures`) rather than re-derived by consumers from message
    # text. Typed as the closed set, so a gate whose reason the wire cannot express fails in the
    # change that added it.
    reason: RefusalReason | None = None


class SkillLoadedSignal(BaseModel):
    """A skill whose body this turn actually read — the join key a counter cannot be.

    **It exists for the self-confirmation guard and for nothing else yet, and that is stated
    rather than hidden.** Nothing distilled may count evidence it itself produced: a trajectory
    that happened *because* a skill was already shaping that turn is not independent evidence for
    proposing that skill. The predicate is one line; what it had nothing to read was a per-turn
    record of which skills were loaded, and `chemclaw_skill_loads_total{skill}` cannot be it —
    a Prometheus counter says a skill was read, never in which turn, and is not a join key.

    **Not rendered to the chemist**, unlike every other member of this union. The others exist
    because something happened that the person should see; this one is bookkeeping the turn's own
    cost row absorbs. It rides the same channel anyway because the channel is what carries a fact
    from a backend three layers down to the runner without the model being able to author it —
    which is this module's whole subject — and a second mechanism for one field would be the
    `job_events` duplication D-091 folded back in.

    Both tiers, deliberately: a personal skill shapes a turn exactly as a reviewed one does, and a
    guard blind to the personal tier would be blind to the tier most likely to be
    self-confirming, since that is the one the agent can propose into.
    """

    # The name alone: that is all any consumer asks about.
    skill: str


class HandoffSignal(BaseModel):
    """Control moved from one peer agent to another, raised by the handoff tool itself.

    **A signal rather than something the stream reads off the update, and both attempts at the
    latter were wrong.** The obvious producer scans a completed node's `tool_calls` for a transfer,
    which fails twice over: a handoff carries its agent's *whole* message list into the parent (it
    must, or the `AIMessage` holding the call is dropped and the thread keeps an orphan
    `ToolMessage`), so every later update replays every earlier hop — measured, a two-hop turn
    announced **seven** handoffs. And the peer's name is not recoverable from the tool's name,
    because a tool name cannot carry `-`: `transfer_to_evidence_peer` reads back as
    `evidence_peer` for a profile called `evidence-peer`, so a surface would print a name no
    profile has.

    Raised from the tool, both problems are gone rather than mitigated: it fires once per call
    because a call happens once, and it carries the peer's real name because the name is what the
    tool closed over. That is the same argument every other member of this union rests on — a
    signal comes from the act itself, never from anything a model can author or a reader can
    reconstruct.
    """

    #: The peer giving up control.
    from_agent: str
    #: The peer receiving it, and the author of what the chemist reads next.
    to_agent: str
    #: The handing model's own stated reason, written for the agent it hands to. Prose for a
    #: human; nothing branches on it.
    reason: str


class ExhibitSignal(BaseModel):
    """An artefact the agent created or revised during this turn (`agent/exhibit_tools.py`).

    The header only — the body is fetched over `GET /sessions/{id}/exhibits/{xid}`, for the reason
    `D-2026-08-09-a-preview-is-not-a-result` gives about a result block: a stream event is the
    announcement, never the document. A signal rather than something read off the tool's result,
    for this union's own reason: the store wrote the revision, so the announcement comes from the
    write and the author comes from the turn's identity — neither from text the model returned.
    """

    exhibit_id: str
    revision: int
    kind: str
    title: str
    op: Literal["created", "revised"]
    author_kind: Literal["agent", "human"]
    author: str


Signal = (
    JobSignal
    | ToolQueuedSignal
    | NoteRecordedSignal
    | QuestionSignal
    | SkillLoadedSignal
    | ToolFailureSignal
    | HandoffSignal
    | ExhibitSignal
)


# The key a signal rides under in the shared custom stream (other nodes write their own payloads),
# so `api/graph_stream._custom_event` can dispatch by shape.
_KEY = "chemclaw_signal"


def _emit(signal: Signal) -> None:
    """Publish one signal on the turn's stream, or drop it where nothing is streaming.

    The same tools run in a chat turn's tool node (writer present) and in a Temporal activity
    replaying a template step (no graph, no watcher), and a tool narrating must never fail a durable
    job. Outside a graph there is no reader, so dropping loses nothing.
    """
    writer = stream_writer_or_none()
    if writer is None:
        return
    writer({_KEY: signal})


def stream_writer_or_none() -> Any | None:
    """The graph's custom-stream writer, or `None` where there is no graph to write to.

    `get_stream_writer` raises rather than returning `None` off a graph, and which exception is an
    implementation accident: `RuntimeError` outside any runnable context, `KeyError` inside
    `StructuredTool.ainvoke` (the template-step path), and plausibly `AttributeError` in future. One
    helper so every caller catches the same set.
    """
    try:
        return get_stream_writer()
    except (RuntimeError, LookupError, AttributeError):
        return None


def record_job_started(job_id: str, kind: str) -> None:
    """Note that `kind` job `job_id` was launched. A no-op where nothing is streaming.

    The plan step is read here from the ambient `core.plan_context`, so every launch announcement is
    stamped uniformly and callers outside the harness contribute an empty string.
    """
    plan_step, _ = get_current_plan_link()
    _emit(JobSignal(job_id=job_id, kind=kind, plan_step=plan_step))


def record_tool_queued(
    tool: str, job_id: str, state: Literal["queued", "running"], waiting: int | None
) -> None:
    """Note that a queued `tool` call is waiting (`queued`) or has a slot (`running`)."""
    _emit(ToolQueuedSignal(tool=tool, job_id=job_id, state=state, waiting=waiting))


def record_note_written(note_id: str, reference: str) -> None:
    """Note that a note reached the graph. A no-op where nothing is streaming."""
    _emit(NoteRecordedSignal(note_id=note_id, reference=reference))


def record_exhibit(signal: ExhibitSignal) -> None:
    """Announce an artefact write to the chemist's stream. A no-op where nothing is streaming."""
    _emit(signal)


def record_skill_loaded(skill: str) -> None:
    """Note that this turn read one skill's body. A no-op where nothing is streaming.

    Called from the two backends that deliver a skill body (one per tier), beside the counter that
    books the load, so the per-turn array and the counter agree on what a load is.
    """
    _emit(SkillLoadedSignal(skill=skill))


def record_question(question: str, options: list[str]) -> None:
    """Note that the agent asked the chemist to disambiguate. A no-op where nothing streams."""
    _emit(QuestionSignal(question=question, options=options))


def record_tool_failure(
    tool: str, message: str, call_id: str = "", reason: RefusalReason | None = None
) -> None:
    """Note that `tool` failed, by raising or by answering. A no-op where nothing is streaming.

    `reason` is `agent/audit.refusal_reason`'s verdict when there was an exception, `None`
    otherwise: gates refuse by raising, so a returned failure names no gate.
    """
    _emit(ToolFailureSignal(tool=tool, message=message, call_id=call_id, reason=reason))


def record_handoff(from_agent: str, to_agent: str, reason: str) -> None:
    """Announce that control moved to `to_agent`, from inside the tool that moved it.

    Called by `agent/handoff.py`'s transfer tool; `tests/test_event_producers.py` requires every
    event producer to have a caller.

    Args:
        from_agent: The peer giving up control; empty only if the turn graph could not name it.
        to_agent: The peer receiving it.
        reason: The handing model's own account of why.
    """
    _emit(HandoffSignal(from_agent=from_agent, to_agent=to_agent, reason=reason))
