"""Creating, listing and reading conversations — everything about a session except running it.

The turn stream itself is `chemclaw/api/routes/turns.py`; this module is the surrounding
lifecycle: mint a session (on a profile), list the caller's own, read a transcript back, attach a
working file, and discover which profiles exist. Every session-scoped route resolves ownership
through `chemclaw.api.deps` before doing anything (404 for a non-owner, no existence leak).
"""

import logging
import uuid

from fastapi import Depends, FastAPI, HTTPException, Request, Response, UploadFile

from chemclaw.agent.attachments import STORE as ATTACHMENTS
from chemclaw.agent.attachments import (
    AttachmentError,
    AttachmentSummary,
    AttachmentUnavailable,
    parse_attachment_off_loop,
)
from chemclaw.agent.profiles import get_profile, registered_profile_names
from chemclaw.agent.session import TurnSession
from chemclaw.agent.session_fork import SessionForkError, fork_session
from chemclaw.agent.session_store import SessionOwnerStore, encode_session_cursor
from chemclaw.api import app as front_door
from chemclaw.api.deps import CurrentSession, CurrentUser, resolve_session
from chemclaw.api.schemas import (
    SessionIn,
    SessionOut,
    SessionSummary,
    TranscriptMessage,
    _transcript,
)
from chemclaw.api.state import (
    _WORKER_ID,
    SessionOwners,
    SessionTurns,
    _claim_turn_slot,
    _release_turn_claim,
    _release_turn_slot,
    state,
)
from chemclaw.core.config import settings
from chemclaw.core.logging import log_event

logger = logging.getLogger(__name__)

# Where the next page's cursor is returned, because the body cannot carry it. `GET /sessions`
# answers with a bare JSON array — the companion UI parses it as one — so an envelope
# (`{"sessions": [...], "next": ...}`) would have broken every deployed client to add a field, and
# a per-row field would have to be added to `SessionSummary`, which is a shape shared with the
# transcript surface. A header is additive to both: a client that does not read it sees exactly
# what it saw before, and one that does can page.
#
# A bare cursor rather than RFC 8288's `Link: <url>; rel="next"`, deliberately: this service is
# reached through the companion UI's BFF, which maps `/api/sessions` onto `/sessions`, so any URL
# this process built would name a path the browser cannot use. The cursor is the part that is
# actually ours to state.
_NEXT_CURSOR = "X-Next-Cursor"


def _tombstone_owner() -> str:
    """An owner string a deleted session's cached handle can be parked under, matching nobody.

    Random per delete rather than one literal, so it cannot collide with a principal's `oid` in any
    deployment, and truthy so `chemclaw.api.deps._owner_authorizes` compares it rather than falling
    into the dev-mode "no recorded owner, so anyone" branch — an owner-less entry would make a
    deleted conversation *more* reachable than an ordinary one.
    """
    return f"deleted:{uuid.uuid4().hex}"


async def create_session(
    request: Request,
    principal: CurrentUser,
    body: SessionIn | None = None,
) -> SessionOut:
    """Start a new conversation session and return its id (requires an authenticated user).

    An optional `profile` picks which configured agent the session talks to — the selection
    step that makes a filesystem-authored profile reachable by a user instead of only by a
    redeploy. It is resolved here so an unknown name is a 400 at session creation rather
    than a 500 on the first turn, and it is fixed for the session's life: a conversation
    whose instructions and tools changed underneath it would have a thread that no longer
    matches its own history.
    """
    front = state(request)
    session_id = uuid.uuid4().hex
    profile = body.profile if body is not None else None
    if profile is not None:
        try:
            # Resolved here rather than left to the factory: whether a profile name exists
            # is a property of the registry, not of how this deployment builds agents, and a
            # test's injected factory must not be able to make an unknown name look valid.
            get_profile(profile)
        except ValueError as exc:  # a caller error, not a server fault
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    # Persist ownership first (durable path only), so the session reattaches after a restart
    # even if the pod dies before the first turn writes any history.
    if front.session_owners is not None:
        await front.session_owners.record(session_id, principal.oid, profile)
    front.live_sessions.add(session_id, TurnSession(session_id=session_id), principal.oid, profile)
    return SessionOut(session_id=session_id)


