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

**The middleware chain is the same chain** (M3). Six `@wrap_tool_call` wrappers in the same nesting
order as the MAF agent's, over the *same* decision functions — `tool_authz.dry_run_refusal`,
`.denial_result`, `.domain_error_result`, `.failure_detail`, `repeat_guard.count_call`, and
`audit._recording`. Only the plumbing was ported; a second copy of any of those sentences would let
an authorization decision, a dry-run refusal or a GxP audit row depend on which engine a deployment
happens to run, which is the one drift this migration must be incapable of.

What is deliberately *not* here yet, because nothing calls it: the extra state fields (they arrive
with the phase that reads them), skills (M4), the human gate and the plan approval middleware (M5),
the checkpointer (M6) and the per-turn connector tools (M7). A stub advertising a capability this
engine does not have would read as coverage while proving nothing.
"""

import uuid
from typing import Any

from langchain.agents import create_agent

# `_capability_tools` keeps its underscore deliberately. It is named in six merged ADRs (D-040,
# D-075, D-086 among them) and merged ADRs are never edited, so renaming it to mark this second
# caller would break every one of those citations to buy nothing — the same argument that freezes
# the `D-NNN` sequence. Three tests already import it across module boundaries; within one package
# that is the established idiom here.
from chemclaw.agent.audit import AuditSink, make_langgraph_audit_middleware
from chemclaw.agent.chemclaw_agent import _capability_tools, instructions_for
from chemclaw.agent.llm_provider import build_chat_model
from chemclaw.agent.profiles import AgentProfile, get_profile
from chemclaw.agent.repeat_guard import lg_refuse_repeated_calls
from chemclaw.agent.tool_authz import (
    lg_announce_tool_failures,
    lg_enforce_tool_authz,
    lg_refuse_writes_on_dry_run,
    lg_surface_authorization_denials,
    lg_surface_domain_errors,
)


def build_langgraph_agent(
    model: Any | None = None,
    *,
    profile: str | AgentProfile | None = None,
    actor: str = "",
    correlation_id: str | None = None,
    audit_sink: AuditSink | None = None,
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

    Returns:
        A compiled graph. No network call happens here; construction only, exactly as
        `build_agent` promises.
    """
    prof = profile if isinstance(profile, AgentProfile) else get_profile(profile)
    audit = make_langgraph_audit_middleware(
        correlation_id=correlation_id if correlation_id is not None else uuid.uuid4().hex,
        actor=actor,
        sink=audit_sink,
    )
    return create_agent(
        model=model if model is not None else build_chat_model(),
        tools=list(_capability_tools(prof)),
        system_prompt=instructions_for(prof),
        middleware=_middleware(audit),
        name="chemclaw",
    )


def _middleware(audit: Any) -> list[Any]:
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

    The plan-approval gate is the seventh and arrives in M5, inserted before
    `lg_announce_tool_failures` to keep that one innermost.
    """
    return [
        lg_surface_authorization_denials,
        lg_surface_domain_errors,
        audit,
        lg_enforce_tool_authz,
        lg_refuse_writes_on_dry_run,
        lg_refuse_repeated_calls,
        lg_announce_tool_failures,
    ]
