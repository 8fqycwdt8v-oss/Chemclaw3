"""The front door's authorization gates, as dependencies every route shares (H1, R3.2).

`CurrentUser` is `Depends(require_principal)` spelled once. Before this module every route wrote
`principal: Principal = Depends(require_principal)` verbatim — twenty copies of the same line
enforcing "no route skips authentication or the per-principal rate budget" (`_within_budget`
lives inside `require_principal`, `chemclaw.api.auth:129-154`). A convention repeated twenty times
is a convention the twenty-first route can forget; nothing failed if it did.

This does not make the gate itself any stronger — `require_principal` is unchanged, and this is
still a `Depends`, not middleware (see `tests/test_request_limits.py` for why the gate must stay a
dependency: it needs to run *inside* FastAPI's request handling to raise a clean `HTTPException`
rather than reject at the ASGI layer). What it buys is a single spelling to grep for, and a
`route.dependant` tree with exactly one shape to look for `require_principal` in — which is what
`tests/test_route_auth_coverage.py` walks to make "every route is gated" an assertion instead of a
convention.

A handler that takes `principal: CurrentUser` and never reads the value (a handful of routes:
`GET /schedules`, `GET /profiles`, `GET /jobs`, `GET /jobs/{job_id}`) is not a mistake — it is
"authenticated, deliberately unscoped": the route needs a caller to exist, but nothing about the
answer depends on *which* caller it is. The alias is what makes that legible; the old spelled-out
`Depends()` looked identical whether the ownership check was intentionally absent or simply
forgotten.

The second half of this module (R3.2) is the resource-level gates the routes in
`chemclaw/api/routes/` resolve before touching anything: session ownership (`CurrentSession`,
which also rehydrates a durable session after a restart), approval-hold ownership, proposal
visibility, and the reviewer check. The first two share one refusal — `_refuse_unless_owner`, the
"same 404 for unknown and not-yours" rule — because they authorize the same way
(`_owner_authorizes` against a stored owner). Proposal visibility deliberately does **not**: a
reviewer may see *any* proposal, a privilege sessions and holds have no analogue for, and its
dev-mode opening comes from `_is_reviewer` rather than `_owner_authorizes` — see
`_visible_proposal` before you are tempted to unify it.
"""

from typing import Annotated

from fastapi import Depends, HTTPException, Request

from chemclaw.agent.session import TurnSession
from chemclaw.api.auth import Principal, require_principal
from chemclaw.api.state import LiveSession, SessionOwners, state
from chemclaw.core.config import settings
from chemclaw.kg.proposal import NoteProposal, read_proposal

# The authenticated caller for this request (401/429 handled inside `require_principal`). Every
# route that is not in the health/metrics probe allowlist takes this — see
# `tests/test_route_auth_coverage.py` for the enforced list.
CurrentUser = Annotated[Principal, Depends(require_principal)]


def _owner_authorizes(owner: str | None, principal: Principal) -> bool:
    """Whether a stored owner (session, approval hold, ...) lets `principal` reach the row.

    Mirrors `_is_reviewer`'s dev/enforced split, applied to ownership rather than role: in dev
    (`entra_required` off) there is no real actor, so an owner-less row degrades open, exactly as
    every other route does. Once identity is enforced, a *recorded* absence of an owner is no
    longer "everyone's" — `entra_required` never mints a new owner-less row, so a `None`/empty
    owner surviving into enforcement is a leftover from a dev-mode write, and treating it as
    "anyone's" would let it be read, resumed or decided by every authenticated principal instead
    of nobody. `owner` is falsy for both `None` (sessions) and `""` (the empty-string sentinel a
    Temporal query returns for an approval hold's owner), so one check covers both call shapes.
    """
    if not owner:
        return not settings.entra_required
    return owner == principal.oid


def _refuse_unless_owner(owner: str | None, principal: Principal, detail: str) -> None:
    """404 unless the stored owner authorizes `principal` — the shared no-existence-leak gate (S3).

    One helper for exactly the two gates whose rule is identical (`_owner_authorizes` over a
    stored owner): session resolution and approval-hold ownership. An unknown row and someone
    else's row are indistinguishable from outside, which is the entire point — a 403 would
    confirm the id exists. `detail` stays the resource's own wording so the split changed no
    response body.
    """
    if not _owner_authorizes(owner, principal):
        raise HTTPException(status_code=404, detail=detail)


def _is_reviewer(principal: Principal) -> bool:
    """Whether the caller may see and decide *other people's* proposals.

    The same role set that guards every write tool (`entra_privileged_roles`), rather than a
    new one: signing off on machine-written knowledge is the most consequential write in the
    system, so inventing a second, weaker role for it would be strange. Dev (`entra_required`
    off) has no real roles and is open, exactly as `authorize_tool` is; a deployment that
    enables identity and names no privileged role fails closed, also as `authorize_tool` does —
    a queue nobody can review is a misconfiguration to notice, not one to paper over.
    """
    if not settings.entra_required:
        return True
    return bool(principal.roles & settings.entra_privileged_role_set)


