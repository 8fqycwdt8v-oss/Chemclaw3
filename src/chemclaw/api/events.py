"""The turn event contract (plan step F2-T3): one typed schema every surface shares.

A turn does not just return a final string — the experience is watching the agent *work*: its plan,
its tool calls, streamed tokens, a launched async job, an approval prompt, then the answer. Modeling
these as a discriminated union (on `type`) means the web UI now — and Slack/mobile later — render
the same events instead of each parsing a bespoke stream. The runner emits these; the app serializes
each as one SSE `data:` line via `model_dump_json()`.
"""

from typing import Literal

from pydantic import BaseModel, Field


class QueuedEvent(BaseModel):
    """The turn was accepted but is waiting for a free admission permit (D-166).

    Admission control sheds rather than queues *indefinitely*, but it does wait — up to
    `service_turn_admission_timeout_seconds` — and that wait used to happen before the response
    existed, so a busy front door was indistinguishable from a dead one for its whole duration.
    This is the first event of a turn that had to wait, and only of such a turn: the common case
    takes its permit without blocking and never emits it.

    No fields. The client is already connected and has nothing to decide — the event's entire job
    is to say "accepted, waiting", and the next event says which way it went.
    """

    type: Literal["queued"] = "queued"


class PlanEvent(BaseModel):
    """The agent's current plan/todo list (harness mode) — rendered as a checklist."""

    type: Literal["plan"] = "plan"
    todos: list[str]


class ToolCallEvent(BaseModel):
    """A single tool invocation in the turn's trace (name + a short argument preview).

    Emitted when the call is *issued* — as soon as its arguments are complete — not when it
    returns (D-159). The difference is the whole dead-air window: an inline calc job waits up to
    `inline_wait_seconds` and an MCP tool up to its `request_timeout`, and until this moved, none
    of that was visible. A working twenty-second calculation and a hung server looked identical.
    """

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


class JobFailedEvent(BaseModel):
    """An async job failed after its turn ended, and the asker is told rather than left waiting.

    The counterpart `JobCompletedEvent` had no opposite, so a job that was announced as running and
    then failed produced exactly nothing on this channel — the promise stood indefinitely and the
    failure was reachable only by polling `get_durable_job_status` with an id the chemist would have
    had to keep. `reason` is the innermost message in Temporal's failure chain, because the outer
    ones say "Child Workflow execution failed" and the inner one says what actually went wrong.
    """

    type: Literal["job_failed"] = "job_failed"
    job_id: str
    reason: str = ""


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
    """A capability did not come up, so this turn answers with fewer tools (REV-6).

    The turn is not failed by this and must not be: an unreachable connector costs its tools, not
    the conversation. But the chemist has no other way to tell. The model does not know a tool is
    missing — it reasons from the surface it was given — so an answer assembled without the ELN
    reads exactly like one assembled with it, and "the ELN says nothing about that batch" and "the
    ELN was unreachable" arrive as the same sentence. This event is the difference between them.

    Emitted before the first token, so a surface can mark the answer as partial while it streams
    rather than retroactively.

    **`connectors` is not only connectors, and a reader must not assume the name resolves in the
    registry.** The durable execution layer rides in the same list as `durable-jobs (Temporal)`
    when the broker is unreachable — it is not a bundle, it is every bundle's jobs at once, but
    what a surface does with the name is identical (say this capability is missing this turn), and
    a second event type carrying one more unreachable capability would be a contract change for no
    additional meaning. The name is prefixed so it cannot be mistaken for a bundle.
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

    `review_required` is the routing signal a thin UI shows a review affordance on (and a future
    durable hold, D-032, keys off the same flag). **Two independent checks can raise it, each
    behind its own knob**, so it is not a function of `confidence` alone:

    - `verifier_enabled` (plan F10-B) — `confidence` carries the aggregate citation-faithfulness
      score in [0, 1] against what *this turn's tools returned*, `unsupported_claims` lists what
      the evidence did not support, and the flag is set when `confidence` falls below
      `verifier_confidence_threshold`.
    - `answer_shape_gate_enabled` — a deterministic scan for method-parameter shapes no tool in
      the turn produced. It sets the flag and appends what it matched to `unsupported_claims`,
      and leaves `confidence` at `None`: it found something or it did not, and that is not a
      score. So `review_required` can be `True` while `confidence is None`.

    A third condition sets the flag, and it is not a score: a verdict the LLM judge did not produce
    (`verified_by == "citation-gate"` while verification was on) means the check that earns
    confidence did not run, so the turn is routed to review with an explicit reason appended to
    `unsupported_claims` rather than a bare flag beside a high `confidence`.

    With every knob off — the default — the scored fields stay `None`/`False`/empty, `verified_by`
    stays `None`, and the event is byte-for-byte today's answer.
    """

    type: Literal["answer"] = "answer"
    text: str
    confidence: float | None = None
    unsupported_claims: list[str] = []
    review_required: bool = False
    # Which check produced `confidence`, when one did. `None` means verification was off.
    #
    # The routing flag alone cannot carry this: a degraded turn and a genuinely low-confidence turn
    # both arrive as `review_required=True`, and a reviewer needs to know whether the judge was
    # even reachable. It is on the wire because the surface is where "this was scored by the weaker
    # check" has to be legible; the flag is the safety property and this is the transparency.
    verified_by: Literal["judge", "citation-gate"] | None = None


