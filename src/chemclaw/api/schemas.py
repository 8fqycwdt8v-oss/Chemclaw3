"""The front door's HTTP request/response shapes, plus the pure projections that fill them.

These models are the wire contract a browser (or the companion UI repo) programs against, kept
apart from the routes that serve them (R3.2) because a shape change is an API-compatibility
decision while a route change is a behavior one — a reviewer should see each kind of diff on its
own. Nothing here touches `app.state`, the database or Temporal: the two functions beside the
models (`_transcript`, `_proposal_summary`) are pure projections from stored records onto these
shapes, which is what lets `tests/test_jobs_api.py` drive them without an app.

`content_address` is imported for the same reason and is no exception to it: it is `hashlib` over a
string, and the *decision* it feeds — whether a past tool call's full result is still fetchable —
is a set of refs the route reads and passes in. Naming a result and finding out whether it still
exists are two questions, and only the second one needs a database.
"""

from collections.abc import Collection, Sequence
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator

from chemclaw.api.tool_results import content_address
from chemclaw.core.config import settings
from chemclaw.kg.proposal import NoteProposal

# How much of a tool's arguments or result the transcript carries. The same bound the audit trail
# applies for the same reason: a tool argument can be a whole optimization problem or an evidence
# sweep, and a reload must not ship one per call.
_TRANSCRIPT_ARG_CHARS = 400


class MessageIn(BaseModel):
    """One turn's user message posted to the messages endpoint."""

    message: str
    # Plan the turn without launching anything expensive (gap IDEA-4). Every expensive path is
    # idempotent and cached, but there was no way to ask "what would you do, what would it cost"
    # without doing it — a natural primitive for a deployment whose default autonomy is
    # `plan_only`.
    dry_run: bool = False

    @field_validator("message")
    @classmethod
    def _bounded(cls, value: str) -> str:
        """Reject a message past the configured cap (SEC-4) — a clean 422, not an unbounded read.

        Read from `settings` at validation time (not as a frozen `Field(max_length=…)`) so the cap
        is genuinely config-driven and adjustable per deployment.
        """
        if len(value) > settings.service_max_message_chars:
            raise ValueError(f"message exceeds the {settings.service_max_message_chars}-char limit")
        return value


class SessionIn(BaseModel):
    """Options for a new session; all optional, so a bodyless `POST /sessions` still works."""

    # Which configured agent this conversation talks to (`agents.profile_discovery`). `None` is
    # the default profile — today's global agent — so an existing client that sends no body is
    # unaffected.
    profile: str | None = None


class SessionOut(BaseModel):
    """The identifier of a freshly created session."""

    session_id: str


class SessionSummary(BaseModel):
    """One of the caller's sessions, for the conversation list."""

    session_id: str
    created_at: datetime


class TranscriptToolCall(BaseModel):
    """One tool the agent invoked during a turn, as the transcript remembers it.

    The same pair the live stream reports as `ToolCallEvent` + `ToolResultEvent`, recovered from
    storage. `result` is `None` while the pairing is incomplete — a turn that failed mid-call, or a
    call whose result row was pruned — which is a real state a surface should render as "this ran
    and we do not know how it ended", not as a success with an empty answer.

    `result_ref` is the same handle `ToolResultEvent.result_ref` carries on the live stream, and it
    is here because without it a reload was the one path on which a result stopped being reachable.
    `result` is 400 characters — the same "prose about the data" the preview was, which is what
    `D-2026-08-09-a-preview-is-not-a-result` exists to stop being the only thing a surface can
    render — so a chemist coming back to a conversation could see *that* `screen_hazards` ran and
    never what it found, while the full text sat in `tool_result_blobs`. It resolves through
    `GET /sessions/{id}/tool-results/{ref}`, the same route the live stream's ref resolves through
    (`D-2026-08-09-a-derivable-ref-is-not-a-fetchable-one`).

    **The three states are distinct on purpose**, and the middle one is the one retention creates:

    - `result is None` — the call has no result at all. It ran and nobody knows how it ended.
    - `result` set, `result_ref == ""` — there is a result, and only these 400 characters of it.
      The bytes were never stored (the store is off, the result was over `stream_max_result_bytes`,
      the write failed) **or** they were stored and retention has since swept them. A surface
      renders the text it has and offers no link.
    - `result` set, `result_ref` non-empty — the full text is fetchable now.

    "Swept" and "never stored" are deliberately *not* separated. Both mean the same thing to the
    only consumer that acts on this — there is nothing to fetch — and telling them apart would
    mean keeping a tombstone per expired blob, which is a durable record of a rendering, on the one
    table in the schema that grows per tool call.
    """

    tool: str
    arguments: str = ""
    result: str | None = None
    result_ref: str = ""


