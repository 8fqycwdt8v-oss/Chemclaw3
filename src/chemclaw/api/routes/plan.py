"""The pre-execution plan gate: read the plan a session proposes, and record the human decision.

These routes are the HTTP half of the harness's approval line (D-137/D-167): the agent proposes a
plan, a human approves the exact plan they were shown (hash-bound), and `chemclaw.agent.plan_gate`
enforces the recorded decision. Deliberately routes and not agent tools — see `decide_plan`.

**Two of them are per session and the third is not, and that asymmetry is the point.** A decision
belongs to the conversation that raised it, but *finding* the conversation cannot: a chemist who
closed the tab holds no session id, and every other plan surface is addressed by one.
`pending_plans` is the cross-session read — see its docstring for the narrower predicate an inbox
needs and why it is not the one the in-turn card uses.
"""

import logging
from dataclasses import dataclass
from datetime import datetime

from fastapi import FastAPI, HTTPException, Request
from starlette.responses import Response

from chemclaw.agent.plan_approval_store import ApprovalStore
from chemclaw.agent.plan_gate import EMPTY_PLAN_HASH, gate_applies, plan_identity
from chemclaw.agent.plan_state import session_todos
from chemclaw.agent.profiles import get_profile
from chemclaw.agent.session_store import SessionOwnerStore, encode_session_cursor
from chemclaw.api.deps import CurrentSession, CurrentUser
from chemclaw.api.schemas import PendingPlan, PendingPlansOut, PlanDecisionIn, PlanStatusOut
from chemclaw.api.state import SessionOwners, state
from chemclaw.core.config import settings

logger = logging.getLogger(__name__)

# One row of the ownership listing, as `SessionOwners.list_for_owner` returns it:
# `(session_id, created_at, updated_at, title, profile)`. Named here rather than repeated at
# each signature — it is the shape `_owned_sessions` pages over and `pending_plans` unpacks.
_OwnedSession = tuple[str, datetime, datetime, str | None, str | None]


@dataclass(frozen=True)
class _PlanRead:
    """One session's plan as the two stores answer it, before either route interprets it.

    Extracted so `get_plan` and `pending_plans` read a plan the same way. They ask different
    questions of the answer — one renders it, one filters on it — and a second read path would let
    the inbox and the session's own page disagree about what this session is proposing, which is
    the class of defect `agent/plan_state` exists to prevent one layer down.

    `todos` is `None` when the plan could not be read at all, and that is deliberately not folded
    into `[]`: a session whose checkpoint is unreachable has an *unknown* plan, which the inbox
    counts as unread rather than reporting as nothing waiting.
    """

    todos: list[str] | None
    # The identity a decision is recorded against, or `None` when there is nothing to decide on.
    approvable: str | None
    # The latest *effective* `(approved, actor)`; `None` when nobody has decided at all.
    decision: tuple[bool, str] | None

    @property
    def plan_hash(self) -> str:
        """The identity to display: the approvable one, or the global empty-plan constant.

        A client needs *an* identity even for a session proposing nothing — see `get_plan`.
        """
        return self.approvable or EMPTY_PLAN_HASH


async def _read_plan(session_id: str, approvals: ApprovalStore) -> _PlanRead:
    """The plan `session_id` proposes and the decision standing against it — one read, both routes.

    The decision is looked up only for an approvable plan, because `plan_identity` returns `None`
    for an empty one and a row recorded against the empty-plan constant would say "someone approved
    the empty plan" — an identity every session in every deployment shares.
    """
    todos = await session_todos(session_id)
    approvable = plan_identity(todos or [])
    decision = await approvals.decision(session_id, approvable) if approvable else None
    return _PlanRead(todos=todos, approvable=approvable, decision=decision)


