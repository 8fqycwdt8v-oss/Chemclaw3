"""Capture the out-of-band signals a piece of code publishes, by running it inside a real graph.

`chemclaw.core.turn_signals` publishes through `get_stream_writer()`, resolved from LangGraph's
ambient config. A real one-node graph, not a patched writer, proves the writer resolves where a
tool runs; the publish call swallows `RuntimeError`, so a patch could hide a signal that never
reached a writer.
"""

from collections.abc import Awaitable, Callable
from typing import Any, TypedDict

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph

from chemclaw.core.turn_signals import _KEY, Signal


class _State(TypedDict):
    """The node has no state to carry; the graph exists only to supply a runtime."""

    done: bool


async def collect_signals(body: Callable[[], Awaitable[Any]]) -> tuple[Any, list[Signal]]:
    """Run `body` inside a graph node and return `(its result, the signals it published)`.

    Both, because what a tool returns to the model and what it announces to the chemist are two
    halves of one contract and often differ (a job id vs. a `JobStartedEvent`).
    """
    captured: list[Any] = []

    # `state`/`config` by name: LangGraph's node Protocol is name-sensitive, so underscore-prefixed
    # names stop `add_node` from matching an overload.
    async def _node(state: _State, config: RunnableConfig) -> dict[str, Any]:
        captured.append(await body())
        return {"done": True}

    graph = StateGraph(_State)
    graph.add_node("body", _node)
    graph.add_edge(START, "body")
    graph.add_edge("body", END)
    compiled = graph.compile()

    signals: list[Signal] = []
    async for payload in compiled.astream({"done": False}, stream_mode="custom"):
        if isinstance(payload, dict) and isinstance(payload.get(_KEY), Signal):
            signals.append(payload[_KEY])
    return (captured[0] if captured else None), signals
