"""The pre-execution plan gate: read the plan a session proposes, and record the human decision.

The agent proposes a plan, a human approves the exact plan they were shown (hash-bound), and
`chemclaw.agent.plan_gate` enforces the recorded decision. These are routes, not agent tools, so a
model can never approve its own plan. Two routes are per session; `pending_plans` is the
cross-session inbox, because a chemist who closed the tab holds no session id.
"""

import logging
from dataclasses import dataclass
from datetime import datetime

from fastapi import FastAPI, HTTPException, Request
from starlette.responses import Response

from chemclaw.agent.plan_approval_store import ApprovalStore, Decision
from chemclaw.agent.plan_gate import EMPTY_PLAN_HASH, gate_applies, may_decide, plan_identity
from chemclaw.agent.plan_scope import declared_scope
from chemclaw.agent.plan_state import session_plan
from chemclaw.agent.profiles import get_profile
from chemclaw.agent.session_members import session_member_store
from chemclaw.agent.session_store import SessionOwnerStore, encode_session_cursor
from chemclaw.api.deps import CurrentSession, CurrentUser, record_refusal
from chemclaw.api.schemas import PendingPlan, PendingPlansOut, PlanDecisionIn, PlanStatusOut
from chemclaw.api.state import SessionOwners, state
from chemclaw.core.config import settings

logger = logging.getLogger(__name__)

# One row of `SessionOwners.list_for_owner`: `(session_id, created_at, updated_at, title, profile)`.
_OwnedSession = tuple[str, datetime, datetime, str | None, str | None]


@dataclass(frozen=True)
class _Candidate:
    """A plan-gated session the inbox may read: the caller's own, or one they are a member of.

    `owned` decides whose plans are the caller's to decide: in their own session, their plans and
    unattributed ones; in a session they are only a member of, only the plans they authored.
    """

    session_id: str
    updated_at: datetime
    title: str | None
    owned: bool
    # The caller for an owned session, the owner of record for a shared one (`None` if unknown).
    owner: str | None


@dataclass(frozen=True)
class _PlanRead:
    """One session's plan as the two stores answer it, shared by `get_plan` and `pending_plans`.

    One read path keeps the inbox and the session page from disagreeing about the plan. `todos` is
    `None` when the plan could not be read: an unknown plan counts as unread, not as nothing
    waiting.
    """

    todos: list[str] | None
    # Every tool the plan's steps declare: what approving it would authorize, shown beside the
    # steps.
    scope: list[str]
    # The identity a decision is recorded against, or `None` when there is nothing to decide on.
    approvable: str | None
    # The latest *effective* decision; `None` when nobody has decided at all.
    decision: Decision | None
    # Whose turn last wrote the plan, if recorded — the person who may decide on it.
    author: str | None

    @property
    def plan_hash(self) -> str:
        """The identity to display: the approvable one, or the global empty-plan constant."""
        return self.approvable or EMPTY_PLAN_HASH


async def _read_plan(session_id: str, approvals: ApprovalStore) -> _PlanRead:
    """The plan `session_id` proposes and the decision standing against it.

    The decision is looked up only for an approvable plan: a row against the empty-plan constant
    would be shared by every session.
    """
    plan = await session_plan(session_id)
    todos = None if plan is None else [str(step["content"]) for step in plan]
    approvable = plan_identity(plan or [])
    decision = await approvals.decision(session_id, approvable) if approvable else None
    author = await approvals.author(session_id, approvable) if approvable else None
    return _PlanRead(
        todos=todos,
        scope=sorted(declared_scope(plan or [])),
        approvable=approvable,
        decision=decision,
        author=author,
    )


def _plan_gated(profile_name: str | None) -> bool:
    """Whether a plan on this profile can be waiting on a person.

    Uses `gate_applies`, the predicate the runner uses for the decision card, so the inbox and the
    card cover the same sessions. A skipped session costs no checkpointer statement. A profile the
    registry no longer knows is treated as gated: guessing away from a possible plan loses blocked
    work, and the cost is one read.
    """
    try:
        return gate_applies(get_profile(profile_name))
    except ValueError:
        logger.info(
            "session profile %r is no longer registered; its plan is read rather than skipped",
            profile_name,
        )
        return True


