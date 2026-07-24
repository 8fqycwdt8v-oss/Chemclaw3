"""Named agent profiles — the seam for per-use-case agent configuration (Stage 1).

Why this exists: today there is exactly one global agent. Every dimension a use case would
vary — the instructions, the advertised tool subset, the MCP subset, whether the harness runs
and in which mode — is already an input `build_agent` draws from module constants or global
config, but there is no way to bind those into a named, selectable bundle. This module adds that
bundle without a new execution engine: a profile is an *override set* over `build_agent`'s
existing dimensions, and the sole `"default"` profile reproduces today's agent byte-for-byte.

Design (see `docs/audit/10-config-extensibility.md` §6):

- **`None` means "use the global default."** Every override field defaults to `None`, and
  `build_agent` resolves `None` against the module instructions / `settings` — so this module
  imports neither `chemclaw_agent` nor `settings` (no cycle, no second config source), and the
  default profile is simply `AgentProfile(name="default")` with every field unset.
- **A profile *attenuates*, it never *authorizes*.** The tool/MCP subsets can only *narrow* the
  advertised surface. The GxP audit + per-tool authz middleware and the skill role-gates run in
  `build_agent` *after* this narrowing, so a profile that names a tool the caller may not use is
  still denied at call time, and a profile that omits the PR-gate tools merely removes capability.
  A profile is a narrowing seam layered *under* RBAC, never a bypass.
- **Rule of Three.** Only the default profile exists today; the registry and the
  `build_agent(profile=…)` plumbing are the seam, not speculative profiles. Front-door selection
  (Stage 2) and filesystem-discovered profiles (Stage 3) wait until a second use case forces them.

The registry mirrors `sources.registry` / `bo.objectives` (a `{name: thing}` dict + a resolver
that raises with the valid keys), and `AgentProfile` mirrors `config.McpServerSpec` (a small
pydantic spec). No new pattern is introduced.
"""

from pydantic import BaseModel, ConfigDict, Field


class AgentProfile(BaseModel):
    """A named override-bundle over `build_agent`'s dimensions; unset fields fall back to global.

    `instructions` swaps the system prompt; `tool_names` / `mcp_server_names` *narrow* the
    advertised in-process tools / MCP capability servers to the named subset (a name absent from
    the built surface is a loud error in `build_agent`, not a silent drop); `harness_enabled` /
    `harness_autonomy` override the harness dimension. Every field is `None` by default, so
    `AgentProfile(name="default")` reproduces today's agent exactly. `extra="forbid"` rejects a
    misspelled override rather than silently ignoring it (the same fail-fast the config models use).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1)
    instructions: str | None = None
    tool_names: frozenset[str] | None = None
    mcp_server_names: frozenset[str] | None = None
    harness_enabled: bool | None = None
    harness_autonomy: str | None = None


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
