"""The LangGraph conversation agent — layer 1 rebuilt (D-2026-08-10, phase M1).

`build_langgraph_agent` builds the compiled graph a turn runs on: the instructions, the in-process
capability tools, the per-task model route, the middleware chain, the skills and the human gates.
It was written as the twin of `chemclaw_agent.build_agent` and lived behind a config switch until
it carried the whole suite; the switch and the other engine are gone (M13 Step 3), so this is what
a deployment gets.

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

**The middleware chain is the same chain** (M3). Seven `@wrap_tool_call` wrappers in the same
nesting order as the previous engine's, over the *same* decision functions —
`tool_authz.dry_run_refusal`, `.denial_result`, `.domain_error_result`, `.failure_detail`,
`repeat_guard.count_call`, and `audit._recording`. Only the plumbing was ported; a second copy of
any of those sentences would let an authorization decision, a dry-run refusal or a GxP audit row
depend on which engine a deployment happens to run, which is the one drift this migration must be
incapable of.

This paragraph used to list what was "deliberately not here yet, because nothing calls it" — the
extra state fields, the plan-approval middleware, a durable checkpointer, the per-turn connector
tools. Every item on it has since arrived (M5–M7), and the list outlived its subject: it was still
telling a reader in M13 that the Postgres saver behind `checkpointer=` did not exist, four phases
after `agent/checkpointer.py` shipped it. The rule it stated is the part worth keeping — a stub
advertising a capability this engine does not have reads as coverage while proving nothing — and
`agent/compaction.py` records what happens when the inverse is left standing: prose that keeps
advertising a mechanism after the mechanism is gone.
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
from chemclaw.agent.audit import AuditSink, make_audit_middleware
from chemclaw.agent.challenge import CHALLENGER_PROFILE
from chemclaw.agent.challenge_gate import build_challenge_gate
from chemclaw.agent.chemclaw_agent import (
    _advertised_names,
    _capability_tools,
    instructions_for,
)
from chemclaw.agent.compaction import context_compaction_middleware
from chemclaw.agent.llm_provider import build_chat_model, prompt_caching_middleware
from chemclaw.agent.loop_cap import enforce_loop_cap
from chemclaw.agent.plan_gate import enforce_plan_approval, gate_applies, harness_enabled_for
from chemclaw.agent.profiles import AgentProfile, get_profile
from chemclaw.agent.repeat_guard import refuse_repeated_calls
from chemclaw.agent.skill_access import skill_permits
from chemclaw.agent.skill_backend import NarrowedSkillsBackend, skill_read_tool
from chemclaw.agent.skill_manifest import declared_tools
from chemclaw.agent.state import ChemclawState
from chemclaw.agent.team import SPECIALISTS, build_team_middleware, team_enabled
from chemclaw.agent.tool_authz import (
    announce_tool_failures,
    enforce_tool_authz,
    refuse_undeclared_writes,
    refuse_writes_on_dry_run,
    surface_authorization_denials,
    surface_domain_errors,
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
    connectors: list[Any] | None = None,
    response_format: Any | None = None,
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
            invocation. The durable one is `chemclaw.agent.checkpointer.checkpointer()`, which the
            caller supplies rather than this function building: it is an async factory that
            migrates on first use, and `build_langgraph_agent` is synchronous and resource-free by
            the same promise `build_agent` makes.
        connectors: This turn's already-open connector tools
            (`chemclaw.connectors.registry.open_connector_specs`), or `None` for an agent with no
            out-of-process capability.
        response_format: A pydantic model the agent must finish by producing, surfaced on the
            returned state's `structured_response`. `None` — the conversational default — leaves the
            agent answering in prose. This exists for callers whose *whole* output is a datum rather
            than a reply: `agent/challenge.py`'s panel members each return a `ChallengeVerdict`, and
            letting the framework enforce that makes the failure mode "no structured answer"
            (handled) instead of "prose that almost parses" — the same reason `verify_answer` uses
            `with_structured_output` rather than reading a judge's free text. It is a passthrough to
            `create_agent` and deliberately not a profile field: which shape an answer must take is
            a property of the *call*, not of the agent's capability.

    Returns:
        A compiled graph. No network call happens here; construction only, exactly as
        `build_agent` promises.

    **Compiled per turn, because LangGraph binds tools at construction.** MAF appends run-scoped
    tools with `agent.run(tools=…)`, so one process-lived `Agent` served every turn and took that
    turn's connectors as an argument. A compiled graph's `ToolNode` is built from the tool list it
    was given, and `wrap_model_call`'s `request.override(tools=…)` narrows only what the *model
    sees*, never what the executor can run — so a connector tool absent at compile time cannot be
    called at all. A connector's session must belong to exactly one turn (measured: two concurrent
    turns over one MCP tool object deadlock, and the second turn's calls travel over the first
    turn's connection, misattributing them in the connector's own log), so the graph's lifetime is
    pinned to its connectors' — one turn. `chemclaw_agent.connector_tools` records the rule; this
    is what the rule costs on this engine, measured in `tests/test_langgraph_connectors.py`
    against the ~90 ms MAF agent build D-123 recorded.
    """
    prof = profile if isinstance(profile, AgentProfile) else get_profile(profile)
    # Resolved before the skills, because the skills are narrowed by them: a skill is judgment
    # *about* tools, so which tools this profile advertises decides which judgment is worth
    # offering (`skills_middleware`).
    tools = _capability_tools(prof)
    audit = make_audit_middleware(
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
        #
        # The connectors are already narrowed by the profile and already open: `connector_specs`
        # applies `mcp_server_names`, the manifest allow-list bounds each surviving bundle, and
        # `open_connector_specs` returns only what a reachable server actually advertised. An
        # unreachable one contributes nothing here, which is the degradation the turn survives.
        tools=[*tools, *(connectors or []), skill_read_tool(backend)],
        system_prompt=instructions_for(prof),
        state_schema=ChemclawState,
        middleware=[
            *_harness_middleware(prof),
            _skills_middleware(backend),
            # `[*tools, *connectors]`, not `tools`: the widening assertion this feeds compares
            # *connector* tool names against the supervisor's surface, and `tools` is the in-process
            # half only — so every connector tool a specialist kept read as a widening and
            # `_narrowed_connectors` raised `TeamError` before the model was ever called. Measured
            # live with `agent_teams_enabled=true`: 15 of 15 turns failed at graph construction,
            # each booking a `turn_costs` row with `completed=false` and zero tokens. The guard was
            # right and the set it was given was the wrong half of the surface.
            *_team_middleware(
                prof,
                actor,
                correlation_id,
                audit_sink,
                connectors,
                [*tools, *(connectors or [])],
            ),
            *_challenge_middleware(prof, actor, correlation_id, audit_sink, connectors),
            *tool_call_middleware(audit, prof),
            # Above the compaction group so that group keeps the innermost position its own
            # docstring argues for. The two do not contend: caching marks the *system prompt and
            # tool schemas*, which compaction never touches, and the message-tail breakpoint is
            # placed by the provider at request time — on whatever list compaction hands it.
            # Provider-specific, so which middleware this is (or that it is none) is decided in the
            # F0 seam rather than here.
            *prompt_caching_middleware(),
            # Unconditional, unlike the harness middleware above it: an unbounded thread is a
            # property of a session, not of the plan/execute mode, and the single-turn agent
            # accumulates one just as fast. Last in the list, so the reduction is the last thing
            # between the assembled request and the model and it sees everything the middleware
            # above added.
            *context_compaction_middleware(),
        ],
        name="chemclaw",
        checkpointer=checkpointer,
        response_format=response_format,
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


def _harness_middleware(profile: AgentProfile) -> list[Any]:
    """The plan/execute harness: the todo list the plan gate reads, and the runaway cap.

    Both conditional on `harness_enabled_for`, matching MAF: the classic agent has no todo list and
    no loop cap, so attaching either unconditionally would make this engine behave differently from
    the other while both are live — a safer difference, but a difference.

    `enforce_loop_cap` both enforces the cap and records it, and `loop_cap.loop_capped` reads that
    record. One counter for one number — see its docstring for why the framework's own
    `ModelCallLimitMiddleware` could not supply the observation half.
    """
    if not harness_enabled_for(profile):
        return []
    return [TodoListMiddleware(), enforce_loop_cap]


def _team_middleware(
    profile: AgentProfile,
    actor: str,
    correlation_id: str | None,
    audit_sink: AuditSink | None,
    connectors: list[Any] | None,
    supervisor_tools: list[Any],
) -> list[Any]:
    """The specialist team, when this deployment routes turns through one (M9).

    Empty unless `agent_teams_enabled`, so the default agent is byte-identical to the one before
    teams existed — a capability M12 has not yet shown to help is not something to switch on for
    everybody (`agent/team.py` says why at length).

    **A specialist is only ever built for a profile that is not itself a specialist**, which is what
    stops the recursion: `build_team_middleware` builds each one through this same function, and a
    specialist whose own profile enabled a team would build five more. The guard is the profile's
    membership in `SPECIALISTS` rather than a depth counter, because "a specialist does not have a
    team" is the rule, and a depth counter would merely bound how badly it was broken.

    The turn's identity and sink are passed down so a specialist audits under the same correlation
    id. Its connectors are **narrowed to its own profile before they are passed**, which
    `build_team_middleware` does — handing them down whole was a real widening: `reject_widening`
    compares *declarations*, and the connector tools arrive already open, so a specialist declaring
    two bundles received every bundle the supervisor had.
    """
    if not team_enabled() or profile.name in SPECIALISTS:
        return []
    return [
        build_team_middleware(
            profile,
            actor=actor,
            correlation_id=correlation_id,
            audit_sink=audit_sink,
            connectors=connectors,
            supervisor_tool_names=frozenset(
                str(getattr(t, "name", None) or getattr(t, "__name__", ""))
                for t in supervisor_tools
            ),
        )
    ]


def _challenge_middleware(
    profile: AgentProfile,
    actor: str,
    correlation_id: str | None,
    audit_sink: AuditSink | None,
    connectors: list[Any] | None,
) -> list[Any]:
    """The automatic challenge round, when this deployment runs one (`agent/challenge_gate.py`).

    Empty unless `challenge_enabled`, so the default agent is byte-identical to the one before the
    panel existed — the same promise `_team_middleware` keeps, for the same reason.

    **A challenger never gets a challenge gate of its own**, which is what stops the recursion, and
    the guard is the profile rather than a depth counter for `_team_middleware`'s reason: "a
    reviewer is not itself reviewed" is the rule, and a counter would only bound how far a broken
    version got. A specialist is excluded too — it reports to the supervisor, whose *assembled*
    answer is the thing worth challenging; putting a panel on each helper's intermediate report
    would pay N panels to review work no chemist will ever read.

    The turn's identity and sink go down so the panel audits under the same correlation id, and its
    connectors are passed for the challenger builds — narrowed by the challenger profile inside
    `build_langgraph_agent`, exactly as any other profile narrows them.
    """
    if not settings.challenge_enabled:
        return []
    if profile.name in SPECIALISTS or profile.name == CHALLENGER_PROFILE:
        return []
    return [
        build_challenge_gate(
            profile,
            actor=actor,
            correlation_id=correlation_id,
            audit_sink=audit_sink,
            connectors=connectors,
        )
    ]


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


def tool_governance_middleware(audit: Any, profile: AgentProfile) -> list[Any]:
    """What governs a tool call, outermost first — the MAF agent's order, ported not redesigned.

    The order is load-bearing and every position was argued for once already, so it is reproduced
    rather than re-derived:

    - audit outermost, so a denied or refused attempt is a recorded attempt;
    - authorization inside audit, then the dry-run and repeat gates beside it, for the same reason:
      each is a decision worth recording;
    - `announce_tool_failures` innermost, closest to the tool body, because it is the only one that
      must see the raw exception from *every* failure — including the ones the converters above
      this list turn into results — so the chemist's transcript shows the step that did not work
      (D-138).

    All of them are no-ops on the dev path: the sink is log-only, authz is open until
    `entra_required`, `is_dry_run()` is False off the request path, and the repeat counter is
    absent unless a turn started one.

    **Separate from the two model-facing converters, and the split is a decision.** They used to be
    one list, because the only caller was a chat turn and every tool call there is answered *to a
    model*. `agent/tool_invocation.py` is the caller that made the difference visible: a template
    step has no model, and handing it a refusal converted into prose is actively wrong — the
    refusal became the step's `${steps.<id>.result}`, so a `job` step launched the workflow it had
    just been denied, and a `tool` step interpolated "you are not authorized" into a later step as
    though it were an answer. Governance must raise for a caller that has no model to read it.
    """
    return [
        # Outside everything that *raises*, and inside both converters. Nesting is list order, so
        # a middleware below this one cannot be seen by it: `announce_tool_failures` used to sit
        # last — "innermost, closest to the tool body" — and that is exactly why a governance
        # refusal never reached the chemist. `enforce_plan_approval` raises *before* calling its
        # handler, so the announcer it wrapped never ran, and a gated call surfaced only as a
        # `tool_result` whose text begins "Refused:". A surface renders that as a step that
        # worked.
        #
        # Measured, because the opposite was written down and believed: `tests/test_m12_probes.py`
        # asserted that a plan refusal and a broken tool "arrive on the stream as the same event
        # type", and the live M12 plan-gate suite scored 0 refusals against a gate that had
        # refused twice in the same run — the gate held, and nothing could see it hold.
        # Innermost is the right place to catch a *tool body* raising and the wrong place to catch
        # the chain above it; the announcement belongs where every refusal passes.
        announce_tool_failures,
        audit,
        # Only for a profile that narrows, and inside `audit` so what an auditor reads is the
        # refusal rather than the library's guess at what went wrong. Before `enforce_tool_authz`
        # because it answers a coarser question — "was this agent even built with that tool" — and
        # asking whether the *user* may call something the agent does not hold would word the
        # refusal around the wrong subject.
        #
        # **It is the wording, never the enforcement**, and the enforcement is structural:
        # `tool_names` removes the tool from `_capability_tools` and from every connector's
        # allow-list before `create_agent` is called, and a compiled graph's `ToolNode` is built
        # from the list it was given. This repo has twice rejected filtering an advertised list
        # while leaving the capability reachable, and this is not a third time — with this
        # middleware deleted the call still cannot execute (measured), it just comes back as
        # LangGraph's `status="error"` "not a valid tool, try one of […]", which invites the retry
        # a refusal is worded to prevent and writes the whole tool inventory into the audit trail's
        # `detail`.
        *([refuse_undeclared_writes(profile.tool_names)] if profile.tool_names is not None else []),
        enforce_tool_authz,
        refuse_writes_on_dry_run,
        refuse_repeated_calls,
        *([enforce_plan_approval] if gate_applies(profile) else []),
    ]


def tool_call_middleware(audit: Any, profile: AgentProfile) -> list[Any]:
    """The governed chain plus the two converters that answer a *model*.

    The converters go outermost, so an exception still reaches audit unchanged and is recorded as
    an `error` outcome before either turns it into what the model reads. LangChain nests
    `wrap_tool_call` middleware in list order, so first here is outermost, exactly as MAF's list
    was read.
    """
    return [
        surface_authorization_denials,
        surface_domain_errors,
        *tool_governance_middleware(audit, profile),
    ]
