"""The front door's HTTP request/response shapes, plus the pure projections that fill them.

These models are the wire contract a browser programs against, kept apart from the routes so a shape
change (API compatibility) and a route change (behaviour) review separately. Nothing here touches
`app.state`, the database or Temporal: `_transcript` and its helpers are pure projections, so tests
drive them without an app. `content_address` is just a hash; whether a ref is still fetchable is a
set the route reads and passes in.
"""

from collections.abc import Collection, Sequence
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, computed_field, field_validator

from chemclaw.agent.session_store import (
    TurnStatus,
    stored_authorship,
    stored_correlation_id,
    stored_turn_status,
)
from chemclaw.agent.tool_result_size import full_result_ref, stored_result_ref, was_cut
from chemclaw.api.tool_results import content_address
from chemclaw.core.authorship import Authorship
from chemclaw.core.config import settings
from chemclaw.core.result_handle import without_handle_line
from chemclaw.exhibits.models import ExhibitRef

# How much of a tool's arguments or result the transcript carries — the audit trail's bound, so a
# reload never ships a whole evidence sweep per call.
_TRANSCRIPT_ARG_CHARS = 400

# How much of the opening message becomes the session's name; generous, so each client truncates to
# its own width.
_TITLE_CHARS = 120


class MessageIn(BaseModel):
    """One turn's user message posted to the messages endpoint."""

    message: str
    # Plan the turn without launching anything expensive: "what would you do, what would it cost".
    dry_run: bool = False
    # Artefacts the chemist points at in this message. Each is resolved within the session before
    # the turn starts (unknown is a 422) and copied, framed and bounded, into the turn's note
    # (`agent/exhibit_notes`).
    exhibit_refs: list[ExhibitRef] = Field(default_factory=list)

    @field_validator("exhibit_refs")
    @classmethod
    def _few_refs(cls, value: list[ExhibitRef]) -> list[ExhibitRef]:
        """Reject more references than `exhibit_max_refs`, read at validation time like the cap."""
        if len(value) > settings.exhibit_max_refs:
            raise ValueError(f"at most {settings.exhibit_max_refs} artefacts may be referenced")
        return value

    @field_validator("message")
    @classmethod
    def _bounded(cls, value: str) -> str:
        """Reject a message past the configured cap — a clean 422, not an unbounded read.

        Read from `settings` at validation time rather than a frozen `Field(max_length=…)`, so the
        cap is configurable per deployment.
        """
        if len(value) > settings.service_max_message_chars:
            raise ValueError(f"message exceeds the {settings.service_max_message_chars}-char limit")
        return value


class SessionIn(BaseModel):
    """Options for a new session; all optional, so a bodyless `POST /sessions` still works."""

    # Which configured agent this conversation talks to (`agents.profile_discovery`); `None` is the
    # default profile, so a client that sends no body is unaffected.
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
    # Null when the session was never named, as distinct from named with an empty string.
    title: str | None = None


class SessionMemberOut(BaseModel):
    """One person the owner has let into a session, and since when."""

    actor: str
    added_at: datetime


class SessionMembersOut(BaseModel):
    """Who may reach a session: its owner, and the members that owner admitted.

    `owner` is `None` for a session with no recorded owner, which can have no members — nobody holds
    the standing to have admitted them (`agent/session_members.participant_permits`).
    """

    owner: str | None
    members: list[SessionMemberOut]


class SharedSessionSummary(BaseModel):
    """A session somebody else owns that the caller has been let into — `GET /sessions/shared`.

    `owner` and `title` are `None` under the in-process session store, which keeps memberships and
    no conversation list.
    """

    session_id: str
    owner: str | None = None
    title: str | None = None
    added_at: datetime


class QueuedMessageOut(BaseModel):
    """One message waiting in a session's line — `GET /sessions/{id}/queue`.

    No text: the line holds the order and never the message
    (`D-2026-10-01-a-queued-message-waits-in-its-senders-request`), which lives in the sender's own
    waiting request until it runs. `position` is how many are ahead (0 = next); `mine` is whether
    the caller sent it — the one they may withdraw with `DELETE /sessions/{id}/queue/{ticket}`,
    beside the owner, who may withdraw any.
    """

    ticket: int
    sender: str
    enqueued_at: datetime
    position: int
    mine: bool


