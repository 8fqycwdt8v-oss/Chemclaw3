"""One `StructuredTool` per capability function, derived once per process rather than per turn.

The graph is compiled per turn (a connector session belongs to one turn), and `ToolNode` converts
every plain callable it is handed into a `BaseTool` — most of the compile cost. A first-party
tool's schema depends only on its signature and docstring, fixed at import, so it is converted once
and shared; `ToolNode` stores a `BaseTool` unchanged. Connector tools are per-turn `BaseTool`s and
pass straight through.
"""

from collections.abc import Callable
from functools import cache
from typing import Any

from langchain_core.tools import BaseTool
from langchain_core.tools import tool as create_tool


@cache
def as_structured_tool(fn: Callable[..., Any]) -> BaseTool:
    """Convert one capability function to its `BaseTool`, once per process.

    Keyed on the function object; callers pass registry functions, so the cache holds one entry per
    registered tool and cannot grow with turns.

    Args:
        fn: A registered in-process capability tool (a plain callable, not a `BaseTool`).

    Returns:
        The `BaseTool` `ToolNode` would otherwise build for it on every compile.
    """
    return create_tool(fn)
