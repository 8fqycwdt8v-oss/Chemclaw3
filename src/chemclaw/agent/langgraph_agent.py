"""The LangGraph conversation agent — layer 1 rebuilt (D-2026-08-10, phase M1).

`build_langgraph_agent` builds the compiled graph a turn runs on: the instructions, the in-process
capability tools, the per-task model route, the middleware chain, the skills and the human gates.
It was written as the twin of the previous framework's builder and lived behind a config switch
until it carried the whole suite; the switch and the other engine are gone (M13 Step 3), so this is
what a deployment gets. That builder is deliberately not named here: it no longer exists, and a
backticked pointer to a deleted symbol reads as a place to go and look.

**Named for the engine, not for "graph", and that is not fussiness.** In this codebase *the graph*
is the Markdown knowledge graph — layer 4, `kg/graph.py`, whose own `build_graph` builds a NetworkX
index of the notes. A second `build_graph`, in a module named for the graph, would put two
unrelated
`build_graph`s one import apart, in a tree whose `ARCHITECTURE.md` exists largely to explain the
name pairs that look like duplicates and are not. The engine's name is the unambiguous half.

**Why `create_deep_agent` rather than a hand-built `StateGraph`, or a hand-assembled middleware
list.** The decision to rebuild rather than port was about using the framework's own machinery
instead of re-implementing it, and `create_deep_agent` is that machinery two layers up: it wraps
`create_agent`, which *is* a `StateGraph` with the model/tool loop already wired and with the
middleware system (`wrap_tool_call`, `wrap_model_call`, `before_model`) that the audit trail, the
authorization gate and the plan approval hang off. Assembling those nodes by hand would reproduce
that loop and lose the hooks, which is the opposite of the decision.

The middle position — calling `create_agent` and composing deepagents' middleware by hand — is the
one this module held until the scratchpad arrived, and it is the one that stopped paying. Two of the
three capabilities the harness now wants are reachable *only* through `create_deep_agent`:
`permissions=` has no public seam on `FilesystemMiddleware`, and `subagents=` is what makes the
`task` tool's roster something this repository decides rather than inherits. Hand-assembly bought
control of the middleware order; `_apply_custom_middleware` gives that back by splicing on `.name`,
which is what `_middleware` below is arranged around and what `tests/test_middleware_order.py`
pins. Where Chemclaw genuinely adds a step of its own, it becomes a node in a graph that wraps this
one; it does not become a reason to build this one twice.

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
any of those sentences would let an authorization decision, a dry-run refusal or an audit row
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
from typing import Annotated, Any, NotRequired

from deepagents import create_deep_agent
from deepagents.backends import CompositeBackend, StateBackend
from deepagents.middleware.filesystem import FilesystemMiddleware
from deepagents.middleware.skills import SkillMetadata, SkillsMiddleware, SkillsState
from langchain.agents.middleware import TodoListMiddleware
from langchain.agents.middleware.types import PrivateStateAttr
from langgraph.channels.untracked_value import UntrackedValue

# `_capability_tools` keeps its underscore deliberately. It is named in six merged ADRs (D-040,
# D-075, D-086 among them) and merged ADRs are never edited, so renaming it to mark this second
# caller would break every one of those citations to buy nothing — the same argument that freezes
# the `D-NNN` sequence. Several callers outside this module already import it — five test modules
# and `durable/template_activities.py` — and within one package that is the established idiom here.
# (Unnumbered deliberately: this said "three tests", and it was six importers including a
# production one, which is what a count in a comment does.)
from chemclaw.agent.audit import AuditSink, make_audit_middleware
from chemclaw.agent.chemclaw_agent import (
    _advertised_names,
    _capability_tools,
    instructions_for,
)
from chemclaw.agent.compaction import context_compaction_middleware, disabled_summarizer
from chemclaw.agent.llm_provider import build_chat_model, prompt_caching_middleware
from chemclaw.agent.loop_cap import enforce_loop_cap
from chemclaw.agent.plan_gate import enforce_plan_approval, gate_applies, harness_enabled_for
from chemclaw.agent.profiles import AgentProfile, get_profile
from chemclaw.agent.repeat_guard import refuse_repeated_calls
from chemclaw.agent.scratchpad import (
    filesystem_permissions,
    scratchpad_backend,
    scratchpad_tools,
)
from chemclaw.agent.skill_access import skill_permits
from chemclaw.agent.skill_backend import NarrowedSkillsBackend
from chemclaw.agent.skill_manifest import declared_tools
from chemclaw.agent.state import ChemclawState
from chemclaw.agent.subagents import general_purpose_helper
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
    store: Any | None = None,
    helper: bool = False,
) -> Any:
    """Compile the LangGraph conversation agent for one profile.

    Args:
        model: The LangChain chat model to run on. Injectable for the same reason
            `build_langgraph_agent(chat_client=...)` is: the wiring must be assemblable and testable
            without live credentials. `None` builds the config-selected provider
            (`llm_provider.build_chat_model`).
        profile: The profile to narrow by (a name, an `AgentProfile`, or `None` for the default,
            which advertises the full in-process surface). Narrowing is attenuation only — the
            audit trail and the per-tool authorization gate are attached *after* it, so a profile
            attenuates capability and can never bypass either.
        actor: Fallback audit actor, used only when a turn stamps no ambient identity. Same
            precedence and same reason as `build_langgraph_agent`: an agent outlives a turn, so
            anything bound here would be shared by every user on the pod.
        correlation_id: Fallback correlation id, same precedence.
        audit_sink: The durable trail. `None` means `default_audit_sink()`; pass `NullAuditSink()`
            to opt out explicitly, never by forgetting.
        checkpointer: Where turn state is persisted between turns. `None` keeps state in the
            invocation. The durable one is `chemclaw.agent.checkpointer.checkpointer()`, which the
            caller supplies rather than this function building: it is an async factory that
            migrates on first use, and `build_langgraph_agent` is synchronous and resource-free by
            the same promise `build_langgraph_agent` makes.
        connectors: This turn's already-open connector tools
            (`chemclaw.connectors.registry.open_connector_specs`), or `None` for an agent with no
            out-of-process capability.
        store: This process's memory store (`agent/scratchpad.memory_store`), or `None` for a turn
            with no durable memory — which is every turn under the default configuration. A
            parameter rather than something built here for the reason `checkpointer` is one:
            creating it is `await`, and this builder is sync because all four of its callers are.
        response_format: A pydantic model the agent must finish by producing, surfaced on the
            returned state's `structured_response`. `None` — the conversational default — leaves the
            agent answering in prose. This exists for callers whose *whole* output is a datum rather
            than a reply, where letting the framework enforce the shape makes the failure mode "no
            structured answer" (handled) instead of "prose that almost parses" — the same reason
            `verify_answer` uses `with_structured_output` rather than reading a judge's free text.
            It has no caller today, and is kept because it is a passthrough to
            `create_deep_agent` and deliberately not a profile field: which shape an answer must
            take is a property of the *call*, not of the agent's capability.
        helper: Whether this graph *is* a helper rather than the agent a chemist talks to. The one
            thing it changes is that a helper gets no helpers of its own, which is the recursion
            guard: `_subagents` builds its spec by calling this function, so a graph that handed its
            helper a helper would not terminate. It is a parameter rather than a depth counter
            because one level is the whole design — `agent/subagents.py` says why the roster is one
            name and why a helper holds no connector tools — so a counter would be a knob for a
            depth nobody has asked for.

    Returns:
        A compiled graph. No network call happens here; construction only, exactly as
        `build_langgraph_agent` promises.

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
    # One walk of the skills trees per build, shared by the backend that routes them and the
    # middleware that labels them. They used to derive it independently — two `_skill_dirs()`
    # fan-outs (each a `Path.is_dir()` per enabled bundle) and two `_labelled()` passes per turn —
    # which also left "routes and sources cannot disagree" resting on the two calls happening to see
    # the same filesystem. Passing one value makes that structural, which is what `_skill_dirs`'s
    # own docstring says the single definition is for. It matters twice as much now: `_subagents`
    # compiles a second graph through this same function, so an un-shared walk is four.
    labelled = _labelled(_skill_dirs())
    skills = skills_backend(prof, tools, labelled=labelled)
    # The scratchpad wraps the skills routes rather than replacing them: the skills middleware and
    # the filesystem tools must read the *same* backend object, or the role narrowing computed for
    # one would not apply to the other.
    backend = scratchpad_backend(skills, store)
    chat_model = model if model is not None else build_chat_model()
    # The connectors are already narrowed by the profile and already open: `connector_specs` applies
    # `mcp_server_names`, the manifest allow-list bounds each surviving bundle, and
    # `open_connector_specs` returns only what a reachable server actually advertised. An
    # unreachable one contributes nothing here, which is the degradation the turn survives.
    bound = [*tools, *(connectors or [])]
    shared: dict[str, Any] = {
        "model": chat_model,
        "tools": bound,
        "system_prompt": instructions_for(prof),
        "state_schema": ChemclawState,
        "middleware": _middleware(prof, backend, audit, chat_model, labelled),
        "name": "chemclaw",
        "checkpointer": checkpointer,
        "response_format": response_format,
    }
    if helper:
        # **A helper is built by `create_agent`, and this branch is a correction the measurement
        # forced.** The first version routed both through `create_deep_agent` and let `_subagents`
        # return `[]` for a helper. That is not what "no helpers" means to upstream: with no spec
        # claiming the name, `create_deep_agent` auto-inserts its *own* general-purpose subagent —
        # so the recursion guard produced, one level down, exactly the ungoverned `task` surface it
        # exists to prevent. Compiling the helper on `create_agent` removes `SubAgentMiddleware`
        # outright, which is the only arrangement in which the absence is structural rather than
        # arranged.
        #
        # The two arguments it loses are the two `create_deep_agent` was adopted for, and a helper
        # wants neither. `subagents=` is the thing being denied. `permissions=` bounds where a write
        # may point, and a helper's backend has nowhere out of bounds to point at: it is built with
        # `store=None`, so there is no `/memories/` route, and what is left is the skills tree —
        # which `NarrowedSkillsBackend` refuses writes to on every call — over a `StateBackend` that
        # is this helper's own graph state. The bound is the same bound, arrived at by construction.
        from langchain.agents import create_agent

        return create_agent(**shared)
    return create_deep_agent(
        backend=backend,
        # `skills=` is deliberately absent: it is what would make upstream compose a second skills
        # middleware beside `ReloadingSkillsMiddleware`. `_skills_middleware` says why one is right.
        permissions=filesystem_permissions(),
        subagents=_subagents(prof, chat_model, audit_sink, correlation_id, actor),
        **shared,
    )