async def _owned_sessions(
    owners: SessionOwners, oid: str | None, budget: int
) -> tuple[int, list[_OwnedSession], bool]:
    """Every session of the caller's the inbox could still spend its scan budget on.

    Returns `(considered, gated, truncated)`-shaped data: sessions enumerated, the plan-gated ones
    newest first, and whether the walk stopped before the listing ran out. The walk stops on a short
    page or after `budget` pages — the same budget the reads spend — so a deployment where nothing
    is gated cannot page through the caller's whole history. Stopping at the ceiling is reported as
    `truncated`. A registry without `page_for_owner` (a test seam) answers one call.
    """
    if not isinstance(owners, SessionOwnerStore):
        rows = await owners.list_for_owner(oid)
        return len(rows), [row for row in rows if _plan_gated(row[4])], False
    considered = 0
    gated: list[_OwnedSession] = []
    cursor: str | None = None
    for _page in range(budget):
        page = await owners.page_for_owner(oid, after=cursor)
        considered += len(page)
        gated.extend(row for row in page if _plan_gated(row[4]))
        if len(page) < settings.service_max_listed_sessions or len(gated) > budget:
            return considered, gated, False
        session_id, _created_at, updated_at = page[-1][:3]
        cursor = encode_session_cursor(updated_at, session_id)
    return considered, gated, True


async def _shared_sessions(oid: str | None) -> list[_Candidate]:
    """The plan-gated sessions somebody else owns that the caller is a member of.

    Read from the registry `GET /sessions/shared` uses, so the inbox never names a session the
    caller would be refused. Not paged: memberships are deliberate grants, and the plan reads share
    the `service_max_plan_scans` budget.
    """
    if not oid:
        return []
    return [
        _Candidate(
            session_id=shared.session_id,
            # A session with no turn yet has no `updated_at`; the admission time keeps it orderable.
            updated_at=shared.updated_at or shared.added_at,
            title=shared.title,
            owned=False,
            owner=shared.owner,
        )
        for shared in await session_member_store().shared_with(oid)
        if _plan_gated(shared.profile)
    ]


async def get_plan(
    request: Request,
    session_id: str,
    live: CurrentSession,
) -> PlanStatusOut:
    """The plan awaiting a decision, with the hash a client must post back to approve it.

    `approved` is the effective state: a yes that the turn it authorized has not yet spent
    (`ApprovalStore.decision` folds in `consumed_at`), so the surface matches what the gate
    enforces. `decided_by` still names whoever decided. A session proposing no work items is asked
    nothing, but its global `EMPTY_PLAN_HASH` is still reported so a client has an identity to
    display. The plan is read from the checkpointer, so it survives eviction and pod rolls; there is
    no stored mode.
    """
    read = await _read_plan(session_id, state(request).plan_approvals)
    # One read, one question: a second query via `approval_stands` could disagree with this one.
    approved = bool(read.decision and read.decision[0])
    return PlanStatusOut(
        session_id=session_id,
        plan_hash=read.plan_hash,
        plan=read.todos or [],
        # What approving this plan would authorize; the decision is informed only if this is shown.
        scope=read.scope,
        mode="execute" if approved else "plan",
        approved=approved,
        decided_by=read.decision.actor if read.decision else None,
        author=read.author,
    )


