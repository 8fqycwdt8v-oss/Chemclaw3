"""The harness's pre-execution approval, applied to the act rather than latched onto the session.

`SECURITY.md` and `docs/guides/harness-konzept.md` §6 describe an approval gate: under
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
up to build the plan it needs approved — so the deployments that want the gate would turn it
off, which is the worst outcome available. The line is drawn at state change
(`chemclaw.agent.authz.STATE_CHANGING_TOOLS`, plus every durable launcher), so an unapproved
session can research and propose and can do nothing else.
"""

import asyncio
import logging
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Final, Literal

from langchain.agents.middleware import wrap_tool_call

from chemclaw.agent.authz import AuthorizationError, side_effecting_call
from chemclaw.agent.plan_approval_store import plan_approval_store
from chemclaw.agent.plan_state import session_todos
from chemclaw.agent.profiles import AgentProfile
from chemclaw.core.config import settings
from chemclaw.core.config.agent import HarnessAutonomy
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
# `tool_failed` event. `api/events.ToolFailedEvent.reason` carries it; `evals/live.py` classifies on
# it; `api/graph_stream._signal_event` stamps it.
#
# **It replaces a substring match on the sentence above**, and that is the whole point. A refusal is
# prose written for a chemist, so it is exactly the kind of text somebody improves — and the eval
# harness held a *copy* of one phrase of it ("has not been approved yet") as its only way to tell
# "the gate held" from "a tool fell over". Those two findings are opposites: one is the control
# working, the other is a fault. A reword would have flipped every gated turn from the first to the
# second, silently and retroactively, with every test still green because the tests pinned the same
# copy. A discriminator on the wire cannot drift that way — a consumer either reads the field or
# does not.
PLAN_GATE_REASON: Final = "plan_gate"


def plan_gate_failure_reason(detail: str) -> Literal["plan_gate"] | None:
    """`PLAN_GATE_REASON` if this tool-failure detail is this gate's refusal, else `None`.

    `detail` is what `agent/tool_authz.failure_detail` built from the raised exception and put on
    the turn's failure signal: `"<exception class>: <message>"`. So what is matched here is the
    **class**, not the sentence — a Python identifier that a rename has to touch at its definition,
    under `mypy --strict` and this module's own tests, rather than prose anyone may reword.

    Read from the detail line rather than from the exception itself because the exception does not
    survive the trip: `announce_tool_failures` records a signal carrying two strings, and by the
    time `api/graph_stream` turns that signal into an event there is nothing left to `isinstance`.
    The alternative — a new field on `ToolFailureSignal` — is a third repository's contract for a
    fact this side can already derive, so it is not worth the coordination.
    """
    return PLAN_GATE_REASON if detail.startswith(f"{PlanNotApprovedError.__name__}:") else None


def gated_call(tool_name: str, arguments: Mapping[str, Any]) -> bool:
    """Whether this call is one the plan gate governs at all.

    The call rather than the tool, for the reason `authz.side_effecting_call` gives: `write_file`
    is durable under `/memories/` and turn-local under `/scratch/`, and refusing both would deny an
    unapproved turn the scratchpad it needs in order to produce a plan worth approving.
    """
    return side_effecting_call(tool_name, arguments)


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
    counts as "this batch's rewrite". `plan_after_batch` reduces the result to bare `content`
    strings for the identity hash, which is all *it* needs; `plan_link`'s caller needs `status`
    too, to find the step the batch marks `in_progress`, so this returns the items unreduced.

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
    perturb `plan_identity` (the hash reads `content` only, which is what lets an approved plan
    start a job without revoking itself), so judging the call against the plan the batch *writes*
    lets the canonical shape through — while the DARK-1 batch (`write_todos(plan B)` beside a
    write, under plan A's approval) still refuses, because plan B has no approval. Fails closed on
    anything unanswerable: two rewrites in one message, or arguments the middleware itself would
    reject.

    Returns `None` when the message cannot be found rather than guessing. That is not a hole: the
    approval check then runs against the pre-batch plan, which is the behaviour this function's
    predecessor was added to tighten, not a new one.
    """
    items = rewrite_todos_in_batch(request)
    if items is None or items is _UNANSWERABLE:
        return items
    contents = [item.get("content") for item in items]
    if not all(isinstance(c, str) for c in contents):
        return _UNANSWERABLE
    return contents


async def _plan_behind(request: Any, session_id: str) -> list[str] | None:
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
        return [todo["content"] for todo in state.get("todos") or []]
    return await session_todos(session_id)


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
    it; turn 2 emits `write_todos(plan B)` and `propose_knowledge_note(...)` together; the gate sees
    plan A, the approval stands, and the write executes under an approval given for a different
    plan. That is the DARK-1 sequence this module exists to prevent.

    So a gated call that arrives beside a plan rewrite is judged against the plan the batch
    *writes* — read from the `write_todos` arguments in the same message (`plan_after_batch`) —
    because the batch is atomic to the model and its post-state is the one answer to "which plan
    is this call part of" that holds under either execution order. An earlier version refused the
    whole shape outright, which failed closed and also failed the canonical harness pattern:
    "tick the completed step, do the next one" batches a status-flip `write_todos` beside every
    step's tool call, and refusing it livelocked approved multi-step plans against the repeat
    guard. A status flip hashes identically (`plan_identity` reads `content` only), so the
    canonical shape passes on its standing approval; a genuine rewrite is approved or refused on
    *its own* hash, which is exactly D-167's question. Anything unanswerable — two rewrites in one
    batch, unparseable arguments — still refuses without asking the store.

    **Waiting jobs need no exclusion here.** Under MAF a todo waiting on a durable job was marked by
    prefixing its description, and the identity had to filter those out or an approved plan revoked
    its own approval the moment it launched a job. Nothing writes that bookkeeping into `todos` now
    — a launched job is a `job_records` row and a `session_events` push-back — so the list this
    hashes is the plan and only the plan, and there is nothing to filter.

    Raises:
        PlanNotApprovedError: The plan behind this call has no live approval. The body never runs;
            the audit middleware records the refusal and `surface_authorization_denials` relays
            the reason to the model.
    """
    name = request.tool_call["name"]
    if not gated_call(name, request.tool_call.get("args") or {}):
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
    lines = rewritten if rewritten is not None else await _plan_behind(request, session_id)
    if lines is not None and await approval_stands(session_id, plan_identity(lines)):
        return await handler(request)
    raise plan_approval_refusal(name)
