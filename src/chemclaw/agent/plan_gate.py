"""The harness's pre-execution approval, applied to the act rather than latched onto the session.

`SECURITY.md` and `docs/guides/harness-konzept.md` §6 describe a GxP gate: under
`harness_autonomy="plan_only"` the agent proposes a plan and waits for a human before executing.
D-137 built the human-only path into execute mode and retracted MAF's self-service `mode_set` tool,
which closed the hole where the *model* granted itself autonomy. What it did not do was make the
approval mean anything afterwards, and a live pass found the consequence (DARK-1):

    approve a four-item plan               → mode flips to execute, a plan_approvals row is written
    ask a completely different question    → new plan_hash, approved=false, mode still execute,
                                             and the turn autonomously ran compute_xtb_energy and
                                             propose_knowledge_note — a knowledge-graph write

`PlanApprovalStore.decision` was read in exactly one place: the front door's **display** route. No
execution path consulted it. The only thing gating the loop was MAF's session mode, `grant_execute`
had no mirror, and so nothing ever returned a session to `plan` — which also meant a rejection
recorded after an approval revoked nothing, against migration 020's stated contract.

**Why this is a function middleware.** The unit an approval authorizes is an *action*, and the only
place every action passes through is the tool-invocation boundary — the same reasoning
`chemclaw.agent.tool_authz` records for per-tool RBAC. Checking at `before_run` instead is where the
obvious fix goes wrong, and it is worth being explicit about because it looks sufficient: on the
repro turn the todo list still holds the previous, approved plan when `before_run` runs. The model
rewrites it *afterwards*. A check there would read the approved plan, find it approved, and wave
through everything that followed. `PlanApprovalModeProvider` does demote a stale session there
anyway, because a mode a surface displays should be true — but the enforcement is here.

**Why reads stay open.** MAF's own plan-mode instructions tell the agent to "do some exploratory
checks to help build a plan", and `gather_evidence`/`find_notes` are how a chemist gets an answer
at all. A gate over every tool would make `plan_only` unusable — the agent could not look anything
up to build the plan it needs approved — so the deployments that want the GxP posture would turn it
off, which is the worst outcome available. The line is drawn at state change
(`chemclaw.agent.authz.STATE_CHANGING_TOOLS`, plus every durable launcher), so an unapproved
session can research and propose and can do nothing else.
"""

import inspect
from collections.abc import Awaitable, Callable
from typing import Any

from agent_framework import AgentSession, FunctionInvocationContext, function_middleware
from agent_framework._harness._loop import ShouldContinueCallable, ShouldContinueResult

from chemclaw.agent.authz import STATE_CHANGING_TOOLS, AuthorizationError
from chemclaw.agent.harness_mode import (
    EXECUTE_MODE,
    current_plan_hash,
    revoke_execute,
    session_mode,
)
from chemclaw.agent.plan_approval_store import plan_approval_store
from chemclaw.agent.session_context import get_current_session


class PlanNotApprovedError(AuthorizationError):
    """A state-changing tool was called while the session's current plan has no human approval.

    An `AuthorizationError` subclass rather than a new type, because the two behaviours already
    built around that class are exactly the two wanted here: the audit middleware records the
    refusal as an `error` outcome, and `surface_authorization_denials` hands the model the message
    verbatim so it can tell the chemist why instead of guessing at "a temporary service issue".
    A subclass rather than the base so a caller — and a test — can still tell "you lack a role"
    apart from "nobody has approved this yet", which are different problems with different remedies.
    """


def gated_tools() -> frozenset[str]:
    """Every tool an unapproved plan may not call.

    Three sources, each owned where its knowledge lives:

    - `STATE_CHANGING_TOOLS` — the in-process writes, classified in `chemclaw.agent.authz` and held
      to a partition of the tool registry by `tests/test_authz.py`;
    - every enabled connector's own declaration (`state_changing_tool_names`): its endpoint's
      declared `state_changing` subset, plus every declared job, since a job is durable work by
      construction. **This half is what makes the gate cover the live repro** — `compute_xtb_energy`
      is a `calc` *endpoint* tool, not a job, so a set built only from in-process names and job
      names would have missed one of the two things the unapproved turn actually ran;
    - every enabled template launcher — a template starts a fixed sequence of the above, and is the
      one thing that can reach a job step without the model naming the job.

    Nothing here is a list core maintains about other people's tools, which is the property that
    matters: a bundle added next year is gated the day it is enabled, not the day someone
    remembers. Imported lazily because the connector and template registries reach the agent
    builder, which reaches this module.
    """
    from chemclaw.connectors.registry import state_changing_tool_names
    from chemclaw.templates.registry import template_tool_names

    return (
        STATE_CHANGING_TOOLS
        | frozenset(state_changing_tool_names())
        | frozenset(template_tool_names())
    )


