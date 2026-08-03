"""The pre-execution plan gate: read the plan a session proposes, and record the human decision.

These two routes are the HTTP half of the harness's GxP line (D-137/D-167): the agent proposes a
plan, a human approves the exact plan they were shown (hash-bound), and `chemclaw.agent.plan_gate`
enforces the recorded decision. Deliberately routes and not agent tools — see `decide_plan`.
"""

from fastapi import FastAPI, HTTPException, Request
from starlette.responses import Response

from chemclaw.agent.harness_mode import (
    EMPTY_PLAN_HASH,
    approvable_plan_hash,
    grant_execute,
    session_mode,
)
from chemclaw.agent.harness_todo import todo_titles
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
    """
    plan = await todo_titles(live.session)
    approvable = await approvable_plan_hash(live.session)
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
        mode=session_mode(live.session),
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
    """Approve (or reject) a harness plan — the pre-execution GxP gate, finally enforced.

    Deliberately an HTTP route and **not** an agent tool, for the same reason
    `POST /approvals/{id}/decision` is not (D-005): MAF advertises a `mode_set` tool to the
    model by default, so until this existed the agent moved itself out of plan mode and the
    audit trail recorded that under the asking chemist's identity. `PlanApprovalModeProvider`
    retracts that tool; this is the only remaining path into execute mode.

    The posted `plan_hash` must match the plan the session is proposing *now*. A mismatch is a
    409, not a silent approval of the current plan: it means the plan changed between being
    shown and being approved, and the human agreed to something else.

    A session proposing **no** work items has nothing to decide on, and this refused nothing:
    the empty todo list hashes to a global constant, so a decision could be recorded against
    "the empty plan" — an identity every session shares and comes back to whenever it loses its
    todo state. The CLI's `/approve` already refused; this is the same refusal at the route that
    matters, and `harness_mode.approvable_plan_hash` is where the two now get their answer.
    """
    plan_hash = await approvable_plan_hash(live.session)
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
    if body.approved:
        grant_execute(live.session)
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