class SessionQueueOut(BaseModel):
    """A session's line, first in line first, and whether a turn is running ahead of it here.

    `running` is this replica's view: the turn's pump lives in one process, so another replica
    answers `false` for a turn it cannot see — the same scope `POST /sessions/{id}/turn/stop` has.
    """

    running: bool
    waiting: list[QueuedMessageOut]


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

    `result_cut` is `ToolResultEvent.result_cut` recovered from the stored message, with the same
    meaning: the model was shown a cut of this result, and `result_ref` (when set) opens the full
    text the tool returned rather than the cut — `result` stays the model's text, like the stream's
    `preview` does.
    """

    tool: str
    arguments: str = ""
    result: str | None = None
    result_ref: str = ""
    result_cut: bool = False


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
    # The turn that stored this message, so a detached client recovers that turn's answer by
    # identity. `None` for rows stored off the request path or before the column. Optional and
    # additive.
    correlation_id: str | None = None
    # Who wrote this message: the person it was written for and the agent that wrote it (`agent`
    # null for the chemist's own words; `core/authorship.py`). `None` when neither is recorded.
    # Optional and additive.
    author: Authorship | None = None
    # How the turn this question opened has ended so far: `running`, then `done`, `failed`,
    # `stopped`, or `interrupted` when its process died. `None` on every other message and on older
    # questions. Optional and additive.
    turn_status: TurnStatus | None = None


class PlanDecisionIn(BaseModel):
    """The human Yes/No on a harness plan, bound to the exact plan that was shown.

    `plan_hash` is required and is not defaulted to "whatever the plan is now": the whole point of
    the binding is that a plan which changed after being displayed is a different plan. A client
    posts back the hash it received with the plan.
    """

    approved: bool
    plan_hash: str


class WorkflowApprovalIn(BaseModel):
    """A person's Yes to the durable jobs one composed workflow may launch, bound to its version.

    `fingerprint` is required and is not defaulted to "whatever the workflow is now", for the
    reason `PlanDecisionIn.plan_hash` is not: the binding is the whole control. A workflow that
    changed after being displayed is a different procedure, and approving it because it happens to
    share a name is approving something nobody read.
    """

    fingerprint: str = Field(min_length=1)


class WorkflowSummaryOut(BaseModel):
    """One composed workflow in a listing: enough to choose one, not enough to approve it."""

    name: str
    summary: str = ""
    step_count: int = 0
    # The steps that cost compute, so a listing shows what needs a decision without a request per
    # row.
    job_steps: list[str] = Field(default_factory=list)
    # Whether this version's job steps may run; derived, so a document changed after approval is not
    # approved.
    approved: bool = False


class WorkflowListOut(BaseModel):
    """A caller's own composed workflows, most recently changed first."""

    workflows: list[WorkflowSummaryOut] = Field(default_factory=list)
    # Says this is a page clamped at `composed.MAX_PER_OWNER`, not the whole set.
    truncated: bool = False


class WorkflowStepOut(BaseModel):
    """One step of a composed workflow, as the person approving it needs to see it.

    Ids alone are what this used to be, and they are not a procedure: a person shown
    `["rank", "say"]` and asked to authorize real compute has approved a name the model chose.
    That is `D-2026-09-12-an-approval-that-names-no-tool-authorizes-every-tool` one layer over —
    an approval that names no job authorizes every job — and it contradicted the sentence the
    widening rests on, that "the job's name and its arguments are in the document they approved".
    """

    id: str
    kind: str
    # The tool or job this step calls (empty for a reasoning step) — what the run will actually
    # invoke.
    calls: str = ""
    # The arguments as written, `${…}` references and all.
    arguments: dict[str, Any] = Field(default_factory=dict)
    # An agent step's prompt, where the model's judgment enters the procedure.
    prompt: str = ""


