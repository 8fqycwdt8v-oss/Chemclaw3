"""The harness's pre-execution approval, applied to the act rather than latched onto the session.

Under `harness_autonomy="plan_only"` the agent proposes a plan and a human approves it before
anything state-changing runs (D-137, D-167). The unit an approval authorizes is an action, so the
check is a `wrap_tool_call` middleware at the tool-invocation boundary, as for per-tool RBAC; a
check at turn start would read the previous plan before the model rewrites it.

Reads stay open: the agent must be able to research in order to build the plan it needs approved.
The line is drawn at state change (`authz.side_effecting_call`), so an unapproved session can
research and propose and do nothing else.
"""

import asyncio
import logging
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Final

from langchain.agents.middleware import wrap_tool_call

from chemclaw.agent.authz import AuthorizationError, side_effecting_call
from chemclaw.agent.framing import safe_id
from chemclaw.agent.plan_approval_store import plan_approval_store
from chemclaw.agent.plan_scope import step_declaration
from chemclaw.agent.plan_state import session_plan
from chemclaw.agent.profiles import AgentProfile
from chemclaw.agent.refusal_route import routed
from chemclaw.agent.session_store import owner_permits
from chemclaw.core.config import settings
from chemclaw.core.config.agent import HarnessAutonomy
from chemclaw.core.identity_context import get_current_actor
from chemclaw.core.ids import stable_hash
from chemclaw.core.metrics_bridge import degraded
from chemclaw.core.session_context import get_current_session_id
from chemclaw.core.turn_signals import RefusalReason

logger = logging.getLogger(__name__)


class PlanNotApprovedError(AuthorizationError):
    """A state-changing tool was called while the session's current plan has no human approval.

    An `AuthorizationError` subclass, so audit records the refusal and
    `surface_authorization_denials` relays the message to the model, while callers can still tell
    "no approval" from "missing role".
    """


# The identity of "no plan". `plan_identity` never returns it, since every session proposes it and
# an approval against it would approve the empty plan globally. Exported for the display route.
EMPTY_PLAN_HASH = stable_hash([])


def plan_identity(steps: Sequence[Mapping[str, Any]]) -> str | None:
    """The hash a human decision is recorded against, or `None` when there is no plan.

    Framework-free, so every reader keys approvals identically. `None` for an empty plan, because a
    constant every session proposes cannot be meaningfully decided on. It hashes each step's
    `content`
    and its declaration (via `plan_scope.step_declaration`), so a rewrite that widens `tools` is a
    different plan
    (D-2026-09-13-a-plan-identity-that-omits-the-scope-approves-a-plan-nobody-read). `status` is not
    hashed, so an approved plan does not revoke itself by making progress.

    Args:
        steps: The plan's steps as `write_todos` writes them, carrying `content` and the `tools`
            declaration. Callers drop steps without readable `content` first.
    """
    if not steps:
        return None
    return stable_hash([[str(step.get("content", "")), step_declaration(step)] for step in steps])


async def approved_scope(session_id: str, plan_hash: str | None) -> frozenset[str] | None:
    """The tools a live, unspent approval for this plan authorizes, or `None` when none stands.

    Spent approvals (`plan_approvals.consumed_at`) do not stand. `None` (no standing approval) and
    `frozenset()` (an approval of a plan declaring no state-changing tool) are different answers;
    callers that only need the first ask `approval_stands`.
    """
    if plan_hash is None:
        return None
    decision = await plan_approval_store().decision(session_id, plan_hash)
    if decision is None or not decision.approved:
        return None
    if not approval_binds(decision.actor, get_current_actor()):
        return None
    return decision.scope


async def plan_author(session_id: str, plan_hash: str) -> str | None:
    """Whose turn last wrote this plan, or `None` when nobody is recorded.

    Through this module's store handle, so the approval card and the gate ask the same store.
    """
    return await plan_approval_store().author(session_id, plan_hash)


def approval_binds(approver: str, actor: str | None) -> bool:
    """Whether an approval `approver` gave authorizes a turn `actor` is running.

    An approval is its approver's consent to their own next turn, not the session's
    (D-2026-09-27-in-a-shared-session-the-sender-governs). With no actor on the turn it is open when
    identity is not enforced and closed when it is; a blank approver binds nobody once identity is
    enforced.
    """
    if not actor or not approver:
        return not settings.entra_required
    return approver == actor