async def fork_session_route(
    request: Request,
    session_id: str,
    principal: CurrentUser,
    live: CurrentSession,
) -> SessionOut:
    """Branch this session onto a new one carrying its whole history, and return the new id.

    **The authorization is the `CurrentSession` dependency and nothing else here.** A fork reads
    every message of the parent and hands it to the caller under a new id, so it is exactly as
    sensitive as `GET /sessions/{id}/messages` — and `resolve_session` is the check that route
    already uses. Doing it that way rather than re-deriving ownership in the body is the point:
    `chemclaw.api.deps` refuses with 404 rather than 403, so a caller cannot use this endpoint to
    discover that a session id exists.

    **The fork inherits the parent's profile**, taken from the resolved live session rather than
    from the request. A profile only ever narrows, so accepting one from the caller would let a
    fork *widen* what the parent could do, and defaulting it would do the same silently — the
    argument `_rehydrate_session` makes for the same field.

    Durable only. With no session store there is no thread to copy and no ownership row to write, so
    the honest answer is that the deployment does not have the feature rather than a new empty
    session that looks like a fork.

    **A turn in flight refuses with 409, exactly as `delete_session` does and for the same reason.**
    A fork *reads* five of the parent's tables and writes them under a new id; at READ COMMITTED
    each statement in that transaction takes its own snapshot, so a turn committing partway through
    lands a checkpoint in the child whose blob rows were copied before it existed — the "resumes
    with holes" failure `agent/session_fork.py` opens by naming, reached through concurrency rather
    than through copying the tip. The forkability guard is exposed the same way: the parent's first
    transcript row can arrive between the count and the copy. So this route claims the session's
    turn slot the way `POST /sessions/{id}/messages` claims it — the in-process lease first, then
    the durable cross-process one — and gives both back in a `finally`, because a fork that held the
    slot would lock the parent out of its own next turn for a whole lease.
    """
    front = state(request)
    if front.session_owners is None:
        raise HTTPException(
            status_code=501,
            detail="forking needs a durable session store; this deployment has none configured",
        )
    # Nothing may sit between this claim and the `try` — the rule `delete_session` states: the
    # reservation has no expiry until `_start_turn_lease` starts one, which nothing here ever does,
    # so only that `finally` gives it back.
    slot = _claim_turn_slot(front.active_turns, session_id)
    if slot is None:
        raise HTTPException(
            status_code=409,
            detail="a turn is running on this session; stop it before forking the session",
        )
    claims: SessionTurns | None = front.turn_claims
    claimed = False
    try:
        if claims is not None:
            claimed = await claims.claim(
                session_id, _WORKER_ID, settings.service_turn_claim_lease_seconds
            )
            if not claimed:
                raise HTTPException(
                    status_code=409,
                    detail="a turn is running on this session; stop it before forking the session",
                )
        child_id = await fork_session(session_id, principal.oid, live.profile)
    except SessionForkError as exc:  # a caller error: nothing to fork from yet
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    finally:
        if claimed and claims is not None:
            # Through the shielded release for the reason D-130 gives — this `finally` also runs
            # when the caller is cancelled, and a bare `await` in a cancelled task raises at its
            # first suspension point, so the release would start on every abandoned fork and finish
            # on none.
            await _release_turn_claim(claims, session_id)
        _release_turn_slot(front.active_turns, session_id, slot)
    front.live_sessions.add(child_id, TurnSession(session_id=child_id), principal.oid, live.profile)
    log_event(
        logger,
        "session.forked",
        "session %s forked onto %s",
        session_id,
        child_id,
        session_id=session_id,
        forked_session_id=child_id,
    )
    return SessionOut(session_id=child_id)


