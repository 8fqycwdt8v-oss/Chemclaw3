"""The LangGraph engine builds and runs — layer 1's rebuild, phase M1 (D-2026-08-10).

These prove the claims `agent/langgraph_agent.py` makes and nothing it does not yet make. The
engine is selected by `settings.agent_engine`, lands phase by phase, and this file grows with it;
asserting here on middleware, skills or gates that M3–M5 have not built would be a test of a plan
rather than of the code.

The claims under test:

1. **The graph compiles and completes a tool-using turn.** Driven by a scripted fake model rather
   than a live one, so the assertion is about the wiring — the model is offered Chemclaw's tools,
   its tool call is executed, and its result comes back into the conversation — and not about model
   behaviour. That is the same bargain `tests/test_agent.py` strikes for the MAF path.
2. **The in-process capability surface transfers unchanged.** `core/tool_registry` holds plain
   callables, so the same functions the MAF agent advertises reach the model here with no adapter
   and no second declaration. If that ever stops being true, every tool would need a LangGraph
   twin, which is the cost the D-118/R2 seam was built to avoid — so it is worth a test rather
   than an assumption.
"""

import asyncio
from typing import Any

from langchain_core.language_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, ToolMessage

from chemclaw.agent.chemclaw_agent import _capability_tools
from chemclaw.agent.langgraph_agent import build_langgraph_agent
from chemclaw.agent.profiles import AgentProfile
from chemclaw.core.tool_registry import registered_tool_names


class _ScriptedModel(GenericFakeChatModel):
    """A model that replays a fixed script, and accepts tool binding without honouring it.

    Subclassed because `create_agent`'s model node calls `.bind_tools(...)` on every request and
    `GenericFakeChatModel.bind_tools` raises `NotImplementedError` — measured, not assumed. Binding
    returns `self` here: the script already contains the tool call under test, so the point of the
    override is that the graph gets a model it can bind, not that the fake reasons about tools.

    What that costs is worth naming. This proves the *loop* — that a tool call is dispatched, run
    and fed back — and cannot prove that the tool schemas Chemclaw hands over are ones a real model
    can call. `test_every_in_process_tool_reaches_the_graph_unchanged` covers the surface, and only
    a live run covers the schemas; M12's re-validation is where that happens.
    """

    def bind_tools(self, tools: Any, **kwargs: Any) -> Any:
        """Accept the binding and keep replaying the script."""
        return self


def _scripted(tool_name: str, tool_args: dict[str, Any]) -> Any:
    """A fake model that calls `tool_name` once and then produces a final answer."""
    return _ScriptedModel(
        messages=iter(
            [
                AIMessage(
                    content="",
                    tool_calls=[{"name": tool_name, "args": tool_args, "id": "call-1"}],
                ),
                AIMessage(content="done"),
            ]
        )
    )


def test_the_graph_runs_a_tool_call_to_a_final_answer() -> None:
    """A scripted tool call is executed and its result rejoins the conversation.

    `ask_clarifying_question` is the tool under test because it is in-process, side-effect free and
    needs no database, connector or credential — so this exercises the loop rather than a
    capability. What is asserted is the shape of a completed turn: the model's tool call, a
    `ToolMessage` carrying the result, and a final answer after it.

    Driven through `ainvoke`, and that is a property of the surface rather than a test style:
    every Chemclaw capability tool is `async def`, so LangChain wraps each as a `StructuredTool`
    with no sync path and `graph.invoke` raises. The production path is async on both engines, so
    the async-only surface is the honest one to exercise.
    """
    graph = build_langgraph_agent(
        model=_scripted("ask_clarifying_question", {"question": "which solvent?"})
    )

    result = asyncio.run(graph.ainvoke({"messages": [("user", "help")]}))

    messages = result["messages"]
    assert any(isinstance(m, ToolMessage) for m in messages), (
        f"the tool call was never executed; got {[type(m).__name__ for m in messages]}"
    )
    assert messages[-1].content == "done"


def test_every_in_process_tool_reaches_the_graph_unchanged() -> None:
    """The graph advertises exactly the registry, so no tool needs a LangGraph-specific twin.

    Compared against `_capability_tools` rather than against a written list, because that function
    is what the MAF agent advertises: the property worth pinning is that the two engines offer the
    *same* surface, not that this one offers some particular set. A tool that reached one engine
    and not the other would be a capability that appears or vanishes with a config flag.
    """
    graph = build_langgraph_agent(model=_scripted("ask_clarifying_question", {"question": "x"}))

    advertised = {t.name for t in graph.nodes["tools"].bound.tools_by_name.values()}
    assert advertised == {tool.__name__ for tool in _capability_tools()}
    assert advertised == set(registered_tool_names())


def test_a_profile_narrows_the_graph_surface() -> None:
    """A profile attenuates this engine exactly as it attenuates the MAF one.

    Built from an explicit `AgentProfile` rather than a discovered name, because discovery reads
    `data/profiles/` plus every enabled connector bundle and would make this a test of what the
    repository currently ships. The property is about the dial, not about the shipped set:
    `tool_names` narrows, and narrowing is strict.

    `ask_clarifying_question` is the one in-process tool named here because the shipped
    `property-lookup` profile's other four live in the `calc` connector, and connector tools do not
    reach this engine until M7.
    """
    kept = "ask_clarifying_question"
    full = build_langgraph_agent(model=_scripted(kept, {"question": "x"}))
    narrowed = build_langgraph_agent(
        model=_scripted(kept, {"question": "x"}),
        profile=AgentProfile(name="narrow", tool_names=frozenset({kept})),
    )

    def names(graph: Any) -> set[str]:
        return {t.name for t in graph.nodes["tools"].bound.tools_by_name.values()}

    assert names(narrowed) == {kept}
    assert names(narrowed) < names(full), "a profile must attenuate, never widen or match"
