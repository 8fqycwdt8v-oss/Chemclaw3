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
from chemclaw.agent.profiles import AgentProfile
from chemclaw.core.config import settings
from chemclaw.core.ids import stable_hash

logger = logging.getLogger(__name__)

# The tool MAF injects that lets the model change its own mode. Named here rather than inlined so
# the retraction below and the test that pins it cannot drift apart.
MODEL_MODE_TOOL = "mode_set"

# The mode in which the harness loop runs (`todos_remaining(looping_modes=["execute"])`).
EXECUTE_MODE = "execute"
PLAN_MODE = "plan"

# The autonomy setting that asks for the approval-first posture — the value `harness_autonomy` takes
# when a human must approve the plan before anything executes. A constant because three decisions
# compare against it (which mode a session starts in, whether the loop predicate is conditioned on
# an approval, whether the tool gate is attached at all), and a deployment that ran two of the three
# would be one that cannot do anything or one that cannot be stopped.
PLAN_ONLY = "plan_only"


def harness_enabled_for(profile: AgentProfile) -> bool:
    """Whether the harness runs for `profile`: its own override, or the deployment's default."""
    return bool(
        settings.harness_enabled if profile.harness_enabled is None else profile.harness_enabled
    )


def autonomy_for(profile: AgentProfile) -> str:
    """The autonomy `profile` runs under: its own override, or the deployment's default.

    **One resolver for both dimensions, because the three decisions that read them must agree.**
    The `X if profile.X is None else profile.X` rule was written out three times — once in
    `build_agent` for whether to wire the harness at all, once in `_build_harness_agent` for the
    starting mode and the loop predicate, once in `plan_gate.gate_applies` for whether the tool gate
    is attached and whether a finished turn spends its approval. That triplication has already cost
    a live defect once: `chemclaw.api.runner` read `settings` directly instead, so a profile
    narrowed to `plan_only` under a global `execute` got the gate attached and its approval never
    spent, and one decision authorized every later turn (`gate_applies` records it). A rule spelled
    out in three places is a rule three places can disagree about; this is the one place.
    """
    return str(
        settings.harness_autonomy if profile.harness_autonomy is None else profile.harness_autonomy
    )


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


# What `stable_hash` returns for a session with no work items. A constant, identical in every
# session of every deployment for all time — which is exactly why it must never be an approvable
# identity (see `approvable_plan_hash`). Computed rather than written out, so it cannot drift from
# the hashing rule it describes.
EMPTY_PLAN_HASH = stable_hash([])


async def approvable_plan_hash(session: AgentSession) -> str | None:
    """The plan identity a human decision may be recorded against, or None when there is no plan.

    **"Nothing" is not a plan, and it used to be an approvable one.** `current_plan_hash` over an
    empty todo list is `EMPTY_PLAN_HASH` — a constant, not a fact about this session — so a
    decision recorded against it says "someone approved the empty plan", which every other session
    also proposes whenever it holds no todos. `POST /sessions/{id}/plan/decision` recorded exactly
    that with no emptiness check (the CLI's `/approve` already refused).

    Worse, it did not stay spent, because the consumed marker used to live in `session.state` —
    which an LRU eviction or a pod roll drops (`chemclaw.api.deps._rehydrate_session` rebuilds the
    handle over the durable history alone) — while the `plan_approvals` row was durable. A
    rehydrated session had lost its todo state too, so it proposed the empty plan again, hashed to
    the same global constant, and found a live approval waiting. Consumption is durable now
    (`plan_approvals.consumed_at`), which closes that composition from the other side as well; this
    check stands on its own reason regardless, and it is the first one: an identity every session in
    every deployment shares is not something a person can meaningfully decide about.

    So emptiness is answered before hashing, at the one boundary that matters: this is what the
    gate asks and what the decision route records against, and `current_plan_hash` stays a
    total function for the *display* route, which has to show something either way.
    """
    items = await todo_plan_items(session)
    return stable_hash(items) if items else None


async def current_plan_hash(session: AgentSession) -> str:
    """The hash of the plan this session is currently proposing — the approval key.

    Over the plan's **work items** (`chemclaw.agent.harness_todo.todo_plan_items`): the titles in
    order, without the checkbox and without the `awaiting-job:` rows the launcher writes.

    **This used to hash the rendered lines, and that made the gate unenforceable** (D-167). The
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

    Total, unlike `approvable_plan_hash`: `GET /sessions/{id}/plan` reports an identity for whatever
    the session currently proposes, including nothing at all (`EMPTY_PLAN_HASH`). Posting that
    identity back is refused — deciding is the operation emptiness invalidates, not displaying.
    """
    return await approvable_plan_hash(session) or EMPTY_PLAN_HASH


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


# **Whether an approval has been spent is not kept here.** It was — a `chemclaw_plans_consumed`
# list in `session.state`, with `consume_plan` / `plan_consumed` / `rearm_plan` around it — and that
# put the two halves of one control on different lifetimes: the `plan_approvals` row survives a pod
# roll and an LRU eviction, the marker did not, so a session that reconstructed a byte-identical
# todo list met its own already-spent approval looking fresh. It now lives on the decision itself
# (`plan_approvals.consumed_at`, `infra/sql/034`), which is why this module has no third function
# about it and why re-arming needs none either: recording a fresh decision *is* the re-arm.


def revoke_execute(session: AgentSession) -> str:
    """Return the session to plan mode — the mirror `grant_execute` never had (D-167).

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