class WorkflowApprovalOut(BaseModel):
    """What a person is being asked to approve: the procedure, not a list of names."""

    name: str
    summary: str = ""
    description: str = ""
    # The whole procedure in order, each step naming what it calls and with what.
    steps: list[WorkflowStepOut] = Field(default_factory=list)
    # The ids of the subset that costs compute — what approving this actually releases.
    job_steps: list[str] = Field(default_factory=list)
    # Whether this version's job steps may run. Derived: `approved_by`/`approved_at` describe the
    # version `approved_fingerprint` names, which a re-composed document no longer is, so a client
    # must not infer approval from them.
    approved: bool = False
    # The conversation this procedure was composed in, so an approver can find what asked for it.
    # Empty off the service path.
    composed_in_session: str = ""
    # Who approved the `approved_fingerprint` version, and when.
    approved_at: datetime | None = None
    # What to post back; a client that posts a version it did not show gets a 409.
    fingerprint: str = ""
    approved_fingerprint: str = ""
    approved_by: str = ""


class PendingRequestOut(BaseModel):
    """One held-open question, as an inbox renders it.

    **Narrower than the stored record, and that is the shape rather than an omission.** It is built
    from `durable.pending_store.PendingRequest` by `**model_dump()`, and it used to restate that
    record's `answered_at`, `answered_by` and `answer` too — three fields the only route that builds
    this model cannot ever fill: `pending_store.open_requests` is `WHERE state = 'waiting'` in SQL,
    so an answered row never reaches here. A response field that is structurally always empty is not
    a quiet feature a client might one day read, it is boilerplate, and a surface that does serve
    answered rows will need to say what an answer *is* — whose payload it carries, who may see it —
    which is a decision to take then rather than a default to inherit now.

    `reminders` stays, and it is the one field here that earns its place by saying something the
    rest cannot: on a waiting row it separates "asked an hour ago" from "asked on Tuesday and
    chased three times", which is the difference between an inbox and a list.
    """

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
    created_at: str = ""


class PendingRequestsOut(BaseModel):
    """One page of what is waiting on this caller, soonest deadline first.

    `count` is the length of `requests` rather than a total, and the list is bounded by the store.
    An inbox that said "12" over five rows would be describing a page as a population.

    **That was honest to a code reader and silent on the wire**, which is the same defect one
    remove: the docstring reasoned carefully about the distinction and the JSON carried only the
    page, so a client with 35 waiting rows rendered 20 as the whole inbox with nothing to say
    otherwise. `total_routed_to_you` and `truncated` are that reasoning made into fields.

    `total_routed_to_you` is counted over the store's **routing** predicate, before separation of
    duties is applied — so it can exceed `count` for two different reasons, and `verdict` says
    both: rows the page did not reach, and rows this caller may not answer (an approval they
    raised themselves). Not counted post-gate, because the gate turns on the *kind* and the
    requester, which no SQL predicate here expresses.
    """

    requests: list[PendingRequestOut] = Field(default_factory=list)
    count: int = 0
    # Everything matching this caller's routing, before the page bound and before the gate.
    total_routed_to_you: int = 0
    # Whether waiting rows exist that this page did not carry.
    truncated: bool = False

    @computed_field  # type: ignore[prop-decorator]
    @property
    def verdict(self) -> str:
        """What this page is, in one sentence a client can render above the list."""
        if self.truncated:
            return (
                f"PARTIAL: {self.count} shown of {self.total_routed_to_you} routed to you, "
                "soonest deadline first. The rest are still waiting — ask for a larger `limit`."
            )
        if self.total_routed_to_you > self.count:
            return (
                f"COMPLETE: every request you may answer is shown. "
                f"{self.total_routed_to_you - self.count} further request(s) are routed to you "
                "but not yours to answer (you raised them)."
            )
        if not self.count:
            return "NOTHING WAITING: no open request is routed to you."
        return "COMPLETE: every request waiting on you is shown."


class PendingAnswerIn(BaseModel):
    """The answer to a held-open question.

    Carries no actor. The answering identity is the authenticated principal and is stamped by the
    route — a body-supplied name would be a caller writing their own attribution into a record the
    workflow persists.
    """

    payload: dict[str, Any] = Field(default_factory=dict)


