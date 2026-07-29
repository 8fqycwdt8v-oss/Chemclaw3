"""The turn event contract (plan step F2-T3): one typed schema every surface shares.

A turn does not just return a final string — the experience is watching the agent *work*: its plan,
its tool calls, streamed tokens, a launched async job, an approval prompt, then the answer. Modeling
these as a discriminated union (on `type`) means the web UI now — and Slack/mobile later — render
the same events instead of each parsing a bespoke stream. The runner emits these; the app serializes
each as one SSE `data:` line via `model_dump_json()`.
"""

from typing import Literal

from pydantic import BaseModel


class PlanEvent(BaseModel):
    """The agent's current plan/todo list (harness mode) — rendered as a checklist."""

    type: Literal["plan"] = "plan"
    todos: list[str]


class ToolCallEvent(BaseModel):
    """A single tool invocation in the turn's trace (name + a short argument preview)."""

    type: Literal["tool_call"] = "tool_call"
    tool: str
    arguments: str = ""


class TokenEvent(BaseModel):
    """One streamed chunk of the assistant's answer text."""

    type: Literal["token"] = "token"
    text: str


class JobStartedEvent(BaseModel):
    """An async (Temporal/HPC/BO) job was launched; the UI shows "job started (id …)"."""

    type: Literal["job_started"] = "job_started"
    job_id: str
    # What kind of durable job this is ("qm", "report", "campaign"), so a surface can label it
    # without parsing the id. Defaulted so the field is additive for any existing consumer.
    kind: str = "job"


class JobCompletedEvent(BaseModel):
    """An async job finished and pushed its result back to the session (F3-T3, no polling)."""

    type: Literal["job_completed"] = "job_completed"
    job_id: str
    summary: dict[str, object] = {}


class QuestionEvent(BaseModel):
    """The agent needs the chemist to disambiguate before it can answer well (gap AGT-5).

    `_INSTRUCTIONS` tells the agent to "say plainly when the data is silent", but there was no
    contract for it to *ask*. An ambiguous question ("what did we get on the Suzuki?") therefore
    produced a best-guess sweep across every matching campaign — both worse and more expensive than
    asking which one. `options` are concrete choices when the agent can enumerate them, so a
    surface can render buttons instead of free text.
    """

    type: Literal["question"] = "question"
    question: str
    options: list[str] = []


class NoteProposedEvent(BaseModel):
    """A note was opened on a branch for human review through the PR-gate (gap RCH-4).

    The GxP "AI proposes, human signs off" line is the architecture's spine, but it lived only in
    a git host's UI: `propose_note` returned its reference into the model's context and the chemist
    never learned their contribution landed. This carries the branch reference back to the surface
    that produced it.
    """

    type: Literal["note_proposed"] = "note_proposed"
    note_id: str
    reference: str


class CapabilityDegradedEvent(BaseModel):
    """One or more connectors did not come up, so this turn answers with fewer tools (REV-6).

    The turn is not failed by this and must not be: an unreachable connector costs its tools, not
    the conversation. But the chemist has no other way to tell. The model does not know a tool is
    missing — it reasons from the surface it was given — so an answer assembled without the ELN
    reads exactly like one assembled with it, and "the ELN says nothing about that batch" and "the
    ELN was unreachable" arrive as the same sentence. This event is the difference between them.

    Emitted before the first token, so a surface can mark the answer as partial while it streams
    rather than retroactively.
    """

    type: Literal["capability_degraded"] = "capability_degraded"
    connectors: list[str]


class ApprovalRequestEvent(BaseModel):
    """The turn is waiting on a human decision (plan approval or an interaction approval)."""

    type: Literal["approval_request"] = "approval_request"
    prompt: str
    # The durable hold's handle (`InteractionApprovalWorkflow` id), so a surface can actually
    # answer it via `POST /approvals/{id}/decision` (gap RCH-3). Empty for a plan-approval
    # prompt, which is answered by the next turn rather than by a durable hold.
    approval_id: str = ""


class AnswerEvent(BaseModel):
    """The turn's final assembled answer (the complete text, after the token stream).

    When answer verification is enabled (plan F10-B), `confidence` carries the verifier's aggregate
    citation-faithfulness score in [0, 1] and `unsupported_claims` lists the claim texts the
    evidence did not support. `review_required` is the routing signal: it is `True` exactly when
    `confidence < verifier_confidence_threshold`, so a thin UI shows a review affordance (and a
    future durable hold, D-032, keys off the same flag) only on a genuinely low-confidence answer.
    All three stay `None`/`False`/empty on the verifier-off path, so the event is byte-for-byte
    today's answer unless verification is switched on.
    """

    type: Literal["answer"] = "answer"
    text: str
    confidence: float | None = None
    unsupported_claims: list[str] = []
    review_required: bool = False


class ToolFailedEvent(BaseModel):
    """One tool call raised. The turn continues — this is a step that did not work, not a failure.

    Distinct from `ErrorEvent`, which ends the turn. A tool can fail and the answer still be good
    (the model routes around it), or the answer can go thin without the turn ever erroring — and
    in that second case this is the only event that says why (D-138).
    """

    type: Literal["tool_failed"] = "tool_failed"
    tool: str
    message: str


class ErrorEvent(BaseModel):
    """The turn failed; the message is safe to show the user (no stack traces)."""

    type: Literal["error"] = "error"
    message: str


# The closed set of events a turn can emit. New surfaces switch on `type`; adding an event is a new
# class here plus one branch in the runner and the UI — never a bespoke per-surface stream.
Event = (
    PlanEvent
    | ToolCallEvent
    | TokenEvent
    | JobStartedEvent
    | JobCompletedEvent
    | CapabilityDegradedEvent
    | ApprovalRequestEvent
    | NoteProposedEvent
    | QuestionEvent
    | AnswerEvent
    | ToolFailedEvent
    | ErrorEvent
)
