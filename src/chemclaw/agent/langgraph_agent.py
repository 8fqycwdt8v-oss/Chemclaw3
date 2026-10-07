"""The LangGraph conversation agent (layer 1).

`build_langgraph_agent` compiles the graph a turn runs on: instructions, in-process capability
tools, the per-task model route, the middleware chain, skills and human gates. Named for the engine
because "the graph" in this codebase is the Markdown knowledge graph (`kg/graph.py`).

Built on `create_deep_agent`, which wraps `create_agent` (the model/tool loop plus the
`wrap_tool_call`/`wrap_model_call`/`before_model` hooks the audit trail and gates hang off) and is
the only route to `subagents=` and filesystem permissions. Middleware is spliced into upstream's
stack by `.name` (`_middleware`; pinned by `tests/test_middleware_order.py`).

Tools cross unchanged: `core/tool_registry` stores plain callables and LangChain derives schemas
from signatures and docstrings. Skills are narrowed by `skill_access.skill_permits` on the backend,
not on the advertised list, because deepagents publishes skill paths into the prompt
(`agent/skill_backend.py`). The `@wrap_tool_call` chain runs over shared decision functions
(`tool_authz`, `repeat_guard`, `audit`); `tool_call_middleware` is the list.
"""

import logging
import uuid
from collections.abc import Callable, Collection, Mapping, Sequence
from functools import partial
from pathlib import Path
from typing import Annotated, Any, NotRequired, cast

from deepagents import create_deep_agent
from deepagents.backends import CompositeBackend, StateBackend
from deepagents.middleware.filesystem import FilesystemMiddleware
from deepagents.middleware.skills import (
    SKILLS_SYSTEM_PROMPT,
    SkillMetadata,
    SkillsMiddleware,
    SkillsState,
)
from langchain.agents.middleware.types import PrivateStateAttr
from langchain_core.runnables import RunnableLambda
from langgraph.channels.untracked_value import UntrackedValue

# `_capability_tools` keeps its underscore because merged ADRs cite it by name; importing it from
# other modules in this package is the established idiom.
from chemclaw.agent import audit as audit_module
from chemclaw.agent.audit import (
    AuditSink,
    NullAuditSink,
    make_audit_middleware,
)
from chemclaw.agent.chemclaw_agent import (
    _advertised_names,
    _capability_tools,
    instructions_for,
)
from chemclaw.agent.compaction import context_compaction_middleware, disabled_summarizer
from chemclaw.agent.exhibit_notes import ExhibitListing
from chemclaw.agent.exhibit_tools import described_for_deployment
from chemclaw.agent.handoff import PEER_BRIEF
from chemclaw.agent.llm_provider import build_chat_model
from chemclaw.agent.local_skills import (
    LOCAL_SKILLS_LABEL,
    LOCAL_SKILLS_ROOT,
    PERSONAL_TIER_TOOLS,
    personal_skills_available,
)
from chemclaw.agent.loop_cap import AnswerAtTheCap, enforce_loop_cap
from chemclaw.agent.model_calls import model_call_middleware, refuse_unparsed_arguments
from chemclaw.agent.org_skills import ORG_SKILLS_LABEL, ORG_SKILLS_ROOT
from chemclaw.agent.plan_gate import enforce_plan_approval, gate_applies, harness_enabled_for
from chemclaw.agent.plan_link import stamp_plan_link
from chemclaw.agent.plan_scope import ScopedTodoListMiddleware
from chemclaw.agent.preferences import StandingPreferences
from chemclaw.agent.profile_discovery import ProfileError, load_profiles
from chemclaw.agent.profiles import AgentProfile, get_profile, registered_profile_names
from chemclaw.agent.repeat_guard import refuse_repeated_calls
from chemclaw.agent.scratchpad import (
    expire_stale_scratch,
    filesystem_permissions,
    scratchpad_backend,
    scratchpad_tools,
)
from chemclaw.agent.skill_access import SkillNarrowing, skill_permits
from chemclaw.agent.skill_backend import NarrowedSkillsBackend
from chemclaw.agent.skill_manifest import declared_tools, required_tools
from chemclaw.agent.spend_cap import MeterTurnSpend, enforce_spend_cap
from chemclaw.agent.state import ChemclawState
from chemclaw.agent.stored_skill_tools import StoredSkillTools
from chemclaw.agent.subagents import (
    HELPER_BRIEF,
    describe_helper,
    general_purpose_helper,
    governed_roster,
    helper_connectors,
    helper_profile,
    specialist_override,
)
from chemclaw.agent.tool_authz import (
    announce_tool_failures,
    enforce_tool_authz,
    refuse_undeclared_writes,
    refuse_writes_on_dry_run,
    surface_authorization_denials,
    surface_domain_errors,
)
from chemclaw.agent.tool_framing import frame_connector_results, stamp_result_handles
from chemclaw.agent.tool_result_size import bound_tool_results
from chemclaw.agent.tool_schema import as_structured_tool
from chemclaw.connectors.registry import ConnectorError, skills_dirs
from chemclaw.core.config import settings
from chemclaw.core.logging import log_event
from chemclaw.core.model_prose import ModelProse

logger = logging.getLogger(__name__)


def bindable_capability_tools(prof: AgentProfile) -> list[Any]:
    """The in-process tools a graph built for `prof` binds — its capability tools, less the dead.

    Shared by the graph builder and `turn_graph.root_surface`, which predicts what the root binds;
    one filter so the prediction and the graph cannot drift.

    Args:
        prof: The resolved profile.

    Returns:
        The tool functions, in `_capability_tools`' order.
    """
    tools = _capability_tools(prof)
    # A tool whose only outcome is unreachable is not bound: `propose_skill` needs the personal tier
    # (see `local_skills.personal_skills_available`). Filtered here rather than refused inside the
    # tool, because an unbound tool costs nothing in the prefix.
    if not personal_skills_available():
        tools = [fn for fn in tools if fn.__name__ not in PERSONAL_TIER_TOOLS]
    return tools


