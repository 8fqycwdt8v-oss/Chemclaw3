"""The LangGraph conversation agent — layer 1 rebuilt (D-2026-08-10, phase M1).

`build_langgraph_agent` is the LangGraph twin of `chemclaw_agent.build_agent`: same instructions,
same in-process capability tools, same per-task model route, and — as later phases land — the same
middleware chain, skills and human gates. Which one a deployment gets is `settings.agent_engine`,
so an unfinished engine is never what runs in production.

**Named for the engine, not for "graph", and that is not fussiness.** In this codebase *the graph*
is the Markdown knowledge graph — layer 4, `kg/graph.py`, whose own `build_graph` builds a NetworkX
index of the notes. A `agent/graph.py::build_graph` beside it would put two unrelated
`build_graph`s one import apart, in a tree whose `ARCHITECTURE.md` exists largely to explain the
name pairs that look like duplicates and are not. The engine's name is the unambiguous half.

**Why `create_agent` rather than a hand-built `StateGraph`.** The decision to rebuild rather than
port was about using the framework's own machinery instead of re-implementing it, and
`create_agent` *is* a `StateGraph` — it returns a compiled graph with the model/tool loop already
wired and, more importantly, with the middleware system (`wrap_tool_call`, `wrap_model_call`,
`before_model`) that phases M3–M5 need for the audit trail, the authorization gate and the plan
approval. Assembling those nodes by hand would reproduce that loop and lose the hooks, which is the
opposite of the decision. Where Chemclaw genuinely adds a step of its own, it becomes a node in a
graph that wraps this one; it does not become a reason to build this one twice.

**Tools cross unchanged.** `core/tool_registry` stores plain callables — its `@tool` decorator is
identity, and the registry imports nothing but `typing` and `collections.abc`. LangChain derives a
tool schema from a callable's signature and docstring exactly as MAF did, so the whole in-process
capability surface transfers with no adapter and no second declaration. That is the seam D-118 and
the R2 layering move bought, collected here rather than argued about.

**Skills are the same skills** (M4), narrowed by the same three predicates
(`skill_access.skill_permits`) — but enforced on the *backend* rather than on the advertised list,
because deepagents publishes skill paths into the system prompt and expects a filesystem tool to
fetch the bodies. `agent/skill_backend.py` says why that difference is a security property and not
an API detail.

**The middleware chain is the same chain** (M3). Six `@wrap_tool_call` wrappers in the same nesting
order as the MAF agent's, over the *same* decision functions — `tool_authz.dry_run_refusal`,
`.denial_result`, `.domain_error_result`, `.failure_detail`, `repeat_guard.count_call`, and
`audit._recording`. Only the plumbing was ported; a second copy of any of those sentences would let
an authorization decision, a dry-run refusal or a GxP audit row depend on which engine a deployment
happens to run, which is the one drift this migration must be incapable of.

What is deliberately *not* here yet, because nothing calls it: the extra state fields (they arrive
with the phase that reads them), the human gate and the plan-approval middleware (M5), a *durable*
checkpointer (M6 — the parameter exists, the Postgres saver behind it does not) and the per-turn
connector tools (M7). A stub advertising a capability this engine does not have would read as
coverage while proving nothing.
"""

import uuid
from pathlib import Path
from typing import Any

from deepagents.backends import CompositeBackend, StateBackend
from deepagents.middleware.skills import SkillsMiddleware
from langchain.agents import create_agent
from langchain.agents.middleware import TodoListMiddleware
from langchain_core.runnables import RunnableConfig