class TranscriptMessage(BaseModel):
    """One stored message of a session's transcript, as a chat surface renders it.

    Role plus text rather than the MAF `Message` shape: the durable row is a MAF serialization, and
    exposing it would make a MAF version bump a breaking change to the HTTP contract.

    **`tool_calls` is the part that was missing, and it was never missing from storage.** The live
    SSE stream carries fourteen event types; a reload got `role` and `text`, so everything the
    agent *did* vanished and a UI could not render history at parity with the live view — the
    largest single blocker for the frontend repo. But a MAF message already holds
    `function_call`/`function_result` contents; the route was flattening them away. Nothing new is
    persisted here: this reads what was always there.

    `index` is the message's position in the transcript, so a client has a stable key without the
    HTTP contract having to expose a database row id.
    """

    index: int = 0
    role: str
    text: str
    tool_calls: list[TranscriptToolCall] = []


class ApprovalDecisionIn(BaseModel):
    """The human Yes/No posted to a pending approval hold."""

    approved: bool


class ApprovalStatusOut(BaseModel):
    """A hold's handle and current state, for a polling review surface."""

    approval_id: str
    status: str


class ProposalSummary(BaseModel):
    """One note proposal as the review queue shows it — everything but the note body.

    A second shape rather than the whole `NoteProposal`, for the reason `JobRecordSummary` is one:
    a queue may hold dozens of rows and a rendered note is a document, so handing them all back
    would spend a page of transfer to answer "what is waiting for me". The body is one lookup away
    by id once a proposal is worth opening.
    """

    id: int
    note_id: str
    note_type: str
    state: str
    branch: str
    reference: str
    actor: str
    submitted_at: datetime | None
    decided_at: datetime | None
    decided_by: str
    reason: str


class ProposalFile(BaseModel):
    """One further file the submission would write beside its subject note."""

    path: str
    content: str


class ProposalDetail(ProposalSummary):
    """A proposal with everything it would write, exactly as it would land in the tree.

    The bytes rather than a summary of them: a reviewer signing off on machine-written knowledge is
    signing off on the bytes, and a paraphrase is the one thing a GxP review must not be given.

    `dependencies` is the rest of the submission — the `compound` note a `job-result` cites, say —
    and it is here because without it the sentence above was false for exactly the submissions that
    need review most. A note and the notes its links depend on are one reviewable unit (D-133);
    showing one file of it invited a reviewer to approve a link they could not see the far end of.
    """

    content: str
    dependencies: list[ProposalFile] = Field(default_factory=list)
    session_id: str
    correlation_id: str


class ProposalDecisionIn(BaseModel):
    """The human decision on one open proposal, with the reason it went that way.

    `reason` is required on a rejection and optional on a merge, because "why was this refused" is
    the question a rejected proposal exists to answer — before this table there was no record of a
    rejection at all, and a record that says only "no" would reproduce that gap one level up.
    """

    approved: bool
    reason: str = ""


class KnowledgeMergedIn(BaseModel):
    """The notes a git host reports as merged, so their proposals can be closed.

    Optional: an operator calling this by hand to force a reindex still may, and an empty list
    keeps exactly the pre-existing behaviour (rebuild the index, decide nothing).
    """

    note_ids: list[str] = []


class PlanDecisionIn(BaseModel):
    """The human Yes/No on a harness plan, bound to the exact plan that was shown.

    `plan_hash` is required and is not defaulted to "whatever the plan is now": the whole point of
    the binding is that a plan which changed after being displayed is a different plan. A client
    posts back the hash it received with the plan.
    """

    approved: bool
    plan_hash: str


class PlanStatusOut(BaseModel):
    """The plan a session is currently proposing, its hash, and who (if anyone) approved it."""

    session_id: str
    plan_hash: str
    plan: list[str]
    mode: str
    approved: bool
    decided_by: str | None = None


