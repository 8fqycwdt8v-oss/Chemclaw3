"""Side-channel for things a tool learns that the turn's event stream must surface (gaps RCH-4/5).

`api/events.py` has carried `JobStartedEvent` since F2 and the chat UI has rendered it since
F2-T2, but nothing ever emitted one: a tool that launches a durable job returns a job id *into the
model's context*, and the runner — which only sees the model's streamed updates — has no way to know
a job started. The same is true of a PR-gate proposal: `propose_note` opens a branch and returns a
reference to the model, so the chemist never learns their contribution landed (the "a human
decides" line lived only in a git host's UI, disconnected from the conversation that produced it).

The carrier is LangGraph's own custom stream (`get_stream_writer()`), which is what the rebuild
bought here. It was a task-local contextvar buffer the runner drained after every streamed update,
plus a `begin_turn`/`end_turn` pair the runner's non-awaiting `finally` had to police, plus two
extra drains at the points where "there is no next iteration to carry the last signal" — one after
the graph returned and one after a mid-turn resume. All of that existed because MAF had no
side-channel, so ordering had to be reconstructed by the reader. A writer publishes into the same
stream the tokens ride, so the order is the stream's rather than something the runner maintains,
and the three drains and the reset go with it.

What did *not* change is why a side-channel exists at all: the information must stay out of the
*model-facing* tool signature, so the model cannot fabricate "a job started" or "a note was
proposed". A tool returns its job id to the model; it reports the launch to the chemist here.

**In `core/` rather than `agent/`, since the R2 layering move**: a few pydantic records and one
publish call, with both ends outside the conversation layer — a connector job or a template step
records the signal, and the front-door stream renders it. That is also why the LangGraph import
here is declared rather than avoided (`tests/test_third_party_layering.py`): moving this into
`agent/` to keep the kernel engine-free would make `connectors/` and `templates/` import layer 1,
which is the worse trade. The event types it feeds still live in `api/events.py`; nothing here
imports them, and that one-way relationship is what lets the recording side stay ignorant of the
transport.

**One sink, not one per kind.** A second mechanism carrying job ids only (`job_events`, a
Replit-only addition, D-091) was built independently and folded in here rather than kept beside
this one: two sinks read separately leave the *relative order* of a launched job and a proposed
note undefined, which is precisely what a transcript must get right. Its four caller-facing names
survived the fold as aliases and were removed in D-149 — three had never had a caller, and the
fourth discarded the `kind` this module's whole point is to carry.
"""

from typing import Any

from langgraph.config import get_stream_writer
from pydantic import BaseModel


class JobSignal(BaseModel):
    """A durable job a tool started during this turn."""

    job_id: str
    kind: str


class QuestionSignal(BaseModel):
    """A disambiguation the agent asked for during this turn."""

    question: str
    options: list[str]


class ProposalSignal(BaseModel):
    """A note a tool proposed through the PR-gate during this turn."""

    note_id: str
    reference: str


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
    # The call this failure belongs to, so a consumer can match it to the `tool_call` event rather
    # than to the tool *name*. Additive and defaulted, because a signal shape is a contract two
    # other repositories read; empty means "not attributed", never "the first call to this tool".
    #
    # It exists because matching by name is wrong in the one case that matters: a model may issue
    # two calls to the same tool in a single batch, and suppressing the result of both because one
    # failed loses a real answer.
    call_id: str = ""


Signal = JobSignal | ProposalSignal | QuestionSignal | ToolFailureSignal


# The key a signal rides under in the graph's custom stream. Namespaced because the channel is
# shared: any node may write any payload to it (`gather_evidence`'s per-source counts do), and
# `api/graph_stream._custom_event` dispatches on shape rather than on a schema neither side owns.
_KEY = "chemclaw_signal"


def _emit(signal: Signal) -> None:
    """Publish one signal on the turn's stream, or drop it where nothing is streaming.

    **The guard is the design, not a precaution.** `get_stream_writer()` resolves the writer off
    LangGraph's ambient runnable config, and outside a graph it does not return `None` — it raises.
    The same tools run in two places: a chat turn's tool node, where a writer exists and a chemist
    is watching, and a Temporal activity replaying a template step
    (`agent/tool_invocation.invoke_governed`), where neither is true. Letting the second raise would
    fail a durable job because a tool tried to *narrate*.

    **Two exception types for one condition, both measured**, which is why this catches a pair that
    otherwise looks careless. A bare call outside any runnable context raises `RuntimeError: Called
    get_config outside of a runnable context`. A call from inside `StructuredTool.ainvoke` — a
    runnable context, but not a graph — raises `KeyError: '__pregel_runtime'` instead, because the
    config exists and the runtime key in it does not. That second one *is* the template-step path,
    so catching only the first left the exact caller this guard was written for still failing.

    Dropping here costs nothing that was not already lost. The only consumers are the front door's
    stream and `api/graph_stream`, so a signal recorded in an activity had no reader before this
    either — it accumulated in a buffer nobody drained.
    """
    writer = stream_writer_or_none()
    if writer is None:
        return
    writer({_KEY: signal})


def stream_writer_or_none() -> Any | None:
    """The graph's custom-stream writer, or `None` where there is no graph to write to.

    **One helper because two call sites were asserting two different things about one upstream
    call.** This module caught `(RuntimeError, KeyError)` and `retrieval/fanout.py` caught
    `(RuntimeError, LookupError)` — and since `KeyError` is a `LookupError`, the second strictly
    subsumed the first, so a change upstream would have broken one and not the other.

    The exception types are an accident of the implementation, not a contract: `get_stream_writer`
    reaches a private config key by bare subscript, which is why it raises `RuntimeError` off any
    runnable context but `KeyError` inside `StructuredTool.ainvoke` — both measured.
    `AttributeError` is caught too, for the plausible upstream shape where the runtime resolves to
    `None` and is then attributed.

    The cost of getting this wrong is specific: the same tools run in a chat turn's tool node and in
    a Temporal activity replaying a template step, where no graph exists. An unguarded call fails a
    durable job because a tool tried to narrate.
    """
    try:
        return get_stream_writer()
    except (RuntimeError, LookupError, AttributeError):
        return None


def record_job_started(job_id: str, kind: str) -> None:
    """Note that `kind` job `job_id` was launched. A no-op where nothing is streaming."""
    _emit(JobSignal(job_id=job_id, kind=kind))


def record_proposal(note_id: str, reference: str) -> None:
    """Note that a note was proposed through the PR-gate. A no-op where nothing is streaming."""
    _emit(ProposalSignal(note_id=note_id, reference=reference))


def record_question(question: str, options: list[str]) -> None:
    """Note that the agent asked the chemist to disambiguate. A no-op where nothing streams."""
    _emit(QuestionSignal(question=question, options=options))


def record_tool_failure(tool: str, message: str, call_id: str = "") -> None:
    """Note that `tool` failed, by raising or by answering. A no-op where nothing is streaming."""
    _emit(ToolFailureSignal(tool=tool, message=message, call_id=call_id))
