"""Profiles as files: the authoring path for a per-use-case agent.

A profile is a YAML file discovered like a skill: `profiles/<name>.yaml` in the configured tree,
or `connectors/<name>/profiles/<p>.yaml` declared by a bundle's manifest. The stem is the name;
the body is `AgentProfile`'s validated schema (`extra="forbid"`), so a typo fails at startup.
A profile can only narrow (`chemclaw.agent.profiles`), so a dropped file cannot widen what its
caller may do.
"""

import logging
from pathlib import Path

from pydantic import ValidationError

from chemclaw.agent.profiles import AgentProfile, register_profile, registered_profile_names
from chemclaw.connectors.registry import profiles_dirs as connector_profiles_dirs
from chemclaw.core.config import settings
from chemclaw.core.errors import ChemclawError
from chemclaw.core.manifest_io import read_manifest

logger = logging.getLogger(__name__)


class ProfileError(ChemclawError):
    """A profile file is malformed, or two of them claim the same name.

    A `ChemclawError` (a `ValueError`) so entry points catch misconfiguration uniformly; also listed
    by name in `chemclaw.durable.publish._BAD_DATA_TYPES`, since Temporal matches by exact name.
    """


def _load(path: Path) -> AgentProfile:
    """Parse and validate one profile file, whose stem is its name.

    Read through `core/manifest_io.read_manifest` (alias, depth and duplicate-key guards), since
    `instructions` becomes a system prompt.
    """
    raw = read_manifest(path, ProfileError)
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

    Sorted within each directory so discovery order is reproducible.
    """
    roots = [Path(d) for d in settings.profiles_dirs] + [Path(d) for d in connector_profiles_dirs()]
    return [path for root in roots if root.is_dir() for path in sorted(root.glob("*.yaml"))]


def load_profiles() -> list[AgentProfile]:
    """Discover, validate and register every profile file; return what was registered.

    Idempotent: a profile already registered under the same name is skipped. Two different files
    claiming one name is still an error.

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
