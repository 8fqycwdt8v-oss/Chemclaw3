"""Creating, listing and reading conversations — everything about a session except running it.

The turn stream itself is `chemclaw/api/routes/turns.py`; this module is the surrounding
lifecycle: mint a session (on a profile), list the caller's own, read a transcript back, attach a
working file, and discover which profiles exist. Every session-scoped route resolves ownership
through `chemclaw.api.deps` before doing anything (404 for a non-owner, no existence leak).
"""

import logging
import uuid

from fastapi import Depends, FastAPI, HTTPException, Request, Response, UploadFile

from chemclaw.agent.attachments import (
    AttachmentError,
    AttachmentSummary,
    AttachmentUnavailable,
    default_attachment_store,
    parse_attachment_off_loop,
)
from chemclaw.agent.profiles import get_profile, registered_profile_names
from chemclaw.agent.session import TurnSession
from chemclaw.agent.session_fork import SessionForkError, fork_session
from chemclaw.agent.session_store import SessionOwnerStore, encode_session_cursor
from chemclaw.api import app as front_door
from chemclaw.api.deps import (
    CurrentSession,
    CurrentUser,
    OwnedSession,
    resolve_owned_session,
    resolve_session,
)
from chemclaw.api.runner import settle_interrupted_turns
from chemclaw.api.schemas import (
    SessionIn,
    SessionOut,
    SessionSummary,
    TranscriptMessage,
    _transcript,
)
from chemclaw.api.state import (
    SessionOwners,
    SessionTurns,
    _claim_turn_slot,
    _release_turn_claim,
    _release_turn_slot,
    claim_holder,
    state,
)
from chemclaw.core.config import settings
from chemclaw.core.logging import log_event

logger = logging.getLogger(__name__)

# Where the next page's cursor is returned. `GET /sessions` answers a bare JSON array that clients
# parse as one, so a header is the additive place for it. A bare cursor rather than an RFC 8288
# `Link` URL: the service sits behind the UI's BFF, so any URL built here would name a path the
# browser cannot use.
_NEXT_CURSOR = "X-Next-Cursor"


def _tombstone_owner() -> str:
    """An owner string a deleted session's cached handle can be parked under, matching nobody.

    Random per delete so it cannot collide with any `oid`, and truthy so `_owner_authorizes`
    compares it rather than taking the dev-mode "no recorded owner, so anyone" branch.
    """
    return f"deleted:{uuid.uuid4().hex}"


