"""In-process capability-tool registry — the extension seam for the agent's function tools.

A tool registers itself with `@tool` where it is defined, and `build_langgraph_agent` assembles the
advertised set from the registry, so adding a tool never edits orchestration code. Mirrors
`chemclaw.evals.metric` (registry + decorator + duplicate-name guard). It changes how tools are
collected, never how they are gated: audit and authorization middlewares wrap every tool uniformly.
Registration happens on import, so the assembler imports tool-bearing modules for their side effect.
In `core` because its users span `agent`, `connectors` and `templates`, and it imports nothing but
typing.
"""

from collections.abc import Callable
from typing import Any, TypeVar

# Any callable the agent can advertise; the framework derives its schema from the signature and
# docstring, so the decorator is identity.
CapabilityTool = Callable[..., Any]
_ToolT = TypeVar("_ToolT", bound=CapabilityTool)

# Insertion order == advertisement order; a dict preserves it (the list this replaces was ordered).
_REGISTRY: dict[str, CapabilityTool] = {}


def register_tool(fn: CapabilityTool) -> None:
    """Register one in-process capability tool under its function name.

    Keyed by `fn.__name__`, the name advertised to the model, so the two cannot drift. A duplicate
    is a programming error.
    """
    name = fn.__name__
    if name in _REGISTRY:
        raise ValueError(f"capability tool {name!r} already registered")
    _REGISTRY[name] = fn


def tool(fn: _ToolT) -> _ToolT:
    """Decorator form of `register_tool` — the idiom a tool uses at its definition site.

    Returns the function unchanged.
    """
    register_tool(fn)
    return fn


def registered_tools() -> list[CapabilityTool]:
    """Every registered in-process capability tool, in registration order."""
    return list(_REGISTRY.values())


def registered_tool_names() -> list[str]:
    """The names of all registered capability tools, sorted (for tests and validation)."""
    return sorted(_REGISTRY)


# Names of tools whose answer to identical arguments legitimately changes within one turn, declared
# at the definition site (`@polls_moving_state`) so consumers ask rather than keep their own lists.
_POLLS: set[str] = set()


def polls_moving_state(fn: _ToolT) -> _ToolT:
    """Declare that this tool *reads something that moves*, so asking again is not a repeat.

    A status poll answers `running`, then `completed`, to identical arguments, so
    `agent/repeat_guard.py` must not refuse it. Identity, so it composes with `@tool` in either
    order.
    """
    _POLLS.add(fn.__name__)
    return fn


def is_a_poll(name: str) -> bool:
    """Whether the tool registered under `name` declared `@polls_moving_state`.

    An unregistered (hallucinated or injected) name is never in the set, so it cannot claim the
    exemption.
    """
    return name in _POLLS
