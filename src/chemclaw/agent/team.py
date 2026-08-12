"""The specialist team: a supervisor that delegates, and five agents it may delegate to (M9).

Decided in `docs/decisions/D-2026-08-10-a-subagent-is-an-attenuation-not-a-new-actor.md`. The
substrate was already here — an `AgentProfile` is an attenuate-only bundle of tool names, MCP
server names and instructions, discovered from `data/profiles/*.yaml` — so **a specialist is a
profile plus a compiled subgraph, and not a new concept.** That is the whole reason this module is
short: nothing about delegation needed a new security model, it needed the existing one enforced
one level down.

**What a team buys, and what it costs.** A single agent holding sixty tools chooses badly among
them and pays for the whole surface in every prompt; five agents holding a dozen each choose within
a coherent set. The cost is that a supervisor which mis-routes is *worse* than no team at all —
which is why this ships disabled (`settings.agent_teams_enabled`) until M12 measures routing
accuracy and per-specialist token cost against the single-agent baseline. A capability that is not
yet known to help is not a default.

**The four invariants, and where each one lives.** They are what the ADR records, and three of them
needed code that did not exist:

1. **A subagent's surface is an attenuation of its caller's, never a widening.** `reject_widening`
   compares the *advertised* names of parent and child and fails the build on any addition. This is
   new: `_reject_unknown_tool_names` already checked a profile against the whole deployment, which
   catches a typo and says nothing about privilege — a specialist naming a tool the caller does not
   hold would have built cleanly.
2. **`require_actor` reject-if-absent holds inside every subagent.** This needed no code, and that
   is a finding rather than an assumption: Chemclaw's actor is a contextvar bound around the whole
   turn (`core/identity_context`), a subagent runs inside a parent tool call, and both LangGraph's
   executor and LangChain's sync-in-async bridge spawn with `copy_context()`. So the actor reaches
   a specialist by the same route it reaches any tool, `SubAgentMiddleware._EXCLUDED_STATE_KEYS` is
   irrelevant to it (there is nothing identity-shaped in state to filter), and propagation is
   strictly downward — a specialist cannot leak an identity change back up.
   `tests/test_agent_team.py` asserts it rather than trusting the reasoning.
3. **The audit trail names the specialist beside the human.** `_AttributedSpecialist` stamps the
   running agent's name for the duration of its invocation, and `agent/audit.py` reads it.
   Attribution to "the agent" is what makes a trail worthless in a regulated system, and
   overloading `actor` — the human's Entra oid — would be D-040's failure repeated.

   **The same bracket announces the routing to the chemist watching**
   (`D-2026-08-11-a-handoff-is-observable-where-the-specialist-runs`): `running_specialist` raises
   `HandoffEvent` on entry and its hand back in the `finally`, so the span a surface draws and the
   span the trail claims cannot disagree. Observed at the specialist's *invocation* rather than at
   the delegation, which is what keeps it true whichever way the still-open routing question
   settles — `task` tool or routing node, the compiled specialist is invoked either way.
4. **Skills do not inherit.** Each specialist's skills are narrowed by its own profile through the
   same `skill_permits` predicate the main agent uses, because `build_langgraph_agent` builds each
   one the ordinary way. A specialist holding fewer tools therefore sees fewer skills, which is the
   capability scope working rather than a rule applied twice.

**`safety` is not attenuable away**, and that is the one rule here that is not attenuation. A team
may be narrowed to any subset of specialists; a subset omitting `safety` is refused. It is a gate,
not one capability among five — the check a chemist wants *before* deciding whether to approve
work — and its three tools are read-only precisely so the plan gate cannot refuse them.
"""

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, cast

from deepagents.backends import StateBackend
from deepagents.middleware.subagents import CompiledSubAgent, SubAgentMiddleware
from langchain_core.runnables import Runnable

from chemclaw.agent.profiles import AgentProfile, get_profile
from chemclaw.connectors.registry import endpoint_tool_names
from chemclaw.core.config import settings

logger = logging.getLogger(__name__)

# The five specialists, in the order a supervisor is told about them. One per natural capability
# cluster across the seven connector bundles and the in-process surface — the clusters are the
# reason there are five rather than one per bundle: `evidence` spans the knowledge graph, the
# fingerprint search and the job history, while `qm` and `calc` are one specialist because
# "compute a property" is one job whichever engine answers it.
SPECIALISTS: tuple[str, ...] = ("evidence", "computation", "design", "safety", "reporting")

# The specialist that may never be narrowed out of a team. Named rather than inlined because two
# things read it — the build guard and its test — and a rule enforced against a literal in one
# place is a rule that moves when someone edits the other.
REQUIRED_SPECIALIST = "safety"

