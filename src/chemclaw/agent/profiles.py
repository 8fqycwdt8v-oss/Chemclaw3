"""Named agent profiles — the seam for per-use-case agent configuration.

A profile is an override set over `build_langgraph_agent`'s existing dimensions (instructions, tool
and MCP subsets, harness mode, model route, skills). Every field defaults to `None`, meaning "use
the global default", so this module imports neither the agent nor `settings`, and
`AgentProfile(name="default")` is today's agent.

A profile attenuates and never authorizes: the subsets only narrow the advertised surface, and
audit, authz and skill role gates run after the narrowing. Profiles are YAML files
(`chemclaw.agent.profile_discovery`); this module holds the model and the `{name: profile}`
registry.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from chemclaw.core.config.agent import HarnessAutonomy
from chemclaw.core.manifest_io import MAX_MANIFEST_TEXT_CHARS


class AgentProfile(BaseModel):
    """Override-bundle over `build_langgraph_agent`'s dimensions; unset fields fall back to global.

    `instructions` swaps the system prompt; `tool_names` / `mcp_server_names` *narrow* the
    advertised in-process tools / MCP capability servers to the named subset (a name absent from
    the built surface is a loud error in `build_langgraph_agent`, not a silent drop);
    `harness_enabled` / `harness_autonomy` override the harness dimension. Every field is `None` by
    default, so `AgentProfile(name="default")` reproduces today's agent exactly. `extra="forbid"`
    rejects a misspelled override rather than silently ignoring it (the same fail-fast the config
    models use).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1)
    # Bounded because `instructions` is the system prompt: an unbounded value is unbounded spend on
    # every model call.
    instructions: str | None = Field(default=None, max_length=MAX_MANIFEST_TEXT_CHARS)
    tool_names: frozenset[str] | None = None
    mcp_server_names: frozenset[str] | None = None
    harness_enabled: bool | None = None
    # `HarnessAutonomy`, not `str`, so a misspelled value (e.g. `plan-only`) is rejected instead of
    # silently taking the plan gate off.
    harness_autonomy: HarnessAutonomy | None = None
    # Reasoning effort, overriding `llm_effort` for this profile. A `Literal` so a misspelled value
    # is rejected: the endpoint would drop it silently or answer 400. Kept in sync with
    # `LlmSettings` by `tests/test_llm_effort.py`, since this module imports no settings.
    effort: Literal["low", "medium", "high"] | None = None
    # Which entry of `settings.model_routes` builds this agent's model — a route key, never a model
    # id, so no site's model name is checked in. `None` takes the `"agent"` route. A model is not a
    # capability, so this does not need to narrow; cost is bounded by `agent/spend_cap.py`. A key
    # with no entry in `model_routes` reuses the model already built.
    model_route: str | None = None
    # Which skills this agent may reach, narrowing the discovered set; `None` leaves the surface to
    # `agent/skill_access.py`. It lets an A/B arm vary skills while holding the tool surface still.
    # An unknown name is caught by `make skill-validate`, not at build time, because the skills tree
    # is deployment configuration; it narrows to nothing.
    skill_names: frozenset[str] | None = None
    # What this profile is for, in one sentence, written for the model deciding whether to delegate
    # to it; unset on profiles nobody rosters. A written field rather than a derived first sentence,
    # so roster entries are distinguishable. `subagents.describe_helper` appends the tools the
    # helper actually bound.
    description: str | None = Field(default=None, max_length=MAX_MANIFEST_TEXT_CHARS)


# The one profile that exists today: every field unset, so it resolves to the global agent verbatim.
DEFAULT_PROFILE = AgentProfile(name="default")

# `{name: profile}`, mirroring sources.registry / bo.objectives. Seeded with the default only.
_REGISTRY: dict[str, AgentProfile] = {DEFAULT_PROFILE.name: DEFAULT_PROFILE}


def register_profile(profile: AgentProfile) -> None:
    """Register a profile under its name; a duplicate name is a programming error."""
    if profile.name in _REGISTRY:
        raise ValueError(f"agent profile {profile.name!r} already registered")
    _REGISTRY[profile.name] = profile


def get_profile(name: str | None) -> AgentProfile:
    """Resolve a profile by name; `None` yields the default. Unknown names raise with valid keys."""
    if name is None:
        return DEFAULT_PROFILE
    profile = _REGISTRY.get(name)
    if profile is None:
        raise ValueError(f"unknown agent profile {name!r}; known: {sorted(_REGISTRY)}")
    return profile


def registered_profile_names() -> list[str]:
    """The names of all registered profiles, sorted."""
    return sorted(_REGISTRY)