class PlanStatusOut(BaseModel):
    """The plan a session is currently proposing, its hash, and who (if anyone) approved it.

    `scope` is what approving it would authorize: every tool the plan's steps declare
    (`D-2026-09-12-an-approval-that-names-no-tool-authorizes-every-tool`). It belongs in the same
    payload as the steps because the gate enforces it — a state-changing tool no step declared is
    refused even under a live approval — so a surface that showed the steps alone would be asking
    a person to approve a thing it had not shown them.
    """

    session_id: str
    plan_hash: str
    plan: list[str]
    scope: list[str] = []
    mode: str
    approved: bool
    decided_by: str | None = None
    # Whose turn last wrote this plan — the one person who may decide on it. `None` when unrecorded,
    # in which case the session's owner decides.
    author: str | None = None


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
    # What approving this plan would authorize; see `PlanStatusOut.scope`.
    scope: list[str] = []
    # The session owner's actor id — the caller's own, or somebody else's for a shared session — so
    # a surface can open a shared one as shared. `None` where no owner is kept. Defaulted, so
    # additive.
    owner: str | None = None


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
    # Whether the listing walk stopped short of the caller's history. Defaulted, so additive.
    truncated: bool = False


def session_title(message: str) -> str:
    """A session's name, from the message that opened it.

    Derived from the plain message string, not the stored serialization, which the store must not
    interpret. Collapsed and bounded, never paraphrased: a paraphrase can be wrong.
    """
    return " ".join(message.split())[:_TITLE_CHARS]


def _transcript(
    stored: "Sequence[Any]", *, fetchable: "Collection[str]" = ()
) -> list[TranscriptMessage]:
    """Flatten stored messages into the transcript contract, pairing calls with their results.

    Results arrive in a later message than their call, so pairing (on `tool_call_id`) takes a pass
    over the whole transcript first. Plan snapshots and attachment references are never persisted,
    so they are not recovered here.

    A result's ref is computed, not looked up: it is the SHA-256 of the result's text as
    `message_text` flattens it, the same flattening `api/graph_stream.py` hashed when storing, so
    the pairing is identity of bytes. `fetchable` (`tool_results.fetchable_refs`) is passed in,
    keeping this pure and the database read to one per transcript; a ref outside it is reported as
    `""`.
    """
    results: dict[str, tuple[str, str, bool]] = {}
    for message in stored:
        call_id = getattr(message, "tool_call_id", None)
        if not call_id:
            continue
        # `message_text` is the flattening `graph_stream` hashed, so the computed ref matches a
        # stored blob. An empty result gets no ref. A cut or stamped result names its stored bytes
        # by the stamp (`FULL_RESULT_REF_KEY` or the handle stamp), since the row holds the model's
        # cut or a trailing handle line; the hash is the fallback for unstamped rows. The handle
        # line is removed before display.
        text = without_handle_line(message_text(message))
        ref = (
            full_result_ref(message)
            or stored_result_ref(message)
            or (content_address(text) if text else "")
        )
        results[str(call_id)] = (
            _truncate_for_transcript(text),
            ref if ref in fetchable else "",
            was_cut(message),
        )
    transcript: list[TranscriptMessage] = []
    for index, message in enumerate(stored):
        calls: list[TranscriptToolCall] = []
        for call in getattr(message, "tool_calls", None) or []:
            paired = results.get(str(call.get("id", "")))
            result, ref, cut = paired if paired is not None else (None, "", False)
            calls.append(
                TranscriptToolCall(
                    tool=str(call.get("name", "")),
                    arguments=_truncate_for_transcript(call.get("args", "")),
                    result=result,
                    result_ref=ref,
                    result_cut=cut,
                )
            )
        # A tool message's result is already attached to its call; a bubble of its own would show it
        # twice.
        role = message_role(message)
        if role == "tool" and not calls:
            continue
        transcript.append(
            TranscriptMessage(
                index=index,
                role=role,
                text=message_text(message),
                tool_calls=calls,
                correlation_id=stored_correlation_id(message),
                author=stored_authorship(message),
                turn_status=stored_turn_status(message),
            )
        )
    return transcript


# LangChain's message `type` to the role names surfaces already render.
_ROLES = {"human": "user", "ai": "assistant"}


def message_role(message: Any) -> str:
    """The word a human reads for who said this, from LangChain's `type`.

    Public because `chemclaw.cli.explain` renders the same conversation and must name roles the same
    way.
    """
    return str(_ROLES.get(message.type, message.type))


def message_text(message: Any) -> str:
    """The prose of one message, whether its content is a string or a list of blocks.

    Public and the single implementation: `api/graph_stream.py` hashes its output to name a stored
    result, so a second flattening would produce refs that cannot be fetched. Blocks without `text`
    contribute nothing.
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