_SUPERVISOR_PROMPT = (
    "You lead a team of specialists. Delegate a question to the specialist whose surface fits it "
    "rather than answering from memory, wait for what it returns, and assemble the final answer "
    "yourself with the citations it gave you. Delegate to `evidence` for what this programme "
    "already knows, `computation` for a property or a quantum-chemistry job, `design` for which "
    "experiment to run next, `safety` for any hazard, genotoxicity or impurity-limit question, and "
    "`reporting` to turn finished work into a report or a proposed note. Ask `safety` whenever the "
    "work involves handling a substance, whether or not the chemist raised it."
)


class TeamError(ValueError):
    """A team declaration that would widen a surface, or would drop a gate.

    A `ValueError` for the same reason `ProfileError` is one: this is a configuration error caught
    at build time, so one `except ValueError` at an entry point catches every "this deployment is
    misconfigured" failure.
    """


def team_enabled() -> bool:
    """Whether this deployment routes turns through a specialist team.

    Off by default, and the ADR says why: a supervisor that mis-routes is worse than the single
    agent it replaces, and that is not a property any unit test can establish — only M12's
    measurement against the single-agent baseline can.
    """
    return settings.agent_teams_enabled


def specialist_profiles(names: tuple[str, ...] = SPECIALISTS) -> list[AgentProfile]:
    """Resolve the team's specialist profiles, refusing a team with no safety gate.

    Args:
        names: The specialists to build. Defaults to all five; a deployment narrowing the team
            passes a subset.

    Returns:
        The resolved profiles, in the order given.

    Raises:
        TeamError: `names` omits the safety specialist.
        ValueError: A name resolves to no discovered profile (`get_profile`).
    """
    if REQUIRED_SPECIALIST not in names:
        raise TeamError(
            f"a team must include the {REQUIRED_SPECIALIST!r} specialist: it is a gate rather than "
            f"a capability, and dropping it removes the check a chemist runs before approving "
            f"work, not one option among several (got {sorted(names)})"
        )
    return [get_profile(name) for name in names]


def reject_widening(parent: AgentProfile, child: AgentProfile) -> None:
    """Fail the build when a specialist would advertise a tool its caller does not hold.

    **The invariant this exists for is not the one `_reject_unknown_tool_names` already enforces.**
    That check asks whether a profile names a tool the *deployment* provides, which catches a typo;
    it is answered against the whole surface, so a specialist naming a tool the supervisor was
    narrowed out of passes it cleanly. Delegation must not be a way to reach a capability the
    delegating agent could not reach directly — otherwise a narrow profile is a suggestion rather
    than a boundary, and every profile in `data/profiles/` is load-bearing security.

    Compared on *advertised* names, which spans both halves of the surface (in-process tools and
    each connector's allow-list) and — deliberately — does not build a single connector client:
    `advertised_tool_names` answers from the manifests for exactly this reason.

    Args:
        parent: The delegating agent's profile.
        child: The specialist's profile.

    Raises:
        TeamError: The child advertises a name the parent does not.
    """
    # Imported here rather than at module load: `chemclaw_agent` imports the profile machinery and
    # the connector registry, and a module-level import would put this module inside that cycle for
    # a function two callers reach.
    from chemclaw.agent.chemclaw_agent import advertised_tool_names

    widened = advertised_tool_names(child) - advertised_tool_names(parent)
    if widened:
        raise TeamError(
            f"specialist {child.name!r} would widen the surface of {parent.name!r} with "
            f"{sorted(widened)}; a subagent is an attenuation of its caller, never a new actor"
        )


@contextmanager
def running_specialist(name: str, reason: str = "") -> Iterator[None]:
    """Enter `name` as the agent running — on the audit trail and on the turn's stream — and leave.

    Ambient rather than threaded through every call for the same reason the actor is: a subagent
    runs inside the turn's context, so the trail can read it without every tool signature growing a
    field it would then have to be trusted to pass on. Restored in a `finally` so a specialist that
    raises does not leave its name stamped on the supervisor's next tool call — which would
    misattribute a record in the one table that must not be wrong.

    **The handoff is announced from here, and that is the point.** Invariant 3 already made this
    block the interval the audit trail attributes to the specialist; `HandoffEvent` is the same fact
    told to the chemist watching, and telling it anywhere else would create two brackets that can
    disagree about when a specialist was running. They cannot disagree now — the trail's stamp and
    the stream's pair of events are the same `try`/`finally`. The exit fires in the `finally` for
    the reason the unstamp does: a specialist that raises has still stopped running, and a trace
    that never closes the handoff would show a turn stuck inside a specialist it left.
    """
    # Imported lazily so this module does not depend on the identity layer's import order; the
    # contextvar lives beside the actor's because they are read together and reset together.
    from chemclaw.core.identity_context import (
        reset_current_specialist,
        set_current_specialist,
    )
    from chemclaw.core.turn_signals import record_handoff

    token = set_current_specialist(name)
    record_handoff(name, reason)
    try:
        yield
    finally:
        reset_current_specialist(token)
        # Empty `to` is "control returned to the agent above", which `HandoffEvent.to` already
        # declares. Unambiguous because specialists do not nest — `build_langgraph_agent` gives a
        # specialist no team of its own — so the only agent to return to is the supervisor.
        record_handoff("")


