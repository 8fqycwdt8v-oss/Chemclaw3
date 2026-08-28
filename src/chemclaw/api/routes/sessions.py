"""Creating, listing and reading conversations — everything about a session except running it.

The turn stream itself is `chemclaw/api/routes/turns.py`; this module is the surrounding
lifecycle: mint a session (on a profile), list the caller's own, read a transcript back, attach a
working file, and discover which profiles exist. Every session-scoped route resolves ownership
through `chemclaw.api.deps` before doing anything (404 for a non-owner, no existence leak).
"""

import uuid

from fastapi import Depends, FastAPI, HTTPException, Request, UploadFile

from chemclaw.agent.attachments import STORE as ATTACHMENTS
from chemclaw.agent.attachments import (
    AttachmentError,
    AttachmentSummary,
    AttachmentUnavailable,
    parse_attachment_off_loop,
)
from chemclaw.agent.profiles import get_profile, registered_profile_names
from chemclaw.agent.session import TurnSession
from chemclaw.api import app as front_door
from chemclaw.api.deps import CurrentSession, CurrentUser, resolve_session
from chemclaw.api.schemas import (
    SessionIn,
    SessionOut,
    SessionSummary,
    TranscriptMessage,
    _transcript,
)
from chemclaw.api.state import SessionOwners, state
from chemclaw.core.config import settings


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


async def list_sessions(
    request: Request,
    principal: CurrentUser,
) -> list[SessionSummary]:
    """The caller's own sessions, newest first — the conversation list.

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
    """
    owners: SessionOwners | None = state(request).session_owners
    if owners is None:
        return []
    return [
        SessionSummary(
            session_id=session_id, created_at=created_at, updated_at=updated_at, title=title
        )
        # The profile comes back on the same row and is not a session's *summary* — it is what
        # `GET /plans/pending` filters on. Dropped here rather than added to `SessionSummary`,
        # which describes a conversation to a person.
        for session_id, created_at, updated_at, title, _ in await owners.list_for_owner(
            principal.oid
        )
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
    app.post("/sessions/{session_id}/attachments", dependencies=[Depends(resolve_session)])(
        upload_attachment
    )
    app.get("/profiles")(profiles)
