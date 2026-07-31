"""The pre-execution approval gate: only a human moves the harness from plan to execute.

`SECURITY.md`, `docs/guides/harness-konzept.md` §6 and `build_agent`'s own docstring all describe a
GxP gate — in `plan_only` the agent proposes a plan and waits for a human before executing. The
shipped production configuration runs exactly that (`harness_enabled=true`,
`harness_autonomy=plan_only`).

**It did not exist.** MAF's `AgentModeProvider.before_run` injects a `mode_set` tool into the
model's own tool surface on every run, declared `approval_mode="never_require"`, and its
instructions tell the model to call it: *"When approval is granted, always switch to execute mode
(using the `mode_set` tool)"* — where "approval is granted" is the model's own reading of the
conversation. Nothing bound an approval to a plan, and because the audit middleware attributes
every tool call to the ambient actor, the trail recorded the agent's self-authorization under the
*chemist's* Entra oid. That is worse than an unrecorded flip: it is an attributable-looking
approval with no human act behind it.

Two changes make the documented gate real, and they are deliberately small:

1. **`mode_set` stops being advertised.** `before_run` runs MAF's implementation unchanged and then
   *retracts* that one tool from the invocation's tool list. Retracting rather than reimplementing
   matters: MAF also injects `mode_get`, the mode instructions, and an external-change notification
   from the same method, and a reimplementation would silently lose whichever of those upstream
   adds next. `mode_get` stays — reading the mode is harmless and keeps the model honest about
   which mode it is in.
2. **The flip moves to a human-only path.** `chemclaw.api.app` exposes an owner-scoped route that
calls
   MAF's own `set_agent_mode`, which is the supported external entry point (it records the previous
   mode so the next `before_run` tells the agent the mode changed underneath it). That route is
   explicitly *not* an agent tool, for the same reason `POST /approvals/{id}/decision` is not:
   a tool would let the agent approve its own candidate and collapse the line the whole PR-gate
   exists to draw (D-005).

**Why the approval is bound to a plan hash.** An approval that only said "this session may execute"
would authorize whatever the plan happened to become — the model could present a modest plan, have
it approved, then rewrite its todo list and run something else under the same authorization.
Hashing the plan the human actually saw makes that a different plan, and a different plan is
unapproved. The hash is over the rendered todo lines, because that is exactly what the surfaces
show a chemist (`chemclaw.agent.harness_todo.todo_titles` feeds `PlanEvent`) — hashing richer
internal
state would let the authorized artifact drift from the displayed one.
"""

import logging
from typing import Any

from agent_framework import AgentSession
from agent_framework._harness._mode import AgentModeProvider, get_agent_mode, set_agent_mode

from chemclaw.agent.harness_todo import todo_plan_items
from chemclaw.core.ids import stable_hash

logger = logging.getLogger(__name__)

# The tool MAF injects that lets the model change its own mode. Named here rather than inlined so
# the retraction below and the test that pins it cannot drift apart.
MODEL_MODE_TOOL = "mode_set"

# The mode in which the harness loop runs (`todos_remaining(looping_modes=["execute"])`).
EXECUTE_MODE = "execute"
PLAN_MODE = "plan"


class PlanApprovalModeProvider(AgentModeProvider):
    """An `AgentModeProvider` whose mode the model cannot set.

    Everything else MAF does in `before_run` — the mode instructions, `mode_get`, the
    external-change notification — is left exactly as upstream wrote it.
    """

    async def before_run(self, *args: Any, **kwargs: Any) -> None:
        """Demote a session whose approval no longer covers its plan, then retract `mode_set`.

        The retraction is by advertised name and is deliberately tolerant of the tool being absent:
        an upstream version that stops injecting it, or renames it, must not raise here — it would
        turn a harmless upstream change into a failed turn. The regression test asserts the tool is
        gone, so a rename that silently reopened the gate fails there instead.

        The demotion runs **first**, before MAF's `before_run`, so the mode instructions and the
        `{current_mode}` the model is shown describe the mode it is actually in. Demoting afterwards
        would inject "you are in execute mode" and then quietly move the session to plan, which is
        the confusing half-state this is meant to remove.

        It is not the enforcement — `chemclaw.agent.plan_gate` is, at the tool boundary, because
        this method runs once per `agent.run` and the model rewrites its todo list *during* the run
        that follows. What it fixes is a session that comes back later still holding an execute mode
        nothing supports: a pod restart, a plan rewritten on a previous turn, or a rejection
        recorded after an approval, which migration 020 says revokes and which nothing acted on.
        """
        session = kwargs.get("session") or next(
            (a for a in args if isinstance(a, AgentSession)), None
        )
        if isinstance(session, AgentSession):
            await self._demote_if_unapproved(session)
        await super().before_run(*args, **kwargs)
        context = kwargs.get("context") or next(
            (a for a in args if hasattr(a, "tools") and hasattr(a, "extend_tools")), None
        )
        tools = getattr(context, "tools", None)
        if isinstance(tools, list):
            tools[:] = [tool for tool in tools if _advertised_name(tool) != MODEL_MODE_TOOL]

    async def _demote_if_unapproved(self, session: AgentSession) -> None:
        """Return an execute-mode session to plan when its current plan has no approval.

        Only meaningful for the approval-first posture, which is what `default_mode == PLAN_MODE`
        identifies: a deployment configured for `harness_autonomy="execute"` starts every session in
        execute deliberately and has no approval path at all, so demoting there would strand it.

        Failures are swallowed on purpose. This is a *consistency* repair on a display value, and
        the control that matters does not depend on it — `plan_gate` re-asks the same question at
        the tool boundary and fails closed. Letting an unreachable approval store turn every turn
        into an error would trade a cosmetic inconsistency for an outage.
        """
        if self.default_mode != PLAN_MODE:
            return
        if get_agent_mode(session, default_mode=self.default_mode) != EXECUTE_MODE:
            return
        from chemclaw.agent.plan_gate import plan_is_approved

        try:
            approved = await plan_is_approved(session)
        except Exception:  # noqa: BLE001 - a display repair must never fail the turn it precedes
            logger.warning(
                "could not check the plan approval for session %s; leaving the displayed mode "
                "alone (the tool-level gate still decides, and fails closed)",
                session.session_id,
                exc_info=True,
            )
            return
        if not approved:
            revoke_execute(session)