def _middleware(
    profile: AgentProfile,
    backend: CompositeBackend,
    audit: Any,
    model: Any,
    labelled: list[tuple[str, str]],
) -> list[Any]:
    """What this repository adds to — and takes over from — upstream's assembled stack.

    **`_apply_custom_middleware` splices this list by `.name`, and both halves of that are used.**
    An entry whose name matches one upstream already composed *replaces it in place*, keeping
    upstream's position; an entry whose name is new lands immediately after the last core member,
    which is to say inside every middleware that registers a tool and outside the profile and
    prompt-caching tail. So this list is not a stack in itself — it is two instructions, and reading
    it as a sequence is the mistake `tests/test_middleware_order.py` exists to catch.

    Exactly one entry is a replacement, and it is the security-critical one.
    **`FilesystemMiddleware` is composed by upstream unconditionally**, over the same backend,
    registering all eight verbs —
    `execute` and `delete` included. The instance here carries `tools=scratchpad_tools()`, which is
    where those two are withheld, and sharing upstream's name is what makes it take upstream's place
    rather than sit beside it offering the withheld pair anyway. The other route to the same
    narrowing is `HarnessProfile.excluded_tools`, and it was rejected on measurement: a profile is
    resolved by the model's self-reported `provider:identifier`, and on a key miss it is *silently*
    not applied. A narrowing that fails open on a model swap is not a narrowing.

    Everything else is new, and lands as a block after the last tool-registering middleware. That
    position is the invariant, not the order within the block: `create_agent` nests `wrap_tool_call`
    in list order, so being *after* `FilesystemMiddleware` and `SubAgentMiddleware` is what makes a
    scratchpad write and a `task` spawn cross the audit row and the authorization gate. Being after
    them was previously arranged by putting them first in a hand-built list; it is now arranged by
    upstream's splice rule, which is why the rule is asserted rather than assumed.

    Args:
        profile: The profile this agent was narrowed by.
        backend: The turn's composite backend — the *same* object the skills gate was computed for.
        audit: The audit middleware from `make_audit_middleware`.
        model: The resolved chat model, needed only to construct the summarizer this list switches
            off — upstream's constructor demands one for a code path that cannot run.
        labelled: The one walk of the skills trees this build made, so the middleware labels exactly
            what the backend routed rather than walking them again.

    Returns:
        The list to hand `create_deep_agent(middleware=…)`.
    """
    return [
        *_harness_middleware(profile),
        FilesystemMiddleware(backend=backend, tools=list(scratchpad_tools())),
        # The second replacement, and the one that would otherwise have arrived by default rather
        # than by decision: `create_deep_agent` composes a summarizer unconditionally, and this
        # deployment has declined one since D-025 on indirect-prompt-injection grounds that the
        # deepagents variant answers only half of. `agent/compaction.py` carries the whole argument.
        disabled_summarizer(model, backend),
        _skills_middleware(backend, labelled),
        *tool_call_middleware(audit, profile),
        # Provider-specific, so which middleware this is — or that it is none — is decided in the F0
        # seam rather than here. When the provider is Anthropic this replaces upstream's own by
        # name; when it is not, upstream composed none and this contributes none, which is the same
        # answer arrived at from both directions.
        *prompt_caching_middleware(),
        # Unconditional, unlike the harness middleware above it: an unbounded thread is a property
        # of a session, not of the plan/execute mode, and the single-turn agent accumulates one just
        # as fast. Last, so the reduction sees everything the middleware above it added.
        *context_compaction_middleware(),
    ]


