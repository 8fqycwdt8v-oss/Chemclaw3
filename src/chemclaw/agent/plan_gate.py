"""The harness's pre-execution approval, applied to the act rather than latched onto the session.

`SECURITY.md` and `docs/guides/harness-konzept.md` §6 describe an approval gate: under
`harness_autonomy="plan_only"` the agent proposes a plan and waits for a human before executing.
D-137 built the human-only path into execute mode and retracted MAF's self-service `mode_set` tool,
which closed the hole where the *model* granted itself autonomy. What it did not do was make the
approval mean anything afterwards, and a live pass found the consequence (DARK-1):

    approve a four-item plan               → mode flips to execute, a plan_approvals row is written
    ask a completely different question    → new plan_hash, approved=false, mode still execute,
                                             and the turn autonomously ran compute_xtb_energy and
                                             record_knowledge_note — a knowledge-graph write

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
up to build the plan it needs approved — so the deployments that want the gate would turn it
off, which is the worst outcome available. The line is drawn at state change
(`chemclaw.agent.authz.STATE_CHANGING_TOOLS`, plus every durable launcher), so an unapproved
session can research and propose and can do nothing else.
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
from chemclaw.core.config import settings
from chemclaw.core.config.agent import HarnessAutonomy
from chemclaw.core.ids import stable_hash
from chemclaw.core.metrics_bridge import degraded
from chemclaw.core.session_context import get_current_session_id
from chemclaw.core.turn_signals import RefusalReason

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


def plan_identity(steps: Sequence[Mapping[str, Any]]) -> str | None:
    """The hash a human decision is recorded against, or `None` when there is no plan.

    The decision, framework-free, so both engines bind an approval to the same identity. A second
    hashing rule would be an approval that is valid under one engine and unrecognised under the
    other — for a *durable* row that outlives the turn that wrote it, which is worse than a
    divergence in wording.

    `None` for an empty plan is D-167's first fix, and it is a rule rather than a guard: hashing
    "nothing" yields a constant every session in every deployment also proposes, so a decision
    recorded against it approves the empty plan globally rather than this session's work. An
    identity nobody can distinguish is not something a person can meaningfully decide about.

    **It takes the steps, not their text, and that is the whole of
    `D-2026-09-13-a-plan-identity-that-omits-the-scope-approves-a-plan-nobody-read`.** Hashing
    `content` alone left the freshness guard on `POST /sessions/{id}/plan/decision` unable to see a
    rewrite that kept every step's text and widened its `tools`: the chemist's own hash still
    matched, and the route stamped the *live* plan's declaration as what they had approved. Driven
    through the real route, an approval shown as authorizing nothing came back authorizing
    `record_knowledge_note` and `watch_for`, and both ran. So what a decision is keyed on is now
    what a decision is about — each step's `content` beside its declaration, read by the same
    `plan_scope.step_declaration` the recorded scope is derived from, so the two cannot disagree
    about what a malformed `tools` means.

    **The status is still not in it**, which is the property the content-only rule existed for: the
    canonical "tick the completed step, run the next one" batch leaves the identity alone, so an
    approved plan does not revoke itself by making progress.

    Every `plan_approvals` row written before this change is keyed on the old, narrower hash and can
    no longer be matched. That is the fail-closed direction and it is cheap: an approval authorizes
    one turn and is spent when that turn ends (D-167), so the cost is a chemist re-approving a plan
    that is still on their screen.

    Args:
        steps: The plan's steps as `write_todos` writes them — mappings carrying `content` and the
            `tools` declaration. A step with no readable `content` contributes its empty text; the
            callers (`plan_state.session_plan`, `plan_after_batch`) drop such steps before this.
    """
    if not steps:
        return None
    return stable_hash([[str(step.get("content", "")), step_declaration(step)] for step in steps])


async def approved_scope(session_id: str, plan_hash: str | None) -> frozenset[str] | None:
    """The tools a live, unspent approval for this plan authorizes, or `None` when none stands.

    Folds "and it has not already been spent" in, because consumption is recorded on the decision
    itself (`plan_approvals.consumed_at`) rather than in session state. That fold is D-167's last
    fix: the spent-ness of an approval used to live where a pod roll could drop it while the
    approval survived.

    **`None` and an empty set are different answers, and collapsing them would lose the control**
    (`D-2026-09-12-an-approval-that-names-no-tool-authorizes-every-tool`). `None` means nobody has
    approved this plan, or the approval has had its turn. `frozenset()` means somebody approved a
    plan that declared no state-changing tool — a real and common decision, and one that refuses
    every gated call while still being an approval. A caller that only needs the first question
    asks `approval_stands`.
    """
    if plan_hash is None:
        return None
    decision = await plan_approval_store().decision(session_id, plan_hash)
    if decision is None or not decision.approved:
        return None
    return decision.scope


async def approval_stands(session_id: str, plan_hash: str | None) -> bool:
    """Whether a live, unspent human approval exists for this plan — the shared lookup.

    The question `api/runner._pending_plan_approval` asks to decide whether to show the decision
    card: it is about *whether the chemist has been asked*, not about what any one call may do, so
    it must not read an approval that authorizes nothing as no approval at all.
    """
    return await approved_scope(session_id, plan_hash) is not None


def plan_approval_refusal(tool_name: str) -> PlanNotApprovedError:
    """The refusal an unapproved state-changing call earns — one sentence, both engines.

    The sentence is the chemist's; the footer (`agent/refusal_route`) is the model's, and this is
    the gate where the two readers want most different things. The chemist wants "nobody has
    approved this"; the model wants to know that a plan is a thing it can *write* — the declaration
    a step carries is what an approval is later keyed on (`plan_scope.step_declaration`), so the
    sanctioned path really is a `write_todos` call it can make right now, followed by a wait. Left
    to the sentence alone, the two moves available are stalling and retrying the same call.

    `tool_name` is not reduced here, and that is not an oversight: this is reached only past
    `authz.side_effecting_call`, so the name is a member of a set this repository owns. The two
    refusal sites that interpolate a string nothing validated are `authz.authorize_tool` and
    `out_of_scope_refusal` below, and only those two reduce.
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

    A *different sentence* from `plan_approval_refusal`, and deliberately so: "nobody has approved
    this" and "this was approved and does not cover that tool" are different problems with
    different remedies, and a chemist reading the second while the plan is visibly approved would
    reasonably conclude the gate was broken. It names what the approval does cover, because the
    remedy — rewrite the plan so a step declares this tool, and have it approved — is only obvious
    once the reader can see the declaration they are outside of.

    Same exception class, so the audit outcome, the `plan_gate` refusal reason and the relay to the
    model are unchanged: the class answers "which gate refused", and the sentence answers "why".

    **This was the only one of the eleven gated refusals that already named a tool the model could
    call instead**, and measuring that asymmetry is what produced `agent/refusal_route` — five
    others named an action in prose and five named nothing at all. Its footer therefore
    *points at* the list rather than repeating it: the scope is a model-authored declaration bounded
    only in count (`plan_max_tools_per_step` × `plan_max_steps`), and `core/config/agent.py` records
    a measured 600,192-character sentence built out of one — a second copy in the footer would
    double a length that is bounded only at `tool_authz._refusal_message`, and unbounded in the
    exception, the log and the audit row before it.

    **Each declared name is reduced by `framing.safe_id`.** `plan_scope.step_declaration` keeps
    every string in a step's `tools` list, so this is the one refusal that interpolates text the
    *model itself* authored — which is exactly the shape that could spell a second
    `sanctioned path:` field and have the model read it as this system's routing. The charset
    cannot spell a field, a separator or an envelope delimiter, and a real tool name is unchanged
    by it.
    """
    declared = ", ".join(safe_id(name) for name in sorted(scope)) or "no tools at all"
    # **The first clause is dropped when the scope is empty, because it would name nothing.**
    # An approval whose steps declare no tools is not a corner case:
    # `D-2026-09-12-an-approval-that-names-no-tool-authorizes-every-tool` calls it a real and
    # common shape. With it, the sentence reads "its steps declared no tools at all" while the
    # path read "call one of the tools the approval already covers — they are named above", which
    # points at an empty list. That is precisely the fabricated path `refusal_route`'s docstring
    # forbids — worse than none, because it sends the model round the loop again against a wall
    # that has not moved — and it is the one arm where the clause is dead rather than merely terse.
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


# What a turn that ends holding an unapproved plan asks the chemist, carried by the
# `approval_request` event whose empty `approval_id` marks it as the plan-approval shape
# (`api/events.ApprovalRequestEvent` documents exactly that discriminator). Beside the refusal
# above because they are two sentences about one gate, and a surface shows them in sequence:
# the refusal says why a step did not run, this says what to do about it.
PLAN_APPROVAL_PROMPT: Final = (
    "This plan is waiting for your decision. Approve it to let the agent carry out its "
    "state-changing steps on the next request, or reject it and ask for a different approach."
)


# The name a consumer tells this gate's refusal by, once the refusal has left the process as a
# `tool_failed` event. `core/turn_signals.RefusalReason` is the closed set it belongs to and
# `agent/audit._refusal_types` is what maps this gate's exception onto it; this constant is the
# member, kept here because `evals/live.py` classifies on it and reads it from the gate it names.
#
# **It replaces a substring match on the refusal sentence**, and that is the whole point. A refusal
# is prose written for a chemist, so it is exactly the kind of text somebody improves — and the
# eval harness held a *copy* of one phrase of it ("has not been approved yet") as its only way to
# tell "the gate held" from "a tool fell over". Those two findings are opposites: one is the
# control working, the other is a fault. A reword would have flipped every gated turn from the
# first to the second, silently and retroactively, with every test still green because the tests
# pinned the same copy. A discriminator on the wire cannot drift that way — a consumer either reads
# the field or does not.
#
# `plan_gate_failure_reason` used to live beside this and recovered the same verdict by testing
# whether a *detail string* started with `"PlanNotApprovedError:"`. It is gone: the exception is in
# scope in `agent/tool_authz.announce_tool_failures`, `agent/audit.refusal_reason` already names
# all five gates from the class, and re-deriving one of the five downstream from a truncated string
# was a second opinion about a question the audit trail had already answered.
PLAN_GATE_REASON: Final[RefusalReason] = "plan_gate"


# The autonomy setting that asks for the approval-first posture — the value `harness_autonomy`
# takes when a human must approve the plan before anything executes. A constant because two
# decisions compare against it (whether the tool gate is attached at all, and whether a finished
# turn spends its approval), and a deployment that ran one of the two would be one that cannot do
# anything or one that cannot be stopped.
PLAN_ONLY: HarnessAutonomy = "plan_only"


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
    immediately and *everything after it in the block is skipped*: the budget booking and the
    `turn_costs` row it writes, and the contextvar resets `chemclaw.api.runner._unstamp`
    performs. Leaking the ambient identity of a disconnected turn into the next turn on that
    worker is a worse defect than the one this
    function exists to fix. So it is called on the two paths where awaiting is safe, and a turn torn
    down *before* it answered deliberately does not spend the approval: a turn that was undone has
    not used its authorization.

    A turn torn down *after* it answered is not rolled back at all — its answer is committed
    history, and deleting that was a real defect (`chemclaw.api.runner`) — so the cancellation can
    land inside this call, before the consumption is written. The approval then survives into the
    next request: the same one-turn residual D-167 already accepts on the disconnect path, and now
    the *only* one, since the write itself is durable (`plan_approvals.consumed_at`) rather than a
    session-state marker an eviction could drop long afterwards.

    **Session-wide, not hash-targeted, and that closes a leak the targeted form had.** Spending
    only the approval matching the plan *as it stands at turn end* left a hole a mid-turn reword
    opened: the turn hashes plan B, finds no decision for it, and returns — while plan A's
    approval stays live indefinitely, re-authorizing any future turn whose todo list hashes back
    to A. "The turn used its authorization" is a fact about the session's turn, whatever identity
    the plan drifted to, so every live approval the session holds is spent. That also removes the
    checkpoint read this function used to pay to recompute a hash it no longer needs — and with
    it the unreadable-plan branch, since there is nothing left to fail to read.

    Idempotent, because it is called on two paths that can both run for one turn and because the
    store spends only live approvals: asking twice costs a no-op UPDATE, not a second plan's worth
    of authorization.

    Never raises. A store that cannot be reached must not turn a completed turn into a failed one;
    the gate itself fails closed on the next call regardless, because an unreadable decision is not
    an approval.
    """
    try:
        await plan_approval_store().consume_all(session_id)
        # Nothing else to un-set. There used to be a session *mode* representing the same
        # authorization, which had to be revoked here or the surface kept reporting `execute`
        # for a session whose every state-changing call would now be refused — the same
        # disagreement between the displayed state and the enforced one that let DARK-1 go
        # unnoticed. The mode is gone; the route derives what it displays from this row.
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

    The abandoned-turn half of D-167's rule. A turn torn down mid-flight used to keep its approval
    armed on the argument that "a turn that was undone has not used its authorization" — and that
    premise is false the moment the turn has *issued a state-changing call*: durable jobs, note
    proposals and calibration rows are not rolled back by the teardown, so the authorization was
    used. Leaving it live made "drop the connection after the tools ran" a way to act under one
    approval twice, the same shape as the token-budget bypass that vetoed stream_events v3.

    Synchronous by the same contract as `turn_cost.record_turn_cost`: the caller is a
    cancellation path in which an `await` re-raises immediately and skips everything after it. The
    write runs on its own task, swallows its own failure, and is held in `_PENDING_SPENDS` until
    it finishes. The caller decides *whether* the turn acted; this only spends.
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