def build_langgraph_agent(
    model: Any | None = None,
    *,
    profile: str | AgentProfile | None = None,
    actor: str = "",
    correlation_id: str | None = None,
    audit_sink: AuditSink | None = None,
    checkpointer: Any | None = None,
    connectors: list[Any] | None = None,
    response_format: Any | None = None,
    store: Any | None = None,
    stored_skills: StoredSkillTools | None = None,
    helper: bool = False,
    specialist: AgentProfile | None = None,
    handoffs: list[Any] | None = None,
    peer: str = "",
) -> Any:
    """Compile the LangGraph conversation agent for one profile.

    Compiled per turn, because LangGraph binds tools at construction (`ToolNode` can only run what
    it was built with) and a connector session must belong to exactly one turn.

    Args:
        model: The chat model to run on; `None` builds the gateway client
            (`llm_provider.build_chat_model`). Injectable so the wiring is testable offline.
        profile: The profile to narrow by (name, `AgentProfile`, or `None` for the default).
            Narrowing is attenuation only; audit and authorization attach after it.
        actor: Fallback audit actor, used only when a turn stamps no ambient identity.
        correlation_id: Fallback correlation id, same precedence.
        audit_sink: The durable trail. `None` means `default_audit_sink()`; pass `NullAuditSink()`
            to opt out explicitly.
        checkpointer: Where turn state persists between turns, supplied by the caller
            (`chemclaw.agent.checkpointer.checkpointer()` is async and this builder is sync). `None`
            keeps state in the invocation.
        connectors: This turn's already-open connector tools, or `None` for no out-of-process
            capability.
        response_format: A pydantic model the agent must finish by producing (surfaced on
            `structured_response`); `None` answers in prose. A property of the call, so not a
            profile field.
        store: This process's memory store (`agent/scratchpad.memory_store`), or `None` for no
            durable memory. Passed in because creating it is async.
        stored_skills: What the two stored skill tiers declare about tools
            (`agent/stored_skill_tools.stored_skill_declarations`), read by the async caller.
            Omitted, the stored tiers declare nothing.
        helper: Whether this graph is a `task` helper. A helper gets no helpers of its own (the
            recursion guard), `helper_profile`'s in-process narrowing and `helper_connectors`'
            connector narrowing, so "a helper reads, it does not act" travels with this switch.
        specialist: The rostered profile this helper is named for, or `None`. Ignored unless
            `helper`. It only intersects the caller's surface; it replaces just the instructions and
            model route, which carry no authority.
        handoffs: The `transfer_to_<peer>` tools from `agent/handoff.handoff_tools`, or `None`. Only
            the turn graph passes them, so a `task` helper can never hold one.
        peer: This agent's node name in a turn graph, or `""`. Recorded as the audit row's `agent=`
            so peers are distinguishable.

    Returns:
        A compiled graph. Construction only; no network call.
    """
    prof = profile if isinstance(profile, AgentProfile) else get_profile(profile)
    # Resolved before the skills, because skills are narrowed by the tools a profile advertises; a
    # helper's skills therefore narrow with its tools.
    tools = bindable_capability_tools(prof)
    # The helper narrowing lives here, keyed on `helper=True`, so no caller can compile a governed
    # but unnarrowed helper. Names come from `tools` because only this side has run
    # `_register_generated_tools()`. The connector half needs its own subtraction: connector tools
    # never pass through `tool_names`. `tests/test_subagents.py` compares the compiled graphs.
    if helper:
        prof = helper_profile(prof, frozenset(fn.__name__ for fn in tools), specialist)
        tools = _capability_tools(prof)
        connectors = helper_connectors(connectors, specialist)
    # Resolved once and passed to both the middleware and `instructions_for(durable_trail=…)`, so
    # the prompt's description of the trail matches the sink actually used. Looked up through the
    # module so tests patching `chemclaw.agent.audit.default_audit_sink` take effect.
    sink = audit_sink if audit_sink is not None else audit_module.default_audit_sink()
    audit = make_audit_middleware(
        correlation_id=correlation_id if correlation_id is not None else uuid.uuid4().hex,
        actor=actor,
        sink=sink,
        # Which graph wrote the row: the derived `<caller>-helper` name for a helper, the peer name
        # for a peer, empty for the agent the chemist talks to. A helper spawned by a peer is a
        # helper first.
        agent=prof.name if helper else peer,
    )
    # One walk of the skills trees per build, shared by the backend that routes them and the
    # middleware that labels them, so routes and sources cannot disagree.
    labelled = _labelled(_skill_dirs())
    # Effort comes from the profile: it is a property of this agent, not of the deployment.
    chat_model = _resolve_chat_model(model, prof)
    # Connectors arrive already narrowed and open; an unreachable server contributes nothing. The
    # in-process half is converted here once (`agent/tool_schema.py`); connector tools pass through.
    bound = _bound_surface(tools, connectors, handoffs)
    # Built after `bound`, so skills are narrowed by the tools this turn actually binds rather than
    # by manifest allow-lists, which do not change when a server is unreachable. Computed once and
    # handed to every mount (filed and stored tiers ask different questions; see
    # `skill_access.SkillNarrowing`).
    permits = skill_narrowing(
        prof, tools, labelled, available={t.name for t in bound}, stored=stored_skills
    )
    skills = skills_backend(
        prof, tools, labelled=labelled, available={t.name for t in bound}, permits=permits
    )
    # The scratchpad wraps the skills routes rather than replacing them, so the skills middleware
    # and the filesystem tools read the same backend object.
    backend = scratchpad_backend(skills, store, permits=permits)
    shared: dict[str, Any] = {
        "model": chat_model,
        "tools": bound,
        # A helper is told what it is in addition to the caller's prompt: it reads and reports, sees
        # no conversation, and reaches no connector. Narrowed to the tools in `bound`, connectors
        # included, so the prompt never promises a capability the model cannot find.
        "system_prompt": instructions_for(
            prof,
            {tool.name for tool in bound},
            durable_trail=not isinstance(sink, NullAuditSink),
        )
        + (HELPER_BRIEF if helper else "")
        # A peer is told it may not have started the conversation, that the chemist reads it
        # directly, and that handing on extends nothing. A helper never gets a `peer`.
        + (PEER_BRIEF if peer else "")
        + (
            specialist_override(
                specialist,
                frozenset(fn.__name__ for fn in tools)
                | frozenset(tool.name for tool in (connectors or [])),
            )
            if helper and specialist is not None
            else ""
        ),
        "state_schema": ChemclawState,
        "middleware": _middleware(prof, backend, audit, chat_model, labelled),
        "name": "chemclaw",
        # `False`, not `None`, for a helper: `None` makes a subgraph inherit the parent's
        # checkpointer through the run config, writing the helper's thread onto the caller's saver.
        "checkpointer": False if helper else checkpointer,
        "response_format": response_format,
    }
    if helper:
        # A helper is built by `create_agent`, so `SubAgentMiddleware` is structurally absent; with
        # `create_deep_agent` an empty roster would make upstream insert its own ungoverned helper.
        # A helper needs neither `subagents=` nor `permissions=`: it has no store, so no
        # `/memories/` route, and the skills tree refuses writes.
        from langchain.agents import create_agent

        _log_bound_surface(prof, bound, handoffs, [], helper=True, peer=peer)
        return create_agent(**shared)
    helpers = _subagents(prof, chat_model, sink, correlation_id, actor, connectors)
    _log_bound_surface(prof, bound, handoffs, helpers, helper=False, peer=peer)
    return create_deep_agent(
        backend=backend,
        # `skills=` is deliberately absent: it is what would make upstream compose a second skills
        # middleware beside `ReloadingSkillsMiddleware`. `_skills_middleware` says why one is right.
        permissions=filesystem_permissions(),
        subagents=helpers,
        **shared,
    )