async def _resolve_session(request: Request, session_id: str, principal: Principal) -> LiveSession:
    """Return the caller's live session — from the cache, or rehydrated from durable ownership.

    A live-cache hit is authorized against its stored owner. On a miss, if durable rehydration
    is on (`session_store="postgres"`), the durable owner is looked up: a session the caller
    owns is rebuilt as a live handle over its persisted history, so a pod restart no longer
    forces the client onto a new session (orphaning its history and unconsumed push-back).
    An unknown session — or one owned by someone else — is a 404 with no existence leak
    either way.
    """
    entry = state(request).live_sessions.get(session_id)
    if entry is not None:
        _refuse_unless_owner(entry.owner, principal, "unknown session")
        return entry
    return await _rehydrate_session(request, session_id, principal)


async def _rehydrate_session(
    request: Request, session_id: str, principal: Principal
) -> LiveSession:
    """Rebuild a live session from its durable owner record, or 404 if it cannot reattach."""
    front = state(request)
    owners: SessionOwners | None = front.session_owners
    if owners is None:
        raise HTTPException(status_code=404, detail="unknown session")
    found, owner, profile = await owners.lookup(session_id)
    if not found:
        raise HTTPException(status_code=404, detail="unknown session")
    _refuse_unless_owner(owner, principal, "unknown session")
    # Re-check the cache after the awaited lookup: two racing requests would otherwise each
    # mint a live handle over the same durable thread, and the loser's handle would keep
    # writing outside the cache. The first rehydrator's handle wins; both callers share it.
    entry = front.live_sessions.get(session_id)
    if entry is not None:
        return entry
    # The durable history provider reloads the thread on the session's first use, so
    # rebuilding the handle is enough to resume the conversation; register it so later turns
    # hit the cache.
    #
    # On its own profile, not the default (REV-14). This used to come back on the default and
    # was documented as degrading gracefully — "the conversation resumes with the full tool
    # surface rather than a narrowed one". That has the direction backwards: a profile is
    # *attenuation only* (`agents.chemclaw_agent`), so restoring the full surface is a silent
    # widening, and it did not need a restart to happen. The live LRU has a capacity and no
    # TTL, so on a busy pod one session evicts another while both are in use; a chemist
    # mid-conversation regained every tool their profile had removed, having done nothing.
    session = TurnSession(session_id=session_id)
    return front.live_sessions.add(session_id, session, owner, profile)


async def resolve_session(request: Request, session_id: str, principal: CurrentUser) -> LiveSession:
    """`_resolve_session` as a FastAPI dependency — the session-scoped routes' ownership gate.

    Depending on `CurrentUser` (rather than taking a bare `Principal`) is what keeps
    `require_principal` in each session-scoped route's dependency tree, so
    `tests/test_route_auth_coverage.py` resolves the gate through this dependency exactly as it
    did through the handler's own parameter.
    """
    return await _resolve_session(request, session_id, principal)


# The caller's own live session for a `{session_id}` route — resolved (and rehydrated if durable
# ownership allows) before the handler runs, 404ing a non-owner with no existence leak.
CurrentSession = Annotated[LiveSession, Depends(resolve_session)]


async def owned_approval(approval_id: str, principal: CurrentUser) -> None:
    """Authorize the caller against a hold's owner, or 404 (no existence leak either way).

    Mirrors `resolve_session`: an unknown hold and someone else's hold are indistinguishable
    from outside. The dev path (`entra_required` off) has no real actor, so an unowned hold
    stays answerable — matching how every other route degrades in dev; under enforcement an
    unowned hold is nobody's (`_owner_authorizes`), not everyone's.
    """
    # Read through the front-door module at call time, not imported here: the suite patches this
    # collaborator on `chemclaw.api.app` (`monkeypatch.setattr("chemclaw.api.app.approval_owner",
    # …)`), and that seam must keep working wherever the gate lives. Imported lazily to keep this
    # module free of the app module at import time (the routes already import `app`, and a
    # module-level import here would close a cycle whose resolution depends on import order).
    from chemclaw.api import app as front_door

    try:
        owner = await front_door.approval_owner(approval_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="no such approval hold") from exc
    _refuse_unless_owner(owner, principal, "no such approval hold")


async def _visible_proposal(proposal_id: int, principal: Principal) -> NoteProposal:
    """One proposal the caller may see, or 404 — no existence leak, as with sessions/holds.

    **Deliberately not `_refuse_unless_owner`**, though it looks like the third copy of that
    gate. The rule here is different in both halves: a reviewer may see *any* proposal — a
    privilege sessions and approval holds have no analogue for — and the dev-mode opening comes
    from `_is_reviewer` (role check), not from `_owner_authorizes` (owner check). Folding it into
    the shared helper would either grant reviewers session access or lose them proposal access.
    """
    proposal = await read_proposal(proposal_id)
    if proposal is None:
        raise HTTPException(status_code=404, detail="no such proposal")
    if not _is_reviewer(principal) and proposal.actor != principal.oid:
        raise HTTPException(status_code=404, detail="no such proposal")
    return proposal


async def visible_proposal(proposal_id: int, principal: CurrentUser) -> NoteProposal:
    """`_visible_proposal` as a FastAPI dependency, for the read route.

    The decision route calls `_visible_proposal` in its body instead: its reviewer (403) and
    reason (422) refusals must keep running *before* the visibility 404, and a dependency would
    reorder them.
    """
    return await _visible_proposal(proposal_id, principal)


# The proposal a `{proposal_id}` read route may show this caller — resolved before the handler.
VisibleProposal = Annotated[NoteProposal, Depends(visible_proposal)]