async def create_session(
    request: Request,
    principal: CurrentUser,
    body: SessionIn | None = None,
) -> SessionOut:
    """Start a new conversation session and return its id (requires an authenticated user).

    An optional `profile` picks the configured agent. It is resolved here so an unknown name is a
    400 now rather than a 500 on the first turn, and it is fixed for the session's life so the
    thread keeps matching its own history.
    """
    front = state(request)
    session_id = uuid.uuid4().hex
    profile = body.profile if body is not None else None
    if profile is not None:
        try:
            # Resolved against the registry here, so a test's injected factory cannot make an
            # unknown name look valid.
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
    live: OwnedSession,
) -> SessionOut:
    """Branch this session onto a new one carrying its whole history, and return the new id.

    Owner only: a shared session holds other people's words, so a member gets 403 (a stranger 404).
    The fork inherits the parent's profile, never one from the request. Durable stores only. A turn
    in flight is a 409: the copy spans several statements, so the route holds the turn slot
    (in-process, then durable) and releases both in a `finally`.
    """
    front = state(request)
    if front.session_owners is None:
        raise HTTPException(
            status_code=501,
            detail="forking needs a durable session store; this deployment has none configured",
        )
    # Nothing may sit between this claim and the `try`: the reservation never expires, so only that
    # `finally` gives it back. `actor=None`: a hold that excludes a turn is not a turn and must not
    # count against the per-actor cap.
    slot = _claim_turn_slot(front.active_turns, session_id, actor=None)
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
                session_id, claim_holder(slot), settings.service_turn_claim_lease_seconds
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
        # Release the in-process slot first: it never expires, so losing it is permanent, while a
        # second cancellation inside the awaited durable release below costs at most one lease.
        _release_turn_slot(front.active_turns, session_id, slot)
        if claimed and claims is not None:
            # Shielded release: this `finally` also runs on cancellation, where a bare `await` would
            # raise at its first suspension point.
            await _release_turn_claim(claims, session_id, claim_holder(slot))
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

    Read from the durable ownership registry `resolve_session` authorizes against (empty under the
    in-memory store), ordered by last activity; never-used sessions are not listed.
    `service_max_listed_sessions` is the page; `after` resumes from a keyset cursor, and
    `X-Next-Cursor` on a full page says there may be more.
    """
    owners: SessionOwners | None = state(request).session_owners
    if owners is None:
        return []
    # Paging needs `page_for_owner`, which only the durable `SessionOwnerStore` has. Every shipped
    # configuration builds one; the other arm serves registries injected through `create_app` in
    # tests, answers one page, and advertises no cursor it could not honour.
    if isinstance(owners, SessionOwnerStore):
        try:
            rows = await owners.page_for_owner(principal.oid, after=after or None)
        except ValueError as exc:  # a cursor this service did not mint
            raise HTTPException(status_code=422, detail="not a session cursor") from exc
        if len(rows) == settings.service_max_listed_sessions:
            # A full page is the only evidence there might be more; the next request answers for
            # free.
            last_id, _, last_activity = rows[-1][:3]
            response.headers[_NEXT_CURSOR] = encode_session_cursor(last_activity, last_id)
    elif after:
        raise HTTPException(status_code=422, detail="this session registry cannot resume a listing")
    else:
        rows = await owners.list_for_owner(principal.oid)
    return [
        SessionSummary(
            session_id=session_id, created_at=created_at, updated_at=updated_at, title=title
        )
        # The profile is not part of a session's summary (it feeds `GET /plans/pending`), so drop
        # it.
        for session_id, created_at, updated_at, title, _ in rows
    ]


async def get_messages(
    request: Request,
    session_id: str,
    live: CurrentSession,
) -> list[TranscriptMessage]:
    """One session's stored transcript, in order — what a client reads back after a reload.

    Gated by `resolve_session`; read through the agent's own history provider so read and write
    cannot drift. Each message carries its tool calls; a `result_ref` is advertised only if
    `fetchable_refs` (read once) says the full result can still be served.
    """
    history = state(request).history
    # Mark a turn whose process died as `interrupted`; a turn whose claim is still live is left
    # alone.
    await settle_interrupted_turns(history, session_id, state=live.session.state)
    stored = await history.get_messages(session_id, state=live.session.state)
    return _transcript(stored, fetchable=await front_door.fetchable_refs(session_id))


async def delete_session(
    request: Request,
    session_id: str,
    principal: CurrentUser,
) -> Response:
    """Delete one conversation and everything keyed by it — the owner's own erasure.

    `resolve_owned_session` gates it: a non-reader gets the same 404 as an unknown id, a member 403.
    Removes only what belongs to the conversation, never the person's memory or the retained audit
    tier. A turn in flight is a 409 (the turn slot is held for the sweep), and the live handle is
    replaced by an unmatchable one (`_tombstone_owner`).
    """
    front = state(request)
    # Nothing may sit between this claim and the `try`: only that `finally` gives it back.
    # `actor=None`: this hold excludes a turn, it is not one.
    slot = _claim_turn_slot(front.active_turns, session_id, actor=None)
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
            claimed = await claims.claim(session_id, claim_holder(slot), lease)
            if not claimed:
                raise HTTPException(
                    status_code=409,
                    detail="a turn is running on this session; stop it before deleting the session",
                )
        owners = front.session_owners
        removed = (
            await owners.delete_session(session_id) if isinstance(owners, SessionOwnerStore) else {}
        )
        # `_resolve_session` consults the live handle first and the cache has no invalidation
        # channel, so overwrite it with an unmatchable owner; otherwise this pod would keep serving
        # the deleted conversation. Sibling pods find out on their next durable lookup.
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
        # The slot first, for the reason `fork_session_route`'s `finally` gives.
        _release_turn_slot(front.active_turns, session_id, slot)
        if claimed and claims is not None:
            # Ordinarily a no-op (the sweep deleted the claim row); here for when the sweep raised,
            # so the session does not refuse its owner's turns for a whole lease. Shielded, as in
            # the fork route.
            await _release_turn_claim(claims, session_id, claim_holder(slot))
    return Response(status_code=204)


async def upload_attachment(
    session_id: str,
    file: UploadFile,
    principal: CurrentUser,
) -> AttachmentSummary:
    """Attach a working file to a conversation.

    Stored in `session_attachments` when sessions are durable, so any replica can serve it;
    `principal` is recorded so an erasure reaches it. Working material, not knowledge. Unsupported
    formats are a 422 naming what is supported; oversize bodies a 413 from `BodySizeLimit`. Parsing
    runs in a bounded worker thread (`parse_attachment_off_loop`), since a hostile file can hold a
    CPU for seconds; past the cap an upload gets a retryable 503.
    """
    raw = await file.read()
    try:
        attachment = await parse_attachment_off_loop(
            file.filename or "upload", raw, file.content_type
        )
    except AttachmentError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except AttachmentUnavailable as exc:
        # 503, not 422: the file is fine and the client should retry.
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    await default_attachment_store().add(session_id, attachment, uploaded_by=principal.oid)
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

    Read from the in-memory profile registry, not `load_profiles()`: that returns only newly
    registered profiles (empty after startup) and does blocking file I/O. The registry is already
    sorted and includes `default`, which no file declares.
    """
    return registered_profile_names()


def register(app: FastAPI) -> None:
    """Attach this module's routes to `app` — called once, by `create_app` only.

    Registered on the app rather than via the lazy `include_router`, which would hide routes from
    tests that walk the route table and disable `app.dependency_overrides`.
    """
    app.post("/sessions")(create_session)
    app.get("/sessions")(list_sessions)
    app.get("/sessions/{session_id}/messages")(get_messages)
    app.delete(
        "/sessions/{session_id}", status_code=204, dependencies=[Depends(resolve_owned_session)]
    )(delete_session)
    app.post("/sessions/{session_id}/fork")(fork_session_route)
    app.post("/sessions/{session_id}/attachments", dependencies=[Depends(resolve_session)])(
        upload_attachment
    )
    app.get("/profiles")(profiles)