def _log_bound_surface(
    profile: AgentProfile,
    bound: Sequence[Any],
    handoffs: Sequence[Any] | None,
    helpers: Sequence[Mapping[str, Any]],
    *,
    helper: bool,
    peer: str,
) -> None:
    """Record what one compiled graph can delegate to at INFO, and every tool it binds at DEBUG.

    Distinguishes a model declining to hand off from a roster that never bound the tool. The full
    tool list is DEBUG because a graph is compiled per turn. Names only, never content.
    """
    transfers = sorted(tool.name for tool in handoffs or [])
    roster = sorted(str(spec.get("name", "")) for spec in helpers)
    # A helper can neither hand off nor spawn (`_subagents`), and a turn compiles one per rostered
    # name, so its line is DEBUG: at INFO the roster would drown the one line per turn that matters.
    log_event(
        logger,
        "tools.delegation_bound",
        "profile %s%s binds %d tool(s); handoffs: %s; helpers via task: %s",
        profile.name,
        f" (peer {peer})" if peer else " (helper)" if helper else "",
        len(bound),
        ", ".join(transfers) or "none",
        ", ".join(roster) or "none",
        profile=profile.name,
        peer=peer,
        helper=helper,
        tools=len(bound),
        handoffs=", ".join(transfers),
        helpers=", ".join(roster),
        level=logging.DEBUG if helper else logging.INFO,
    )
    if logger.isEnabledFor(logging.DEBUG):
        names = sorted(tool.name for tool in bound)
        log_event(
            logger,
            "tools.bound",
            "profile %s binds: %s",
            profile.name,
            ", ".join(names),
            level=logging.DEBUG,
            profile=profile.name,
            tools=", ".join(names),
        )


def _resolve_chat_model(supplied: Any | None, profile: AgentProfile) -> Any:
    """The model this graph runs on: a caller's, this profile's route, or the deployment default.

    A route the deployment actually mapped in `settings.model_routes` wins (so a helper can read on
    a smaller model). Otherwise a supplied model is reused, avoiding a second identical client per
    turn; with neither, the default is built. Effort travels with whichever branch runs.

    Args:
        supplied: The model a caller handed to `build_langgraph_agent`, if any.
        profile: The resolved profile — for a helper, the narrowed one.

    Returns:
        A LangChain chat model. Construction only; no network call.
    """
    routed = profile.model_route is not None and profile.model_route in settings.model_routes
    if routed:
        return build_chat_model(cast(str, profile.model_route), effort=profile.effort)
    if supplied is not None:
        return supplied
    return build_chat_model(effort=profile.effort)