def _subagents(
    profile: AgentProfile,
    model: Any,
    audit_sink: AuditSink | None,
    correlation_id: str | None,
    actor: str,
) -> list[Any]:
    """The helpers this agent may spawn — one, compiled here so it carries this chain.

    **Not optional, and that is the reason this function exists.** `SubAgentMiddleware` is in
    upstream's `_REQUIRED_MIDDLEWARE` and `_apply_excluded_middleware` raises rather than let a
    profile strip it, so the `task` tool ships regardless. What is decidable is *what it reaches*:
    left alone, upstream inserts a `general-purpose` helper holding every tool this agent holds and
    none of this repository's middleware. Returning a spec that claims that name is what displaces
    it, and `agent/subagents.py` records why the name is the reliable suppression and the harness
    profile is not.

    Three things the helper does *not* inherit, each for its own reason:

    - **No connector tools**, which is a concurrency bound rather than a narrowing. A helper is
      concurrent with its caller by construction, and two concurrent turns over one MCP tool object
      deadlock — the measurement this module's own docstring gives as the reason a graph is compiled
      per turn at all. It is expressed by omitting `connectors=` below, and asserted against the two
      *compiled* graphs in `tests/test_subagents.py`, because under a one-name roster any build-time
      comparison of the caller's profile with the helper's would compare a value with itself.
    - **No checkpointer.** Upstream's contract is that a helper sees the prompt it was given and
      returns one report; a thread to resume would be a second conversation nobody addresses.
    - **No durable memory and no store.** `store=` is not forwarded, so the helper's backend has no
      `/memories/` route. A helper's scratch work lives in its own graph state and dies with it,
      which is what "returns one report" means when written down as a data path.
    - **No helpers.** The guard is that `build_langgraph_agent(helper=True)` compiles on
      `create_agent`, so `SubAgentMiddleware` is absent rather than merely unpopulated — see the
      branch there for why returning an empty roster was not enough.

    Args:
        profile: The caller's profile — the helper is built from the same one, so the tool
            narrowing, the skills gate and the plan gate are the caller's.
        model: The already-resolved chat model, shared rather than rebuilt: resolving it twice would
            double a provider handshake to produce the same object.
        audit_sink: The caller's trail, so a helper's tool calls land in the same place.
        correlation_id: The caller's correlation id, so the two halves of one turn are joinable.
        actor: The caller's fallback audit actor.

    Returns:
        The single-entry list to hand `create_deep_agent(subagents=…)`.
    """
    return [
        general_purpose_helper(
            build_langgraph_agent(
                model=model,
                profile=profile,
                actor=actor,
                correlation_id=correlation_id,
                audit_sink=audit_sink,
                helper=True,
            )
        )
    ]


