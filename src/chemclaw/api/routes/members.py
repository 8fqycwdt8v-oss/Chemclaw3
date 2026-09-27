"""Who may reach a session besides its owner — admitting, listing and removing members.

`D-2026-09-27-in-a-shared-session-the-sender-governs`. The owner decides who else is in a
conversation; a member may read it and send into it, and every message a member sends runs as that
member — their roles, their memories, their spend caps — never as the owner. What a membership
grants is reach, not authority: a plan is decided only by the person whose turn wrote it, and
deleting or forking the session stays the owner's.

**Who may do what here.** Listing is open to everybody the session gate admits, because a member
sharing a conversation is entitled to know who else is reading it. Admitting is the owner's alone.
Removing is the owner's, and a member's own — leaving is not something anybody else should have to
be asked for. A stranger gets the session gate's 404 on all three, so none of these is an id oracle.
"""

from typing import Annotated

from fastapi import FastAPI, HTTPException, Path, Response

from chemclaw.agent.session_members import session_member_store
from chemclaw.agent.session_store import owner_permits
from chemclaw.api.deps import CurrentSession, CurrentUser, require_owner
from chemclaw.api.schemas import SessionMemberOut, SessionMembersOut, SharedSessionSummary

# The member's Entra object id, as the owner names it. Stripped for `Principal.oid`'s reason: the
# turn reads its actor stripped, so a padded id stored here would name a person no request can be.
MemberId = Annotated[str, Path(min_length=1)]


async def list_members(session_id: str, live: CurrentSession) -> SessionMembersOut:
    """The session's owner and the members that owner admitted, earliest first."""
    members = await session_member_store().members(session_id)
    return SessionMembersOut(
        owner=live.owner,
        members=[SessionMemberOut(actor=m.actor, added_at=m.added_at) for m in members],
    )


async def add_member(
    session_id: str, actor: MemberId, principal: CurrentUser, live: CurrentSession
) -> Response:
    """Let `actor` into this session — the owner's act, and only the owner's.

    Idempotent: admitting somebody twice is one membership. The owner cannot be admitted to their
    own session (409) — they already hold more than a membership grants, and a row saying otherwise
    would be a second answer to who the session belongs to. A session with no recorded owner admits
    nobody, because nobody holds the standing to (`require_owner` refuses every caller of one under
    enforced identity).
    """
    require_owner(live, principal, session_id, "admit somebody")
    member = actor.strip()
    if not member:
        raise HTTPException(status_code=422, detail="a member is named by a non-blank actor id")
    if owner_permits(live.owner, member):
        raise HTTPException(status_code=409, detail="the owner is not a member of their session")
    await session_member_store().add(session_id, member)
    return Response(status_code=204)


async def remove_member(
    session_id: str, actor: MemberId, principal: CurrentUser, live: CurrentSession
) -> Response:
    """Take `actor` out of this session — the owner's act, or a member leaving.

    Effective on the removed person's very next request: the session gate reads membership per
    request rather than from the live cache. A turn they already started runs to its end as them,
    exactly as a turn does when a token expires mid-stream. 404 when `actor` was not a member, so
    "removed" and "there was nobody to remove" stay different answers.
    """
    member = actor.strip()
    if member != principal.oid:
        require_owner(live, principal, session_id, "remove somebody else")
    if not await session_member_store().remove(session_id, member):
        raise HTTPException(status_code=404, detail="not a member of this session")
    return Response(status_code=204)


async def shared_sessions(principal: CurrentUser) -> list[SharedSessionSummary]:
    """Every session somebody else owns that the caller has been let into, newest admission first.

    The other half of `GET /sessions`, which lists only what the caller owns: without this a member
    could reach a shared conversation only by being handed its id. Unpaged — a person is a member
    of as many sessions as other people have let them into, which is not a list that grows by
    itself the way their own conversations do.
    """
    shared = await session_member_store().shared_with(principal.oid)
    return [
        SharedSessionSummary(
            session_id=row.session_id, owner=row.owner, title=row.title, added_at=row.added_at
        )
        for row in shared
    ]


def register(app: FastAPI) -> None:
    """Attach this module's routes to `app` — called once, by `create_app` only.

    With the app's own decorators rather than an `APIRouter`, for the reason
    `chemclaw/api/routes/sessions.py`'s `register` gives. `GET /sessions/shared` has no
    `{session_id}` segment, so it cannot collide with a session-scoped route.
    """
    app.get("/sessions/shared")(shared_sessions)
    app.get("/sessions/{session_id}/members")(list_members)
    app.put("/sessions/{session_id}/members/{actor}", status_code=204)(add_member)
    app.delete("/sessions/{session_id}/members/{actor}", status_code=204)(remove_member)