def _middleware(
    profile: AgentProfile,
    backend: CompositeBackend,
    audit: Any,
    model: Any,
    labelled: list[tuple[str, str]],
) -> list[Any]:
    """What this repository adds to — and takes over from — upstream's assembled stack.

    `_apply_custom_middleware` splices by `.name`: an entry matching an upstream name replaces it in
    place; a new name lands after the last core member, inside every tool-registering middleware.
    Being after `FilesystemMiddleware` and `SubAgentMiddleware` is what makes scratchpad writes and
    `task` spawns cross the audit row and authorization gate (`create_agent` nests `wrap_tool_call`
    in list order). `tests/test_middleware_order.py` asserts this.

    The security-critical replacement is `FilesystemMiddleware`, which upstream composes
    unconditionally with all eight verbs; this instance carries `tools=scratchpad_tools()`, which
    withholds `execute` and `delete`. `HarnessProfile.excluded_tools` is not used because it
    silently fails to apply when the model's provider key changes. Upstream's
    `AnthropicPromptCachingMiddleware` remains last in the chain and no-ops on `ChatOpenAI`.

    Args:
        profile: The profile this agent was narrowed by.
        backend: The turn's composite backend — the same object the skills gate was computed for.
        audit: The audit middleware from `make_audit_middleware`.
        model: The resolved chat model, needed only to construct the disabled summarizer.
        labelled: The build's one walk of the skills trees.

    Returns:
        The list to hand `create_deep_agent(middleware=…)`.
    """
    return [
        *_harness_middleware(profile),
        # `_permissions` is passed because a replacement inherits nothing:
        # `create_deep_agent(permissions=…)` only reaches the instance upstream builds, which this
        # replaces. `tools=` decides which verbs exist; `_permissions=` decides where they may point
        # (`agent/scratchpad.py`). The keyword is private, so `tests/test_upstream_surface.py` pins
        # it, and `tests/test_scratchpad.py` drives an out-of-bounds write through the compiled
        # graph. `permissions=` stays on the outer call as the public statement of the rules.
        FilesystemMiddleware(
            backend=backend,
            tools=list(scratchpad_tools()),
            _permissions=filesystem_permissions(),
        ),
        # The `files` channel's retention, once per turn before the first model call
        # (`agent/scratchpad.py`).
        expire_stale_scratch,
        # Replaces the summarizer `create_deep_agent` composes unconditionally; this deployment
        # declines one on prompt-injection grounds (`agent/compaction.py`).
        disabled_summarizer(model, backend),
        _skills_middleware(backend, labelled, profile),
        *tool_call_middleware(audit, profile),
        # The chemist's standing preferences, appended to every model call's instructions; above the
        # compaction group so it is charged as prefix and never cut by the window.
        StandingPreferences(),
        # The session's artefact listing, on the same terms: request-time state rides on the
        # instructions.
        ExhibitListing(),
        # Unconditional: an unbounded thread is a property of a session, not of harness mode. Last,
        # so the reduction sees everything added above it.
        *context_compaction_middleware(),
        # Innermost: observers of the provider call itself, below the compaction group so
        # first-party token counting is not folded into `chemclaw_model_call_duration_seconds`
        # (`agent/model_calls.py`).
        *model_call_middleware(),
    ]


def _subagents(
    profile: AgentProfile,
    model: Any,
    audit_sink: AuditSink | None,
    correlation_id: str | None,
    actor: str,
    connectors: list[Any] | None,
) -> list[Any]:
    """The helpers this agent may spawn, each compiled here so it carries this chain.

    `SubAgentMiddleware` is required upstream, so `task` always ships; left alone, upstream inserts
    a `general-purpose` helper with every tool and none of this repository's middleware. Claiming
    that name displaces it (`agent/subagents.py`). The unnamed helper plus each
    `CHEMCLAW_AGENT_HELPER_ROSTER` entry is built with this function, so each is an intersection of
    the caller's surface; a roster name varies only instructions and model route. A rostered entry
    that would bind nothing is dropped; an unknown name is skipped here and refused at startup
    (`api/app.py`).

    What a helper does and does not inherit:

    - **Connectors:** the caller's already-open read-only connector tools, never state-changing ones
      (narrowed in `build_langgraph_agent`); no new sessions are opened.
    - **No checkpointer** (`checkpointer=False`): a helper answers one prompt with one report.
    - **No store:** no `/memories/` route, so nothing reaches durable memory. The `files` channel is
      not isolated: upstream copies helper state except `messages`, `todos` and
      `structured_response` back to the caller, and `files` is checkpointed, so a helper's scratch
      file persists in the caller's thread. `agent/tool_framing.py` defangs every scratchpad read.
    - **No helpers:** compiled on `create_agent`, so `SubAgentMiddleware` is absent.

    Args:
        profile: The caller's profile; the helper is narrowed from it.
        model: The already-resolved chat model, shared rather than rebuilt.
        audit_sink: The caller's trail, so a helper's tool calls land in the same place.
        correlation_id: The caller's correlation id, so the two halves of one turn are joinable.
        actor: The caller's fallback audit actor.
        connectors: This turn's already-open connector tools, narrowed by the helper's own build.

    Returns:
        The list to hand `create_deep_agent(subagents=…)`.
    """

    def compile_helper(specialist: AgentProfile | None) -> Any:
        """One helper graph, built from the caller's own profile and this turn's open connectors."""
        return build_langgraph_agent(
            model=model,
            profile=profile,
            actor=actor,
            correlation_id=correlation_id,
            audit_sink=audit_sink,
            connectors=connectors,
            helper=True,
            specialist=specialist,
        )

    specs: list[dict[str, Any]] = [general_purpose_helper(compile_helper(None))]
    if not settings.helper_roster:
        return governed_roster(specs)

    # Profile files are registered here so rostered names resolve in any process. Only when the
    # roster names something the registry does not yet hold, because `load_profiles` re-reads every
    # file on each call; inside a `try` so a `ProfileError` cannot kill the turn.
    try:
        if not set(settings.helper_roster) <= set(registered_profile_names()):
            load_profiles()
    except ProfileError:
        logger.warning(
            "helper roster: the profile files could not be loaded, so no named helper is offered",
            exc_info=True,
        )
        return governed_roster(specs)

    held = frozenset(fn.__name__ for fn in _capability_tools(profile))
    offered: set[str] = {specs[0]["name"]}
    for name in settings.helper_roster:
        if name in offered:
            # Upstream keys helpers by name, last wins, so a repeat is skipped. `general-purpose` is
            # reserved from the start: it is the name that displaces upstream's ungoverned helper.
            logger.warning(
                "helper roster: %r is already offered, so the repeat is ignored; upstream keys "
                "subagents by name and would keep only the last",
                name,
            )
            continue
        try:
            rostered = get_profile(name)
        except ValueError:
            # Fail-soft here and loud at startup (`api/app.py`): an absent helper costs delegation,
            # never authority.
            logger.warning(
                "helper roster: no profile named %r, so it is not offered; known: %s",
                name,
                sorted(registered_profile_names()),
            )
            continue
        surface = predicted_helper_surface(profile, held, rostered, connectors)
        if not surface:
            # INFO, not WARNING: a rostered profile whose bundle is off is a normal state, and a
            # narrow caller can empty a helper by intersection.
            logger.info(
                "helper roster: %r is not offered on this turn — nothing survives the intersection "
                "of the caller's surface (%s) with what that profile names. Either its connector "
                "bundle is disabled, or this caller is too narrow for it",
                name,
                profile.name,
            )
            continue
        offered.add(name)
        specs.append(
            {
                "name": name,
                "description": describe_helper(rostered, surface),
                # Compiled on first use, not here, so turns do not pay for helpers they never spawn.
                # The surface above is predicted; `tests/test_subagents.py` asserts it equals what a
                # compiled helper binds.
                "runnable": _compiled_on_first_use(partial(compile_helper, rostered)),
            }
        )
    return governed_roster(specs)