class ToolFailedEvent(BaseModel):
    """One tool call raised. The turn continues — this is a step that did not work, not a failure.

    Distinct from `ErrorEvent`, which ends the turn. A tool can fail and the answer still be good
    (the model routes around it), or the answer can go thin without the turn ever erroring — and
    in that second case this is the only event that says why (D-138).
    """

    type: Literal["tool_failed"] = "tool_failed"
    tool: str
    message: str


class ToolResultEvent(BaseModel):
    """What a tool call returned, as data rather than as the model's paraphrase (D-159).

    The stream carried invocations only, so a computed number reached the chemist exclusively
    through whatever the model chose to say about it — and a turn that died after a successful
    twenty-second calculation lost the value entirely, with nothing on the wire to recover it
    from. `TracePanel` in the UI even documents that constraint as an honesty rule: it says what
    was called and never implies it is showing what came back. This is what lets that change.

    Success only. A call that raised already has `ToolFailedEvent`, which carries the reason;
    emitting both for one outcome would make every consumer decide which to believe. The pair is
    exhaustive — a call ends in exactly one of them.

    `preview` is truncated the same way `ToolCallEvent.arguments` is: enough to see the value,
    never a whole evidence sweep streamed to a browser.

    `note_ids` is the machine-readable half, and it is **not** truncated — because it answers a
    different question. A grounding check asks "was this id in front of the model this turn?", and
    scoring that against the preview meant scoring 40 retrieved chunks against the first 200
    characters of them: a live run graded 19 of 36 answers as fabrication and nine of nine checked
    verdicts were false, every one an id or a number the tool really had returned
    (`docs/archive/live-grounded-2026-08-03.md`). Widening the preview would have fixed the check
    by breaking the budget it exists to keep, so the two now coexist: prose for a human, ids for a
    scorer. Bounded by the notes one call can return, which is far smaller than their text.

    `numbers` sits beside `note_ids` for that same stated reason, one step further on. Fixing the
    ids left the *figures* unfixed, and the re-run said so in the grader's own words: "the tool
    results shown are truncated previews that do not display the numerical limits", written about
    the six ICH PDEs `ich_impurity_limit` had returned in full. So the same split is applied to the
    same event — prose for a human, values for a scorer — and a consumer can now ask "did a tool in
    this turn return this figure?" of something other than the first 200 characters.

    Deduplicated, and capped by the producer at `_MAX_RESULT_NUMBERS`: a result is arbitrary text
    and the event goes to a browser, so the list must be bounded. Truncation degrades in the safe
    direction — a dropped value can only cost a figure its verification, never manufacture an
    accusation — and the producer logs what it dropped, because a silent truncation reads as
    completeness. Measured on real results the cap is far out of reach: 5 values for an ICH
    lookup, 27 for a charge table, 49 for a full electronic-properties calculation, 36 for an
    18-chunk evidence sweep.
    """

    type: Literal["tool_result"] = "tool_result"
    tool: str
    preview: str = ""
    note_ids: list[str] = Field(default_factory=list)
    numbers: list[float] = Field(default_factory=list)