class ReloadingSkillsState(SkillsState):
    """`SkillsState` with its cached listing moved to a channel the checkpointer cannot restore.

    One redeclared field, and it is the whole reloading mechanism. Upstream annotates
    `skills_metadata` with `PrivateStateAttr` only, so it resolves to a checkpointed `LastValue`;
    `UntrackedValue` is never written to a checkpoint (`checkpoint()` returns `MISSING`), so the
    key is absent at the start of every run of the graph and upstream's own
    `if "skills_metadata" in state: return None` short-circuit simply does not fire.

    Measured over three turns on one thread, counting whether the key was already in state when
    `before_agent` ran: `[False, True, True]` on upstream's channel and `[False, False, False]` on
    this one.

    The same mechanism `ChemclawState.loop_capped` uses, and for the same reason: "per turn" is a
    property of the channel, not of a caller who remembers to clear it.
    """

    skills_metadata: NotRequired[Annotated[list[SkillMetadata], UntrackedValue, PrivateStateAttr]]


class ReloadingSkillsMiddleware(SkillsMiddleware):
    """`SkillsMiddleware` that re-narrows its listing every turn instead of caching it.

    Upstream loads skills once and then skips the load whenever `skills_metadata` is already in
    state — "from a prior turn or checkpointed session", as its own docstring says. That is a sound
    cache for a fixed skills tree and wrong for a narrowed one: the role gate reads the turn's
    ambient identity, so a listing computed for one caller would be served to the next, and a
    mid-session role change would keep advertising skills the caller no longer holds.

    **The whole subclass is one state field, and that is the point.** Two earlier versions were
    hooks. The first cleared the slot from `before_agent` and was worse than the bug — a state
    update of `{"skills_metadata": None}` leaves the *key* present, so the check still
    short-circuited and the prompt rendered an empty list (measured: 28 skills on turn one, 0 on
    every turn after). The second overrode `before_agent`/`abefore_agent` to hide the key from the
    state upstream reads, which worked and bound this file to the *arity* LangChain invokes the
    hook with — it had to default a third argument because the framework passed two where
    deepagents' own signature declared three. That is a dependency on somebody else's calling
    convention, and it is exactly the kind that breaks on a bump without failing loudly.
    Redeclaring the channel depends only on the field's name, which `tests/test_upstream_surface.py`
    pins.

    **This is a staleness fix, not the gate.** `NarrowedSkillsBackend` refuses the *read* on every
    call regardless, so a stale listing could at worst advertise a skill whose body then came back
    refused. Fixing the listing keeps the two consistent, which is what a caller reading "these are
    your skills" is entitled to.
    """

    state_schema = ReloadingSkillsState


