"""What one profile advertises, without building an agent to find out.

A profile's tools, instructions and connectors are three first-party functions, so tests assert
on them directly rather than on a compiled graph's internals (which would also need a model
credential).
"""

from dataclasses import dataclass
from typing import Any

from chemclaw.agent.chemclaw_agent import _capability_tools, connector_specs, instructions_for
from chemclaw.agent.profiles import AgentProfile, get_profile


@dataclass(frozen=True, slots=True)
class Surface:
    """One profile's advertised surface: its instructions, its tools, and its connectors."""

    instructions: str
    tool_names: frozenset[str]
    connectors: list[Any]


def surface(profile: str | AgentProfile | None = None) -> Surface:
    """Resolve `profile` and report what it advertises.

    Args:
        profile: A profile name, an `AgentProfile`, or `None` for the default — resolved as
            `build_langgraph_agent` resolves it.
    """
    resolved = profile if isinstance(profile, AgentProfile) else get_profile(profile)
    # Names, not objects. A capability tool is a plain `@tool`-registered function until
    # `create_agent` wraps it, so it carries `__name__` and not `.name` — and every caller here is
    # asking *which* tools, never about the objects.
    return Surface(
        instructions=instructions_for(resolved),
        tool_names=frozenset(tool.__name__ for tool in _capability_tools(resolved)),
        connectors=connector_specs(resolved),
    )
