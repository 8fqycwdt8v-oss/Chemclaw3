"""Side-channel for things a tool learns that the turn's event stream must surface (gaps RCH-4/5).

`service/events.py` has carried `JobStartedEvent` since F2 and the chat UI has rendered it since
F2-T2, but nothing ever emitted one: a tool that launches a durable job returns a job id *into the
model's context*, and the runner — which only sees the model's streamed updates — has no way to know
a job started. The same is true of a PR-gate proposal: `propose_note` opens a branch and returns a
reference to the model, so the chemist never learns their contribution landed (the GxP "human signs
off" line lived only in a git host's UI, disconnected from the conversation that produced it).

A contextvar is the right carrier, for exactly the reasons `chemclaw.agent.session_context` gives:
it is
task-local (concurrent turns cannot see each other's signals), it defaults to empty off the request
path, and it keeps the information out of the *model-facing* tool signature — the model must not be
able to fabricate "a job started" or "a note was proposed".

The runner drains this after each streamed update, so signals surface in the order they happened,
interleaved with the tokens and tool calls around them.

**One sink, not one per kind.** A second mechanism (`agents/job_events.py`, carrying job ids only)
was built independently and folded in here rather than kept beside this one: two contextvar sinks
drained separately leave the *relative order* of a launched job and a proposed note undefined,
which is precisely what a transcript must get right. Its four caller-facing names survived the
fold as aliases and were removed in D-149 — three had never had a caller, and the fourth discarded
the `kind` this module's whole point is to carry.
"""

from contextvars import ContextVar

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


class ApprovalSignal(BaseModel):
    """A durable approval hold a tool opened during this turn (gap RCH-3).

    `ApprovalRequestEvent` has carried an `approval_id` field — documented as "the durable hold's
    handle, so a surface can actually answer it via `POST /approvals/{id}/decision`" — since the
    hold was built, but nothing ever populated it: `start_approval` returns the id *into the
    model's context*, and the runner only sees the model's streamed updates. So every approval
    event reached the UI with an empty handle, and a surface could render the request but not the
    button that answers it, which is the whole point of the hold.

    Carried as a turn signal for the same reason as `JobSignal`: the id must come from the tool
    that opened the hold, not from anything the model can author.
    """

    prompt: str
    approval_id: str


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


Signal = JobSignal | ProposalSignal | QuestionSignal | ApprovalSignal | ToolFailureSignal


# One buffer per turn, holding every kind in the order they occurred. A single list (rather than one
# per kind) keeps ordering across kinds, which is what a transcript needs.
_signals: ContextVar[list[Signal] | None] = ContextVar("chemclaw_turn_signals", default=None)


def begin_turn() -> object:
    """Start a fresh signal buffer for a turn; returns a token for `end_turn`."""
    return _signals.set([])


def end_turn(token: object) -> None:
    """Tear the turn's buffer down (mirrors every other ambient's reset)."""
    _signals.reset(token)  # type: ignore[arg-type]


def record_job_started(job_id: str, kind: str) -> None:
    """Note that `kind` job `job_id` was launched. A no-op off the request path (CLI, tests)."""
    buffer = _signals.get()
    if buffer is not None:
        buffer.append(JobSignal(job_id=job_id, kind=kind))


def record_proposal(note_id: str, reference: str) -> None:
    """Note that a note was proposed through the PR-gate. A no-op off the request path."""
    buffer = _signals.get()
    if buffer is not None:
        buffer.append(ProposalSignal(note_id=note_id, reference=reference))


def record_question(question: str, options: list[str]) -> None:
    """Note that the agent asked the chemist to disambiguate. A no-op off the request path."""
    buffer = _signals.get()
    if buffer is not None:
        buffer.append(QuestionSignal(question=question, options=options))


def record_approval_request(prompt: str, approval_id: str) -> None:
    """Note that a durable approval hold was opened. A no-op off the request path."""
    buffer = _signals.get()
    if buffer is not None:
        buffer.append(ApprovalSignal(prompt=prompt, approval_id=approval_id))


def record_tool_failure(tool: str, message: str) -> None:
    """Note that `tool` raised. A no-op off the request path (CLI, tests)."""
    buffer = _signals.get()
    if buffer is not None:
        buffer.append(ToolFailureSignal(tool=tool, message=message))


def drain() -> list[Signal]:
    """Take and clear everything recorded since the last drain (empty off the request path)."""
    buffer = _signals.get()
    if not buffer:
        return []
    taken = list(buffer)
    buffer.clear()
    return taken