def predicted_helper_surface(
    caller: AgentProfile,
    held: frozenset[str],
    specialist: AgentProfile,
    connectors: list[Any] | None,
) -> frozenset[str]:
    """What a rostered helper *would* bind, without compiling its graph.

    Derived from the same two functions the build applies (`helper_profile`, `helper_connectors`);
    `tests/test_subagents.py::test_the_predicted_surface_is_what_a_compiled_helper_binds` compiles
    and compares. Scratch verbs come from `FilesystemMiddleware` and are not included.

    Args:
        caller: The profile of the agent that would spawn this helper.
        held: The caller's own resolved in-process tool names.
        specialist: The rostered profile the helper is named for.
        connectors: This turn's already-open connector tools.

    Returns:
        The capability tool names the compiled helper would hold, in-process and connector alike.
    """
    narrowed = helper_profile(caller, held, specialist)
    # `narrowed.tool_names` equals the resolved set here, because `helper_profile` builds it by
    # removing from the already-resolved `held`; re-resolving would cost a registry pass.
    in_process = narrowed.tool_names or frozenset()
    reachable = helper_connectors(connectors, specialist) or []
    return in_process | frozenset(tool.name for tool in reachable)


def _compiled_on_first_use(build: Callable[[], Any]) -> Any:
    """A runnable that builds its graph the first time it is invoked, then reuses it.

    Deferred so unused helpers cost nothing, memoised so a second delegation is free. The cache is
    per spec and specs are per turn, so nothing outlives the connectors it closed over.
    `RunnableLambda` over an async callable, because `_build_task_tool` calls `.with_config(...)`
    and then awaits `.ainvoke(...)`.
    """
    compiled: list[Any] = []

    async def invoke(state: Any, config: Any = None) -> Any:
        if not compiled:
            compiled.append(build())
        return await compiled[0].ainvoke(state, config)

    return RunnableLambda(invoke)


class ReloadingSkillsState(SkillsState):
    """`SkillsState` with its cached listing moved to a channel the checkpointer cannot restore.

    Upstream's `skills_metadata` is a checkpointed `LastValue`; as `UntrackedValue` it is absent at
    the start of every run, so upstream's "already loaded" short-circuit never fires. Per turn is a
    property of the channel, as with `ChemclawState.loop_capped`.
    """

    skills_metadata: NotRequired[Annotated[list[SkillMetadata], UntrackedValue, PrivateStateAttr]]


class ReloadingSkillsMiddleware(SkillsMiddleware):
    """`SkillsMiddleware` that re-narrows its listing every turn instead of caching it.

    The role gate reads the turn's identity, so a listing cached from one caller or an earlier role
    would be wrong. The subclass is one redeclared state field (`ReloadingSkillsState`), depending
    only on the field name pinned by `tests/test_upstream_surface.py`. A staleness fix, not the
    gate: `NarrowedSkillsBackend` refuses reads on every call regardless.
    """

    state_schema = ReloadingSkillsState

    def _format_skills_list(self, skills: list[SkillMetadata]) -> str:
        """Upstream's listing, except when there is nothing to list — see `NO_SKILLS`.

        Upstream's empty-listing text invites writing a skill, which `NarrowedSkillsBackend`
        refuses. A private-method override, pinned by `tests/test_upstream_surface.py`.
        """
        if not skills:
            return NO_SKILLS
        return str(super()._format_skills_list(skills))


#: What the model is told when its narrowed skills listing is empty. Upstream's text invites
#: creating a skill, which every write verb refuses (`SkillsReadOnlyRefusal`). Empty may mean the
#: deployment ships none or the predicates removed all for this caller; the model cannot tell, so it
#: is told to answer without a procedure and say so.
NO_SKILLS = ModelProse(
    "(None are available to you in this session. This is either a deployment that ships no "
    "skills or a caller whose role reaches none of them; you cannot tell which, and you cannot "
    "create one — the skills tree is read-only to every turn. Answer from the instructions and "
    "the evidence you gather, and say plainly that you have no procedure for a task that "
    "obviously wants one.)"
)