async def list_sessions(
    request: Request,
    principal: CurrentUser,
    response: Response,
    after: str = "",
) -> list[SessionSummary]:
    """One page of the caller's own sessions, newest first — the conversation list.

    Without this a client that lost its local state (a new browser, cleared storage, a
    second device) could not find sessions it still owns: ids are minted server-side and
    returned once into the response that created them, so an id the client forgot was
    unreachable forever
    while its durable history sat in the store.

    Read from the durable ownership registry, which is the same record `resolve_session`
    authorizes against — so this can never list a session the caller would then be refused.
    Empty under the in-memory session store: there is no durable registry to enumerate, and
    reporting the process's live LRU instead would answer a question about the deployment
    with a partial, eviction-dependent guess.

    Ordered by last activity and carrying each session's name, because a list of ids and start
    dates is not a conversation list — see `SessionSummary`. Sessions that were created and never
    used are not listed at all; the query that establishes the last activity is the same one that
    establishes there was any.

    **`service_max_listed_sessions` is now the page, not the end of the list.** It always bounded
    the answer, and nothing said so: a chemist with more conversations than the cap simply could
    not reach the older ones from any client. `after` resumes strictly after the row a cursor
    names, and `X-Next-Cursor` on a full page says there may be more — absent means there is not.
    The cursor is a keyset, not an offset, because this list reorders itself while it is read (see
    `encode_session_cursor`); a page boundary counted in rows would skip the conversations that
    moved down and repeat the ones that moved up.
    """
    owners: SessionOwners | None = state(request).session_owners
    if owners is None:
        return []
    # `page_for_owner` lives on the durable store rather than on the `SessionOwners` protocol,
    # because resuming a listing is a property of a registry that can order and filter one — a
    # front door handed some other registry through `create_app(owner_store=...)` can answer the
    # first page and nothing further, and saying so is better than silently answering the first
    # page again and leaving a client to page forever.
    # **The cursor is advertised in the branch that can honour it, and nowhere else.** It used to
    # be set on any full page, so a deployment whose `create_app(owner_store=...)` registry is not
    # a `SessionOwnerStore` answered `200` with an `X-Next-Cursor` and then `422` to the client
    # that followed it — the route telling a caller to do the one thing it refuses. Absent is
    # already this header's word for "there is no next page", and for such a registry there is not.
    if isinstance(owners, SessionOwnerStore):
        try:
            rows = await owners.page_for_owner(principal.oid, after=after or None)
        except ValueError as exc:  # a cursor this service did not mint
            raise HTTPException(status_code=422, detail="not a session cursor") from exc
        if len(rows) == settings.service_max_listed_sessions:
            # A full page is the only evidence there might be more — asking for one row beyond the
            # ceiling to know for sure would cost every listing an extra row to answer a question
            # the next request answers for free by coming back empty.
            last_id, _, last_activity = rows[-1][:3]
            response.headers[_NEXT_CURSOR] = encode_session_cursor(last_activity, last_id)
    elif after:
        raise HTTPException(
            status_code=422, detail="this deployment's session registry cannot resume a listing"
        )
    else:
        rows = await owners.list_for_owner(principal.oid)
    return [
        SessionSummary(
            session_id=session_id, created_at=created_at, updated_at=updated_at, title=title
        )
        # The profile comes back on the same row and is not a session's *summary* — it is what
        # `GET /plans/pending` filters on. Dropped here rather than added to `SessionSummary`,
        # which describes a conversation to a person.
        for session_id, created_at, updated_at, title, _ in rows
    ]


async def get_messages(
    request: Request,
    session_id: str,
    live: CurrentSession,
) -> list[TranscriptMessage]:
    """One session's stored transcript, in order — what a client reads back after a reload.

    Ownership-gated by the same `resolve_session` the turn route uses, so a transcript is
    readable only by the chemist whose session it is (a non-owner gets the same 404 as an
    unknown id, leaking nothing about which ids exist).

    Read through the agent's own history provider rather than by querying `session_messages`:
    one reader means the write path and the read path cannot drift, and the route works
    unchanged under either store — the in-memory provider keeps its messages in
    `session.state`, which is exactly what `resolve_session` just returned.

    Each message carries the tools invoked alongside it, so a reload renders what the agent
    *did* and not only what it said. See `TranscriptMessage` for what that recovers and
    `_transcript` for what it cannot.

    **The second read is what makes a past tool call resolvable.** `TranscriptToolCall.result` is
    400 characters, so a reloaded conversation could show that `screen_hazards` ran and never what
    it found, while the full text sat in `tool_result_blobs` — the one path on which the ref
    `D-2026-08-09-a-preview-is-not-a-result` added never reached a surface. `fetchable_refs` is the
    set of refs this session can still be served, read once for the whole transcript rather than
    once per call, and it is what lets an advertised ref mean "fetchable" rather than merely
    "computable": a store that is off, unreachable, or has swept these blobs yields an empty set,
    and every `result_ref` is then `""` — the same value with the same meaning the live stream
    gives a result it did not store. See `D-2026-08-09-a-derivable-ref-is-not-a-fetchable-one`.
    """
    stored = await state(request).history.get_messages(session_id, state=live.session.state)
    return _transcript(stored, fetchable=await front_door.fetchable_refs(session_id))


