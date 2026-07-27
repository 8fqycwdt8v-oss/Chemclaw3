"""Validate the SKILL.md files: valid frontmatter, and declared capabilities that really exist.

A skill is discovered by its `SKILL.md` frontmatter (`name`, `description`) — the model sees
those to decide when to load a skill (progressive disclosure). A skill missing either, or a
directory name that disagrees with the declared `name`, silently breaks discovery.

Beyond that shape check, this gate closes the loop between a skill's *judgment* and the
*capabilities* it is written about: a skill may declare the tools it teaches, and those are checked
against the live tool surface (`agents.tool_registry`, plus every tool an enabled connector
advertises). That catches the drift the frontmatter check cannot see — a skill still instructing the
model to call a tool that was renamed or removed — which otherwise survives as plausible, stale
prose. The
configured enable-list (`settings.skills_enabled`) is checked the same way: a name that no
directory provides would silently advertise nothing at run time, so it is a failure here instead.

This is the `make skill-validate` gate: it exits non-zero listing the problems, so CI catches skill
drift like `kg-validate` catches note drift. Read-only; touches nothing.
"""

import sys
from pathlib import Path

import frontmatter
from pydantic import ValidationError

# Importing the agent package's tool modules is what populates the tool registry (the same
# registration side effect `build_agent` relies on), so the declared-tool check sees the real set.
from agents import chemclaw_agent as _agent  # noqa: F401 — imported for tool registration
from agents.skill_manifest import SkillManifest
from agents.tool_registry import registered_tool_names
from chemclaw.config import settings
from connectors.registry import connector_tool_names
from connectors.registry import skills_dirs as connector_skills_dirs


def validate_skills(skills_dirs: list[str]) -> list[str]:
    """Return a list of problems across every skill under `skills_dirs` (empty = all good).

    Walks the skill *directories* rather than globbing `*/SKILL.md`, because the failures this gate
    exists to catch are invisible to the glob: a skill directory whose SKILL.md is missing or
    misnamed, and a configured skills dir that does not exist at all. Each configured dir is checked
    on its own, so one healthy dir cannot mask another's typo.
    """
    problems: list[str] = []
    found_names: set[str] = set()
    for directory in skills_dirs:
        root = Path(directory)
        if not root.is_dir():
            problems.append(f"skills directory {directory!r} does not exist")
            continue
        skill_dirs = sorted(path for path in root.iterdir() if path.is_dir())
        if not skill_dirs:
            problems.append(f"no skill directories found under {directory!r}")
            continue
        for skill_dir in skill_dirs:
            skill_file = skill_dir / "SKILL.md"
            if not skill_file.is_file():
                problems.append(f"{skill_dir}: missing SKILL.md — skill invisible to discovery")
                continue
            found_names.add(skill_dir.name)
            problems.extend(_problems_for(skill_file))
    problems.extend(_enable_list_problems(found_names))
    return problems


def _problems_for(skill_file: Path) -> list[str]:
    """Check one SKILL.md: a valid manifest, a name matching its directory, and real deps."""
    try:
        post = frontmatter.load(skill_file)
    except Exception as exc:  # a malformed file is a problem to report, not a crash
        return [f"{skill_file}: could not parse frontmatter ({exc})"]
    try:
        manifest = SkillManifest.model_validate(post.metadata)
    except ValidationError as exc:
        # One line per invalid/missing/unknown field, so the author sees every problem at once
        # rather than fixing them one CI run at a time.
        return [
            f"{skill_file}: frontmatter {'.'.join(str(p) for p in error['loc']) or '<root>'}: "
            f"{error['msg']}"
            for error in exc.errors()
        ]
    problems: list[str] = []
    directory_name = skill_file.parent.name
    if manifest.name != directory_name:
        problems.append(
            f"{skill_file}: frontmatter name {manifest.name!r} "
            f"does not match directory {directory_name!r}"
        )
    problems.extend(_dependency_problems(skill_file, manifest))
    return problems


def _dependency_problems(skill_file: Path, manifest: SkillManifest) -> list[str]:
    """Check a skill's declared tools against what the system actually provides.

    A declaration is documentation, not a grant (the agent's registry/profile decides what is
    advertised, and `enforce_tool_authz` decides what may run) — but a declaration that no longer
    resolves means the skill is teaching a capability that is gone, which is exactly the stale
    judgment this gate should refuse to ship.

    The known set spans both halves of the tool surface: the in-process registry and everything the
    enabled connectors advertise (their endpoints' allow-listed tools and their generated job
    launchers). One set, because a skill's author does not care which side of the process boundary a
    tool lives on — only that it exists.
    """
    known_tools = {*registered_tool_names(), *connector_tool_names()}
    return [
        f"{skill_file}: declares unknown tool {tool!r}; available tools: {sorted(known_tools)}"
        for tool in sorted(set(manifest.tools) - known_tools)
    ]


def _enable_list_problems(found_names: set[str]) -> list[str]:
    """Every name in `settings.skills_enabled` must be a skill some configured directory provides.

    An unknown name silently advertises nothing at run time (`EnabledSkillsSource` narrows rather
    than raising, so one typo cannot break every live turn) — so the loud failure belongs here.
    """
    unknown = sorted(set(settings.skills_enabled_list) - found_names)
    return [
        f"skills_enabled names unknown skill {name!r}; discovered: {sorted(found_names)}"
        for name in unknown
    ]


def main() -> None:
    """Validate every skill; print problems and exit non-zero if any (the CI gate)."""
    # The same dirs `build_agent` discovers from: the configured tree plus every enabled connector
    # bundle's own `skills/`, so a bundled skill is validated exactly like a shipped one.
    problems = validate_skills([*settings.skills_dirs, *connector_skills_dirs()])
    if problems:
        print("SKILL.md validation failed:")
        for problem in problems:
            print(f"  - {problem}")
        sys.exit(1)
    print("SKILL.md validation passed.")


if __name__ == "__main__":
    main()
