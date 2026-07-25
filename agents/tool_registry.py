"""In-process capability-tool registry — the extension seam for the agent's function tools.

Why this exists: adding an agent tool used to mean editing a hardcoded list inside
`agents.chemclaw_agent._capability_tools` — the *one* extension seam that forced an edit to
orchestration code. Every other capability declares itself at its definition site and is
discovered by name (skills by folder, MCP servers + data sources by config token, metrics by
`@metric`). This module gives function tools the same locality: a tool decorates itself with
`@tool` where it is defined, and `build_agent` assembles the advertised set from the registry.

The shape deliberately mirrors `evals.metric` (`_REGISTRY` + decorator + duplicate-name guard),
so no new pattern is introduced. It holds only the in-process function tools; the MCP capability
servers stay config-driven (`settings.mcp_servers`), and the two shared middlewares (GxP audit +
per-tool authz) still wrap every tool uniformly in `build_agent`. The registry changes *how tools
are collected*, never how they are gated — the safety rubric is untouched.

Registration happens on import, so the caller that assembles the toolset imports the tool-bearing
modules for their side effect (exactly as `evals/__init__.py` seeds the metric registry).
"""

from collections.abc import Callable
from typing import Any, TypeVar

# A capability tool is any callable MAF can advertise; MAF derives its schema from the signature
# and docstring, so the registry stores the function unchanged (the decorator is identity).
CapabilityTool = Callable[..., Any]
_ToolT = TypeVar("_ToolT", bound=CapabilityTool)

# Insertion order == advertisement order; a dict preserves it (the list this replaces was ordered).
_REGISTRY: dict[str, CapabilityTool] = {}


def register_tool(fn: CapabilityTool) -> None:
    """Register one in-process capability tool under its function name.

    The key is `fn.__name__` because that is exactly the name MAF advertises to the model —
    deriving it here rather than passing it separately removes a whole drift class (see the
    name-drift guard in `tests/test_agent.py`). A duplicate name is a programming error, as in
    `evals.metric.register`.
    """
    name = fn.__name__
    if name in _REGISTRY:
        raise ValueError(f"capability tool {name!r} already registered")
    _REGISTRY[name] = fn


def tool(fn: _ToolT) -> _ToolT:
    """Decorator form of `register_tool` — the idiom a tool uses at its definition site.

    Returns the function unchanged so the decorated object is exactly what MAF wraps as a tool.
    """
    register_tool(fn)
    return fn


def registered_tools() -> list[CapabilityTool]:
    """Every registered in-process capability tool, in registration order."""
    return list(_REGISTRY.values())


def registered_tool_names() -> list[str]:
    """The names of all registered capability tools, sorted (for tests and validation)."""
    return sorted(_REGISTRY)