# `_capability_tools` keeps its underscore deliberately. It is named in six merged ADRs (D-040,
# D-075, D-086 among them) and merged ADRs are never edited, so renaming it to mark this second
# caller would break every one of those citations to buy nothing — the same argument that freezes
# the `D-NNN` sequence. Three tests already import it across module boundaries; within one package
# that is the established idiom here.
from chemclaw.agent.audit import AuditSink, make_langgraph_audit_middleware
from chemclaw.agent.chemclaw_agent import (
    _advertised_names,
    _capability_tools,
    instructions_for,
)
from chemclaw.agent.llm_provider import build_chat_model
from chemclaw.agent.plan_gate import gate_applies, lg_enforce_plan_approval
from chemclaw.agent.profiles import AgentProfile, get_profile
from chemclaw.agent.repeat_guard import lg_refuse_repeated_calls
from chemclaw.agent.skill_access import skill_permits
from chemclaw.agent.skill_backend import NarrowedSkillsBackend, skill_read_tool
from chemclaw.agent.skill_manifest import declared_tools
from chemclaw.agent.state import ChemclawState
from chemclaw.agent.tool_authz import (
    lg_announce_tool_failures,
    lg_enforce_tool_authz,
    lg_refuse_writes_on_dry_run,
    lg_surface_authorization_denials,
    lg_surface_domain_errors,
)
from chemclaw.connectors.registry import skills_dirs
from chemclaw.core.config import settings


def build_langgraph_agent(
    model: Any | None = None,
    *,
    profile: str | AgentProfile | None = None,
    actor: str = "",
    correlation_id: str | None = None,
    audit_sink: AuditSink | None = None,
    checkpointer: Any | None = None,
) -> Any:
    """Compile the LangGraph conversation agent for one profile.

    Args:
        model: The LangChain chat model to run on. Injectable for the same reason
            `build_agent(chat_client=...)` is: the wiring must be assemblable and testable without
            live credentials. `None` builds the config-selected provider
            (`llm_provider.build_chat_model`).
        profile: The profile to narrow by (a name, an `AgentProfile`, or `None` for the default,
            which advertises the full in-process surface). Narrowing is attenuation only — the
            audit trail and the per-tool authorization gate are attached *after* it, so a profile
            attenuates capability and can never bypass either.
        actor: Fallback audit actor, used only when a turn stamps no ambient identity. Same
            precedence and same reason as `build_agent`: an agent outlives a turn, so anything
            bound here would be shared by every user on the pod.
        correlation_id: Fallback correlation id, same precedence.
        audit_sink: The durable trail. `None` means `default_audit_sink()`; pass `NullAuditSink()`
            to opt out explicitly, never by forgetting.
        checkpointer: Where turn state is persisted between turns. `None` keeps state in the
            invocation, which is every caller today; M6 is what supplies the durable one. Accepted
            now because the behaviour that depends on it exists now — `ReloadingSkillsMiddleware`
            answers a staleness question only a persisted session can pose, and a fix whose proof
            waits for a later phase is a fix nobody has checked.

    Returns:
        A compiled graph. No network call happens here; construction only, exactly as
        `build_agent` promises.
    """
    prof = profile if isinstance(profile, AgentProfile) else get_profile(profile)
    # Resolved before the skills, because the skills are narrowed by them: a skill is judgment
    # *about* tools, so which tools this profile advertises decides which judgment is worth
    # offering (`skills_middleware`).
    tools = _capability_tools(prof)
    audit = make_langgraph_audit_middleware(
        correlation_id=correlation_id if correlation_id is not None else uuid.uuid4().hex,
        actor=actor,
        sink=audit_sink,
    )
    backend = skills_backend(prof, tools)
    return create_agent(
        model=model if model is not None else build_chat_model(),
        # The skills read tool is agent-scoped rather than a `@tool` in the process registry,
        # because it is bound to *this* profile's narrowed backend — a registry entry would have
        # to be bound to none, which is the one thing it must not be.
        tools=[*tools, skill_read_tool(backend)],
        system_prompt=instructions_for(prof),
        state_schema=ChemclawState,
        middleware=[
            TodoListMiddleware(),
            _skills_middleware(backend),
            *_middleware(audit, prof),
        ],
        name="chemclaw",
        checkpointer=checkpointer,
    )


