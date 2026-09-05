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

# How much of the opening message becomes the session's name. Sized so a client can truncate to
# whatever its sidebar is wide enough for — a server that pre-truncated to 40 would have thrown away
# what a wider surface wanted, and nothing downstream can put it back.
_TITLE_CHARS = 120


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
    """One of the caller's sessions, for the conversation list.

    `session_id` and `created_at` were the whole of this, and a sidebar cannot be built from them:
    there is no name to show and no way to order by recency. The companion UI worked around it by
    labelling every restored conversation with the same placeholder and renaming it only once the
    chemist opened it and its transcript came back — so ten restored conversations were ten
    identical rows until nine of them had been clicked.

    `updated_at` is the last stored message, not this row's `created_at`, which is when the session
    was *started* — the difference between "what have I been working on" and "what did I once open".
    """

    session_id: str
    created_at: datetime
    updated_at: datetime
    # Null for a session whose first turn predates this field, so a client can tell "never named"
    # from "named with an empty string" — only one of those is a bug worth reporting.
    title: str | None = None


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

    Role plus text rather than the stored row's own shape: that row is a library serialization,
    and exposing it would make a dependency bump a breaking change to the HTTP contract.

    **`tool_calls` is the part that was missing, and it was never missing from storage.** The live
    SSE stream carries fourteen event types; a reload got `role` and `text`, so everything the
    agent *did* vanished and a UI could not render history at parity with the live view — the
    largest single blocker for the frontend repo. But a stored message already holds its
    `tool_calls` and the `tool_call_id` answering them; the route was flattening them away. Nothing
    new is persisted here: this reads what was always there.

    `index` is the message's position in the transcript, so a client has a stable key without the
    HTTP contract having to expose a database row id.
    """

    index: int = 0
    role: str
    text: str
    tool_calls: list[TranscriptToolCall] = []


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
    signing off on the bytes, and a paraphrase is the one thing a review must not be given.

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


class PendingRequestOut(BaseModel):
    """One held-open question, as an inbox renders it."""

    request_id: str
    kind: str
    subject: str
    rationale: str = ""
    asked_of: str = ""
    requested_by: str = ""
    session_id: str = ""
    state: str = "waiting"
    due_at: str = ""
    reminders: int = 0
    answered_at: str = ""
    answered_by: str = ""
    answer: dict[str, Any] = Field(default_factory=dict)
    created_at: str = ""


class PendingRequestsOut(BaseModel):
    """Everything waiting on this caller, soonest deadline first.

    `count` is the length of `requests` rather than a total, and the list is bounded by the store.
    An inbox that said "12" over five rows would be describing a page as a population.
    """

    requests: list[PendingRequestOut] = Field(default_factory=list)
    count: int = 0


class PendingAnswerIn(BaseModel):
    """The answer to a held-open question.

    Carries no actor. The answering identity is the authenticated principal and is stamped by the
    route — a body-supplied name would be a caller writing their own attribution into a record the
    workflow persists.
    """

    payload: dict[str, Any] = Field(default_factory=dict)


class PlanStatusOut(BaseModel):
    """The plan a session is currently proposing, its hash, and who (if anyone) approved it."""

    session_id: str
    plan_hash: str
    plan: list[str]
    mode: str
    approved: bool
    decided_by: str | None = None


class PendingPlan(BaseModel):
    """One session whose plan is waiting for a first human decision.

    Carries the conversation's identity as well as the plan, because the inbox is read outside the
    conversation that raised it: a chemist who closed the tab has the session id nowhere else, and
    `title`/`updated_at` are what makes a row recognisable as "the impurity question from Tuesday"
    rather than a hex string.

    `plan_hash` is *not* here to be posted back. A decision is bound to the plan as displayed, and
    the decision route re-reads the plan and 409s a stale hash — so a hash carried from a listing
    that was rendered ten minutes ago would buy nothing but a race with the agent. It is here for
    the same reason `PlanStatusOut` carries it: two rows showing the same steps under different
    hashes are two different plans, and a surface that cannot tell them apart cannot say so.
    """

    session_id: str
    title: str | None
    updated_at: datetime
    plan_hash: str
    plan: list[str]


class PendingPlansOut(BaseModel):
    """The caller's undecided plans, with what the scan actually covered.

    **The three counts are the point, and an empty `plans` is why.** A list with no rows has three
    different meanings and a surface that cannot separate them shows a confident emptiness it has
    not earned — the failure the companion UI recorded when a deleted `GET /approvals` 404 was
    swallowed into `[]` and rendered as "nothing is waiting on you":

    - `gated == 0` — no session of the caller's runs a plan-gated profile, so this deployment has
      no plan gate to be waiting on. Nothing can ever appear here, and a surface should say that
      rather than imply an empty queue.
    - `gated > 0` and `unread == 0` — every plan that could be waiting was read. The queue is
      genuinely empty.
    - `unread > 0` — the scan hit `service_max_plan_scans` (or could not reach the checkpointer),
      so this answer is partial and the sessions it did not reach are the *older* ones.
    - `truncated` — the *listing* walk stopped before it ran out of sessions, so there are older
      conversations this answer never even classified. A fourth meaning of an empty `plans`, and
      the one `unread` cannot carry: `unread` counts gated sessions that went unread, and a walk
      that stops early has not learned whether the rows beyond it are gated at all. Folding it
      into `unread` would invent plans that may not exist.
    """

    plans: list[PendingPlan]
    # Sessions in the caller's listing — the same set and the same cap `GET /sessions` returns.
    considered: int
    # Of those, the ones that can hold a plan at all: a harness-enabled profile.
    gated: int
    # Gated sessions whose plan was not read, so `plans` is short by an unknown amount.
    unread: int
    # Whether the listing walk itself stopped short of the caller's history (see above). Defaulted
    # so it is additive on the wire: a surface that does not know the field reads the same answer
    # it read before, and one that does can say "older conversations were not checked".
    truncated: bool = False


def session_title(message: str) -> str:
    """A session's name, from the message that opened it.

    Here, in the pure-projections half of this module, because that is what it is: the turn route
    hands over the user's message as a plain string and gets back the string to store. Deriving it
    from the *stored* message instead would mean interpreting the serialization in
    `session_messages`, which `infra/sql/008_sessions.sql` is explicit the store must not do.

    Collapsed and bounded, not summarised. A title that paraphrases is a title that can be wrong,
    and this one names a row a chemist navigates by. The cap is generous — enough that a surface can
    truncate to its own width without the server having pre-truncated to a narrower one, which is
    the mistake that cannot be undone downstream.
    """
    return " ".join(message.split())[:_TITLE_CHARS]


def _transcript(
    stored: "Sequence[Any]", *, fetchable: "Collection[str]" = ()
) -> list[TranscriptMessage]:
    """Flatten stored messages into the transcript contract, pairing calls with their results.

    Results arrive in a *later* message than the call they answer — an assistant message carries
    `tool_calls` and a following `ToolMessage` carries the answer — so pairing needs one pass over
    the whole transcript before any message can be rendered. `tool_call_id` is the join.

    **What this recovers, and what it cannot.** Tool calls and their outcomes were always in
    storage and merely discarded by the route, so they come back for free. Plan snapshots,
    attachment references are **never persisted** — they are turn-time events computed and
    streamed, and nothing writes them to `session_messages`. The answer's
    `confidence`/`review_required` are the one line of this that changed: `turn_costs` has kept
    them since migration 082, keyed by *correlation id*, so they are recoverable for a turn and
    still not for a message. This reader is per-message and joins on nothing that would reach that
    row. Recovering any of it here is a change to what a turn *stores*, not to how it is read, so
    it is a separate decision rather than something this can quietly approximate.

    **The ref is computed here, not looked up.** A stored result's handle is the SHA-256 of the
    result's own text (`api/tool_results.py::content_address`), and the text is sitting in the
    message this is reading. The two sides agree by construction rather than by coincidence:
    `api/graph_stream.py` hashes what `message_text` returns for the same `ToolMessage`, and the
    durable row is that message's JSON round trip — so the read side is not reimplementing the
    write side's flattening, it is calling it. That makes the pairing *identity of bytes* rather
    than a guess from `(session, tool, correlation_id, created_at)`: those four cannot separate two
    calls of one tool in one turn, and a link row's timestamp is the last time those bytes were
    produced by anything, which is not a key at all. A mispaired result would be worse than an
    absent one, and content addressing is the reason there is no pairing step to get wrong.

    `fetchable` is the set of refs the store can serve for this session
    (`tool_results.fetchable_refs`), and a computed ref outside it is reported as `""`. Passed in
    rather than queried here so this stays a pure projection the tests can drive without an app,
    and so the one database read happens once per transcript rather than once per tool call.
    """
    results: dict[str, tuple[str, str]] = {}
    for message in stored:
        call_id = getattr(message, "tool_call_id", None)
        if not call_id:
            continue
        # `message_text`, not the raw attribute: it is the same flattening `graph_stream` hashed
        # when the turn ran, which is the whole reason the computed ref matches a stored blob. A
        # result that came back empty gets no ref, matching `runner_trace._result_text`, which
        # declines to store one — so the two agree on which results are fetchable.
        text = message_text(message)
        ref = content_address(text) if text else ""
        results[str(call_id)] = (
            _truncate_for_transcript(text),
            ref if ref in fetchable else "",
        )
    transcript: list[TranscriptMessage] = []
    for index, message in enumerate(stored):
        calls: list[TranscriptToolCall] = []
        for call in getattr(message, "tool_calls", None) or []:
            paired = results.get(str(call.get("id", "")))
            result, ref = paired if paired is not None else (None, "")
            calls.append(
                TranscriptToolCall(
                    tool=str(call.get("name", "")),
                    arguments=_truncate_for_transcript(call.get("args", "")),
                    result=result,
                    result_ref=ref,
                )
            )
        # A tool message is the carrier for a result that has already been attached to its call,
        # so surfacing it as its own bubble would render every tool twice.
        role = message_role(message)
        if role == "tool" and not calls:
            continue
        transcript.append(
            TranscriptMessage(index=index, role=role, text=message_text(message), tool_calls=calls)
        )
    return transcript


# LangChain's message `type` to the role the transcript contract names. The two agree except for
# `human`/`ai`, and the contract's names are the ones a surface already renders — changing them
# would be a UI break for a rename.
_ROLES = {"human": "user", "ai": "assistant"}


def message_role(message: Any) -> str:
    """The word a human reads for who said this, from LangChain's `type`.

    Public because `chemclaw.cli.explain` renders the same conversation for the audit join and must
    call it the same thing: a transcript that says `assistant` in the browser and `ai` in the audit
    reconstruction makes two records of one turn look like two turns.
    """
    return str(_ROLES.get(message.type, message.type))


def message_text(message: Any) -> str:
    """The prose of one message, whether its content is a string or a list of blocks.

    Public for two readers beyond this module, and the second one makes it load-bearing rather than
    convenient: `chemclaw.cli.explain` renders the same rows, and `api/graph_stream.py` hashes what
    this returns to name a stored tool result. A second implementation of the flattening would mean
    a ref computed on read that no longer matches the one computed on write — a result that exists
    and cannot be fetched.

    Blocks carrying no `text` (an image, a tool-use block) contribute nothing rather than a `repr`.
    """
    content = message.content
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            str(part.get("text", "")) if isinstance(part, dict) else str(part) for part in content
        )
    return str(content)


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
