"""Who may reach a session besides its owner — admitting, listing and removing members.

(`D-2026-09-27-in-a-shared-session-the-sender-governs`.) A member may read and send; every message
runs as its sender (roles, memories, spend caps). Membership grants reach, not authority: a plan is
decided by the person whose turn wrote it, and deleting or forking stays the owner's. Anyone the
session gate admits may list members; only the owner admits; the owner removes, or a member leaves.
A stranger gets the gate's 404.
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

    Idempotent. The owner cannot be admitted to their own session (409). A session with no recorded
    owner admits nobody.
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

    Effective on their next request (membership is read per request); a turn they already started
    runs to its end. 404 when `actor` was not a member.
    """
    member = actor.strip()
    if member != principal.oid:
        require_owner(live, principal, session_id, "remove somebody else")
    if not await session_member_store().remove(session_id, member):
        raise HTTPException(status_code=404, detail="not a member of this session")
    return Response(status_code=204)


async def shared_sessions(principal: CurrentUser) -> list[SharedSessionSummary]:
    """Every session somebody else owns that the caller has been let into, newest admission first.

    The counterpart to `GET /sessions`, which lists only owned sessions. Unpaged.
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

    App decorators, not an `APIRouter`; see `chemclaw/api/routes/jobs.py`'s `register`.
    """
    app.get("/sessions/shared")(shared_sessions)
    app.get("/sessions/{session_id}/members")(list_members)
    app.put("/sessions/{session_id}/members/{actor}", status_code=204)(add_member)
    app.delete("/sessions/{session_id}/members/{actor}", status_code=204)(remove_member)
