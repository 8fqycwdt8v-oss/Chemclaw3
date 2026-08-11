"""What one profile advertises, without building an agent to find out.

Every test that asked "does this profile narrow the tools / swap the instructions / drop that
connector" used to build a whole MAF `Agent` behind a `chat_client=object()` stand-in and read
`agent.default_options["tools"]`. It worked, and it made the assertion about a framework object's
internal keys rather than about the decision.

The three answers are three first-party functions, so this is what a profile *is*. Building a
compiled graph to inspect the same three would be the same mistake in a new framework — and would
need a model credential, which is exactly what the stand-in client existed to dodge.
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
        profile: A profile name, an `AgentProfile`, or `None` for the default — the same three
            forms `build_langgraph_agent` accepts, resolved the same way, so a test cannot be
            asking about a profile the agent would resolve differently.
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