def _harness_middleware(profile: AgentProfile) -> list[Any]:
    """The plan/execute harness: the todo list the plan gate reads, and the runaway cap.

    Both conditional on `harness_enabled_for`, matching MAF: the classic agent has no todo list and
    no loop cap, so attaching either unconditionally would make this engine behave differently from
    the other while both are live — a safer difference, but a difference.

    `enforce_loop_cap` both enforces the cap and records it, and `loop_cap.loop_capped` reads that
    record. One counter for one number — and it counts in `before_model` deliberately: see
    `agent/loop_cap.py` for the four regressions that delegating it to `ModelCallLimitMiddleware`
    produced, the first of which is that an `after_model` counter is skippable by a jump.
    """
    if not harness_enabled_for(profile):
        return []
    return [TodoListMiddleware(), enforce_loop_cap]


def _skills_middleware(backend: CompositeBackend, labelled: list[tuple[str, str]]) -> Any:
    """Wrap a narrowed backend in deepagents' provider — the plumbing around the decision.

    Private, and split from `skills_backend` for the reason `chemclaw_agent.skills_source` is split
    from `_build_skills`: the backend is the part with behaviour and the middleware is somebody
    else's object around it, exposing no reader for the backend it was given. Tests assert against
    `skills_backend`, so this half needs no name outside the module.

    Takes the backend rather than building one, because the caller has to hold it anyway — the
    skills read tool is bound to the same instance, and two backends built from one config would be
    two objects that merely happen to agree. `labelled` arrives for the same reason: the backend was
    routed from it, and re-deriving it here is a second walk of the trees that could disagree.

    **`create_deep_agent(skills=…)` is deliberately not passed**, and this is the middleware that
    would have collided with it. Upstream composes its own `SkillsMiddleware` only when that
    argument is given, so withholding it leaves exactly one skills middleware — this one — with no
    name-splice to arrange and no second listing to keep in step. The alternative (pass the sources,
    override `.name` to `"SkillsMiddleware"`, let `_apply_custom_middleware` replace upstream's in
    place) also works and was written first; it buys only a different index in the list, at the cost
    of declaring the source trees twice and depending on a splice rule for a middleware that
    registers no tools. `FilesystemMiddleware` is the opposite case and does need the splice —
    upstream composes one unconditionally — which is why `_middleware` explains the rule there.
    """
    return ReloadingSkillsMiddleware(
        backend=backend, sources=[(f"/{label}", label) for label, _ in labelled]
    )