class _AttributedSpecialist:
    """A compiled specialist that stamps its own name for the duration of each invocation.

    A wrapper rather than middleware inside the specialist, because the name has to be set *around*
    the whole invocation — a `wrap_tool_call` inside the subagent would miss the model call, and
    the audit trail records tool calls made by the specialist's own middleware chain, which is
    below this point.
    """

    def __init__(self, name: str, runnable: Any) -> None:
        """Bind a compiled specialist to the name its records will carry."""
        self._name = name
        self._runnable = runnable

    def invoke(self, state: Any, config: Any = None, **kwargs: Any) -> Any:
        """Run the specialist synchronously, attributed."""
        with running_specialist(self._name, _stated_reason(state)):
            return self._runnable.invoke(state, config, **kwargs)

    async def ainvoke(self, state: Any, config: Any = None, **kwargs: Any) -> Any:
        """Run the specialist, attributed — the path a turn actually takes."""
        with running_specialist(self._name, _stated_reason(state)):
            return await self._runnable.ainvoke(state, config, **kwargs)

    def with_config(self, *args: Any, **kwargs: Any) -> "_AttributedSpecialist":
        """Re-wrap rather than unwrap; without this override the attribution disappears.

        `SubAgentMiddleware` binds each subagent's config with `with_config` and then invokes *the
        result*, deliberately, "so the original runnable is untouched and a shared instance can be
        registered under multiple names". Left to `__getattr__` below, that call would return the
        bare inner runnable and the middleware would invoke a specialist with no name stamped on
        it — every one of its tool calls landing in the audit trail attributed to the supervisor,
        with nothing failing and no test noticing. Found by mypy objecting that this class is not a
        `Runnable`, which is exactly the kind of complaint worth reading rather than casting away.
        """
        return _AttributedSpecialist(self._name, self._runnable.with_config(*args, **kwargs))

    def __getattr__(self, item: str) -> Any:
        """Everything else is the wrapped runnable's — a specialist, not a facade over one.

        Deliberately *not* a blanket forward for anything that returns a runnable: the two that do
        (`invoke`/`ainvoke` return state, `with_config` returns a runnable) are overridden above.
        A future upstream that reached a specialist through some third re-binding method would lose
        attribution the same way, which is what `test_binding_a_config_keeps_the_specialists_name`
        is there to catch.
        """
        return getattr(self._runnable, item)


def build_team_middleware(
    supervisor: AgentProfile,
    *,
    names: tuple[str, ...] = SPECIALISTS,
    build: Any = None,
    supervisor_tool_names: frozenset[str] | None = None,
    **build_kwargs: Any,
) -> SubAgentMiddleware:
    """Compile the team and hand back the middleware that lets a supervisor delegate to it.

    Each specialist is built by the ordinary `build_langgraph_agent`, which is what makes the four
    invariants hold without restating them: the specialist gets the same middleware chain (audit,
    authorization, dry-run, repeat guard, plan gate), the same skills narrowing through its own
    profile, and the same tool registry — attenuated by its profile and by nothing else.

    Args:
        supervisor: The delegating agent's profile. Every specialist is checked against it.
        names: Which specialists to build. Defaults to all five.
        build: The specialist builder, injectable so a test can compile against a scripted model.
            Defaults to `build_langgraph_agent`.
        supervisor_tool_names: Every name the supervisor's own surface advertises, for the
            runtime half of invariant 1 (see `_narrowed_connectors`). `None` skips that check,
            which is what a unit test constructing a team without a supervisor surface wants.
        **build_kwargs: Passed to each specialist's build — the audit sink, the actor, the
            correlation id and this turn's connectors, so a specialist reaches out-of-process
            capability over the same per-turn sessions the supervisor does. `connectors` is
            narrowed per specialist before it is passed down.

    Returns:
        The `SubAgentMiddleware` carrying one compiled, attributed specialist per name.

    Raises:
        TeamError: A specialist would widen the supervisor's surface, or the safety gate is absent.
    """
    from chemclaw.agent.langgraph_agent import build_langgraph_agent

    builder = build if build is not None else build_langgraph_agent
    profiles = specialist_profiles(names)
    subagents: list[CompiledSubAgent] = []
    connectors = build_kwargs.pop("connectors", None)
    for profile in profiles:
        reject_widening(supervisor, profile)
        narrowed = _narrowed_connectors(profile, connectors, supervisor_tool_names)
        subagents.append(
            CompiledSubAgent(
                name=profile.name,
                description=_description(profile),
                # `cast` because `_AttributedSpecialist` is a structural stand-in rather than a
                # `Runnable` subclass: it forwards everything it does not override, and the two it
                # overrides are the two that matter — invocation, and config re-binding. Subclassing
                # `RunnableSerializable` would drag in a serialization contract this has no use for.
                runnable=cast(
                    "Runnable[Any, Any]",
                    _AttributedSpecialist(
                        profile.name,
                        builder(profile=profile, connectors=narrowed, **build_kwargs),
                    ),
                ),
            )
        )
    logger.info("team built with %d specialist(s): %s", len(subagents), ", ".join(names))
    return SubAgentMiddleware(
        subagents=subagents,
        system_prompt=_SUPERVISOR_PROMPT,
        # `StateBackend`, which holds no filesystem. Upstream requires a backend because its *own*
        # subagent shape can be handed filesystem tools; Chemclaw's specialists are pre-compiled
        # agents that already carry their own narrowed skills backend, so anything with a real root
        # here would be a second, ungated path to the disk — the hazard `agent/skill_backend.py`
        # exists to close. An empty one is the right thing for a lookup that must find nothing.
        backend=StateBackend(),
    )