def may_decide(author: str | None, owner: str | None, actor: str | None) -> bool:
    """Whether `actor` may decide on a plan `author`'s turn last wrote, in `owner`'s session.

    Only the plan's author (from `plan_authors`, stamped when `write_todos` runs). With no recorded
    author the session owner decides, via `owner_permits`; that cannot lend a member the owner's
    authority, because an approval binds only its approver's turns.
    """
    if author:
        return bool(actor) and actor == author
    return owner_permits(owner, actor)


async def approval_stands(session_id: str, plan_hash: str | None) -> bool:
    """Whether a live, unspent human approval exists for this plan — the shared lookup.

    Asked to decide whether to show the decision card, so an approval authorizing nothing still
    counts as an approval.
    """
    return await approved_scope(session_id, plan_hash) is not None


def plan_approval_refusal(tool_name: str) -> PlanNotApprovedError:
    """The refusal an unapproved state-changing call earns.

    The sentence is for the chemist; the footer tells the model the sanctioned path is a
    `write_todos` call followed by waiting for approval. `tool_name` is not reduced: it is reached
    only
    past `authz.side_effecting_call`, so it is a name this repository owns.
    """
    return PlanNotApprovedError(
        routed(
            f"{tool_name} changes stored data or starts work, and the plan it is part of "
            "has not been approved yet; review the plan and approve it, then ask again",
            code="plan_not_approved",
            boundary="the harness plan gate",
            who_can_act="a human, by approving this session's current plan",
            sanctioned_path=(
                f"write the plan with write_todos so a step declares {tool_name}, then wait for "
                "that plan to be approved; read-only tools still run meanwhile"
            ),
        )
    )


def out_of_scope_refusal(tool_name: str, scope: frozenset[str]) -> PlanNotApprovedError:
    """The refusal a call outside the approved plan's declared tools earns.

    A different sentence from `plan_approval_refusal` because the remedy differs: rewrite the plan
    so
    a step declares this tool and have it approved. It names what the approval covers; the footer
    points at that list rather than repeating it, since the scope is model-authored and long. Same
    exception class, so audit and the refusal reason are unchanged. Each declared name is reduced by
    `framing.safe_id`, because it is model-authored text interpolated into the refusal grammar.
    """
    declared = ", ".join(safe_id(name) for name in sorted(scope)) or "no tools at all"
    # With an empty scope the first clause is dropped: pointing at an empty list would be a
    # fabricated
    # path.
    rewrite = (
        f"rewrite the plan so a step declares {tool_name} and ask for that plan to be approved"
    )
    path = (
        f"call one of the tools the approval already covers — they are named above — or {rewrite}"
        if scope
        else rewrite
    )
    return PlanNotApprovedError(
        routed(
            f"{tool_name} changes stored data or starts work, and the approved plan does not "
            f"list it: its steps declared {declared}. Rewrite the plan so a step declares "
            f"{tool_name}, and ask for the new plan to be approved.",
            code="plan_scope_excludes_tool",
            boundary="the tools the approved plan's steps declared",
            who_can_act="a human, by approving a plan whose steps declare this tool",
            sanctioned_path=path,
        )
    )


# What a turn ending with an unapproved plan asks the chemist, carried by the `approval_request`
# event whose empty `approval_id` marks the plan-approval shape.
PLAN_APPROVAL_PROMPT: Final = (
    "This plan is waiting for your decision. Approve it to let the agent carry out its "
    "state-changing steps on the next request, or reject it and ask for a different approach."
)


# The `RefusalReason` member naming this gate on the wire (`tool_failed` events). Consumers such as
# `evals/live.py` classify on it rather than on refusal prose, which can be reworded;
# `agent/audit._refusal_types` maps the exception onto it.
PLAN_GATE_REASON: Final[RefusalReason] = "plan_gate"


# The autonomy setting requiring human approval before anything executes. Both whether the gate is
# attached and whether a finished turn spends its approval compare against it.
PLAN_ONLY: HarnessAutonomy = "plan_only"


def harness_enabled_for(profile: AgentProfile) -> bool:
    """Whether the harness runs for `profile`: its own override, or the deployment's default."""
    return bool(
        settings.harness_enabled if profile.harness_enabled is None else profile.harness_enabled
    )


def autonomy_for(profile: AgentProfile) -> str:
    """The autonomy `profile` runs under: its own override, or the deployment's default.

    One resolver so every decision that reads it agrees.
    """
    return str(
        settings.harness_autonomy if profile.harness_autonomy is None else profile.harness_autonomy
    )


def gate_applies(profile: AgentProfile) -> bool:
    """Whether the plan gate governs an agent built for `profile` — the one predicate, twice used.

    `build_langgraph_agent` uses it to attach the middleware and `chemclaw.api.runner` to decide
    whether a finished turn spends its approval; they must be the same question, or a profile
    narrowed to `plan_only` would never spend its approval.
    """
    return harness_enabled_for(profile) and autonomy_for(profile) == PLAN_ONLY