#: Every passage cut out of upstream's skills prompt, each named as this file refers to it. Removed
#: by substring so the rest of upstream's template keeps arriving on every bump; `_skills_prompt`
#: raises if a passage is no longer found, and `tests/test_upstream_surface.py` pins each one.
#:
#: 1. The Deepagents/Agents provenance sentence: neither label exists here (`_labelled` derives
#:    labels from directories) and there is no machine-wide skills tree.
#: 2. The "Executing Skill Scripts" section: `execute` is withheld (`agent/scratchpad.py`).
#: 3. The example workflow, which instructs running scripts and names a nonexistent skill.
#: 4. The same nonexistent skill again, as an example of a matching request.
_SKILLS_PROMPT_REMOVALS: tuple[tuple[str, str], ...] = (
    (
        'Sources labeled "Deepagents" are specific to this agent tool; sources labeled "Agents" '
        "are shared across all agent tools on this machine.\n\n",
        "the Deepagents/Agents source-label sentence, which names labels no source here "
        "carries and a machine-wide skills tree that does not exist",
    ),
    (
        "**Executing Skill Scripts:**\nSkills may contain Python scripts or other executable "
        "files. Always use absolute paths from the skill list.\n\n",
        "the Executing Skill Scripts section, in a deployment whose `scratchpad_tools()` "
        "withholds `execute` so that nothing here can run one",
    ),
    (
        '**Example Workflow:**\n\nUser: "Can you research the latest developments in quantum '
        'computing?"\n\n1. Check available skills -> See "web-research" skill with its '
        "path\n2. "
        'Read the full skill file: `read_file(file_path="...", limit=1000)`\n3. Follow the '
        "skill's research workflow (search -> organize -> synthesize)\n4. Use any helper "
        "scripts with absolute paths\n\n",
        "the example workflow, which names a web-research skill this tree does not have and "
        "tells the model to use its helper scripts",
    ),
    (
        ' (e.g., "research X" -> web-research skill)',
        "the web-research example in When to Use Skills, naming the same absent skill",
    ),
)


def _skills_prompt() -> str:
    """Upstream's skills prompt minus every passage that is false on this deployment.

    Raises:
        RuntimeError: When one of the passages is no longer in upstream's template, so a bump cannot
            silently restore it. Named one at a time, since the fix differs per passage.
    """
    prompt = SKILLS_SYSTEM_PROMPT
    for passage, description in _SKILLS_PROMPT_REMOVALS:
        if passage not in prompt:
            raise RuntimeError(
                f"deepagents' SKILLS_SYSTEM_PROMPT no longer contains {description}, which this "
                "deployment removes; re-read upstream's template and update "
                f"`_SKILLS_PROMPT_REMOVALS` (agent/langgraph_agent.py). Missing: {passage!r}"
            )
        prompt = prompt.replace(passage, "", 1)
    return prompt


def _bound_surface(
    tools: list[Any], connectors: Sequence[Any] | None, handoffs: Sequence[Any] | None = None
) -> list[Any]:
    """The turn's whole tool surface, refusing a connector tool that claims a first-party name.

    The in-process half is converted here once per process (`agent/tool_schema.py`). `ToolNode` keys
    tools by name and the connector half is appended second, so a connector tool named like a
    first-party one would silently replace it while `authz` and audit still classify it by the
    first-party name. The registry refuses such manifests; this covers the `connectors` argument,
    which bypasses it. Raises a `ConnectorError` worded like the registry's.
    """
    # Handoff tools join `first_party` before the name check, so a connector tool named
    # `transfer_to_<peer>` cannot take over a handoff.
    first_party = [described_for_deployment(as_structured_tool(fn)) for fn in tools] + list(
        handoffs or []
    )
    claimed = {tool.name for tool in first_party}
    for tool in connectors or []:
        if tool.name in claimed:
            raise ConnectorError(
                f"a connector tool named {tool.name!r} was bound alongside the first-party "
                "capability of that name; a connector cannot take a first-party capability's "
                "name, because the name is the authorization key and the model has only one of "
                "them to call"
            )
    return [*first_party, *(connectors or [])]


def _harness_middleware(profile: AgentProfile) -> list[Any]:
    """The plan/execute harness's todo list, and the runaway caps every profile gets.

    The loop cap (`agent/loop_cap.py`) and the spend cap (`agent/spend_cap.py`) are unconditional: a
    runaway is a property of the model/tool cycle, and without them the only bound is
    `agent_recursion_limit`, whose `GraphRecursionError` discards the turn's work. The loop cap
    counts in `before_model`, where no jump can skip it. The spend cap is a `before_model` enforcer
    plus a `wrap_model_call` meter, since only the response carries the bill.

    The todo list is harness-only, and is `ScopedTodoListMiddleware` so each step declares the tools
    an approval of the plan will bound.
    """
    # `AnswerAtTheCap` sits with the hook whose mark it reads: `enforce_loop_cap` authorises one
    # tool-less call per graph past the cap, and this is what makes that call an answer.
    caps = [enforce_loop_cap, enforce_spend_cap, AnswerAtTheCap(), MeterTurnSpend()]
    if not harness_enabled_for(profile):
        return caps
    return [ScopedTodoListMiddleware(), *caps]


