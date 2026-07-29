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
2. **The flip moves to a human-only path.** `service.app` exposes an owner-scoped route that calls
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
show a chemist (`agents.harness_todo.todo_titles` feeds `PlanEvent`) — hashing richer internal
state would let the authorized artifact drift from the displayed one.
"""

from typing import Any

from agent_framework import AgentSession
from agent_framework._harness._mode import AgentModeProvider, get_agent_mode, set_agent_mode

from agents.harness_todo import todo_titles
from chemclaw.ids import stable_hash

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


def grant_execute(session: AgentSession) -> str:
    """Move the session into execute mode — the one place that does, and never the model.

    Uses MAF's `set_agent_mode` rather than writing session state directly, because that helper
    also records the previous mode so the next `before_run` injects a message telling the agent
    the mode changed externally. Without that the agent stays anchored to whatever it last
    believed and can keep behaving as though it were still planning.
    """
    return set_agent_mode(session, EXECUTE_MODE)
