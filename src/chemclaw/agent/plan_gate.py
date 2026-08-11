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

import logging
from collections.abc import Callable, Sequence
from typing import Any

from langchain.agents.middleware import wrap_tool_call

from chemclaw.agent.authz import AuthorizationError, side_effecting_tools
from chemclaw.agent.plan_approval_store import plan_approval_store
from chemclaw.agent.plan_state import session_todos
from chemclaw.agent.profiles import AgentProfile
from chemclaw.core.config import settings
from chemclaw.core.ids import stable_hash
from chemclaw.core.metrics_bridge import degraded
from chemclaw.core.session_context import get_current_session_id

logger = logging.getLogger(__name__)


class PlanNotApprovedError(AuthorizationError):
    """A state-changing tool was called while the session's current plan has no human approval.

    An `AuthorizationError` subclass rather than a new type, because the two behaviours already
    built around that class are exactly the two wanted here: the audit middleware records the
    refusal as an `error` outcome, and `surface_authorization_denials` hands the model the message
    verbatim so it can tell the chemist why instead of guessing at "a temporary service issue".
    A subclass rather than the base so a caller — and a test — can still tell "you lack a role"
    apart from "nobody has approved this yet", which are different problems with different remedies.
    """


# The identity of "no plan". A constant rather than a fact about any session, which is precisely
# why `plan_identity` refuses to return it: a decision recorded against it would say "someone
# approved the empty plan", and every session proposes that whenever it holds no todos. Exported
# for the display route, which has to show *an* identity even when there is nothing to decide on.
#
# It lived in `harness_mode` while the mode did. It belongs beside the function that computes plan
# identities, because the two are one rule read from either end.
EMPTY_PLAN_HASH = stable_hash([])


def plan_identity(items: Sequence[str]) -> str | None:
    """The hash a human decision is recorded against, or `None` when there is no plan.

    The decision, framework-free, so both engines bind an approval to the same identity. A second
    hashing rule would be an approval that is valid under one engine and unrecognised under the
    other — for a *durable* row that outlives the turn that wrote it, which is worse than a
    divergence in wording.

    `None` for an empty plan is D-167's first fix, and it is a rule rather than a guard: hashing
    "nothing" yields a constant every session in every deployment also proposes, so a decision
    recorded against it approves the empty plan globally rather than this session's work. An
    identity nobody can distinguish is not something a person can meaningfully decide about.
    """
    return stable_hash(list(items)) if items else None


async def approval_stands(session_id: str, plan_hash: str | None) -> bool:
    """Whether a live, unspent human approval exists for this plan — the shared lookup.

    Folds "and it has not already been spent" in, because consumption is recorded on the decision
    itself (`plan_approvals.consumed_at`) rather than in session state. That fold is D-167's last
    fix: the spent-ness of an approval used to live where a pod roll could drop it while the
    approval survived.
    """
    if plan_hash is None:
        return False
    decision = await plan_approval_store().decision(session_id, plan_hash)
    return bool(decision and decision[0])


def plan_approval_refusal(tool_name: str) -> PlanNotApprovedError:
    """The refusal an unapproved state-changing call earns — one sentence, both engines."""
    return PlanNotApprovedError(
        f"{tool_name} changes stored data or starts work, and the plan it is part of "
        "has not been approved yet; review the plan and approve it, then ask again"
    )


def gated_call(tool_name: str) -> bool:
    """Whether this tool is one the plan gate governs at all."""
    return tool_name in side_effecting_tools()


# The autonomy setting that asks for the approval-first posture — the value `harness_autonomy`
# takes when a human must approve the plan before anything executes. A constant because two
# decisions compare against it (whether the tool gate is attached at all, and whether a finished
# turn spends its approval), and a deployment that ran one of the two would be one that cannot do
# anything or one that cannot be stopped.
PLAN_ONLY = "plan_only"


def harness_enabled_for(profile: AgentProfile) -> bool:
    """Whether the harness runs for `profile`: its own override, or the deployment's default."""
    return bool(
        settings.harness_enabled if profile.harness_enabled is None else profile.harness_enabled
    )


def autonomy_for(profile: AgentProfile) -> str:
    """The autonomy `profile` runs under: its own override, or the deployment's default.

    **One resolver, because every decision that reads it must agree.** The
    `X if profile.X is None else profile.X` rule was written out three times — for whether to wire
    the harness at all, for the starting mode and the loop predicate, and for whether the tool gate
    is attached. That triplication cost a live defect once: `chemclaw.api.runner` read `settings`
    directly instead, so a profile narrowed to `plan_only` under a global `execute` got the gate
    attached and its approval never spent, and one decision authorized every later turn
    (`gate_applies` records it). A rule spelled out in three places is a rule three places can
    disagree about.

    These two lived in `harness_mode.py` while that module existed to retract MAF's `mode_set`
    tool from the model and hold the plan/execute mode beside the approval. Both are gone — nothing
    advertises such a tool here, and the mode was a second answer to "may this session act" that
    could and did disagree with the approval (DARK-1). What was left was these predicates, which
    belong beside the gate that is their only reason to exist.
    """
    return str(
        settings.harness_autonomy if profile.harness_autonomy is None else profile.harness_autonomy
    )