def _skills_middleware(
    backend: CompositeBackend, labelled: list[tuple[str, str]], profile: AgentProfile
) -> Any:
    """Wrap a narrowed backend in deepagents' provider — the plumbing around the decision.

    Takes the backend and `labelled` from the caller so the skills read tool, routes and listing all
    use the same objects. `create_deep_agent(skills=…)` is deliberately not passed, so this is the
    only skills middleware and no name-splice is needed.
    """
    # Sources are derived from the routes the backend really has: the personal and org tiers are
    # mounted only on conditions this function cannot see, and a source with no route would publish
    # an empty tier.
    #
    # Order is ascending review depth (personal, organisation, reviewed tree), because upstream
    # resolves name collisions last-source-wins: a shipped skill is never shadowed by a personal
    # one, and an administrator's publication is neither blocked nor shadowed by one person's
    # private name.
    #
    # A profile with `skill_names == []` reaches no tier at all, including `/mine`; that is what the
    # `skills-removed` eval control arm relies on.
    sources: list[tuple[str, str]] = []
    if profile.skill_names != frozenset():
        if LOCAL_SKILLS_ROOT in backend.routes:
            sources.append((f"/{LOCAL_SKILLS_LABEL}", LOCAL_SKILLS_LABEL))
        if ORG_SKILLS_ROOT in backend.routes:
            sources.append((f"/{ORG_SKILLS_LABEL}", ORG_SKILLS_LABEL))
    sources += [(f"/{label}", label) for label, _ in labelled]
    return ReloadingSkillsMiddleware(
        backend=backend,
        sources=sources,
        # Upstream's template minus the passages false here, passed through the constructor seam
        # (`_skills_prompt`).
        system_prompt=_skills_prompt(),
    )


def skills_backend(
    profile: AgentProfile,
    tools: list[Any],
    *,
    labelled: list[tuple[str, str]] | None = None,
    available: Collection[str] | None = None,
    permits: SkillNarrowing | None = None,
) -> CompositeBackend:
    """The skills backend for one profile — a backend that can only reach what it may.

    The narrowing is on the backend, not the listing, because the model reads skill bodies by path
    with a filesystem tool; `NarrowedSkillsBackend` closes `read`, `glob`, `grep` and `ls`, refuses
    writes, and runs in virtual mode so `..` cannot leave the tree. The predicate is evaluated per
    reach, since the role gate reads the turn's identity. Each skills tree (the configured one and
    each enabled bundle's) is routed under its own prefix in a `CompositeBackend`; an unrouted path
    reaches an empty `StateBackend`.

    Args:
        profile: The profile whose surface the capability predicate is scoped by.
        tools: This profile's resolved in-process tools, used only when `available` is omitted.
        labelled: The caller's already-walked `(label, directory)` list; omitted, it is walked here.
        available: The tool names this turn actually binds, connectors included. Omitted, it falls
            back to `_advertised_names` (manifest allow-lists), which is right for "what does this
            profile advertise" but not for a turn, since manifests ignore unreachable servers.
        permits: The turn's precomputed `skill_narrowing`; these mounts use its `filed` half.
            Omitted, it is computed here.
    """
    labelled = labelled if labelled is not None else _labelled(_skill_dirs())
    if permits is None:
        permits = skill_narrowing(profile, tools, labelled, available=available)
    return CompositeBackend(
        default=StateBackend(),
        routes={
            f"/{label}/": NarrowedSkillsBackend(directory, permits.filed)
            for label, directory in labelled
        },
    )


def skill_narrowing(
    profile: AgentProfile,
    tools: list[Any],
    labelled: list[tuple[str, str]],
    *,
    available: Collection[str] | None = None,
    stored: StoredSkillTools | None = None,
) -> SkillNarrowing:
    """Whether this turn may reach a skill, by name — **one narrowing, computed once per build**.

    The reviewed tree, the personal tier and the organisation's tier are three mounts read through
    one backend, so the narrowing is computed once and handed to every mount; each mount binds the
    half for its kind of tier (`skill_access.SkillNarrowing`). One computation also means one
    `_log_narrowing` line per build.

    Args:
        profile: The profile whose surface the capability predicate is scoped by.
        tools: This profile's resolved in-process tools, used only when `available` is omitted.
        labelled: The already-walked `(label, directory)` list, for the declared-tools map.
        available: The tool names this turn actually binds, connectors included (see
            `skills_backend` for the fallback).
        stored: What the stored tiers declare (`agent/stored_skill_tools.py`), read by the async
            caller. Omitted, they declare nothing.

    Returns:
        The narrowing per kind of tier, each evaluated per reach because the role gate reads ambient
        identity.
    """
    directories = [directory for _label, directory in labelled]
    declared = declared_tools(directories)
    required = required_tools(directories)
    narrowing = skill_permits(
        enabled=settings.skills_enabled_list,
        # Merged so `ToolScopedSkills` treats every declaration alike. The filed entry wins a name
        # held by both: a stored declaration for a shipped name describes a body no turn can read,
        # and must not hide the reviewed skill.
        declared={**(stored.declared if stored is not None else {}), **declared},
        required={**(stored.required if stored is not None else {}), **required},
        available=available if available is not None else _advertised_names(profile, tools),
        gates=settings.skill_role_gates,
        names=profile.skill_names,
        # The keys of this walk rather than `shipped_skill_names()`, so reserved names come from the
        # same walk the backend routes.
        reserved=frozenset(declared),
    )
    # The filed map only: stored skill names are a chemist's own vocabulary and stay out of logs.
    _log_narrowing(profile, declared, narrowing.filed)
    return narrowing


