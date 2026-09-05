"""The turn event contract (plan step F2-T3): one typed schema every surface shares.

A turn does not just return a final string — the experience is watching the agent *work*: its plan,
its tool calls, streamed tokens, a launched async job, an approval prompt, then the answer. Modeling
these as a discriminated union (on `type`) means the web UI now — and Slack/mobile later — render
the same events instead of each parsing a bespoke stream. The runner emits these; the app serializes
each as one SSE `data:` line via `model_dump_json()`.
"""

from typing import Literal

from pydantic import BaseModel, Field

from chemclaw.core.turn_signals import RefusalReason


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
    """The agent's current plan/todo list (harness mode) — rendered as a checklist.

    **`plan_hash` is here because without it this event cannot be acted on.**
    `POST /sessions/{id}/plan/decision` requires the hash of the exact plan the human was shown —
    that binding is the whole of D-167's fix, so a plan which changed after being displayed cannot
    be approved by a decision aimed at the old one. A client watching the stream had the todo list
    and not the hash, so the only way to answer the plan it had just rendered was a second
    `GET /sessions/{id}/plan` round trip — which races the very change the binding exists to catch:
    between the render and the fetch the agent may have revised the plan, and the client would then
    post back a hash for a plan its user never saw.

    Produced by `plan_gate.plan_identity`, the same function the gate consults and the decision
    route records against. Not a second hashing rule: an approval valid under one spelling and
    unrecognised under the other would be a *durable* row that outlives the turn that wrote it.

    Always a non-empty string here, and that is a property of the emitter rather than of this model
    — `plan_identity` returns `None` for an empty plan (hashing "nothing" yields a constant every
    session in every deployment also proposes), and `graph_stream` does not emit an empty plan.
    """

    type: Literal["plan"] = "plan"
    todos: list[str]
    # Defaulted so a stored or replayed event from before this field cannot fail to parse. An empty
    # string means "this event predates the hash", which a client must treat as "fetch it" rather
    # than as a hash — never as one that will match.
    plan_hash: str = ""


# Which agent raised an event, when it was not the one the chemist is talking to (M9).
#
# **Empty means the main agent**, and that is what keeps the field additive: every event emitted
# before teams existed came from the single agent, so an existing consumer that ignores this reads
# exactly what it read before. A specialist's name is its profile name (`evidence`, `safety`, …),
# which is the same string the `handoff` event carries and the same one the audit trail records —
# one name for one actor, across the stream, the trail and the profile that defined it.
#
# Only the events a specialist can actually raise carry it. A `queued` or `capability_degraded`
# event is a property of the *turn*, decided before any routing happens, so attributing it to an
# agent would be inventing a fact.
_AGENT_FIELD = Field(
    default="",
    description="The specialist that raised this event; empty for the main agent.",
)


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
    agent: str = _AGENT_FIELD


class TokenEvent(BaseModel):
    """One streamed chunk of assistant text: the answer, or a specialist's working prose.

    **The attribution is load-bearing here in a way it is not on the other events.** `api/runner`
    concatenates this stream into the turn's final answer and into the durable transcript, so an
    unattributed specialist chunk is not a mislabelled trace line — it is another agent's working
    notes spliced into the answer a chemist reads, interleaved with the supervisor's own text in
    whatever order the two happened to produce it. The runner therefore concatenates only the
    unattributed ones, and a surface rendering a timeline still sees a specialist's output land
    inside its handoff span.

    Dropping a specialist's tokens outright was the other candidate and is worse: it makes the
    delegation silent for the entire time it runs, which is the longest part of a delegated turn.
    """

    type: Literal["token"] = "token"
    text: str
    agent: str = _AGENT_FIELD