def _transcript(
    stored: "Sequence[Any]", *, fetchable: "Collection[str]" = ()
) -> list[TranscriptMessage]:
    """Flatten stored MAF messages into the transcript contract, pairing calls with their results.

    Results arrive in a *later* message than the call they answer — MAF emits the assistant's
    `function_call` and then a `tool` message carrying the `function_result` — so pairing needs one
    pass over the whole transcript before any message can be rendered. `call_id` is the join.

    **What this recovers, and what it cannot.** Tool calls and their outcomes were always in
    storage and merely discarded by the route, so they come back for free. Plan snapshots,
    attachment references and the answer's `confidence`/`review_required` were **never persisted**
    — they are turn-time events computed and streamed, and nothing writes them to
    `session_messages`. Recovering those is a change to what a turn *stores*, not to how it is
    read, so it is a separate decision rather than something this can quietly approximate.

    **The ref is computed here, not looked up.** A stored result's handle is the SHA-256 of the
    result's own text (`api/tool_results.py::content_address`), and the text is sitting in the
    message this is reading — the same string `api/runner_trace.py::_result_text` hashed when the
    turn ran, because MAF coerces a function result to `str` once, at the content, and the durable
    row is that content's JSON round trip. So the pairing is *identity of bytes*, not a guess from
    `(session, tool, correlation_id, created_at)`: those four cannot separate two calls of one tool
    in one turn, and a link row's timestamp is the last time those bytes were produced by anything,
    which is not a key at all. A mispaired result would be worse than an absent one, and content
    addressing is the reason there is no pairing step to get wrong.

    `fetchable` is the set of refs the store can serve for this session
    (`tool_results.fetchable_refs`), and a computed ref outside it is reported as `""`. Passed in
    rather than queried here so this stays a pure projection the tests can drive without an app,
    and so the one database read happens once per transcript rather than once per tool call.
    """
    results: dict[str, tuple[str, str]] = {}
    for message in stored:
        for content in getattr(message, "contents", []):
            if content.type == "function_result" and content.call_id is not None:
                # `or ""` rather than the raw attribute: MAF stores an absent result as the empty
                # string, and that is also what `_result_text` treats as "nothing came back" and
                # declines to store — so the two agree on which results have a ref.
                #
                # Coerced with `str()` before anything reads it, for the same reason
                # `_truncate_for_transcript` is defensive about the same value one line down. A
                # `result` that is not a `str` is unreachable through the MAF this pins — every
                # result goes through `Content.from_function_result`, which JSON-dumps a non-string
                # — but a *stored row* is not written by the running version: `get_messages`
                # rebuilds it with `Message.from_dict` from JSONB another version wrote, and a row
                # carrying `"result": {…}` reached `content_address` as a `dict` and raised
                # `AttributeError: 'dict' object has no attribute 'encode'`. That is one uncaught
                # exception on `GET /sessions/{id}/messages`, which is a 500 that costs a chemist
                # their entire conversation rather than one result card. `str()` and not `repr()`
                # because `_result_text` coerces the producer's side the same way, and the ref only
                # means anything if both sides hash the same bytes.
                raw = getattr(content, "result", "") or ""
                text = raw if isinstance(raw, str) else str(raw)
                ref = content_address(text) if text else ""
                results[content.call_id] = (
                    _truncate_for_transcript(text),
                    ref if ref in fetchable else "",
                )
    transcript: list[TranscriptMessage] = []
    for index, message in enumerate(stored):
        calls: list[TranscriptToolCall] = []
        for content in getattr(message, "contents", []):
            if content.type != "function_call":
                continue
            paired = results.get(content.call_id or "")
            result, ref = paired if paired is not None else (None, "")
            calls.append(
                TranscriptToolCall(
                    tool=content.name or "",
                    arguments=_truncate_for_transcript(getattr(content, "arguments", "")),
                    result=result,
                    result_ref=ref,
                )
            )
        # A `tool` message is the carrier for a result that has already been attached to its call,
        # so surfacing it as its own bubble would render every tool twice.
        if message.role == "tool" and not calls:
            continue
        transcript.append(
            TranscriptMessage(index=index, role=message.role, text=message.text, tool_calls=calls)
        )
    return transcript


def _truncate_for_transcript(value: object) -> str:
    """Render a tool argument or result as one bounded string (see `_TRANSCRIPT_ARG_CHARS`)."""
    text = value if isinstance(value, str) else repr(value)
    return text if len(text) <= _TRANSCRIPT_ARG_CHARS else text[:_TRANSCRIPT_ARG_CHARS] + "…"


def _proposal_summary(proposal: NoteProposal) -> ProposalSummary:
    """Project a stored proposal onto the listing shape."""
    return ProposalSummary(
        id=proposal.id,
        note_id=proposal.note_id,
        note_type=proposal.note_type,
        state=proposal.state.value,
        branch=proposal.branch,
        reference=proposal.reference,
        actor=proposal.actor,
        submitted_at=proposal.submitted_at,
        decided_at=proposal.decided_at,
        decided_by=proposal.decided_by,
        reason=proposal.reason,
    )