def _log_narrowing(
    profile: AgentProfile, declared: Mapping[str, frozenset[str]], permits: Callable[[str], bool]
) -> None:
    """Record which skills this build will offer, and how many the three predicates removed.

    Answers "was the skill even offered?" for one session. DEBUG because a graph is compiled per
    turn. Names are skill directory names (configuration, not content), from the walk the backend
    routes.
    """
    if not logger.isEnabledFor(logging.DEBUG):
        # The predicate is evaluated per skill and the role gate reads a contextvar each time, so
        # the loop is skipped rather than computed and thrown away — this runs on every turn.
        return
    offered = sorted(name for name in declared if permits(name))
    log_event(
        logger,
        "skills.narrowed",
        "profile %s offers %d of %d discovered skill(s): %s",
        profile.name,
        len(offered),
        len(declared),
        ", ".join(offered) or "none",
        level=logging.DEBUG,
        profile=profile.name,
        offered=len(offered),
        denied=len(declared) - len(offered),
        skills=", ".join(offered),
    )


def _skill_dirs() -> list[str]:
    """Every tree skills are discovered from: the configured one, then each enabled bundle's own.

    One definition for the backend's routes and the middleware's labels.
    """
    return [*settings.skills_dirs, *skills_dirs()]


def shipped_skill_names() -> frozenset[str]:
    """Every name this deployment's *reviewed* trees occupy — declared, or held by a broken file.

    Used by `api/routes/skills.py` and `local_skills.validated_skill` to refuse a personal skill
    taking a shipped name, from the same walk the graph does. A broken `SKILL.md` occupies its
    directory name (`skill_manifest._declared_pair`), so a personal skill cannot shadow a shipped
    skill that is one fix away from working; `tests/test_local_skills.py` holds this. Cheap per
    request: `declared_tools` is cached on the directory tuple.

    Returns:
        Every occupied name, including every enabled connector bundle's own `skills/`.
    """
    return frozenset(declared_tools([directory for _label, directory in _labelled(_skill_dirs())]))


def _labelled(dirs: list[str]) -> list[tuple[str, str]]:
    """`(label, directory)` per skills tree, with labels unique and stable.

    The label is the route prefix and the source shown to the model, so it must be unique. A tree is
    named by its parent directory (bundle trees all end in `skills`); remaining collisions get a
    numeric suffix, advanced until it is free of every label already emitted. Order follows `dirs`,
    so precedence is unchanged.
    """
    used: set[str] = set()
    labelled: list[tuple[str, str]] = []
    for directory in dirs:
        path = Path(directory)
        base = path.parent.name if path.name == "skills" and path.parent.name else path.name
        label, count = base, 0
        while label in used:
            count += 1
            label = f"{base}-{count}"
        used.add(label)
        labelled.append((label, directory))
    return labelled


def tool_governance_middleware(audit: Any, profile: AgentProfile) -> list[Any]:
    """What governs a tool call, outermost first.

    - audit outermost, so a denied or refused attempt is a recorded attempt;
    - authorization, dry-run and repeat gates inside audit, each a decision worth recording;
    - `announce_tool_failures` first in this list (inside the converters), because it must see every
      failure, including refusals raised by gates below it.

    All are no-ops on the dev path. Kept separate from the model-facing converters and framing
    (`tool_call_middleware`), because a template step has no model: governance must raise for it,
    not return a refusal as prose that a later step would interpolate as a result.
    """
    return [
        # Outside everything that raises: nesting is list order, and a gate such as
        # `enforce_plan_approval` raises before calling its handler, so an announcer below it would
        # never see the refusal.
        announce_tool_failures,
        audit,
        # Only for a profile that narrows; inside `audit` and before `enforce_tool_authz`, because
        # "was this agent built with that tool" is the coarser question. It is the wording, not the
        # enforcement: the tool is already absent from the compiled `ToolNode`. Without it the call
        # fails with LangGraph's "not a valid tool" text, which invites a retry and lists the whole
        # inventory in the audit row.
        *([refuse_undeclared_writes(profile.tool_names)] if profile.tool_names is not None else []),
        enforce_tool_authz,
        refuse_writes_on_dry_run,
        refuse_repeated_calls,
        # Below the announcer, audit, authorization and guards, so all of them see a promoted
        # malformed call (`PromoteInvalidToolCalls`); it raises before the tool body. Under the
        # harness, `enforce_plan_approval` and `stamp_plan_link` nest inside it, so the plan gate
        # never sees a promoted call and the turn reports a fault, not a refusal.
        # `tests/test_invalid_tool_calls.py` pins the gated order.
        refuse_unparsed_arguments,
        *([enforce_plan_approval] if gate_applies(profile) else []),
        # Innermost, inside the gate so a refused call binds no link: binds the current plan step
        # and hash for launchers to stamp onto jobs. Attached whenever the harness runs, in any
        # autonomy mode.
        *([stamp_plan_link] if harness_enabled_for(profile) else []),
    ]


def tool_call_middleware(audit: Any, profile: AgentProfile) -> list[Any]:
    """The governed chain plus the entries that exist only because a *model* reads the result.

    The two converters go outermost, so audit records an exception unchanged before it becomes what
    the model reads (`wrap_tool_call` nests in list order). `frame_connector_results` and
    `stamp_result_handles` are presentation for the model, so they are here rather than in
    `tool_governance_middleware`: a template step's `${steps.<id>.result}` must be exactly what the
    tool returned.
    """
    return [
        surface_authorization_denials,
        surface_domain_errors,
        # Outermost of what rewrites a result, so the handle line lies outside the envelope; inside
        # the converters, because a refusal names no stored result.
        stamp_result_handles,
        # Inside the converters, so a refusal (this system's own sentence) is never framed as
        # untrusted evidence. Outside `audit`, so `audit_events.detail` and `announce_tool_failures`
        # see the untouched result.
        frame_connector_results,
        # Inside the framing, so the envelope wraps an already-bounded payload; outside governance,
        # so the audit row records the full result. Every tool, in-process included
        # (`agent/tool_result_size.py`).
        bound_tool_results,
        *tool_governance_middleware(audit, profile),
    ]
