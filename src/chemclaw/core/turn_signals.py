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


class QuestionSignal(BaseModel):
    """A disambiguation the agent asked for during this turn."""

    question: str
    options: list[str]


class ProposalSignal(BaseModel):
    """A note a tool proposed through the PR-gate during this turn."""

    note_id: str
    reference: str


#: The kinds of deliberate refusal a failing tool call can carry.
#:
#: **One definition, because three places have to agree and two of them are contracts.**
#: `agent/audit.refusal_reason` produces it from the exception, `ToolFailureSignal` carries it out
#: of the tool chain, and `api/events.ToolFailedEvent` puts it on the wire for `Chemclaw3_ui` and
#: `Chemclaw3_mock` to mirror. Written here rather than beside the event because `core` is the one
#: layer both the agent and the API may import — the alternative was a `cast` at the boundary,
#: which is a static-typing device standing in for the agreement this makes structural.
#:
#: Adding a gate means adding its reason here, which is what makes `refusal_reason`'s table and the
#: wire's closed set unable to drift apart.
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
    # The call this failure belongs to, so a consumer can match it to the `tool_call` event rather
    # than to the tool *name*. Additive and defaulted, because a signal shape is a contract two
    # other repositories read; empty means "not attributed", never "the first call to this tool".
    #
    # It exists because matching by name is wrong in the one case that matters: a model may issue
    # two calls to the same tool in a single batch, and suppressing the result of both because one
    # failed loses a real answer.
    call_id: str = ""
    # **Which gate refused this call, or empty for a genuine fault.** Exactly the vocabulary
    # `agent/audit.refusal_reason` already classifies — that table is the one place the five gates
    # are named, and this field is what carries its verdict out of the process.
    #
    # It is a field rather than something the consumer re-derives, and that is the correction. The
    # stream used to recover *one* of the five by testing whether `message` started with
    # `"PlanNotApprovedError:"`, on the argument that a new field here is "a third repository's
    # contract for a fact this side can already derive". That argument held while there was one
    # reason; at five it buys five copies of a class name living in a module that cannot see the
    # classes, checked against a string `failure_detail` truncates. The exception is in scope where
    # the signal is recorded (`agent/tool_authz.announce_tool_failures`), so the classification is
    # taken there, from the exception, by the table that already owns the question.
    #
    # `None` rather than a defaulted `str`, and still additive: a signal built without it is
    # exactly the ordinary fault every failure emitted before this field existed already was —
    # never "a refusal whose kind we could not work out". Typed as the closed set rather than as
    # `str` so a gate whose reason the wire cannot express fails here, in the change that added it.
    reason: RefusalReason | None = None


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
    """Note that `kind` job `job_id` was launched. A no-op where nothing is streaming.

    The plan step is folded in here rather than threaded through every launcher: the link is
    ambient by design (`core.plan_context`, bound per tool call by the harness's middleware), so
    reading it at the one place every launch announcement passes stamps all of them uniformly —
    the connector jobs, the report, the memory synthesis — and a caller outside the harness
    contributes the empty string without knowing the field exists.
    """
    plan_step, _ = get_current_plan_link()
    _emit(JobSignal(job_id=job_id, kind=kind, plan_step=plan_step))


def record_proposal(note_id: str, reference: str) -> None:
    """Note that a note was proposed through the PR-gate. A no-op where nothing is streaming."""
    _emit(ProposalSignal(note_id=note_id, reference=reference))


def record_question(question: str, options: list[str]) -> None:
    """Note that the agent asked the chemist to disambiguate. A no-op where nothing streams."""
    _emit(QuestionSignal(question=question, options=options))


def record_tool_failure(
    tool: str, message: str, call_id: str = "", reason: RefusalReason | None = None
) -> None:
    """Note that `tool` failed, by raising or by answering. A no-op where nothing is streaming.

    `reason` is `agent/audit.refusal_reason`'s verdict where the caller had an exception to
    classify, and `None` otherwise — a tool that *returns* its failure has no exception and so no
    gate to name: the gates refuse by raising.
    """
    _emit(ToolFailureSignal(tool=tool, message=message, call_id=call_id, reason=reason))