# The tool `TodoListMiddleware` exposes for rewriting the plan. Named here rather than imported
# because it is the *model-facing* name the batch is inspected for, and the middleware publishes it
# as a literal too — a rename upstream must fail this file's test, not silently reopen the hole.
_PLAN_WRITE_TOOL = "write_todos"

# The sentinel `plan_after_batch` returns when the batch's rewrite is unanswerable — two rewrites
# in one message, or one whose arguments do not parse. Its own object rather than `None`, which
# already means "no rewrite in this batch".
_UNANSWERABLE: Final = object()


def rewrite_todos_in_batch(request: Any) -> Any:
    """This batch's `write_todos` argument, whole: `None` (no rewrite), items, or `_UNANSWERABLE`.

    The raw half of `plan_after_batch` — the batch-scoped lookup both it and
    `plan_link.plan_link_from_todos` need, extracted so the two readings cannot drift on what
    counts as "this batch's rewrite". `plan_after_batch` checks the items are readable and hands
    them to the identity hash; `plan_link`'s caller reads `status` off them as well, to find the
    step the batch marks `in_progress`. Either way the items travel unreduced, which is what lets
    the identity cover each step's declaration as well as its text.

    The batch is read off the *message*, not the state, because that is the only place the other
    calls in it are visible: `ToolNode` hands each call a runtime built from one pre-batch
    snapshot, so state cannot answer "what else is running right now" by construction.

    Returns `None` when the message cannot be found, or when the batch carries no `write_todos`
    call, rather than guessing. `_UNANSWERABLE` when it does but the batch is not one clean
    rewrite: two rewrites gathered concurrently (which lands last is a race), or a `todos` argument
    that is not a list of mappings.
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
    """The plan this batch atomically produces: `None` (no rewrite), a list, or `_UNANSWERABLE`.

    **This is what replaced refusing every gated call batched with a rewrite, and the difference
    is the canonical harness shape.** "Tick the completed step and do the next one" is
    `TodoListMiddleware`'s own pattern — one message carrying `write_todos` (status flip) beside
    the step's tool call — and the blanket refusal denied it on *every* step of a plan: the model
    retried, an identical retry then tripped `refuse_repeated_calls`, and a fully approved
    multi-step plan could burn its whole loop allowance making no progress. A status flip does not
    perturb `plan_identity` (the hash covers each step's `content` and its declaration, never its
    `status` — which is what lets an approved plan start a job without revoking itself), so judging
    the call against the plan the batch *writes*
    lets the canonical shape through — while the DARK-1 batch (`write_todos(plan B)` beside a
    write, under plan A's approval) still refuses, because plan B has no approval. Fails closed on
    anything unanswerable: two rewrites in one message, or arguments the middleware itself would
    reject.

    Returns `None` when the message cannot be found rather than guessing. That is not a hole: the
    approval check then runs against the pre-batch plan, which is the behaviour this function's
    predecessor was added to tighten, not a new one.

    **It returns the steps, not their text.** It returned the text for as long as `plan_identity`
    took text, and the two changed together in
    `D-2026-09-13-a-plan-identity-that-omits-the-scope-approves-a-plan-nobody-read`: a batch whose
    rewrite keeps every step's content and widens its declaration is now a *different* plan here,
    so it is refused for having no approval at all rather than being checked against the standing
    one. `enforce_plan_approval`'s docstring records why that is the same answer by a shorter route.
    """
    items = rewrite_todos_in_batch(request)
    if items is None or items is _UNANSWERABLE:
        return items
    if not all(isinstance(item.get("content"), str) for item in items):
        return _UNANSWERABLE
    return items


async def _plan_behind(request: Any, session_id: str) -> list[dict[str, Any]] | None:
    """The plan this call is being judged against, or `None` when there is none to judge against.

    Normally the turn's own state: `TodoListMiddleware` owns `todos` and `request.state` is this
    turn's view of it.

    **Inside a specialist there is no such view, and reading the absence as an empty plan refused
    everything.** `SubAgentMiddleware` builds a subagent's input from the supervisor's state minus
    `_EXCLUDED_STATE_KEYS`, which contains `todos` — so under the shipped `plan_only` posture a
    delegated specialist saw no plan, matched no approval, and every state-changing call it made was
    refused. The team was unusable with the default autonomy, and the failure looked like an
    authorization decision rather than a missing key.

    So an *absent* `todos` key falls back to the session's checkpointed plan — the same source
    `api/routes/plan.py` shows a chemist and `consume_turn_approval` spends against, which is what
    keeps the three from disagreeing. An absent key and an empty list are told apart deliberately:
    a supervisor that genuinely proposed nothing still has the key, and still gets refused.
    """
    state = request.state or {}
    if "todos" in state:
        return [todo for todo in state.get("todos") or [] if isinstance(todo, dict)]
    return await session_plan(session_id)


@wrap_tool_call
async def enforce_plan_approval(request: Any, handler: Callable[[Any], Any]) -> Any:
    """Refuse a state-changing tool whose session has no approval for its current plan.

    The decision behind `enforce_plan_approval`, over the same identity (`plan_identity`), the
    same durable store (`approval_stands`) and the same sentence (`plan_approval_refusal`). An
    approval is a *durable row* that outlives the turn that wrote it, so the two engines agreeing on
    what it identifies matters more here than anywhere else in the migration: a hash computed
    differently would silently invalidate every decision a chemist has already made.

    **The plan is read from graph state, not from an ambient session object.** `TodoListMiddleware`
    owns `todos` and the `write_todos` tool that maintains them, and `request.state` is the turn's
    view of it.

    **`request.state` is a snapshot taken before the whole tool batch, and that is a hole this gate
    has to close itself.** `ToolNode` builds every call's runtime from one `_extract_state` and then
    `asyncio.gather`s them, so a `write_todos` in the *same assistant message* has not landed yet
    when this runs. Reproduced against the real graph: turn 1 writes plan A and a chemist approves
    it; turn 2 emits `write_todos(plan B)` and `record_knowledge_note(...)` together; the gate sees
    plan A, the approval stands, and the write executes under an approval given for a different
    plan. That is the DARK-1 sequence this module exists to prevent.

    So a gated call that arrives beside a plan rewrite is judged against the plan the batch
    *writes* — read from the `write_todos` arguments in the same message (`plan_after_batch`) —
    because the batch is atomic to the model and its post-state is the one answer to "which plan
    is this call part of" that holds under either execution order. An earlier version refused the
    whole shape outright, which failed closed and also failed the canonical harness pattern:
    "tick the completed step, do the next one" batches a status-flip `write_todos` beside every
    step's tool call, and refusing it livelocked approved multi-step plans against the repeat
    guard. A status flip hashes identically (`plan_identity` reads `content` and the declaration,
    not `status`), so the canonical shape passes on its standing approval; a genuine rewrite is
    approved or refused on *its own* hash, which is exactly D-167's question. Anything
    unanswerable — two rewrites in one batch, unparseable arguments — still refuses without asking
    the store.

    **Waiting jobs need no exclusion here.** Under MAF a todo waiting on a durable job was marked by
    prefixing its description, and the identity had to filter those out or an approved plan revoked
    its own approval the moment it launched a job. Nothing writes that bookkeeping into `todos` now
    — a launched job is a `job_records` row and a `session_events` push-back — so the list this
    hashes is the plan and only the plan, and there is nothing to filter.

    **An approval authorizes the tools its plan declared, and nothing else**
    (`D-2026-09-12-an-approval-that-names-no-tool-authorizes-every-tool`). Until that decision this
    function asked one question — does an approval stand for this plan — so an approval recorded
    against a one-line, read-only plan authorized every name in `authz.side_effecting_tools()`.
    Each step now declares the tools it will call (`agent/plan_scope.py`), the decision stamps the
    union of those declarations onto the row (`plan_approvals.scope`), and the scope is read back
    from **there** rather than from the live plan — so a rewrite cannot widen an approval that has
    already been given.

    **That direction was necessary and not sufficient**
    (`D-2026-09-13-a-plan-identity-that-omits-the-scope-approves-a-plan-nobody-read`). This
    paragraph used to close by noting that `plan_identity` hashed `content` only, so a widening
    rewrite "gains nothing, because the model's declaration is never what is consulted" — true of
    this gate, and false of the route that writes the row it consults, which derives the scope from
    the *live* plan once the chemist's hash has matched. The identity covers each step's declaration
    now, so such a rewrite is a plan with no approval at all: this gate refuses it with
    `plan_approval_refusal` rather than `out_of_scope_refusal`, one step earlier than before and for
    the stronger reason.

    Raises:
        PlanNotApprovedError: The plan behind this call has no live approval, or has one that does
            not name this tool. The body never runs; the audit middleware records the refusal and
            `surface_authorization_denials` relays the reason to the model. The two cases carry
            different sentences (`plan_approval_refusal`, `out_of_scope_refusal`) because they have
            different remedies.
    """
    name = request.tool_call["name"]
    # The *call* rather than the tool, for the reason `authz.side_effecting_call` gives:
    # `write_file` is durable under `/memories/` and turn-local under `/scratch/`, and refusing
    # both would deny an unapproved turn the scratchpad it needs in order to produce a plan worth
    # approving. This is what "the plan gate governs this call at all" means.
    if not side_effecting_call(name, request.tool_call.get("args") or {}):
        return await handler(request)
    session_id = get_current_session_id()
    # No session means no plan to approve and no autonomous loop to gate — a template activity's
    # tool step, or a one-shot CLI call. Not a hole: those paths still pass through
    # `enforce_tool_authz` and `authorize_trigger`, which is what governs them.
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