class ReloadingSkillsMiddleware(SkillsMiddleware):
    """`SkillsMiddleware` that re-narrows its listing every turn instead of caching it.

    Upstream loads skills once and then skips the load whenever `skills_metadata` is already in
    state — "from a prior turn or checkpointed session", as its own docstring says. That is a sound
    cache for a fixed skills tree and wrong for a narrowed one: the role gate reads the turn's
    ambient identity, so a listing computed for one caller would be served to the next, and a
    mid-session role change would keep advertising skills the caller no longer holds. The MAF path
    never had the problem — `RoleScopedSkillsSource._permits` is consulted on every `get_skills` —
    so reloading is what makes the two engines agree, not an optimisation given up.

    **Why a subclass and not a `before_agent` hook that clears the slot.** That was the first
    attempt and it was worse than the bug: a state update of `{"skills_metadata": None}` leaves the
    *key* present, so the upstream check still short-circuited and the prompt rendered an empty
    list — measured, 28 skills on turn one and 0 on every turn after. Hiding the key from the
    state this method reads is the only version that actually reaches the load.

    **This is a staleness fix, not the gate.** `NarrowedSkillsBackend` refuses the *read* on every
    call regardless, so a stale listing could at worst advertise a skill whose body then came back
    refused. Fixing the listing keeps the two consistent, which is what a caller reading "these are
    your skills" is entitled to.
    """

    def before_agent(self, state: Any, runtime: Any, config: RunnableConfig | None = None) -> Any:
        """Reload against this turn's identity (sync path)."""
        return super().before_agent(
            _without_cached_skills(state), runtime, config or RunnableConfig()
        )

    async def abefore_agent(
        self, state: Any, runtime: Any, config: RunnableConfig | None = None
    ) -> Any:
        """Reload against this turn's identity (async path — the one a turn actually takes).

        `config` is defaulted because LangChain invokes the hook with two arguments, not the three
        upstream's own signature declares — measured, from the `TypeError` the three-argument
        override raised on the first run. Forwarding `{}` rather than `None` keeps the upstream
        backend resolution on the path it expects.
        """
        return await super().abefore_agent(
            _without_cached_skills(state), runtime, config or RunnableConfig()
        )


def _without_cached_skills(state: Any) -> Any:
    """`state` minus its cached skill listing, so the upstream load is not skipped.

    A copy rather than a mutation: the state belongs to the graph, and removing a key from it for
    real would be a side effect on everything downstream rather than on one method's view.
    """
    return {key: value for key, value in state.items() if key != "skills_metadata"}


def _skills_middleware(backend: CompositeBackend) -> Any:
    """Wrap a narrowed backend in deepagents' provider — the plumbing around the decision.

    Private, and split from `skills_backend` for the reason `chemclaw_agent.skills_source` is split
    from `_build_skills`: the backend is the part with behaviour and the middleware is somebody
    else's object around it, exposing no reader for the backend it was given. Tests assert against
    `skills_backend`, so this half needs no name outside the module.

    Takes the backend rather than building one, because the caller has to hold it anyway — the
    skills read tool is bound to the same instance, and two backends built from one config would be
    two objects that merely happen to agree.
    """
    return ReloadingSkillsMiddleware(
        backend=backend,
        sources=[(f"/{label}", label) for label, _ in _labelled(_skill_dirs())],
    )