async def plan_is_approved(session: AgentSession) -> bool:
    """Whether a human has approved the plan this session is proposing *right now*.

    Deliberately re-read per call rather than cached on the session: the question is about the plan
    as it stands at this instant, and the whole defect being fixed is an authorization that outlived
    the thing it authorized.
    """
    plan_hash = await current_plan_hash(session)
    decision = await plan_approval_store().decision(session.session_id, plan_hash)
    return bool(decision and decision[0])


@function_middleware
async def enforce_plan_approval(
    context: FunctionInvocationContext,
    call_next: Callable[[], Awaitable[None]],
) -> None:
    """Refuse a state-changing tool whose session has no approval for its current plan.

    Attach *inside* the audit middleware so a refusal is recorded, and inside
    `surface_authorization_denials` so the model is told why in a sentence a chemist can act on —
    the same layering `enforce_tool_authz` uses, for the same two reasons.

    A session with no plan at all is a session with no approved plan, so the first state-changing
    call in a fresh `plan_only` session is refused. That is the documented behaviour rather than an
    edge case: the agent is supposed to propose before it acts.

    Raises:
        PlanNotApprovedError: When the harness's plan gate is in play and the session's current
            plan has no recorded approval. The tool body never runs.
    """
    session = context.session or get_current_session()
    # No session means no harness, so there is no plan to approve and no autonomous loop to gate —
    # a template activity's tool step, or a one-shot CLI call. Not a hole: those paths still pass
    # through `enforce_tool_authz` and `authorize_trigger`, which is what governs them.
    if session is None:
        await call_next()
        return
    if context.function.name not in gated_tools():
        await call_next()
        return
    if await plan_is_approved(session):
        await call_next()
        return
    # Demote before refusing, so the mode `GET /sessions/{id}/plan` reports stops disagreeing with
    # what the session is actually allowed to do — a surface showing `execute` for a session that
    # cannot execute is how this defect stayed invisible. Guarded only to avoid writing the
    # external-change marker `set_agent_mode` leaves when there is no change to announce.
    if session_mode(session) == EXECUTE_MODE:
        revoke_execute(session)
    raise PlanNotApprovedError(
        f"{context.function.name} changes stored data or starts work, and the plan it is part of "
        "has not been approved yet; review the plan and approve it, then ask again"
    )


def approved_todos_remaining(
    inner: ShouldContinueCallable,
) -> Callable[..., Awaitable[ShouldContinueResult]]:
    """Wrap MAF's loop predicate so an unapproved session does not iterate autonomously.

    Returns an always-async predicate, which `ShouldContinueCallable` accepts (MAF awaits whatever
    it gets) and which is stricter than declaring the union back — a caller that awaits the result
    is then correct by type rather than by inspection.

    The tool gate above is what stops an unapproved plan *doing* anything, and this is not a second
    line of defence for the same thing — it stops a different waste. Without it an unapproved
    session still loops: `todos_remaining` sees open todos, the model is re-invoked, every write it
    reaches for is refused, and it spins until `harness_max_loop_iterations`. Burning a runaway
    guard's whole budget to accomplish nothing is not a safe failure, it is an expensive one.

    Ordering matters twice. The inner predicate runs first, so a session that would not loop anyway
    never pays for an approval lookup. And its *feedback* is preserved when it says continue — MAF
    lets a predicate return `(bool, str | None)` and routes that string to `next_message`, so
    dropping it would silently disable `todos_remaining_message`'s "these todos are still open"
    reminder and leave the loop re-invoking the model with nothing new.
    """

    async def _should_continue(**kwargs: Any) -> ShouldContinueResult:
        # A predicate may be sync or async (MAF's own contract), so accept either rather than
        # importing its private `_maybe_await` — three lines is cheaper than a dependency on an
        # underscore-prefixed helper that upstream is free to rename.
        raw = inner(**kwargs)
        keep_going = await raw if inspect.isawaitable(raw) else raw
        # Normalized exactly as MAF's own `_should_continue` does, so the tuple form keeps working.
        proceed, feedback = (
            (bool(keep_going[0]), keep_going[1])
            if isinstance(keep_going, tuple)
            else (bool(keep_going), None)
        )
        if not proceed:
            return (False, feedback)
        session = kwargs.get("session")
        if not isinstance(session, AgentSession):
            return (False, feedback)
        if await plan_is_approved(session):
            return (True, feedback)
        return (
            False,
            "The plan has not been approved, so autonomous execution stops here. Present the plan "
            "and wait for a human decision.",
        )

    return _should_continue