class JobStartedEvent(BaseModel):
    """An async (Temporal calc/BO) job was launched; the UI shows "job started (id …)"."""

    type: Literal["job_started"] = "job_started"
    job_id: str
    # What kind of durable job this is ("calc", "report", "campaign"), so a surface can label it
    # without parsing the id. Defaulted so the field is additive for any existing consumer.
    kind: str = "job"
    # The plan step the launch served — the todo's bare content, so a surface can badge the
    # matching checklist item (D-2026-08-27). Empty when the job was not launched from a plan
    # step. Defaulted so the field is additive for any existing consumer.
    plan_step: str = ""


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

    The "agent proposes, human decides" line is the architecture's spine, but it lived only in
    a git host's UI: `propose_note` returned its reference into the model's context and the chemist
    never learned their contribution landed. This carries the branch reference back to the surface
    that produced it.
    """

    type: Literal["note_proposed"] = "note_proposed"
    note_id: str
    reference: str


class ApprovalRequestEvent(BaseModel):
    """The turn ended holding a plan a human has not approved, so a surface can offer the decision.

    **Only the plan shape exists.** `approval_id` is always empty and the field is kept because it
    is what says so: the event once documented a second shape carrying the handle of a durable
    interaction hold (`POST /approvals/{id}/decision`, D-032), and that whole surface was deleted in
    `D-2026-08-27-a-hold-nothing-can-open-is-not-a-hold` because nothing in `src/` could ever open
    one. A plan approval is answered by `POST /sessions/{id}/plan/decision` and enforced by
    `agent.plan_gate`, not by a hold — the two were never the same mechanism, and collapsing them
    is what let a producerless feature look live for as long as it did.
    """

    type: Literal["approval_request"] = "approval_request"
    prompt: str
    #: Always `""`. See the class docstring: a non-empty value would name a hold that cannot exist.
    approval_id: str = ""


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
    # Whether an independent review panel agreed on a stated objection to this answer
    # by an in-graph review gate, after the model had spent its revision budget trying to answer
    # them. Distinct from `review_required`, which any of the three checks can raise on its own: a
    # confidence score under a threshold and a quorum of agents that each went and checked are
    # different weights of evidence, and a surface that renders them identically is throwing away
    # the more expensive one.
    challenged: bool = False
    # **Both this and `challenged` above are permanently at their defaults** since the challenge
    # panel was removed (D-2026-08-15). They stay declared because removing a member of this union
    # is a coordinated change across `Chemclaw3_ui` and `Chemclaw3_mock`, and they go in the same
    # cut that moves the transcript route off `session_messages`.
    review_hold_id: str | None = None


class ToolFailedEvent(BaseModel):
    """One tool call raised. The turn continues — this is a step that did not work, not a failure.

    Distinct from `ErrorEvent`, which ends the turn. A tool can fail and the answer still be good
    (the model routes around it), or the answer can go thin without the turn ever erroring — and
    in that second case this is the only event that says why (D-138).
    """

    type: Literal["tool_failed"] = "tool_failed"
    tool: str
    message: str
    agent: str = _AGENT_FIELD
    # Which *kind* of failure this is, where the kind is a decision someone made rather than a
    # fault. A refusal is the control working, and a consumer that folds it in with a database
    # outage reports a correctly-gated turn as a broken one — which is what `evals/live.py` did, by
    # matching one phrase of the refusal *sentence*, so a reword would have flipped the finding
    # with every test still green.
    #
    # **All five gates, not one.** This said `plan_gate` alone while `agent/audit.refusal_reason`
    # already classified five, so the other four — a dry-run refusal the chemist themselves asked
    # for, a role denial, a write no narrowed agent was given, a repeat the guard stopped — reached
    # every surface indistinguishable from an unreachable pod. The set here IS that table's
    # vocabulary — imported from `core.turn_signals`, not restated, so the two cannot drift.
    #
    # `None` is "an ordinary failure", which is every failure that was ever emitted before this
    # field existed. Additive, defaulted and a closed set, because this shape is a contract two
    # other repositories read (`Chemclaw3_ui`, `Chemclaw3_mock`): a surface that ignores it is
    # unchanged, and one that switches on it can be exhaustive.
    reason: RefusalReason | None = None


class ResultValue(BaseModel):
    """One number a structured tool result returned, under the name the tool gave it.

    `numbers` beside this is a bare list, and it stays one: it feeds a grounding check that asks
    "did a tool in this turn return this figure?", where a label is irrelevant and a missed value
    is a false accusation. This feeds a *surface*, where the opposite is true — the entity rail
    could only ever say "predict_pka returned 4.76, 1.6", because pairing an unlabelled pair into
    "pKa 4.76 ± 1.6" would invent a relationship the tool never stated.

    So the label is the payload's own key path and the unit is the payload's own `unit`, or empty.
    Nothing here is prettified or inferred; `chemclaw.core.quantities.labelled_values` is where
    that rule is enforced and argued.

    Only for a result that parses as JSON. One that does not carries `numbers` and no `values`,
    which is the honest report: the figures are known, their names are not.
    """

    label: str
    value: float
    unit: str = ""


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

    `result_ref` is the third thing the same split produces, and it is what closes it. `note_ids`
    and `numbers` let a scorer check *ids* and *figures* against the full result; they cannot give
    a surface the result's **shape**, so a `ScreenResult`'s severities and citations, a
    `ChargeTable`'s rows and a solvent ranking still reached the chemist as prose about them.
    This carries a reference to the stored full text — fetched from
    `GET /sessions/{id}/tool-results/{ref}` — instead of the payload, so the wire budget the preview
    exists to keep is untouched: a surface pulls the one result it decided to render, once, rather
    than every result being streamed to every consumer.

    `values` is `numbers` with the names the tool filed them under, for the surfaces that *display*
    a figure rather than check one. See `ResultValue`: the two coexist because a grounding check
    wants every value and no names, and a value strip wants names and refuses to guess them.

    `result_inline` is the small-result shortcut, and it exists because the split above is a rule
    about *large* results applied to every result. A 300-byte ICH limit or a two-field pKa costs a
    second round trip to be rendered as anything but prose, and that round trip buys nothing: the
    payload is smaller than the preview's own budget several times over. Under
    `stream_inline_result_bytes` the text rides along and a surface renders immediately; over it,
    the field is empty and the ref is how the result is reached, exactly as before. The cap is the
    control — this is not a way to stream a 40-chunk evidence sweep to a browser, and the default
    is set well below where that becomes possible.

    **Empty means "not stored", and it is one meaning with three causes** — the store is off
    (`stream_max_result_bytes` at 0), the result was over that cap, or the write failed. A consumer
    has exactly one thing to check, and none of the three ever costs the turn its answer: storing a
    trace blob is a rendering, and no rendering is worth failing a turn over
    (`chemclaw.api.tool_results`).
    """

    type: Literal["tool_result"] = "tool_result"
    tool: str
    preview: str = ""
    note_ids: list[str] = Field(default_factory=list)
    numbers: list[float] = Field(default_factory=list)
    values: list[ResultValue] = Field(default_factory=list)
    result_ref: str = ""
    result_inline: str = ""
    agent: str = _AGENT_FIELD


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
    # The *process* had no admission permit within `service_turn_admission_timeout_seconds`, so
    # this turn was shed. Its own member rather than `budget_exhausted`, because the two are the
    # opposite instruction: this one says "we are busy, retry in a moment" (`retryable=True`) and
    # that one says "your budget is spent, stop retrying until an operator raises the cap"
    # (`retryable=False`). They shared a code, so a surface switching on it — the documented way to
    # choose the next step — had to fall back to `retryable` or to matching the prose, and a retry
    # loop keyed on the code either hammered a saturated pod or gave up on a transient one.
    "at_capacity",
    "loop_cap_reached",
    # `loop_cap_reached`'s sibling in the other unit: the turn was inside its iteration ceiling and
    # reached its billed-token ceiling instead. Its own code rather than `budget_exhausted`,
    # because the two say different things to a surface — `budget_exhausted` is a *session* or
    # *user* refused before the turn started and has no answer with it, while this one stops a turn
    # mid-flight and its partial answer still arrives.
    "spend_cap_reached",
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

    Four of the codes say the turn was *cut off* rather than broken — `turn_timeout`,
    `budget_exhausted`, `loop_cap_reached` and `spend_cap_reached` — and they are one family: the
    turn ran into a guard, so whatever it had said is all it is going to say. The last two are the
    members that share their turn with an answer: a runaway guard — of iterations or of spend —
    stops a turn that has been streaming text all along, so it arrives after those tokens and
    *before* the `AnswerEvent` they add up to, the same "mark the answer partial while it is still
    arriving" ordering `CapabilityDegradedEvent` uses (see `chemclaw.api.runner`).
    `budget_exhausted` is not one of them despite naming a budget: it refuses a turn *before* it
    starts, so there is nothing to mark partial.

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


