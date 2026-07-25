"""Side-channel for things a tool learns that the turn's event stream must surface (gaps RCH-4/5).

`service/events.py` has carried `JobStartedEvent` since F2 and the chat UI has rendered it since
F2-T2, but nothing ever emitted one: a tool that launches a durable job returns a job id *into the
model's context*, and the runner — which only sees the model's streamed updates — has no way to know
a job started. The same is true of a PR-gate proposal: `propose_note` opens a branch and returns a
reference to the model, so the chemist never learns their contribution landed (the GxP "human signs
off" line lived only in a git host's UI, disconnected from the conversation that produced it).

A contextvar is the right carrier, for exactly the reasons `agents.session_context` gives: it is
task-local (concurrent turns cannot see each other's signals), it defaults to empty off the request
path, and it keeps the information out of the *model-facing* tool signature — the model must not be
able to fabricate "a job started" or "a note was proposed".

The runner drains this after each streamed update, so signals surface in the order they happened,
interleaved with the tokens and tool calls around them.
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


# One buffer per turn, holding both kinds in the order they occurred. A single list (rather than one
# per kind) keeps ordering across kinds, which is what a transcript needs.
_signals: ContextVar[list[JobSignal | ProposalSignal | QuestionSignal] | None] = ContextVar(
    "chemclaw_turn_signals", default=None
)


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


def drain() -> list[JobSignal | ProposalSignal | QuestionSignal]:
    """Take and clear everything recorded since the last drain (empty off the request path)."""
    buffer = _signals.get()
    if not buffer:
        return []
    taken = list(buffer)
    buffer.clear()
    return taken
