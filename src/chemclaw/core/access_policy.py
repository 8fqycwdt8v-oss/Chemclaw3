"""The access policy: which role may call which tool, read which source, and open which profile.

A role is a key in one YAML file and nothing else — no role name appears in code, so adding a role
is adding a block. The key is the Entra app-role value in the token. Each role grants:

- `tools` — `*`, `tier:read` (every tool that changes nothing), `tier:write` (every side-effecting
  tool), `connector:<name>` (everything one bundle serves), or a tool name;
- `deny` — the same vocabulary, subtracted from that role's own grants only;
- `sources` and `profiles` — names or `*`;
- `admin` — the reviewer acts: org skills, other people's jobs.

A person's rights are the union over the roles they hold; a token with no listed role holds nothing.
With no policy file configured the policy is absent and the older gates apply unchanged. This module
imports nothing from the agent: callers pass in what they know about a tool.
"""

from collections.abc import Collection, Iterable
from functools import cache
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from chemclaw.core.config import settings
from chemclaw.core.errors import ChemclawError

ALL = "*"
TIER_READ = "tier:read"
TIER_WRITE = "tier:write"
CONNECTOR_PREFIX = "connector:"


class AccessPolicyError(ChemclawError):
    """The policy file is missing, unparseable, or names something this deployment does not have."""


class RoleGrant(BaseModel):
    """What one role may do. Every list defaults to empty: a role grants only what it names."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    description: str = ""
    tools: list[str] = Field(default_factory=list)
    deny: list[str] = Field(default_factory=list)
    sources: list[str] = Field(default_factory=list)
    profiles: list[str] = Field(default_factory=list)
    admin: bool = False

    @model_validator(mode="after")
    def _grants_are_spelled_from_the_vocabulary(self) -> "RoleGrant":
        """Refuse a grant that is a typo of a tier, e.g. `tier:writes`, rather than a tool name."""
        for entry in (*self.tools, *self.deny):
            if entry.startswith("tier:") and entry not in {TIER_READ, TIER_WRITE}:
                raise ValueError(f"unknown tier {entry!r}; known: {TIER_READ}, {TIER_WRITE}")
            if entry == CONNECTOR_PREFIX.rstrip(":") or not entry.strip():
                raise ValueError(f"empty or malformed grant {entry!r}")
        return self


def _matches(entry: str, tool: str, connector: str | None, writes: bool) -> bool:
    """Whether one grant or deny entry covers this tool."""
    if entry == ALL or entry == tool:
        return True
    if entry == TIER_WRITE:
        return writes
    if entry == TIER_READ:
        return not writes
    return connector is not None and entry == f"{CONNECTOR_PREFIX}{connector}"


class AccessPolicy(BaseModel):
    """The whole policy: role name to grant."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    roles: dict[str, RoleGrant]

    def _held(self, held: Collection[str]) -> list[RoleGrant]:
        """The grants of the listed roles among `held`; unlisted roles contribute nothing."""
        return [self.roles[name] for name in held if name in self.roles]

    def permits_tool(
        self, held: Collection[str], tool: str, *, connector: str | None, writes: bool
    ) -> bool:
        """Whether any held role grants `tool` and does not deny it in the same block."""
        return any(
            any(_matches(e, tool, connector, writes) for e in grant.tools)
            and not any(_matches(e, tool, connector, writes) for e in grant.deny)
            for grant in self._held(held)
        )

    def permits_source(self, held: Collection[str], source: str) -> bool:
        """Whether any held role may read the datasource `source`."""
        return any(ALL in g.sources or source in g.sources for g in self._held(held))

    def permits_profile(self, held: Collection[str], profile: str) -> bool:
        """Whether any held role may open a session on `profile`."""
        return any(ALL in g.profiles or profile in g.profiles for g in self._held(held))

    def is_admin(self, held: Collection[str]) -> bool:
        """Whether any held role carries the reviewer acts."""
        return any(g.admin for g in self._held(held))

    def unknown_references(
        self,
        *,
        tools: Iterable[str],
        connectors: Iterable[str],
        sources: Iterable[str],
        profiles: Iterable[str],
    ) -> list[str]:
        """Every name the policy uses that the deployment does not have — a typo grants nothing.

        Tool names are checked against `tools` (tools of bundles not enabled here are expected to
        be absent from it, so pass the discovered set).
        """
        known_tools, known_connectors = set(tools), set(connectors)
        known_sources, known_profiles = set(sources), set(profiles)
        problems: list[str] = []
        for role, grant in self.roles.items():
            for entry in (*grant.tools, *grant.deny):
                if entry in {ALL, TIER_READ, TIER_WRITE}:
                    continue
                if entry.startswith(CONNECTOR_PREFIX):
                    if entry[len(CONNECTOR_PREFIX) :] not in known_connectors:
                        problems.append(f"{role}: unknown connector in {entry!r}")
                elif entry not in known_tools:
                    problems.append(f"{role}: unknown tool {entry!r}")
            problems += [
                f"{role}: unknown source {s!r}"
                for s in grant.sources
                if s != ALL and s not in known_sources
            ]
            problems += [
                f"{role}: unknown profile {p!r}"
                for p in grant.profiles
                if p != ALL and p not in known_profiles
            ]
        return problems


def parse_policy(text: str) -> AccessPolicy:
    """Parse and validate a policy document.

    Raises:
        AccessPolicyError: not YAML, not the policy shape, or a typo'd tier.
    """
    try:
        return AccessPolicy.model_validate(yaml.safe_load(text))
    except (yaml.YAMLError, ValidationError) as exc:
        raise AccessPolicyError(f"access policy is not usable: {exc}") from exc


@cache
def active_policy() -> AccessPolicy | None:
    """The configured policy, or `None` when `access_policy_file` is unset (older gates apply).

    Cached for the process; a changed file is picked up on restart, and the test fixture that clears
    discovery caches clears this one.

    Raises:
        AccessPolicyError: the file is named but unreadable or invalid — refused, never ignored,
            because a policy that silently loads as "none" would open everything.
    """
    path = settings.access_policy_file
    if not path:
        return None
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise AccessPolicyError(f"access_policy_file {path!r} cannot be read: {exc}") from exc
    return parse_policy(text)