class EvidenceSourceEvent(BaseModel):
    """One retrieval source reported what it contributed to a sweep (M10).

    `gather_evidence` asks every configured source at once and merges what comes back, and in the
    merged list a source that returned nothing is indistinguishable from a source nobody asked.
    That is not hypothetical: `D-2026-08-01-a-cap-that-starves-a-source` is a defect in which one
    leg contributed **zero** surviving chunks while the sweep looked healthy in aggregate, it went
    unnoticed until someone counted by hand, and both competing explanations for it turned out to
    be wrong. This is the per-branch arithmetic, emitted while the sweep runs.

    `chunks` is what the source *found*, before the cross-source cap — so a surface can tell "this
    source had nothing to say" from "this source was crowded out of the budget", which are
    different problems with different fixes.

    `failed` is the third of those, and it was missing: a branch whose retriever raises degrades to
    an empty list, so it reported `chunks=0` and became indistinguishable from a source that was
    simply asked and had nothing — the same collapse this event exists to undo, one level down.
    The distinction matters because the remedies do not overlap: a dark source is a question about
    the corpus, a broken one is a page for whoever owns the index. Additive and defaulted, because
    this shape is a contract two other repositories read (`Chemclaw3_ui`, `Chemclaw3_mock`); a
    surface that ignores it renders exactly what it rendered before.

    Emitted only where a consumer is draining the graph's custom stream — the front door is, the
    CLI and a Temporal activity are not. Those still run the same branches and record the same
    counter; they simply have no channel to say so. A surface must therefore treat the absence of
    these as "not reported", never as "no sources".
    """

    type: Literal["evidence_source"] = "evidence_source"
    source: str
    chunks: int
    failed: bool = False