def gate_applies(profile: AgentProfile) -> bool:
    """Whether the plan gate governs an agent built for `profile` — the one predicate, twice used.

    `build_langgraph_agent` decides from it whether to attach the middleware, and
    `chemclaw.api.runner`
    decides from it whether a finished turn spends its approval. **They have to be the same
    question.** Reading `settings` directly in the runner was a real gap: a profile setting
    `harness_autonomy="plan_only"` under a global `execute` got the gate attached and its approval
    never spent, so one decision authorized every later turn — DARK-1 again, for exactly the
    sessions a deployment had narrowed on purpose.

    The two dimensions are resolved just above, which is also where `build_langgraph_agent` reads
    them — so "does the gate apply" and "does the harness attach its todo list" can no longer be
    answered by two copies of the same fallback rule.
    """
    return harness_enabled_for(profile) and autonomy_for(profile) == PLAN_ONLY


async def consume_turn_approval(session_id: str) -> None:
    """Spend the approval this turn ran under, so the next request needs its own.

    Called once when a turn finishes, from `chemclaw.api.runner.run_turn`. At the *end* rather than
    the start because the graph's own loop is what executes an approved plan and it runs inside a
    single turn: consuming on entry would refuse the plan's own second iteration.

    **Not from the runner's `finally`, and that is not a style preference.** `run_turn` is an async
    generator whose `finally` also runs on the disconnect path — which production reaches through
    `CancelledError`, not `aclose()` (D-130). An `await` there re-raises the cancellation
    immediately and *everything after it in the block is skipped*: the budget booking, the turn
    metrics, `end_turn`, and all five context-var resets. Leaking the ambient identity of a
    disconnected turn into the next turn on that worker is a worse defect than the one this
    function exists to fix. So it is called on the two paths where awaiting is safe, and a turn torn
    down *before* it answered deliberately does not spend the approval: a turn that was undone has
    not used its authorization.

    A turn torn down *after* it answered is not rolled back at all — its answer is committed
    history, and deleting that was a real defect (`chemclaw.api.runner`) — so the cancellation can
    land inside this call, before the consumption is written. The approval then survives into the
    next request: the same one-turn residual D-167 already accepts on the disconnect path, and now
    the *only* one, since the write itself is durable (`plan_approvals.consumed_at`) rather than a
    session-state marker an eviction could drop long afterwards.

    Idempotent, because it is called on two paths that can both run for one turn and because the
    store spends only a live approval: asking twice costs a no-op UPDATE, not a second plan's worth
    of authorization.

    Never raises. A store that cannot be reached must not turn a completed turn into a failed one;
    the gate itself fails closed on the next call regardless, because an unreadable decision is not
    an approval.
    """
    try:
        plan_hash = plan_identity(await session_todos(session_id))
        if plan_hash is None:
            return
        decision = await plan_approval_store().decision(session_id, plan_hash)
        if decision and decision[0]:
            await plan_approval_store().consume(session_id, plan_hash)
            # Nothing else to un-set. There used to be a session *mode* representing the same
            # authorization, which had to be revoked here or the surface kept reporting `execute`
            # for a session whose every state-changing call would now be refused — the same
            # disagreement between the displayed state and the enforced one that let DARK-1 go
            # unnoticed. The mode is gone; the route derives what it displays from this row.
    except Exception:  # noqa: BLE001 - a turn must not fail on its way out
        degraded(
            logger,
            "plan_approval",
            "could not spend the plan approval for session %s; the gate still refuses an "
            "unreadable decision, so this costs an extra approval rather than authorizing one",
            session_id,
        )


# --- the LangGraph wiring ------------------------------------------------------------------------


@wrap_tool_call
async def enforce_plan_approval(request: Any, handler: Callable[[Any], Any]) -> Any:
    """Refuse a state-changing tool whose session has no approval for its current plan.

    The LangGraph twin of `enforce_plan_approval`, over the same identity (`plan_identity`), the
    same durable store (`approval_stands`) and the same sentence (`plan_approval_refusal`). An
    approval is a *durable row* that outlives the turn that wrote it, so the two engines agreeing on
    what it identifies matters more here than anywhere else in the migration: a hash computed
    differently would silently invalidate every decision a chemist has already made.

    **The plan is read from graph state, not from an ambient session object.** `TodoListMiddleware`
    owns `todos` and the `write_todos` tool that maintains them, and `request.state` is this turn's
    view of it — so the gate asks the plan as it stands at this instant, which is the property
    `plan_is_approved`'s docstring insists on and had to arrange deliberately under MAF.

    **`awaiting_jobs` needs no exclusion here.** Under MAF a todo waiting on a durable job was
    marked by prefixing its description, and the identity had to filter those out or an approved
    plan revoked its own approval the moment it launched a job. Here they are a separate state
    field (`chemclaw.agent.state.ChemclawState`), so they are not in `todos` and there is nothing to
    filter.

    Raises:
        PlanNotApprovedError: The plan behind this call has no live approval. The body never runs;
            the audit middleware records the refusal and `surface_authorization_denials` relays
            the reason to the model.
    """
    name = request.tool_call["name"]
    if not gated_call(name):
        return await handler(request)
    session_id = get_current_session_id()
    # No session means no plan to approve and no autonomous loop to gate — a template activity's
    # tool step, or a one-shot CLI call. Not a hole: those paths still pass through
    # `enforce_tool_authz` and `authorize_trigger`, which is what governs them.
    if not session_id:
        return await handler(request)
    todos = (request.state or {}).get("todos") or []
    if await approval_stands(session_id, plan_identity([todo["content"] for todo in todos])):
        return await handler(request)
    raise plan_approval_refusal(name)