async def delete_session(
    request: Request,
    session_id: str,
    principal: CurrentUser,
) -> Response:
    """Delete one conversation and everything keyed by it — the owner's own erasure.

    **Authorized exactly as reading it is**, through the same `resolve_session` dependency the
    transcript route uses — declared as a route dependency rather than as a parameter, the way the
    attachment route does it, because this handler needs the *gate* and not the handle. That
    identity is the whole design rather than a convenience: a
    caller who cannot read a session must not be able to delete it, and the cheapest way to
    guarantee that is to have one gate rather than two that can drift apart. So a session that does
    not exist and a session that is somebody else's answer the same **404**, exactly as they do on
    every other session-scoped route (`chemclaw.api.deps._refuse`): a 403 here would confirm which
    ids exist, and turn a delete endpoint into an id oracle. The refusal is recorded server-side,
    which is where that distinction survives.

    This is *not* `make user-erase` (`chemclaw.agent.leaver`) at a smaller scope, and the difference
    is who is asking. Erasure is an operator answering "someone left", scoped to a person, and it
    reports the attributable rows it deliberately keeps. This is a chemist closing one conversation,
    scoped to a session id, and it takes nothing that belongs to the person rather than to the
    conversation — no memory, no preference, no subscription — nor anything from the retained tier:
    the audit trail still records what this session's turns did, because a record that its subject
    can delete is not a record.

    **A turn in flight refuses with 409 rather than racing it.** The session's turn slot is claimed
    the same way `POST /sessions/{id}/messages` claims it — the in-process lease first, then the
    durable cross-process one — so a delete cannot land between a running turn's tool call and the
    row that turn is about to write. Holding the slot for the duration is also what stops a turn
    from *starting* while the sweep runs. The client's answer is the same 409 a second turn gets;
    stopping the turn (`POST /sessions/{id}/turn/stop`) and deleting again is the way through.

    The live in-process handle is not left behind: it is replaced by an entry no principal can
    match, so this pod stops resolving the id at all rather than serving a conversation whose
    durable half is gone (see `_tombstone_owner`).
    """
    front = state(request)
    # Nothing may sit between this claim and the `try` — the same rule the turn route states: the
    # reservation it takes has no expiry until `_start_turn_lease` starts one, which nothing here
    # ever does, so only that `finally` gives it back.
    slot = _claim_turn_slot(front.active_turns, session_id)
    if slot is None:
        raise HTTPException(
            status_code=409,
            detail="a turn is running on this session; stop it before deleting the session",
        )
    claims: SessionTurns | None = front.turn_claims
    claimed = False
    try:
        lease = settings.service_turn_claim_lease_seconds
        if claims is not None:
            claimed = await claims.claim(session_id, _WORKER_ID, lease)
            if not claimed:
                raise HTTPException(
                    status_code=409,
                    detail="a turn is running on this session; stop it before deleting the session",
                )
        owners = front.session_owners
        removed = (
            await owners.delete_session(session_id) if isinstance(owners, SessionOwnerStore) else {}
        )
        # The durable rows are gone; the live handle in this process is not, and it is what
        # `_resolve_session` consults *first*. Overwriting it with an owner no principal can equal
        # is how the id stops resolving here — the cache has no invalidation channel and the LRU
        # would otherwise keep serving a conversation whose transcript no longer exists, and let a
        # new turn write messages under an id nothing can find again (every session-scoped sweep in
        # this system starts from `session_owners`). Sibling pods learn the same way they learn
        # about an erasure: on their next durable lookup.
        front.live_sessions.add(session_id, TurnSession(session_id=session_id), _tombstone_owner())
        log_event(
            logger,
            "session.deleted",
            "deleted session %s for %s: %d durable row(s)",
            session_id,
            principal.oid or "-",
            sum(removed.values()),
            actor=principal.oid,
            session=session_id,
            rows=sum(removed.values()),
        )
    finally:
        if claimed and claims is not None:
            # Ordinarily a no-op: the sweep deleted this session's claim row inside its own
            # transaction. It is here for the path where the sweep raised, so a failed delete does
            # not leave the session refusing its owner's turns for a whole lease. Through the
            # shielded release for the reason D-130 gives — this `finally` also runs when the
            # caller is cancelled, and a bare `await` in a cancelled task raises at its first
            # suspension point, so the release would start on every abandoned delete and finish on
            # none.
            await _release_turn_claim(claims, session_id)
        _release_turn_slot(front.active_turns, session_id, slot)
    return Response(status_code=204)