class HandoffEvent(BaseModel):
    """The turn was routed to a specialist, or handed back (M9).

    A team's work is only legible if the routing is. Without this, a chemist watching a turn sees
    a supervisor fall silent and a different set of tools start running, with nothing saying why —
    and the durable record has the same gap, because the trace *is* the record. That is the whole
    argument for a supervisor over a swarm (the subagent ADR of 2026-08-10): every delegation
    decision passes through one node and is therefore observable.

    `to` is the specialist being entered, or empty when control returns to the main agent — so a
    surface can render a turn's routing as a path rather than as a set of disconnected arrivals.
    `reason` is the supervisor's own stated reason where it gave one; it is prose for a human and
    nothing branches on it.

    **Nothing raises it.** It was raised by `agent/team.running_specialist`, the contextmanager
    that bracketed the interval the audit trail attributed to a specialist, so the span a surface
    drew and the span the record claimed were one `try`/`finally`. That module went with the
    specialist team (D-2026-08-15). It shipped for one release as a declared member nothing produced
    (`D-2026-08-11-a-handoff-is-observable-where-the-specialist-runs`).

    Like every other signal-borne event, emitted only where a consumer is draining the graph's
    custom stream: absence means "not reported", never "no delegation happened".

    **Nothing produces it today** — the specialist team was deleted in D-2026-08-15 — and it is kept
    declared rather than removed because dropping a member of this union is a coordinated change
    across `Chemclaw3_ui` and `Chemclaw3_mock`. It is the one member whose absence is expected to
    be temporary: subagents return for isolation and parallel fan-out, and this is what they raise.
    """

    type: Literal["handoff"] = "handoff"
    to: str
    reason: str = ""


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
    | NoteProposedEvent
    | ApprovalRequestEvent
    | QuestionEvent
    | AnswerEvent
    | ToolFailedEvent
    | ToolResultEvent
    | EvidenceSourceEvent
    | HandoffEvent
    | ErrorEvent
)
