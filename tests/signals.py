"""Capture the out-of-band signals a piece of code publishes, by running it inside a real graph.

`chemclaw.core.turn_signals` publishes through `get_stream_writer()`, which resolves the writer off
LangGraph's ambient runnable config. There is no buffer to inspect any more — a test that wants to
know what a tool announced has to be somewhere a writer exists.

**A real one-node graph rather than a patched `get_stream_writer`.** Patching would make every test
here pass against a `record_*` that never reached a writer at all, which is the failure the port
could actually introduce: the publish call is guarded (`RuntimeError` → drop) precisely so a tool
can run in a Temporal activity, and a guard that swallows everything looks identical to one that
swallows nothing. Driving a real graph proves the writer resolves where a tool actually runs, which
is the claim the whole mechanism rests on, and it costs one `StateGraph` per call.
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

    Returns the result too, because most callers assert on both — what the tool returned to the
    model *and* what it announced to the chemist are two different halves of the same contract, and
    the point of several of these tests is that they disagree (a job id goes to the model, a
    `JobStartedEvent` goes to the surface).
    """
    captured: list[Any] = []

    # `state`/`config` by name, not `_state`/`_config`: LangGraph types a node as a Protocol whose
    # `__call__` declares those parameter names, and a Protocol match is name-sensitive for
    # positional-or-keyword parameters — so the conventional underscore prefix for an unused
    # argument makes the callable stop matching and `add_node` reports no overload.
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
