"""The front door's authorization gates, as dependencies every route shares.

`CurrentUser` is `Depends(require_principal)` spelled once, so every route has the same
dependency shape and `tests/test_route_auth_coverage.py` can assert that every route is gated
(authentication plus the per-principal rate budget). It stays a dependency, not middleware, so it
raises a clean `HTTPException` inside FastAPI's handling. A handler that takes
`principal: CurrentUser` and never reads it is authenticated and deliberately unscoped.

The resource gates: session access (`CurrentSession`, which also rehydrates a durable session after
a restart), where the owner or an admitted member passes and anything else gets the same 404 as an
unknown id (`_refuse_unless_participant`); owner-only acts (`OwnedSession`); and the reviewer
check.
"""

import logging
from typing import Annotated

from fastapi import Depends, HTTPException, Request

from chemclaw.agent.session import TurnSession
from chemclaw.agent.session_members import participant_permits
from chemclaw.agent.session_store import owner_permits
from chemclaw.api.auth import Principal, require_principal
from chemclaw.api.middleware import bind_request_session, clip_for_log
from chemclaw.api.state import LiveSession, SessionOwners, state
from chemclaw.core.config import settings
from chemclaw.core.logging import log_event
from chemclaw.core.metrics import METRICS

logger = logging.getLogger(__name__)

# The resources this module can refuse, as a closed label set of source literals, so
# `chemclaw_authz_refusals_total` cannot grow a series from caller input.
_SESSION = "session"
#: Design writes refuse in their own module with a 403 (a design's reads are open, so its existence
#: is no secret) and record through `record_refusal`.
DESIGN = "design"
#: Organisation-skill writes refuse in their own module with a 403, as for `DESIGN`: the tier's
#: reads are open. The target is a skill name, never a person's oid.
ORG_SKILL = "org-skill"


def _refuse(
    resource: str, reason: str, principal: Principal, target: str, detail: str
) -> HTTPException:
    """Record one authorization refusal, then build the 404 that discloses none of it.

    404, not 403, so an id's existence is not confirmed; the server-side record is therefore the
    only place the distinction survives, which is what makes an enumeration scan visible. `reason`
    names which arm fired. `target` is caller input and is clipped before logging.
    """
    record_refusal(resource, reason, principal, target, status=404)
    return HTTPException(status_code=404, detail=detail)


def record_refusal(
    resource: str, reason: str, principal: Principal, target: str, *, status: int
) -> None:
    """Write the server-side record of one authorization refusal, whatever the response says.

    Separate from `_refuse` because some callers answer 403 (`chemclaw/api/routes/protocols.py`).
    `status` is what the caller was actually told.
    """
    clipped = clip_for_log(target)
    METRICS.increment("chemclaw_authz_refusals_total", labels={"resource": resource})
    log_event(
        logger,
        "authz.refused",
        "refused %s access to %s %s (%s); answered %d",
        principal.oid or "-",
        resource,
        clipped,
        reason,
        status,
        level=logging.WARNING,
        resource=resource,
        reason=reason,
        target=clipped,
        actor=principal.oid,
        status=status,
    )


# The authenticated caller for this request (401/429 handled inside `require_principal`). Every
# route outside the probe allowlist takes this; see `tests/test_route_auth_coverage.py`.
CurrentUser = Annotated[Principal, Depends(require_principal)]


def _owner_authorizes(owner: str | None, principal: Principal) -> bool:
    """Whether a stored owner (a session's, today) lets `principal` reach the row.

    In dev (`entra_required` off) an owner-less row is open. Under enforcement no owner-less row is
    ever written, so one that exists is a dev leftover and is refused to everyone (`None` and `""`
    alike). The rule lives in `agent/session_store.owner_permits`, shared with the agent.
    """
    return owner_permits(owner, principal.oid)


async def _refuse_unless_participant(
    session_id: str, owner: str | None, principal: Principal, detail: str
) -> None:
    """404 unless `principal` owns the session or is a member — the no-existence-leak gate.

    Shared by the live and rehydrated paths. Unknown and not-yours are indistinguishable. A member
    passes (`agent/session_members.participant_permits`); what they may do is decided per act, and
    owner-only acts go through `require_owner`. Membership is checked on every non-owner request, so
    removal takes effect immediately.
    """
    if not await participant_permits(session_id, owner, principal.oid):
        raise _refuse(_SESSION, "not the owner or a member", principal, session_id, detail)


