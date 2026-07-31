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
unapproved.

**And why nothing enforced that.** (D-2026-07-31-plan-approval-binds-to-the-plan.)
The binding above described the *record*: the
`plan_approvals` row was keyed by hash, so the durable evidence was correct. Nothing consulted it
at execution time. `set_agent_mode` had exactly one caller, `grant_execute`, and no counterpart —
so a session that reached execute mode stayed there for the rest of its life, and the second plan,
and the tenth, looped with no human in the loop. The hash protected only the first plan, which is
close to protecting nothing, and it silently defeated the posture the chart ships
(`harness_autonomy=plan_only`). `grant_execute` now records *which* plan was authorized,
`plan_bound` refuses to loop when the session is proposing a different one, and `revoke_execute`
exists so a rejection after an approval actually revokes.

**There are two plan hashes, and they answer different questions.** `current_plan_hash` is over the
rendered todo lines — exactly what the surfaces show a chemist
(`chemclaw.agent.harness_todo.todo_titles` feeds `PlanEvent`) — so the approval handshake cannot
authorize something other than what was displayed; completion state is part of it, and a plan whose
steps have been ticked off is correctly a different plan to re-approve. `plan_identity_hash` is
over the steps alone (`todo_steps`), because binding *execution* to the displayed hash would revoke
the approval the moment the first step completed and the loop would stop after one iteration, every
time. What must revoke an authorization is the plan being rewritten, not progress through it.
"""

import inspect
from typing import TYPE_CHECKING, Any

from agent_framework import AgentSession
from agent_framework._harness._mode import AgentModeProvider, get_agent_mode, set_agent_mode

if TYPE_CHECKING:  # the predicate shape MAF's loop middleware accepts
    from agent_framework._harness._loop import ShouldContinueCallable

from chemclaw.agent.harness_todo import todo_steps, todo_titles
from chemclaw.core.ids import stable_hash

# The tool MAF injects that lets the model change its own mode. Named here rather than inlined so
# the retraction below and the test that pins it cannot drift apart.
MODEL_MODE_TOOL = "mode_set"

# The mode in which the harness loop runs (`todos_remaining(looping_modes=["execute"])`).
EXECUTE_MODE = "execute"
PLAN_MODE = "plan"

# Where the authorized plan is kept. Its own key in the session's state map, beside MAF's
# `agent_mode` rather than inside it: that dict is upstream's, and writing our field into it would
# make this repo's data a hostage to their schema.
_APPROVAL_SOURCE_ID = "chemclaw_plan_approval"
_APPROVED_PLAN_KEY = "approved_plan_hash"


def _approval_state(session: AgentSession) -> dict[str, Any]:
    """The mutable session state holding the approved plan, created on first use."""
    state = session.state.get(_APPROVAL_SOURCE_ID)
    if not isinstance(state, dict):
        state = {}
        session.state[_APPROVAL_SOURCE_ID] = state
    return state


class PlanApprovalModeProvider(AgentModeProvider):
    """An `AgentModeProvider` whose mode the model cannot set.

    Everything else MAF does in `before_run` — the mode instructions, `mode_get`, the
    external-change notification — is left exactly as upstream wrote it.
    """

    async def before_run(self, *args: Any, **kwargs: Any) -> None:
        """Run MAF's `before_run`, then retract the self-service mode tool it injected.

        The retraction is by advertised name and is deliberately tolerant of the tool being absent:
        an upstream version that stops injecting it, or renames it, must not raise here — it would
        turn a harmless upstream change into a failed turn. The regression test asserts the tool is
        gone, so a rename that silently reopened the gate fails there instead.
        """
        await super().before_run(*args, **kwargs)
        context = kwargs.get("context") or next(
            (a for a in args if hasattr(a, "tools") and hasattr(a, "extend_tools")), None
        )
        tools = getattr(context, "tools", None)
        if isinstance(tools, list):
            tools[:] = [tool for tool in tools if _advertised_name(tool) != MODEL_MODE_TOOL]


def _advertised_name(tool: Any) -> str:
    """The name a tool is advertised to the model under, however it carries it.

    MAF advertises an in-process function tool under `.name` and a bare callable under
    `__name__`; reading both means the retraction cannot be defeated by a change in which
    wrapper `@tool` produces.
    """
    return str(getattr(tool, "name", None) or getattr(tool, "__name__", ""))


async def current_plan_hash(session: AgentSession) -> str:
    """The hash of the plan this session is currently proposing.

    Over the rendered todo lines — the same strings the surfaces display — so what is approved and
    what was shown cannot diverge. Completion state is part of the rendering and therefore part of
    the hash, which is the behaviour wanted: a plan whose steps have been ticked off is not the
    plan that was approved, and re-approval is the correct outcome rather than a silent carry-over.
    """
    return stable_hash(await todo_titles(session))


def session_mode(session: AgentSession, *, default_mode: str = PLAN_MODE) -> str:
    """This session's current harness mode, read through MAF's own accessor."""
    return get_agent_mode(session, default_mode=default_mode)


async def plan_identity_hash(session: AgentSession) -> str:
    """The hash of *which plan* this is, ignoring how far it has got (`todo_steps`).

    The counterpart to `current_plan_hash`, and the one an authorization is bound to. See
    `chemclaw.agent.harness_todo.todo_steps` for why the two must differ.
    """
    return stable_hash(await todo_steps(session))


async def grant_execute(session: AgentSession) -> str:
    """Authorize this session to execute *the plan it is proposing now* — never the session itself.

    Uses MAF's `set_agent_mode` rather than writing session state directly, because that helper
    also records the previous mode so the next `before_run` injects a message telling the agent
    the mode changed externally. Without that the agent stays anchored to whatever it last
    believed and can keep behaving as though it were still planning.

    **The approved plan is recorded beside the mode, and that is the whole fix.** D-137 bound the
    approval *record* to a plan hash so that "approve a modest plan, then rewrite it" would be a
    different key — but nothing consulted that binding at execution time. `set_agent_mode` had
    exactly one caller and no counterpart: once a session reached execute mode it stayed there for
    the rest of its life, so the second plan, and the tenth, looped with no human in the loop. The
    hash binding protected only the first plan, which is close to protecting nothing, and it
    defeated the posture the chart ships (`harness_autonomy=plan_only`).

    Storing the authorized plan next to the mode is what lets `plan_bound` compare them. Same
    lifetime as the mode itself, so this introduces no new way for the two to disagree.
    """
    _approval_state(session)[_APPROVED_PLAN_KEY] = await plan_identity_hash(session)
    return set_agent_mode(session, EXECUTE_MODE)


def revoke_execute(session: AgentSession) -> str:
    """Return the session to plan mode and drop the authorization — a rejection after an approval.

    `plan_approvals` keeps every decision and reads the latest, so clicking "no" after "yes" is
    meant to revoke. Without this the row said rejected while the session kept executing.
    """
    _approval_state(session).pop(_APPROVED_PLAN_KEY, None)
    return set_agent_mode(session, PLAN_MODE)


async def execute_is_authorized(session: AgentSession) -> bool:
    """Whether the plan this session is proposing now is the plan a human approved."""
    approved = _approval_state(session).get(_APPROVED_PLAN_KEY)
    return bool(approved) and approved == await plan_identity_hash(session)


def plan_bound(should_continue: "ShouldContinueCallable") -> "ShouldContinueCallable":
    """Wrap a harness loop predicate so it only continues while the approved plan is still the plan.

    Composed around MAF's `todos_remaining` rather than replacing it: that predicate resolves the
    todo provider and the mode from the running agent, and reimplementing it here would silently
    lose whatever upstream adds to it next — the same reasoning as `PlanApprovalModeProvider`
    retracting one tool instead of rewriting `before_run`. For the same reason the inner result is
    passed through *unchanged* rather than coerced to `bool`: MAF lets a predicate return
    `(False, reason)`, and flattening that would discard the explanation upstream surfaces.

    A session with no approval on file is unaffected in practice: it is also not in execute mode,
    so the inner predicate already refuses. The check is stated independently anyway, because
    "authorized" and "in execute mode" became the same thing only by accident and should not be
    relied on to stay that way.
    """

    async def _should_continue(
        *, session: Any = None, agent: Any = None, **kwargs: Any
    ) -> bool | tuple[bool, str | None]:
        """Continue only if the plan is still approved, then defer to the inner predicate."""
        if session is None:
            return False
        if not await execute_is_authorized(session):
            return False, (
                "the plan changed since it was approved; it needs approving again before "
                "execution continues"
            )
        outcome = should_continue(session=session, agent=agent, **kwargs)
        return await outcome if inspect.isawaitable(outcome) else outcome

    return _should_continue