# The closed taxonomy. Each member is a *different thing for the user to do* — retry, wait, fix the
# input, ask an operator — not a different place the traceback came from, which is why it is this
# short. Named here beside the event rather than in the runner, because a surface switching on it
# needs the type as much as the producer does.
ErrorCode = Literal[
    "internal",
    "storage_unavailable",
    "llm_timeout",
    "turn_timeout",
    "budget_exhausted",
    "loop_cap_reached",
    "bad_tool_arguments",
    # The turn ran to completion and wrote nothing. Its own code rather than `internal`, because
    # nothing broke: the model simply never produced prose, and a surface should offer "ask
    # something narrower" rather than "an internal error occurred". Added after a live turn made
    # 29 tool calls over 197 s and emitted an empty answer with no error at all — the silent death
    # every live pass since 2026-07 has found, and the one shape a user cannot even report.
    "empty_answer",
]


class ErrorEvent(BaseModel):
    """The turn failed; the message is safe to show the user (no stack traces).

    Three of the codes say the turn was *cut off* rather than broken — `turn_timeout`,
    `budget_exhausted` and `loop_cap_reached` — and they are one family: the turn ran into a
    guard, so whatever it had said is all it is going to say. `loop_cap_reached` is the only
    member that shares its turn with an answer: the loop's runaway guard stops a turn that has
    been streaming text all along, so it arrives after those tokens and *before* the `AnswerEvent`
    they add up to — the same "mark the answer partial while it is still arriving" ordering
    `CapabilityDegradedEvent` uses (see `chemclaw.api.runner`).

    `code` and `retryable` exist because the message alone made every failure the same failure. A
    surface could not tell a connector being down from an LLM timeout from a database outage from
    a bad tool argument — so it could offer no useful next step, and "try again" was as likely to
    be wrong as right. The set is deliberately small and closed: each member is a *different thing
    for the user to do*, not a different place the traceback came from.

    `correlation_id` is the other half. The generic message named the session, which is the id the
    user already has; the correlation id is the one the **audit trail** is keyed on
    (D-2026-07-31-the-audit-chain-is-versioned), so quoting it in a bug report is what lets an
    operator find the turn. It is not sensitive — a random per-turn hex string.
    """

    type: Literal["error"] = "error"
    message: str
    # `internal` is the honest default: an unclassified failure is one nobody has decided the
    # user-facing meaning of yet, and guessing a friendlier code would be a worse answer than
    # admitting the classification is missing.
    code: ErrorCode = "internal"
    # Whether asking again, unchanged, could plausibly succeed. A transient outage is retryable; a
    # malformed tool argument is not, and telling a user to retry it wastes their time and the
    # deployment's tokens.
    retryable: bool = False
    correlation_id: str = ""


# The closed set of events a turn can emit. New surfaces switch on `type`; adding an event is a new
# class here plus one branch in the runner and the UI — never a bespoke per-surface stream.
Event = (
    QueuedEvent
    | PlanEvent
    | ToolCallEvent
    | TokenEvent
    | JobStartedEvent
    | JobCompletedEvent
    | JobFailedEvent
    | CapabilityDegradedEvent
    | ApprovalRequestEvent
    | NoteProposedEvent
    | QuestionEvent
    | AnswerEvent
    | ToolFailedEvent
    | ToolResultEvent
    | ErrorEvent
)
