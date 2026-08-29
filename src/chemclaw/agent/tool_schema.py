"""One `StructuredTool` per capability function, derived once per process rather than per turn.

**Why this exists: the graph is compiled per turn, and the schema derivation is not per-turn work.**
`ToolNode.__init__` calls `langchain_core.tools.tool` on every plain callable it is handed, which
builds a pydantic model from the function's signature and docstring. That is ~2 ms per tool, and
`build_langgraph_agent` hands it the whole in-process registry — measured at **108 conversions per
build** across the parent graph and the helper `_subagents` compiles beside it, which came to
roughly **four fifths of the compile**.

Compiling per turn is not negotiable (`build_langgraph_agent` says why: a connector session belongs
to exactly one turn), but re-deriving these schemas is, because **a first-party tool's schema cannot
vary between turns**. It is a function of the callable's signature and docstring, both fixed at
import. The per-turn thing is the *connector* tools, which are already `BaseTool` instances built
from that turn's own session and are deliberately not cached here — they are passed straight
through, exactly as before.

`ToolNode` stores a `BaseTool` it is given without touching it (`tool_node.py`: `if not
isinstance(tool, BaseTool): tool_ = create_tool(...)` and then `self._tools_by_name[tool_.name] =
tool_`), so handing it the converted object is the same object it would have built, and the sharing
is what a process-lived agent did before the engine changed. The wrapper holds a reference to the
function and its schema and nothing about a call, so two concurrent turns share it the way they
share the function itself.
"""

from collections.abc import Callable
from functools import cache
from typing import Any

from langchain_core.tools import BaseTool
from langchain_core.tools import tool as create_tool


@cache
def as_structured_tool(fn: Callable[..., Any]) -> BaseTool:
    """Convert one capability function to its `BaseTool`, once per process.

    Keyed on the function object itself. Every caller passes a module-level function out of
    `chemclaw.core.tool_registry`, so the cache holds one entry per registered tool for the life of
    the process and cannot grow with turns.

    Args:
        fn: A registered in-process capability tool (a plain callable, not a `BaseTool`).

    Returns:
        The `BaseTool` `ToolNode` would otherwise build for it on every compile.
    """
    return create_tool(fn)