def _advertised_name(tool: Any) -> str:
    """The name a tool is advertised to the model under, however it carries it.

    MAF advertises an in-process function tool under `.name` and a bare callable under
    `__name__`; reading both means the retraction cannot be defeated by a change in which
    wrapper `@tool` produces.
    """
    return str(getattr(tool, "name", None) or getattr(tool, "__name__", ""))


async def current_plan_hash(session: AgentSession) -> str:
    """The hash of the plan this session is currently proposing — the approval key.

    Over the plan's **work items** (`chemclaw.agent.harness_todo.todo_plan_items`): the titles in
    order, without the checkbox and without the `awaiting-job:` rows the launcher writes.

    **This used to hash the rendered lines, and that made the gate unenforceable** (D-157). The
    argument for including completion state was that "a plan whose steps have been ticked off is
    not the plan that was approved, and re-approval is the correct outcome" — coherent in the
    abstract, and fatal in practice: the hash moves on the *first* ticked box, so an approval can
    never be checked against the plan being executed. It could only ever be recorded, which is
    exactly what the system did — `PlanApprovalStore.decision` was read by one display route and by
    nothing that runs anything. A four-item plan would have needed four approvals, and nobody would
    have operated that; so the only reachable outcome was the one that shipped, where the approval
    latched a session and authorized whatever came next.

    What a person approves is the set of work items, not their completion state. Ticking a box is
    the plan proceeding; adding, removing or rewording one is a different plan, and a different
    plan is unapproved. The displayed rendering keeps its checkboxes (`todo_titles` is untouched),
    so nothing a chemist reads changes.
    """
    return stable_hash(await todo_plan_items(session))


def session_mode(session: AgentSession, *, default_mode: str = PLAN_MODE) -> str:
    """This session's current harness mode, read through MAF's own accessor."""
    return get_agent_mode(session, default_mode=default_mode)


def grant_execute(session: AgentSession) -> str:
    """Move the session into execute mode — the one place that does, and never the model.

    Uses MAF's `set_agent_mode` rather than writing session state directly, because that helper
    also records the previous mode so the next `before_run` injects a message telling the agent
    the mode changed externally. Without that the agent stays anchored to whatever it last
    believed and can keep behaving as though it were still planning.
    """
    return set_agent_mode(session, EXECUTE_MODE)


# Where a session records which approved plans have already had their turn. Session state, not the
# database, because it is scoped to exactly one conversation's progress and shares the lifetime of
# the mode it qualifies — the same reasoning `plan_approval_store` uses for its backend choice.
_CONSUMED_STATE_KEY = "chemclaw_plans_consumed"


def consume_plan(session: AgentSession, plan_hash: str) -> None:
    """Record that an approved plan has now had the turn it was approved for.

    **This is what makes an approval authorize a request rather than a session** (D-157), and it
    exists because the first version of that fix did not close the finding. Binding the approval to
    the plan's *work items* — rather than to its rendered lines, whose hash moved on the first
    ticked box — made the approval checkable at last. It also made it durable in a way nobody
    approved: a live run showed the model answering a completely different question without
    touching its todo list at all, so the plan identity never changed, the approval never lapsed,
    and `compute_xtb_energy` ran under an authorization given for a hazard-screening plan.

    The unit a person actually approves is *this plan, for this ask*. The harness loop runs a plan
    to completion inside one `agent.run`, so one turn is exactly the scope of "execute the approved
    plan" — and the next user message is a new request, which needs its own approval even if the
    todo list happens to look the same.
    """
    consumed = session.state.setdefault(_CONSUMED_STATE_KEY, [])
    if isinstance(consumed, list) and plan_hash not in consumed:
        consumed.append(plan_hash)


def plan_consumed(session: AgentSession, plan_hash: str) -> bool:
    """Whether this plan's approval has already been spent on a turn."""
    consumed = session.state.get(_CONSUMED_STATE_KEY)
    return isinstance(consumed, list) and plan_hash in consumed


def rearm_plan(session: AgentSession, plan_hash: str) -> None:
    """Forget that a plan was consumed, so a fresh human decision authorizes a fresh turn.

    Called when a decision is recorded. Re-approving the same unchanged plan is a person saying
    "yes, again" — a deliberate act, and the only thing that revives a spent authorization.
    """
    consumed = session.state.get(_CONSUMED_STATE_KEY)
    if isinstance(consumed, list) and plan_hash in consumed:
        consumed.remove(plan_hash)


def revoke_execute(session: AgentSession) -> str:
    """Return the session to plan mode — the mirror `grant_execute` never had (D-157).

    Its absence was half of DARK-1. `grant_execute` was a latch: one approval moved a session into
    execute and *nothing* moved it back, so the authorization outlived the plan it was given for,
    a rejection recorded afterwards changed nothing (against migration 020's stated contract), and
    the mode a surface displayed stopped being a fact about anything.

    Through `set_agent_mode` for the same reason `grant_execute` is: the helper records the
    previous mode, so the next `before_run` injects the external-change notification. That matters
    more here than on the way up — a model that has been executing will keep executing on
    instructions alone, and it has to be *told* it is planning again.
    """
    return set_agent_mode(session, PLAN_MODE)
