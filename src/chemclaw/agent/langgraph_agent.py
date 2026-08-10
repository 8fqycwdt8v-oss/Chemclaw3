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

What is deliberately *not* here yet, because nothing calls it: the extra state fields (they arrive
with the phase that reads them), the middleware chain (M3), skills (M4), the human gate (M5), the
checkpointer (M6) and the per-turn connector tools (M7). A stub advertising a capability this
engine does not have would read as coverage while proving nothing.
"""

from typing import Any

from langchain.agents import create_agent

# `_capability_tools` keeps its underscore deliberately. It is named in six merged ADRs (D-040,
# D-075, D-086 among them) and merged ADRs are never edited, so renaming it to mark this second
# caller would break every one of those citations to buy nothing — the same argument that freezes
# the `D-NNN` sequence. Three tests already import it across module boundaries; within one package
# that is the established idiom here.
from chemclaw.agent.chemclaw_agent import _capability_tools, instructions_for
from chemclaw.agent.llm_provider import build_chat_model
from chemclaw.agent.profiles import AgentProfile, get_profile


def build_langgraph_agent(
    model: Any | None = None,
    *,
    profile: str | AgentProfile | None = None,
) -> Any:
    """Compile the LangGraph conversation agent for one profile.

    Args:
        model: The LangChain chat model to run on. Injectable for the same reason
            `build_agent(chat_client=...)` is: the wiring must be assemblable and testable without
            live credentials. `None` builds the config-selected provider
            (`llm_provider.build_chat_model`).
        profile: The profile to narrow by (a name, an `AgentProfile`, or `None` for the default,
            which advertises the full in-process surface). Narrowing is attenuation only — the
            audit trail, the per-tool authorization gate and the skill role gates all run after
            this, so a profile can only hand back a smaller agent, never a wider one.

    Returns:
        A compiled graph. No network call happens here; construction only, exactly as
        `build_agent` promises.
    """
    prof = profile if isinstance(profile, AgentProfile) else get_profile(profile)
    return create_agent(
        model=model if model is not None else build_chat_model(),
        tools=list(_capability_tools(prof)),
        system_prompt=instructions_for(prof),
        name="chemclaw",
    )
