"""A turn that commits each step before the next must still run the graphs it starts inside it.

`api/graph_stream.TURN_DURABILITY` is written into the run's config, and a graph invoked from
inside a tool or as a node reads it from there. Graphs compiled with `checkpointer=False` have no
write to wait for, so each case here runs a real turn through `graph_events` against a checkpointer
and asserts that the nested graph's result arrives.
"""

import asyncio
from typing import Any

import pytest
from langchain_core.runnables.config import ensure_config
from langchain_core.tools import StructuredTool
from langgraph.checkpoint.memory import InMemorySaver

from chemclaw.agent.audit import NullAuditSink
from chemclaw.agent.handoff import handoff_tool_name
from chemclaw.agent.langgraph_agent import build_langgraph_agent
from chemclaw.agent.profiles import AgentProfile
from chemclaw.agent.state import turn_config
from chemclaw.api.events import EvidenceSourceEvent, TokenEvent, ToolFailedEvent, ToolResultEvent
from chemclaw.api.graph_stream import graph_events
from chemclaw.api.runner_trace import ToolCallTrace
from chemclaw.core.graph_durability import ignoring_inherited_durability
from chemclaw.retrieval.fanout import sweep_sources
from tests.fakes_langgraph import ScriptedChatModel
from tests.test_audit import _RecordingSink
from tests.test_evidence_fanout import _Retriever
from tests.test_turn_graph import _mesh


class _Usage:
    def add(self, _usage: Any) -> None:
        """The ledger's shape; nothing here asserts on tokens."""


async def _events(graph: Any, thread: str) -> list[Any]:
    return [
        event
        async for event in graph_events(
            graph,
            "go",
            config=turn_config(thread),
            trace=ToolCallTrace(),
            on_signal=lambda _s: None,
            usage=_Usage(),
        )
    ]


def test_the_evidence_fan_out_runs_inside_a_turn_that_has_a_checkpointer() -> None:
    """The fan-out's branch reports arrive from a turn on a checkpointer."""

    async def sweep(query: str) -> str:
        """Stand in for `gather_evidence`: the same fan-out, sources that need no database."""
        legs = (_Retriever("graph", 4), _Retriever("lexical", 0))
        lists, _failed, _skipped = await sweep_sources([(s.name, s) for s in legs], query, {})
        return f"{sum(len(chunks) for chunks in lists)} chunks"

    graph = build_langgraph_agent(
        ScriptedChatModel([{"name": "sweep", "args": {"query": "q"}}, "done"]),
        audit_sink=NullAuditSink(),
        connectors=[
            StructuredTool.from_function(coroutine=sweep, name="sweep", description="sweep")
        ],
        checkpointer=InMemorySaver(),
    )

    events = asyncio.run(_events(graph, "t-fanout"))

    assert not [e for e in events if isinstance(e, ToolFailedEvent)], events
    assert {e.source for e in events if isinstance(e, EvidenceSourceEvent)} == {"graph", "lexical"}


def test_a_helper_runs_inside_a_turn_that_has_a_checkpointer() -> None:
    """A `task` helper makes its call and reports back, on a turn with a checkpointer."""
    sink = _RecordingSink()
    graph = build_langgraph_agent(
        model=ScriptedChatModel(
            [
                {
                    "name": "task",
                    "args": {"description": "sweep", "subagent_type": "general-purpose"},
                },
                {"name": "ls", "args": {"path": "/"}},
                "nothing found",
                "done",
            ]
        ),
        profile=AgentProfile(name="default"),
        actor="alice@corp",
        audit_sink=sink,
        checkpointer=InMemorySaver(),
    )

    events = asyncio.run(_events(graph, "t-helper"))

    assert not [e for e in events if isinstance(e, ToolFailedEvent)], events
    assert {event.tool: event.agent for event in sink.events}.get("ls") == "default-helper"
    results = [e for e in events if isinstance(e, ToolResultEvent) and e.tool == "task"]
    assert results and "nothing found" in results[0].preview, events


def test_a_peer_runs_inside_a_turn_that_has_a_checkpointer(monkeypatch: pytest.MonkeyPatch) -> None:
    """A peer handed the conversation answers it, on a turn with a checkpointer."""
    graph = _mesh(
        monkeypatch,
        {
            "default": [{"name": handoff_tool_name("safety-peer"), "args": {"reason": "hazards"}}],
            "evidence-peer": ["unused"],
            "safety-peer": ["No alerts."],
        },
        checkpointer=InMemorySaver(),
    )

    events = asyncio.run(_events(graph, "t-peer"))

    answer = "".join(e.text for e in events if isinstance(e, TokenEvent) and not e.agent)
    assert "No alerts." in answer, events


def _tiny(checkpointer: Any) -> Any:
    from langgraph.graph import END, START, StateGraph
    from typing_extensions import TypedDict

    class _State(TypedDict):
        n: int

    graph = StateGraph(_State)
    graph.add_node("bump", lambda state: {"n": state["n"] + 1})
    graph.add_edge(START, "bump")
    graph.add_edge("bump", END)
    return graph.compile(checkpointer=checkpointer)


def test_a_graph_without_a_checkpointer_runs_under_a_sync_caller() -> None:
    """Control: the same graph unwrapped fails, so the guard is what makes it run."""
    config = {"configurable": {"thread_id": "t"}}

    async def run(graph: Any) -> Any:
        return await graph.ainvoke({"n": 1}, config, durability="sync")

    with pytest.raises(AttributeError, match="_put_checkpoint_fut"):
        asyncio.run(run(_tiny(False)))
    assert asyncio.run(run(ignoring_inherited_durability(_tiny(False)))) == {"n": 2}


def test_a_graph_with_a_checkpointer_keeps_the_durability_it_is_asked_for() -> None:
    """The guard changes nothing for a graph that has a saver."""
    graph = ignoring_inherited_durability(_tiny(InMemorySaver()))
    config = ensure_config({"configurable": {"thread_id": "t"}})

    resolved = graph._defaults(
        config,
        stream_mode="values",
        print_mode=(),
        output_keys=None,
        interrupt_before=None,
        interrupt_after=None,
        durability="sync",
    )

    assert resolved[-1] == "sync"
    assert ignoring_inherited_durability(graph) is graph