def require_owner(live: LiveSession, principal: Principal, session_id: str, act: str) -> None:
    """403 unless `principal` is the session's owner — for the acts a member may not perform.

    A 403 because the caller already passed `resolve_session` and knows the session exists.
    Owner-only acts: deleting or forking the session, and admitting or removing members. Recorded
    like every refusal.
    """
    if not owner_permits(live.owner, principal.oid):
        record_refusal(_SESSION, "a member, not the owner", principal, session_id, status=403)
        raise HTTPException(status_code=403, detail=f"only the session's owner may {act}")


def _is_reviewer(principal: Principal) -> bool:
    """Whether the caller may reach other people's jobs and experiment designs.

    The same role set that guards every write tool (`entra_privileged_roles`). Dev is open, as in
    `authorize_tool`; an enforced deployment naming no privileged role fails closed.
    """
    if not settings.entra_required:
        return True
    return bool(principal.roles & settings.entra_privileged_role_set)


async def _resolve_session(request: Request, session_id: str, principal: Principal) -> LiveSession:
    """Return the caller's live session — from the cache, or rehydrated from durable ownership.

    A cache hit is authorized against its stored owner. On a miss under `session_store="postgres"`,
    a session the caller may reach is rebuilt over its persisted history, so a pod restart does not
    orphan it. Unknown and not-yours are the same 404.
    """
    entry = state(request).live_sessions.get(session_id)
    if entry is not None:
        await _refuse_unless_participant(session_id, entry.owner, principal, "unknown session")
        return entry
    return await _rehydrate_session(request, session_id, principal)


async def _rehydrate_session(
    request: Request, session_id: str, principal: Principal
) -> LiveSession:
    """Rebuild a live session from its durable owner record, or 404 if it cannot reattach."""
    front = state(request)
    owners: SessionOwners | None = front.session_owners
    if owners is None:
        raise _refuse(
            _SESSION, "no durable ownership store", principal, session_id, "unknown session"
        )
    found, owner, profile = await owners.lookup(session_id)
    if not found:
        raise _refuse(_SESSION, "no such session", principal, session_id, "unknown session")
    await _refuse_unless_participant(session_id, owner, principal, "unknown session")
    # Re-check the cache after the await so two racing requests share one handle over the thread.
    entry = front.live_sessions.get(session_id)
    if entry is not None:
        return entry
    # The history provider reloads the thread on first use, so a new handle resumes the
    # conversation. Rebuilt on the session's own profile: the default would silently widen the tool
    # surface, since a profile only attenuates and the LRU can evict a session mid-conversation.
    session = TurnSession(session_id=session_id)
    return front.live_sessions.add(session_id, session, owner, profile)


async def resolve_session(request: Request, session_id: str, principal: CurrentUser) -> LiveSession:
    """`_resolve_session` as a FastAPI dependency — the session-scoped routes' ownership gate.

    Depends on `CurrentUser` so `require_principal` stays in each route's dependency tree.
    """
    live = await _resolve_session(request, session_id, principal)
    # Bound here, not at request entry: the session is a routed path parameter, unknown until the
    # router runs. Every session-scoped route passes here. The middleware resets it.
    bind_request_session(request, session_id)
    return live


# The caller's own live session for a `{session_id}` route — resolved (and rehydrated if durable
# ownership allows) before the handler runs, 404ing a non-owner with no existence leak.
CurrentSession = Annotated[LiveSession, Depends(resolve_session)]


async def resolve_owned_session(
    request: Request, session_id: str, principal: CurrentUser
) -> LiveSession:
    """`resolve_session`, then `require_owner` — for a route that only the session's owner may call.

    Forking is owner-only because it copies every member's words into a session the forker alone
    owns.
    """
    live = await resolve_session(request, session_id, principal)
    require_owner(live, principal, session_id, "do this")
    return live


OwnedSession = Annotated[LiveSession, Depends(resolve_owned_session)]
