"""Profiles as files: the authoring path for a per-use-case agent (`AgentProfile` Stage 3).

`chemclaw.agent.profiles` holds the *contract* — the override bundle and the `{name: profile}`
registry —
and until now the only way to add one was a Python call, which meant a use case could not be
configured without a code change. That was the right amount of machinery while exactly one
profile existed. It stopped being the right amount when the capabilities a profile selects from
moved to connectors: a "property-lookup agent" is now a name, three tool names and a sentence of
instructions, and nothing about it should require touching `agent/`.

So a profile is a YAML file, discovered exactly the way a skill is:

- `profiles/<name>.yaml` — the configured tree, for a profile that spans connectors. Most do: a
  profile's whole job is to select *across* capabilities, so a shared home is the common case
  (`reaction-search`'s skill made the same argument for spanning content).
- `connectors/<name>/profiles/<p>.yaml` — a bundle's own, declared in its manifest, for a profile
  that is genuinely about that one capability and should be reviewed and shipped with it.

The file's stem is the profile name; a `name:` key inside it would be a second source of truth
that can disagree with the filename, so the model does not accept one. Everything else is
`AgentProfile`'s own validated schema, `extra="forbid"` included, so a misspelled override fails at
startup rather than silently doing nothing.

Discovery is not enablement of *capability*: a profile can only ever narrow
(`chemclaw.agent.profiles`),
and the audit + authz middleware and the skill role gates run after any narrowing. A file
dropped here cannot widen what its caller may do.
"""

import logging
from pathlib import Path

import yaml
from pydantic import ValidationError

from chemclaw.agent.profiles import AgentProfile, register_profile, registered_profile_names
from chemclaw.connectors.registry import profiles_dirs as connector_profiles_dirs
from chemclaw.core.config import settings

logger = logging.getLogger(__name__)


class ProfileError(ValueError):
    """A profile file is malformed, or two of them claim the same name.

    A `ValueError` subclass for the same reason `ConnectorError` is: this is a "this deployment
    is misconfigured" failure, and one `except ValueError` at an entry point catches all of
    them.
    """


def _load(path: Path) -> AgentProfile:
    """Parse and validate one profile file, whose stem is its name."""
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise ProfileError(f"{path}: unreadable or malformed YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise ProfileError(f"{path}: must contain a YAML mapping, got {type(raw).__name__}")
    if "name" in raw:
        raise ProfileError(
            f"{path}: a profile's name is its filename; remove the 'name' key so the two "
            "cannot disagree"
        )
    try:
        return AgentProfile(name=path.stem, **raw)
    except ValidationError as exc:
        raise ProfileError(f"{path}: invalid profile: {exc}") from exc


def profile_files() -> list[Path]:
    """Every discovered profile file: the configured tree(s), then each enabled bundle's own.

    Sorted within each directory so the discovery order is identical on every machine — the same
    reproducibility reason connector discovery is sorted.
    """
    roots = [Path(d) for d in settings.profiles_dirs] + [Path(d) for d in connector_profiles_dirs()]
    return [path for root in roots if root.is_dir() for path in sorted(root.glob("*.yaml"))]


def load_profiles() -> list[AgentProfile]:
    """Discover, validate and register every profile file; return what was registered.

    Idempotent across repeated calls (the front door builds agents lazily, and tests build
    many), so a profile already registered under the same name is skipped rather than raising
    the registry's duplicate-name error. Two *different* files claiming one name is still an
    error: it is a genuine ambiguity about which agent a caller would get.

    Raises:
        ProfileError: When a file is malformed, or two files claim the same profile name.
    """
    loaded: list[AgentProfile] = []
    seen: dict[str, Path] = {}
    for path in profile_files():
        profile = _load(path)
        if profile.name in seen:
            raise ProfileError(
                f"{path}: profile {profile.name!r} is already defined by {seen[profile.name]}"
            )
        seen[profile.name] = path
        if profile.name in registered_profile_names():
            continue
        register_profile(profile)
        loaded.append(profile)
    if loaded:
        logger.info("registered %d file profile(s): %s", len(loaded), [p.name for p in loaded])
    return loaded