async def upload_attachment(
    session_id: str,
    file: UploadFile,
) -> AttachmentSummary:
    """Attach a working file to a conversation (gap AGT-3).

    The only way data entered the system was the scheduled ELN sync, so a chemist could not
    hand over a CSV of runs or an SOP — the highest-frequency real request for a lab
    assistant.

    Session-scoped and in-memory by design: an attachment is working material for a
    conversation, not knowledge. Anything in it worth keeping goes through the PR-gate like
    every other machine-touched write; routing uploads into the graph would bypass the review
    line.

    Unsupported formats are refused with a message naming what *is* supported (422), never
    silently half-parsed — a PDF "read" by scraping whatever bytes look like text would
    produce confident nonsense a chemist could not tell from a real reading.

    Oversized ones are refused with a 413 by `BodySizeLimit` before the body is read at all;
    by the time this handler runs the upload is already bounded, which is why the read below
    can be a plain one.

    **The parse itself never runs on the event loop.** Size is not cost: a decompression bomb or
    a hostile font map inside the 2 MB cap can hold a CPU for tens of seconds, and this route is
    `async def` on a front door pinned to one uvicorn worker — so parsing inline froze every
    session, stream and health probe on the pod for that whole time. It runs in a bounded worker
    thread instead (`parse_attachment_off_loop`); past the process's parse cap an upload is shed
    with a retryable 503 rather than queued.
    """
    raw = await file.read()
    try:
        attachment = await parse_attachment_off_loop(
            file.filename or "upload", raw, file.content_type
        )
    except AttachmentError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except AttachmentUnavailable as exc:
        # 503, not 422: nothing is wrong with the file, and the client should try again — the
        # same distinction the turn route's shed answer makes.
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    ATTACHMENTS.add(session_id, attachment)
    return AttachmentSummary(
        name=attachment.name,
        content_type=attachment.content_type,
        rows=attachment.rows,
        excerpt=attachment.text[: settings.note_excerpt_chars],
    )


async def profiles(
    principal: CurrentUser,
) -> list[str]:
    """The specialized agents a session may be started as.

    `POST /sessions` accepts a `profile` and 400s an unknown one, and nothing exposed the list —
    so a surface had to hardcode names that live in files it cannot see, and a deployment adding
    a profile had no way to make it discoverable.

    **The registry, not `load_profiles()`, and that is the fix rather than a preference.** This
    route used to return `sorted(p.name for p in load_profiles())`, and `load_profiles` returns
    only what *it* newly registered — a profile already in the registry is skipped (see its own
    docstring). `api/app._lifespan` registers every file profile before the front door serves its
    first request, so by the time any caller reaches this route there is nothing left to register
    and the answer was `[]`, in every deployment, on every call. Measured: first call seven names,
    second call none. The route shipped doing exactly nothing it was written for, and the test that
    should have caught it asserted only that the response was a sorted list — which `[]` is.

    Reading the registry is also what makes this route safe to serve: `load_profiles()` does a
    `glob` + `read_text` + `yaml.safe_load` per file, synchronously, and this is an `async def` on
    a front door pinned to one uvicorn worker — the hazard the attachment route in this same module
    was fixed for. The registry answer is in memory, already sorted, and includes `default`, which
    is a real profile a caller may pass and which no file declares.
    """
    return registered_profile_names()


def register(app: FastAPI) -> None:
    """Attach this module's routes to `app` — called once, by `create_app` only.

    Registered with the app's own decorators rather than an `APIRouter` + `include_router`:
    since FastAPI 0.139 `include_router` is lazy — `app.routes` would hold opaque
    `_IncludedRouter` nodes, invisible to everything that walks the route table by type
    (`tests/test_route_auth_coverage.py`, the session-scope inventory in
    `tests/test_service.py`) — and a standalone router's routes carry no
    `dependency_overrides_provider`, which silently disables `app.dependency_overrides`.
    Registering on the app keeps both exactly as they were when these handlers lived in
    `create_app`.
    """
    app.post("/sessions")(create_session)
    app.get("/sessions")(list_sessions)
    app.get("/sessions/{session_id}/messages")(get_messages)
    app.delete("/sessions/{session_id}", status_code=204, dependencies=[Depends(resolve_session)])(
        delete_session
    )
    app.post("/sessions/{session_id}/fork")(fork_session_route)
    app.post("/sessions/{session_id}/attachments", dependencies=[Depends(resolve_session)])(
        upload_attachment
    )
    app.get("/profiles")(profiles)