def _narrowed_connectors(
    profile: AgentProfile,
    connectors: list[Any] | None,
    supervisor_tool_names: frozenset[str] | None,
) -> list[Any]:
    """The turn's open connector tools, narrowed to what this specialist's profile declares.

    **Invariant 1 has a runtime half, and this is it.** `reject_widening` compares *declarations* —
    it proves the specialist's profile names no tool and no bundle the supervisor's does not. That
    is necessary and it is not sufficient, because the connector tools are handed down **already
    open**: a specialist declaring `mcp_server_names: [calc]` was receiving every bundle the
    supervisor had opened, so the profile bounded only the in-process half of its surface and
    delegation *widened* the out-of-process half. Declaring one thing and receiving another is
    precisely what the invariant forbids.

    A profile that narrows nothing (`mcp_server_names is None`) keeps the supervisor's set, which is
    what "attenuate-only" means at the top of the lattice.

    The `supervisor_tool_names` check is the assertion, not the narrowing: a name that survives here
    and is absent from the supervisor's own surface would be a widening this function failed to
    prevent, and it raises rather than being dropped quietly.
    """
    if not connectors:
        return []
    if profile.mcp_server_names is None:
        kept = list(connectors)
    else:
        allowed = set(endpoint_tool_names(profile.mcp_server_names))
        kept = [tool for tool in connectors if getattr(tool, "name", None) in allowed]
    if profile.tool_names is not None:
        kept = [tool for tool in kept if getattr(tool, "name", None) in profile.tool_names]
    if supervisor_tool_names is not None:
        widened = {getattr(t, "name", "") for t in kept} - supervisor_tool_names
        if widened:
            raise TeamError(
                f"specialist {profile.name!r} would reach connector tool(s) "
                f"{sorted(widened)} that its supervisor cannot — a delegation must attenuate"
            )
    return kept


def _stated_reason(state: Any) -> str:
    """Why the supervisor delegated, read off the state it handed the specialist.

    `SubAgentMiddleware` builds a specialist's state as the parent's, minus the excluded keys, with
    `messages` replaced by exactly `[HumanMessage(description)]` — the `task` tool's `description`
    argument, which *is* the supervisor's stated reason. Read from the invocation payload rather
    than from the tool call's arguments deliberately: the payload is what a specialist is given
    under any dispatch mechanism, so this survives the routing choice D-2026-08-10 leaves open.

    Best-effort by design. The reason is prose nothing branches on, so a state shape this does not
    recognise costs an empty `reason` on an otherwise correct handoff — never a failed delegation.
    """
    if not isinstance(state, dict):
        return ""
    messages = state.get("messages") or []
    if not messages:
        return ""
    return str(getattr(messages[-1], "text", "") or "")


def _description(profile: AgentProfile) -> str:
    """What the supervisor is told a specialist is for.

    Taken from the profile's own instructions rather than written twice: the first sentence of a
    specialist's prompt already says what it does, and a second description beside it is a thing
    that can disagree with the agent it describes.
    """
    instructions = (profile.instructions or "").strip()
    first = instructions.split(". ")[0].strip()
    return first + "." if first and not first.endswith(".") else (first or profile.name)
