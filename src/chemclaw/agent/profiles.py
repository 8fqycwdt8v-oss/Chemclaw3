"""Named agent profiles — the seam for per-use-case agent configuration (Stage 1).

Why this exists: today there is exactly one global agent. Every dimension a use case would
vary — the instructions, the advertised tool subset, the MCP subset, whether the harness runs
and in which mode — is already an input `build_langgraph_agent` draws from module constants or
global config, but there is no way to bind those into a named, selectable bundle. This module adds
that bundle without a new execution engine: a profile is an *override set* over
`build_langgraph_agent`'s existing dimensions, and the sole `"default"` profile reproduces today's
agent byte-for-byte.

Design (see `docs/archive/audit/10-config-extensibility.md` §6):

- **`None` means "use the global default."** Every override field defaults to `None`, and
  `build_langgraph_agent` resolves `None` against the module instructions / `settings` — so this
  module imports neither `chemclaw_agent` nor `settings` (no cycle, no second config source), and
  the default profile is simply `AgentProfile(name="default")` with every field unset.
- **A profile *attenuates*, it never *authorizes*.** The tool/MCP subsets can only *narrow* the
  advertised surface. The audit + per-tool authz middleware and the skill role-gates run in
  `build_langgraph_agent` *after* this narrowing, so a profile that names a tool the caller may not
  use is still denied at call time, and a profile that omits the PR-gate tools merely removes
  capability. A profile is a narrowing seam layered *under* RBAC, never a bypass.
- **Files, not code.** A profile is a YAML file discovered from `data/profiles/` or from a connector
  bundle (`chemclaw.agent.profile_discovery`, D-112), selected per session by name. This module
  holds the
  model and the registry those files populate; nothing here needs editing to add one.

The registry mirrors `chemclaw.ingest.sources.registry` / `chemclaw.science.bo.objectives` (a
`{name: thing}` dict + a resolver that
raises with the valid keys), and `AgentProfile` is a small pydantic spec like every other manifest
in the tree. No new pattern is introduced.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from chemclaw.core.config.agent import HarnessAutonomy


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
    instructions: str | None = None
    tool_names: frozenset[str] | None = None
    mcp_server_names: frozenset[str] | None = None
    harness_enabled: bool | None = None
    # `HarnessAutonomy`, not `str`: `extra="forbid"` above rejects a misspelled field *name*, and
    # this rejects a misspelled *value*. Without it a profile saying `plan-only` loaded silently and
    # took the plan gate off — see the alias's own note for why that is worse than not adding one.
    harness_autonomy: HarnessAutonomy | None = None
    # How hard this agent is asked to think, overriding `llm_effort` for builds on this profile.
    # A `Literal` rather than `str` for the reason the field above is one: `extra="forbid"` catches
    # a misspelled field *name*, and only the type catches a misspelled *value*. That matters more
    # here than for most settings — the value is sent to the endpoint as a parameter, and both
    # clients are `extra="ignore"`, so a rejected value is either dropped in silence or comes back
    # as a 400 that `llm_provider._failover_exceptions` deliberately does not fail over.
    #
    # Typed here as a literal rather than imported from `LlmSettings` because this module
    # deliberately imports no settings (see the module docstring); the two are pinned against each
    # other by `tests/test_llm_effort.py` instead, the way `harness_autonomy` already is.
    #
    # **Only meaningful on `llm_provider='openai_compatible'`**, refused elsewhere by
    # `llm_provider.build_chat_model`: on the Anthropic path the same parameter enables extended
    # thinking rather than setting an effort level (measured), which is a different decision with
    # its own costs.
    #
    # The refusal is named precisely because this comment first credited
    # `LlmSettings._effort_is_provider_scoped`, which reads `self.llm_effort` and therefore never
    # sees this field at all — so the claim was false for exactly the input it was written on.
    effort: Literal["low", "medium", "high"] | None = None
    # Which entry of `settings.model_routes` this agent's model is built from — a **route key**,
    # never a model id. `build_chat_model(task)` already resolves a key to whatever model id a
    # deployment mapped it to, and that indirection is the whole point of the field: a model id
    # written here would be a site's model name checked into this repository, which is exactly what
    # `model_routes` exists so that nobody has to do. `None` takes the `"agent"` route, which is
    # what every build has always used.
    #
    # **Unlike the two fields above, this one does not narrow, and it does not need to.** A profile
    # attenuates a *tool surface*; which model answers is not a capability and carries no authority,
    # so a route pointing at a larger model is not a widening. What it can move is cost, and cost
    # already has its own bound one layer down — `agent/spend_cap.py` meters a turn's bill in a
    # `TurnTotal` channel that a fan-out shares rather than multiplies.
    #
    # The reason it exists is the helper: `agent/subagents.py` derives a profile whose route is
    # `"helper"`, so a deployment makes delegated reading cheaper with
    # `CHEMCLAW_MODEL_ROUTES='{"helper": "<a smaller model>"}'` and no code change. A session
    # profile may name one too — `property-lookup` is the shipped profile whose own header calls it
    # "the question a chemist asks dozens of times a day".
    #
    # **A key with no entry in `model_routes` reuses the model already built** rather than building
    # a second, identical client per turn, so an unconfigured route is today's behaviour exactly.
    # `build_chat_model`'s own contract for an unrouted task is the same answer stated one level
    # down (it falls back to `llm_model`/`agent_model`); this only declines to pay for that twice.
    model_route: str | None = None


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