def skills_backend(profile: AgentProfile, tools: list[Any]) -> CompositeBackend:
    """The skills backend for one profile — a backend that can only reach what it may.

    The LangGraph twin of `chemclaw_agent.skills_source`, narrowed by the *same* three predicates
    (`skill_access.skill_permits`) so a role-gated skill cannot be hidden under one engine and
    offered under the other.

    **The narrowing is on the backend, not on the advertised list, and that is the whole point.**
    `SkillsMiddleware` publishes each skill's path into the system prompt and expects the model to
    read the body with a filesystem tool over this same backend, so filtering only what is listed
    would leave every hidden skill one guessed path away. `NarrowedSkillsBackend` closes `read`,
    `glob` and `grep` as well as `ls`, refuses the write half outright, and runs in virtual mode so
    `..` cannot leave the tree.

    One backend per built agent, and the predicate is evaluated per reach rather than baked in:
    the role gate reads the turn's ambient identity, and one agent serves every concurrent turn.

    **Several trees, one backend, via `CompositeBackend`.** Skills come from the configured
    `skills_dir` *and* from every enabled connector bundle's own `skills/` (D-118), while
    `SkillsMiddleware` takes exactly one backend and virtual mode roots each `FilesystemBackend` at
    a single directory. So each tree gets a virtual prefix routed to its own narrowed backend, and
    an unrouted path reaches `StateBackend` — which is empty, holds no filesystem, and is therefore
    the right thing for a path that matches no skills tree to find.
    """
    dirs = _skill_dirs()
    permits = skill_permits(
        enabled=settings.skills_enabled_list,
        declared=declared_tools(dirs),
        available=_advertised_names(profile, tools),
        gates=settings.skill_role_gates,
    )
    return CompositeBackend(
        default=StateBackend(),
        routes={
            f"/{label}/": NarrowedSkillsBackend(directory, permits)
            for label, directory in _labelled(dirs)
        },
    )


def _skill_dirs() -> list[str]:
    """Every tree skills are discovered from: the configured one, then each enabled bundle's own.

    One definition because two callers derive from it — the backend routes them and the middleware
    labels them — and a listing whose routes and sources disagree would advertise skills at paths
    that resolve to nothing.
    """
    return [*settings.skills_dirs, *skills_dirs()]


def _labelled(dirs: list[str]) -> list[tuple[str, str]]:
    """`(label, directory)` per skills tree, with labels unique and stable.

    The label is both the route prefix and what `SkillsMiddleware` shows a reader as the skill's
    source, so it has to be unique: a bundle's tree is `connectors/<name>/skills`, and every one of
    them has the leaf name `skills`. Naming a tree by its *parent* distinguishes the bundles and
    leaves the configured root as itself; a numeric suffix settles anything still colliding, which
    keeps the function total rather than correct-until-someone-nests-two-trees-alike.

    Order follows `dirs`, so precedence is unchanged: `SkillsMiddleware` loads sources in order and
    a later one wins, matching `FileSkillsSource`'s own first-wins rule once the list is read the
    way each library reads it.
    """
    seen: dict[str, int] = {}
    labelled: list[tuple[str, str]] = []
    for directory in dirs:
        path = Path(directory)
        base = path.parent.name if path.name == "skills" and path.parent.name else path.name
        count = seen.get(base, 0)
        seen[base] = count + 1
        labelled.append((base if not count else f"{base}-{count}", directory))
    return labelled


def _middleware(audit: Any, profile: AgentProfile) -> list[Any]:
    """The tool-call chain, outermost first — the MAF agent's order, ported not redesigned.

    The order is load-bearing and every position was argued for once already, so it is reproduced
    rather than re-derived:

    - the two converters outermost, so an exception still reaches audit *unchanged* and is recorded
      as an `error` outcome before either turns it into what the model reads;
    - audit next, so a denied or refused attempt is a recorded attempt;
    - authorization inside audit, then the dry-run and repeat gates beside it, for the same reason:
      each is a decision worth recording and worth explaining to the model;
    - `announce_tool_failures` innermost, closest to the tool body, because it is the only one that
      must see the raw exception from *every* failure — including the two the converters above turn
      into results — so the chemist's transcript shows the step that did not work (D-138).

    LangChain nests `wrap_tool_call` middleware in list order, so first here is outermost, exactly
    as MAF's list is read. All six are no-ops on the dev path: the sink is log-only, authz is open
    until `entra_required`, `is_dry_run()` is False off the request path, and the repeat counter is
    absent unless a turn started one.

    The plan-approval gate is the seventh, inserted by `_middleware` before
    `lg_announce_tool_failures` to keep that one innermost.
    """
    return [
        lg_surface_authorization_denials,
        lg_surface_domain_errors,
        audit,
        lg_enforce_tool_authz,
        lg_refuse_writes_on_dry_run,
        lg_refuse_repeated_calls,
        *([lg_enforce_plan_approval] if gate_applies(profile) else []),
        lg_announce_tool_failures,
    ]
