"""The pre-execution plan gate: read the plan a session proposes, and record the human decision.

These two routes are the HTTP half of the harness's approval line (D-137/D-167): the agent proposes
a plan, a human approves the exact plan they were shown (hash-bound), and `chemclaw.agent.plan_gate`
enforces the recorded decision. Deliberately routes and not agent tools — see `decide_plan`.
"""

from fastapi import FastAPI, HTTPException, Request
from starlette.responses import Response

from chemclaw.agent.plan_gate import EMPTY_PLAN_HASH, plan_identity
from chemclaw.agent.plan_state import session_todos
from chemclaw.api.deps import CurrentSession, CurrentUser
from chemclaw.api.schemas import PlanDecisionIn, PlanStatusOut
from chemclaw.api.state import state


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
    plan = await session_todos(session_id) or []
    approvable = plan_identity(plan)
    plan_hash = approvable or EMPTY_PLAN_HASH
    decision = (
        await state(request).plan_approvals.decision(session_id, plan_hash) if approvable else None
    )
    # One read, one question. Calling `plan_is_approved` here as well would issue a second query
    # whose answer could differ from this one — a route reporting `approved=false` beside the
    # name of whoever approved it is a worse surface than either fact alone.
    approved = bool(decision and decision[0])
    return PlanStatusOut(
        session_id=session_id,
        plan_hash=plan_hash,
        plan=plan,
        mode="execute" if approved else "plan",
        approved=approved,
        decided_by=decision[1] if decision else None,
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
    `POST /approvals/{id}/decision` is not (D-005): a model must never be able to authorize its
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