async def consume_turn_approval(session_id: str) -> None:
    """Spend the approval this turn ran under, so the next request needs its own.

    Called once when a turn finishes, from `chemclaw.api.runner.run_turn`, at the end because the
    approved plan executes within the turn. Not from the runner's `finally`: on disconnect that runs
    under `CancelledError`, where an `await` re-raises and skips the rest of the cleanup (D-130).
    `spend_approval_after_teardown` covers torn-down turns that already acted.

    Session-wide, not hash-targeted, so a plan reworded mid-turn cannot leave the old plan's
    approval
    live. Idempotent. Never raises: the gate fails closed on the next call anyway.
    """
    try:
        await plan_approval_store().consume_all(session_id)
        # Nothing else to un-set: what the route displays is derived from this row.
    except Exception:
        degraded(
            logger,
            "plan_approval",
            "could not spend the plan approval for session %s; the gate still refuses an "
            "unreadable decision, so this costs an extra approval rather than authorizing one",
            session_id,
        )


#: Strong references to in-flight teardown spends, exactly `agent/turn_cost.py`'s `_PENDING`
#: shape and for the same reason: a bare `create_task` is garbage-collectable mid-write.
_PENDING_SPENDS: set[Any] = set()


def spend_approval_after_teardown(session_id: str) -> None:
    """Spend the session's approvals from a teardown path where awaiting is forbidden.

    A turn torn down after issuing a state-changing call has used its authorization (jobs and writes
    are not rolled back), so its approval is spent; otherwise dropping the connection would allow
    acting twice under one approval. Synchronous like `turn_cost.record_turn_cost`: the write runs
    on its own task, swallows its failure, and is held in `_PENDING_SPENDS`. The caller decides
    whether the turn acted.
    """

    async def _spend() -> None:
        try:
            await plan_approval_store().consume_all(session_id)
        except Exception:
            degraded(
                logger,
                "plan_approval",
                "could not spend session %s's approval after an abandoned turn; the gate still "
                "refuses an unreadable decision, so this risks an extra approval, never a free one",
                session_id,
            )

    try:
        task = asyncio.get_running_loop().create_task(_spend())
    except RuntimeError:  # no running loop — a synchronous caller has nowhere to schedule
        logger.warning("no event loop to spend session %s's approval after teardown", session_id)
        return
    _PENDING_SPENDS.add(task)
    task.add_done_callback(_PENDING_SPENDS.discard)


# --- the LangGraph wiring ------------------------------------------------------------------------


# The model-facing tool name `TodoListMiddleware` exposes for rewriting the plan. A literal so an
# upstream rename fails this file's test.
_PLAN_WRITE_TOOL = "write_todos"

# Returned when the batch's rewrite is unanswerable (two rewrites, or unparseable arguments);
# distinct from `None`, which means no rewrite.
_UNANSWERABLE: Final = object()


def rewrite_todos_in_batch(request: Any) -> Any:
    """This batch's `write_todos` argument, whole: `None` (no rewrite), items, or `_UNANSWERABLE`.

    Shared by `plan_after_batch` and `plan_link` so they agree on what the batch's rewrite is. Read
    off the assistant message, because `ToolNode` gives every call the same pre-batch state.
    `None` when the message is not found or has no `write_todos`; `_UNANSWERABLE` for two rewrites
    or a `todos` argument that is not a list of mappings.
    """
    messages = (request.state or {}).get("messages") or []
    this_call = request.tool_call.get("id")
    for message in reversed(messages):
        calls = getattr(message, "tool_calls", None) or []
        if not any(call.get("id") == this_call for call in calls):
            continue
        rewrites = [call for call in calls if call.get("name") == _PLAN_WRITE_TOOL]
        if not rewrites:
            return None
        if len(rewrites) > 1:
            # Two rewrites gathered concurrently: which one lands last is a race, so "the plan
            # this batch produces" has no answer.
            return _UNANSWERABLE
        items = (rewrites[0].get("args") or {}).get("todos")
        if not isinstance(items, list) or not all(isinstance(item, Mapping) for item in items):
            return _UNANSWERABLE
        return items
    return None