def _plan_gated(profile_name: str | None) -> bool:
    """Whether a plan on this profile can be waiting on a person.

    `gate_applies` — the same predicate `chemclaw.api.runner` uses to decide whether to show the
    decision card at all, so the inbox and the card cover the same sessions. It answers two things
    at once, and both are needed here: with the harness off there is no todo list to read
    (`build_langgraph_agent` attaches `TodoListMiddleware` under `harness_enabled_for`), and under
    `harness_autonomy="execute"` there is a plan but no gate — the agent acts without asking, so
    nothing about that plan is anyone's decision.

    This is also the filter that keeps `GET /plans/pending` free in the default deployment, where
    `harness_enabled` is off: a skipped session costs no checkpointer statement, and every
    checkpointer statement is serialized against every concurrent turn on the pod.

    A profile the registry no longer knows is treated as gated rather than skipped: the deployment
    dropped a profile out from under an existing session, and guessing *away* from a plan that may
    be sitting there is the direction that loses a chemist's blocked work. It costs one read.
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

    Returns how many sessions were enumerated, of those the plan-gated ones in listing order
    (newest activity first), and whether the walk stopped before the listing ran out — the
    `considered`, `gated` and `truncated` the response reports.

    **The loop stops when another page could not change the answer, or when it has spent its
    budget** — and the second half is why this reads as two conditions rather than one. Once more
    than `budget` gated sessions are in hand every further page only adds rows past the scan
    ceiling, and `unread` already says the answer is partial; a short page ends it too, which is
    the listing running out and the only case where the inbox can honestly claim to have seen
    everything. Neither of those can fire when *nothing* is gated — `_plan_gated` is False for
    every session with `harness_enabled` off, which is the code's own default — so the walk used
    to page through the caller's whole history on every request and return `plans: []`: measured
    at 5,000 sessions and the shipped page of 100, **51** keyset statements where the route before
    paging issued one, repeatable by the caller at will.

    So the walk spends the *same* budget the reads do, in the unit it spends it in: at most
    `budget` pages. That is the honest ceiling rather than a second number, because a page can
    contribute at most one page's worth of gated rows — in a gated deployment `budget` pages can
    always fill a budget of `budget` reads, and in an ungated one no number of pages ever can,
    which is exactly the case that has to be capped. Stopping there is reported rather than
    silent: reaching the ceiling means the last page was *full*, so there is more listing behind
    it (at most one page of false alarm, when the history ends on a page boundary), and an inbox
    that quietly answers from a prefix of a chemist's history is the confident emptiness the three
    counts exist to prevent.

    A registry that is not the durable store answers one call and is done: `page_for_owner` lives
    on `SessionOwnerStore` rather than on the `SessionOwners` protocol, and `GET /sessions` makes
    the same split for the same reason — a front door handed some other registry through
    `create_app(owner_store=...)` can answer a listing but not resume one.
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


async def get_plan(
    request: Request,
    session_id: str,
    live: CurrentSession,
) -> PlanStatusOut:
    """The plan awaiting a decision, with the hash a client must post back to approve it.

    `approved` is the **effective** state, not merely the recorded one: a decision exists, it
    was a yes, and it has not already been spent by the turn it authorized. Reporting the stored
    row alone would tell a surface a plan is approved while every state-changing call under it
    is refused — the same disagreement between what a surface displays and what the system
    enforces that let DARK-1 sit unnoticed, reintroduced one layer up. That is now one question
    rather than two: `ApprovalStore.decision` folds `plan_approvals.consumed_at` into the
    verdict, so a route cannot forget the second half. `decided_by` still names whoever decided,
    because "approved earlier, already used" is a different thing to show than "nobody has
    decided".

    A session proposing no work items is asked nothing: its identity is the global
    `EMPTY_PLAN_HASH`, which the gate refuses outright, so a stored row against it — one
    written before the decision route refused to — must not come back as `approved=true` here
    either. The hash is still reported, because a client needs *an* identity to display.

    **The plan is read from the checkpointer** (`agent/plan_state.session_todos`), not from an
    in-process session object. It used to come off `live.session`, the handle the front door held
    per live session, because MAF's harness kept its todo list inside it — and that handle is
    exactly what an LRU eviction or a pod roll dropped, which is half of why a rehydrated session
    used to propose the empty plan and meet its own already-spent approval. The plan is durable now
    because turn state is.

    **`mode` is derived, not stored, and that closes DARK-1 from the other side.** MAF kept a
    session mode beside the approval, so "may this session act" had two answers that could
    disagree — and did: `grant_execute` was a latch, nothing moved a session back, so the mode kept
    saying execute after the approval it was granted for had been spent. There is no mode here;
    what a surface renders is one fact seen twice.
    """
    read = await _read_plan(session_id, state(request).plan_approvals)
    # One read, one question. Calling `plan_is_approved` here as well would issue a second query
    # whose answer could differ from this one — a route reporting `approved=false` beside the
    # name of whoever approved it is a worse surface than either fact alone.
    approved = bool(read.decision and read.decision[0])
    return PlanStatusOut(
        session_id=session_id,
        plan_hash=read.plan_hash,
        plan=read.todos or [],
        mode="execute" if approved else "plan",
        approved=approved,
        decided_by=read.decision[1] if read.decision else None,
    )


async def pending_plans(request: Request, principal: CurrentUser) -> PendingPlansOut:
    """Every plan of the caller's that nobody has decided yet — the cross-session inbox.

    The plan gate is answered per session, and until this route existed *finding* the session was
    the unsolved half: the decision card lives inside a turn, a reload recovers it only for a
    conversation somebody opens, and a chemist who closed the tab holds no session id. So a plan
    could sit blocking work with nothing anywhere able to say which conversation it was in.

    **The predicate is narrower than the card's, deliberately.** `runner._pending_plan_approval`
    prompts whenever a plan holds no *live* approval, which is right inside a turn and wrong for an
    inbox: an approval is spent at the end of the turn it authorized (D-167), so every finished
    plan-gated conversation would sit here forever. This lists a plan with **no decision at all** —
    `plan_approvals` holds no row for this session and this plan hash. A spent approval and a
    rejection are both answers; asking again is the conversation's job, and the card does it there.

    What that misses, stated rather than left to be found: a plan re-proposed byte-identically after
    its approval was spent hashes to a row that exists, so it does not list — while the card, on the
    next turn's end, still shows. The alternative misses nothing and drowns the queue in decided
    work, which is the failure mode that makes an inbox stop being read.

    **Ownership comes from the same registry `GET /sessions` reads**, so this can never name a
    session the caller would then be refused — the property `list_sessions` relies on, for the same
    reason.

    Bounded twice, and the response says so rather than truncating quietly. Sessions that cannot be
    holding a decision are skipped for free (`_plan_gated`); of what remains, at most
    `service_max_plan_scans` have their plan read, because each read is a statement on a
    checkpointer that serializes them against every concurrent turn on the pod. `unread` counts
    what was left — including a session whose checkpoint could not be read at all, which is an
    unknown plan rather than an absent one.

    **The listing is paged through, not read once**, and that is the one bound this route must not
    inherit. `service_max_listed_sessions` became a *page* when `X-Next-Cursor` was added to
    `GET /sessions`; this reader stayed on the first call, so `considered` was a page count
    presented as a population and `unread` counted only what the scan budget skipped *inside* that
    page. The failure is not an inaccurate field: an unanswered plan means the conversation takes
    no further turns, so its `updated_at` never moves and it never rises back above the page
    boundary — a chemist whose blocked plan sits on an older conversation is told "nothing is
    waiting on you" for good. Measured at a page of 2 over five owned sessions:
    `{"plans": [], "considered": 2, "gated": 2, "unread": 0}`.

    Paging costs one indexed keyset statement per page and is bounded by the same
    `service_max_plan_scans` the reads are, counted in pages — a sentence that used to say the
    loop was "bounded by the work the route was already allowed to do" and was true only where
    something is gated. With `harness_enabled` off nothing ever is, so the only remaining exit was
    a short page and the walk ran the caller's whole history on every request; see
    `_owned_sessions` for the measurement and for why a page ceiling is the same budget rather
    than a second one. `truncated` is what that ceiling costs the answer, and it is a fourth
    reading of an empty `plans` rather than a fifth kind of `unread`. The expensive half is
    unchanged: still at most `service_max_plan_scans` checkpointer reads, still serialized behind
    one lock.
    """
    owners = state(request).session_owners
    if owners is None:
        # No durable registry to enumerate — the same emptiness, and for the same reason, that
        # `GET /sessions` returns under `session_store="memory"`. `gated=0` tells the surface this
        # is a property of the deployment rather than of the caller's work.
        return PendingPlansOut(plans=[], considered=0, gated=0, unread=0)
    budget = settings.service_max_plan_scans
    considered, gated, truncated = await _owned_sessions(owners, principal.oid, budget)
    unread = len(gated) - min(len(gated), budget)
    approvals = state(request).plan_approvals
    plans: list[PendingPlan] = []
    for session_id, _created_at, updated_at, title, _profile in gated[:budget]:
        read = await _read_plan(session_id, approvals)
        if read.todos is None:
            unread += 1
            continue
        if read.approvable is not None and read.decision is None:
            plans.append(
                PendingPlan(
                    session_id=session_id,
                    title=title,
                    updated_at=updated_at,
                    plan_hash=read.approvable,
                    plan=read.todos,
                )
            )
    return PendingPlansOut(
        plans=plans,
        considered=considered,
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
    """Approve (or reject) a harness plan — the pre-execution gate, finally enforced.

    Deliberately an HTTP route and **not** an agent tool, for the same reason
    `POST /proposals/{id}/decision` is not (D-005): a model must never be able to authorize its
    own plan. Under MAF that took work — the framework advertised a `mode_set` tool by default, so
    the agent moved itself out of plan mode and the audit trail recorded it under the asking
    chemist's identity, and `PlanApprovalModeProvider` had to subclass-and-mutate to retract it.
    Nothing advertises such a tool here; the model is not given one, which is the same guarantee
    obtained by not building the thing rather than by removing it afterwards.

    The posted `plan_hash` must match the plan the session is proposing *now*. A mismatch is a
    409, not a silent approval of the current plan: it means the plan changed between being
    shown and being approved, and the human agreed to something else.

    A session proposing **no** work items has nothing to decide on, and this refused nothing:
    the empty todo list hashes to a global constant, so a decision could be recorded against
    "the empty plan" — an identity every session shares and comes back to whenever it loses its
    todo state. `plan_identity` returning `None` for an empty plan is where that refusal lives, and
    it is the same function the gate asks, so the route and the enforcement cannot disagree about
    what counts as a plan.
    """
    plan_hash = plan_identity(await session_todos(session_id) or [])
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
    # Recording *is* the re-arm. An approval authorizes one turn and is spent when that turn
    # ends (D-167), so re-approving an unchanged plan has to mean "yes, again" rather than a
    # no-op that silently leaves the session unable to act — and since the store is append-only
    # and reads the latest row, a second decision is a fresh, unspent one by construction. It
    # used to need a separate `rearm_plan` call against session state, which is one more thing a
    # future route could forget to do.
    await state(request).plan_approvals.record(
        session_id, plan_hash, principal.oid or "", body.approved
    )
    # Nothing else to flip. This used to call `grant_execute` as well, moving the session's MAF
    # mode — a second piece of state saying the same thing, on a different lifetime, which is what
    # let the displayed mode outlive the approval it came from. The recorded decision is the whole
    # authorization now, and `enforce_plan_approval` reads exactly it.
    return Response(status_code=204)


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
    app.get("/sessions/{session_id}/plan")(get_plan)
    app.post("/sessions/{session_id}/plan/decision", status_code=204)(decide_plan)
    # Not under `/sessions/…`: it is a question about all of them, and a path that named one
    # session would need an id the caller is asking this route to find.
    app.get("/plans/pending")(pending_plans)