def skills_backend(
    profile: AgentProfile, tools: list[Any], *, labelled: list[tuple[str, str]] | None = None
) -> CompositeBackend:
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

    `labelled` is the caller's already-walked `(label, directory)` list, so one build walks the
    trees once; omitting it walks them here, which is what a test building a backend alone wants.
    """
    labelled = labelled if labelled is not None else _labelled(_skill_dirs())
    dirs = [directory for _label, directory in labelled]
    permits = skill_permits(
        enabled=settings.skills_enabled_list,
        declared=declared_tools(dirs),
        available=_advertised_names(profile, tools),
        gates=settings.skill_role_gates,
    )
    return CompositeBackend(
        default=StateBackend(),
        routes={
            f"/{label}/": NarrowedSkillsBackend(directory, permits) for label, directory in labelled
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
    a later one wins, matching the first-wins rule the previous framework's file source applied once
    the list is read the way each library reads it.
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
    - `announce_tool_failures` **first in this list — outermost within the group, inside both
      converters** — because it is the only one that must see *every* failure, including a refusal
      raised by a gate below it, so the chemist's transcript shows the step that did not work
      (D-138). This bullet used to say "innermost, closest to the tool body", which is where the
      announcer sat when a plan-gate refusal was measured reaching nobody: `enforce_plan_approval`
      raises *before* calling its handler, so an announcer it wrapped never ran. The inline comment
      on the entry below carries the measurement; a reader following the old bullet would restore
      the defect.

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