def plan_after_batch(request: Any) -> Any:
    """The plan this batch atomically produces: `None` (no rewrite), a list of steps, or
    `_UNANSWERABLE`.

    A call batched beside a plan rewrite is judged against the plan the batch writes. The canonical
    harness batch (status flip beside the next step's tool) keeps the same identity and passes on
    its
    standing approval; a genuine rewrite needs its own approval. `None` when the message cannot be
    found, in which case the pre-batch plan is judged.
    """
    items = rewrite_todos_in_batch(request)
    if items is None or items is _UNANSWERABLE:
        return items
    if not all(isinstance(item.get("content"), str) for item in items):
        return _UNANSWERABLE
    return items


async def _plan_behind(request: Any, session_id: str) -> list[dict[str, Any]] | None:
    """The plan this call is being judged against, or `None` when there is none to judge against.

    Normally `request.state["todos"]`. `SubAgentMiddleware` strips `todos` from a helper's state, so
    an *absent* key falls back to the session's checkpointed plan, the same source the plan route
    shows and `consume_turn_approval` spends against. An empty list is still an empty plan and is
    refused.
    """
    state = request.state or {}
    if "todos" in state:
        return [todo for todo in state.get("todos") or [] if isinstance(todo, dict)]
    return await session_plan(session_id)


async def _record_plan_author(request: Any) -> None:
    """Stamp this turn's sender as the author of the plan this `write_todos` call wrote.

    Keyed by `plan_identity` over the call's own arguments; an unanswerable batch stamps nothing.
    Never raises: without a stamp, `may_decide` falls back to the owner, which authorizes no member.
    """
    session_id = get_current_session_id()
    actor = get_current_actor()
    if not session_id or not actor:
        return
    steps = plan_after_batch(request)
    if steps is None or steps is _UNANSWERABLE:
        return
    plan_hash = plan_identity(steps)
    if plan_hash is None:
        return
    try:
        await plan_approval_store().record_author(session_id, plan_hash, actor)
    except Exception:
        degraded(
            logger,
            "plan_approval",
            "could not record who wrote session %s's plan; its owner decides on it instead",
            session_id,
        )


@wrap_tool_call
async def enforce_plan_approval(request: Any, handler: Callable[[Any], Any]) -> Any:
    """Refuse a state-changing tool whose session has no approval for its current plan.

    The plan is the turn's `todos` (`TodoListMiddleware`). `request.state` is a snapshot taken
    before
    the whole tool batch, so a call batched beside a `write_todos` is judged against the plan the
    batch writes (`plan_after_batch`); a status flip keeps the identity, a real rewrite needs its
    own
    approval, and an unanswerable batch is refused without asking the store.

    An approval authorizes only the tools its plan declared
    (D-2026-09-12-an-approval-that-names-no-tool-authorizes-every-tool). The scope is read from the
    recorded `plan_approvals.scope`, never from the live plan, so a later rewrite cannot widen it;
    since the identity covers each step's declaration, a widened plan has no approval at all.

    Raises:
        PlanNotApprovedError: The plan behind this call has no live approval
            (`plan_approval_refusal`), or has one that does not name this tool
            (`out_of_scope_refusal`). The body never runs.
    """
    name = request.tool_call["name"]
    if name == _PLAN_WRITE_TOOL:
        # Not gated — writing the plan is how a turn asks for approval — but *stamped*: the turn
        # that last wrote a plan is its author, and only its author may decide on it (`may_decide`).
        result = await handler(request)
        await _record_plan_author(request)
        return result
    # The *call* rather than the tool: `write_file` is durable under `/memories/` but turn-local
    # under
    # `/scratch/`, which an unapproved turn needs to draft a plan.
    if not side_effecting_call(name, request.tool_call.get("args") or {}):
        return await handler(request)
    session_id = get_current_session_id()
    # No session means no plan to approve. Session-less paths are governed elsewhere:
    #
    # - A template `agent` step never reaches this middleware (`harness_enabled=False`);
    #   `step_profile`
    #   removes every side-effecting tool the step did not declare before the graph is built.
    # - A template `tool` step reaches it via `invoke_governed`; the tool is named in a reviewed,
    #   git-committed template, and an agent-composed workflow may name no side-effecting tool.
    # - The CLI stamps no session id; it is the operator's own terminal.
    #
    # Which side-effecting tools no other gate refuses for such calls is held as a set by
    # `tests/test_plan_gate.py`.
    if not session_id:
        return await handler(request)
    rewritten = plan_after_batch(request)
    if rewritten is _UNANSWERABLE:
        raise plan_approval_refusal(name)
    steps = rewritten if rewritten is not None else await _plan_behind(request, session_id)
    scope = None if steps is None else await approved_scope(session_id, plan_identity(steps))
    if scope is None:
        raise plan_approval_refusal(name)
    if name not in scope:
        raise out_of_scope_refusal(name, scope)
    return await handler(request)