async def pending_plans(request: Request, principal: CurrentUser) -> PendingPlansOut:
    """Every plan of the caller's that nobody has decided yet — the cross-session inbox.

    Lists a plan with no decision at all (no `plan_approvals` row for the session and hash). This is
    narrower than the in-turn card, which prompts whenever no live approval exists: a spent approval
    or a rejection is an answer, and listing them would flood the inbox with finished work. A plan
    re-proposed byte-identically after its approval was spent therefore does not list.

    Owned sessions come from the `GET /sessions` registry and shared ones from `GET
    /sessions/shared`, so a listed session is never refused. A plan is listed only for the person
    who may decide it.

    Bounded and reported, never silently truncated: ungated sessions are skipped for free, at most
    `service_max_plan_scans` plans are read (each a serialized checkpointer statement), `unread`
    counts what was left including unreadable checkpoints, and `truncated` says the listing walk hit
    its page ceiling. The listing is paged rather than read once, because a blocked conversation
    never moves its `updated_at` and would otherwise fall off the first page for good.
    """
    owners = state(request).session_owners
    if owners is None:
        # No durable registry to enumerate, as `GET /sessions` under `session_store="memory"`;
        # `gated=0` says this is the deployment, not the caller's work.
        return PendingPlansOut(plans=[], considered=0, gated=0, unread=0)
    budget = settings.service_max_plan_scans
    considered, owned, truncated = await _owned_sessions(owners, principal.oid, budget)
    shared = await _shared_sessions(principal.oid)
    gated = sorted(
        [
            _Candidate(session_id, updated_at, title, owned=True, owner=principal.oid)
            for session_id, _created_at, updated_at, title, _profile in owned
        ]
        + shared,
        key=lambda candidate: candidate.updated_at,
        reverse=True,
    )
    unread = len(gated) - min(len(gated), budget)
    approvals = state(request).plan_approvals
    plans: list[PendingPlan] = []
    for candidate in gated[:budget]:
        read = await _read_plan(candidate.session_id, approvals)
        if read.todos is None:
            unread += 1
            continue
        # Only the plan's decider sees it: an owner's inbox skips a member's plan, and a member's
        # skips the owner's and unattributed ones.
        decides = (
            may_decide(read.author, principal.oid, principal.oid)
            if candidate.owned
            else bool(principal.oid) and read.author == principal.oid
        )
        if not decides:
            continue
        if read.approvable is not None and read.decision is None:
            plans.append(
                PendingPlan(
                    session_id=candidate.session_id,
                    title=candidate.title,
                    updated_at=candidate.updated_at,
                    plan_hash=read.approvable,
                    plan=read.todos,
                    scope=read.scope,
                    owner=candidate.owner,
                )
            )
    return PendingPlansOut(
        plans=plans,
        considered=considered + len(shared),
        gated=len(gated),
        unread=unread,
        truncated=truncated,
    )


async def decide_plan(
    request: Request,
    session_id: str,
    body: PlanDecisionIn,
    principal: CurrentUser,
    live: CurrentSession,
) -> Response:
    """Approve (or reject) a harness plan — the pre-execution gate.

    An HTTP route and never an agent tool: a model must not be able to authorize its own plan.

    The posted `plan_hash` must match the plan proposed *now*, including each step's declared tools;
    a mismatch is a 409, because the plan changed between being shown and being approved. An empty
    plan has no identity (`plan_identity` returns `None`) and is refused, using the same function
    the gate asks so the route and enforcement agree on what counts as a plan.
    """
    plan = await session_plan(session_id) or []
    plan_hash = plan_identity(plan)
    if plan_hash is None:
        raise HTTPException(
            status_code=409,
            detail="this session is not proposing a plan; ask it something first, then decide "
            "on the plan it comes back with",
        )
    if body.plan_hash != plan_hash:
        raise HTTPException(
            status_code=409,
            detail="the plan changed since it was shown; re-read it and decide again",
        )
    # Only the plan's author decides on it: another member's yes is not consent to it. 403 rather
    # than 404, because the caller is already in the session and can see the plan.
    approvals = state(request).plan_approvals
    author = await approvals.author(session_id, plan_hash)
    if not may_decide(author, live.owner, principal.oid):
        record_refusal("session", "not the plan's author", principal, session_id, status=403)
        raise HTTPException(
            status_code=403,
            detail="only the person whose message produced this plan may decide on it",
        )
    # Recording is the re-arm: an approval authorizes one turn, and the append-only store reads the
    # latest row, so re-approving an unchanged plan yields a fresh, unspent approval.
    await approvals.record(
        session_id,
        plan_hash,
        principal.oid or "",
        body.approved,
        # The scope is fixed here, from the plan being decided on; the gate reads this row and never
        # the live declaration, so the model cannot widen an approval it already has. The hash guard
        # above covers the declared tools, so a widened rewrite gets a 409 rather than being
        # stamped.
        declared_scope(plan),
    )
    # The recorded decision is the whole authorization; `enforce_plan_approval` reads exactly it.
    return Response(status_code=204)


def register(app: FastAPI) -> None:
    """Attach this module's routes to `app` — called once, by `create_app` only.

    Registered on the app rather than via `include_router`: since FastAPI 0.139 that is lazy, which
    hides routes from tests that walk the route table and disables `app.dependency_overrides`.
    """
    app.get("/sessions/{session_id}/plan")(get_plan)
    app.post("/sessions/{session_id}/plan/decision", status_code=204)(decide_plan)
    # Not under `/sessions/…`: it asks about all sessions, without an id.
    app.get("/plans/pending")(pending_plans)
