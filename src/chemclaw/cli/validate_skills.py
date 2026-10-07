"""Validate the SKILL.md files: valid frontmatter, and declared capabilities that really exist.

A skill is discovered by its frontmatter (`name`, `description`); a missing field or a directory
name disagreeing with `name` silently breaks discovery. Beyond that shape check:

- **Declared ⇒ exists.** `tools:` are checked against the in-process registry plus every tool a
  discovered connector declares, catching renamed or removed tools.
- **Taught ⇒ declared.** Every tool the body names must appear in `tools:`, because
  `ToolScopedSkills` uses that list to decide whether the skill is advertised; the body is read
  with `validate_prose_contract.taught_tool_names`, so both gates agree on what a skill says.
- **Required ⇒ declared.** `requires:` must be a subset of `tools:`.

Configured names are checked too, since none fails loudly at run time: `settings.skills_enabled`
(advertises nothing), `settings.skill_role_gates` (gates nothing — fails open), and every
registered profile's `skill_names` (silently drops a skill).

`make skill-validate`: exits non-zero listing the problems. Read-only.
"""

import argparse
from collections.abc import Sequence
from pathlib import Path

import frontmatter
from pydantic import ValidationError

# Importing the agent package populates the tool registry, so the declared-tool check sees the real
# set.
from chemclaw.agent import chemclaw_agent as _agent  # noqa: F401 — imported for tool registration
from chemclaw.agent.chemclaw_agent import declared_tool_names
from chemclaw.agent.profile_discovery import ProfileError, load_profiles
from chemclaw.agent.profiles import get_profile, registered_profile_names
from chemclaw.agent.skill_manifest import SKILL_FILENAME, SkillManifest
from chemclaw.cli.validate_prose_contract import taught_tool_names
from chemclaw.connectors.registry import declared_skills_dirs as connector_skills_dirs
from chemclaw.core.config import settings


def validate_skills(skills_dirs: list[str]) -> list[str]:
    """Return a list of problems across every skill under `skills_dirs` (empty = all good).

    Walks skill directories rather than globbing `*/SKILL.md`, so a directory with a missing or
    misnamed SKILL.md, or a configured dir that does not exist, is reported. Each dir is checked on
    its own.
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
            skill_file = skill_dir / SKILL_FILENAME
            if not skill_file.is_file():
                problems.append(f"{skill_dir}: missing SKILL.md — skill invisible to discovery")
                continue
            found_names.add(skill_dir.name)
            problems.extend(_problems_for(skill_file))
    problems.extend(_enable_list_problems(found_names))
    problems.extend(_role_gate_problems(found_names))
    problems.extend(_profile_skill_problems(found_names))
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
    problems.extend(_requires_problems(skill_file, manifest))
    problems.extend(_undeclared_problems(skill_file, manifest, post.content))
    return problems


def _dependency_problems(skill_file: Path, manifest: SkillManifest) -> list[str]:
    """Check a skill's declared tools against what the system actually provides.

    A declaration grants nothing, but one that no longer resolves teaches a capability that is gone.
    The known set is the in-process registry plus every *discovered* (not enabled) bundle's declared
    tools and job launchers, so a skill bundled with an opt-in connector validates where the
    connector is off.
    """
    known_tools = declared_tool_names()
    return [
        f"{skill_file}: declares unknown tool {tool!r}; available tools: {sorted(known_tools)}"
        for tool in sorted(set(manifest.tools) - known_tools)
    ]


def _requires_problems(skill_file: Path, manifest: SkillManifest) -> list[str]:
    """Check that every `requires:` entry is also declared in `tools:` — the subset rule.

    `tools` entries are held against the live surface by `_dependency_problems`; a `requires` entry
    outside `tools` would be held by nothing, yet `ToolScopedSkills` hides the skill when it is
    unavailable. A subset check rather than a second existence check, so one renamed tool is one CI
    failure.
    """
    undeclared = sorted(set(manifest.requires) - set(manifest.tools))
    return [
        f"{skill_file}: requires {tool!r} but does not declare it in `tools:` — `requires` is a "
        "subset of `tools`, and an entry outside it is checked by nothing while still hiding the "
        "skill wherever that tool is absent"
        for tool in undeclared
    ]


def _undeclared_problems(skill_file: Path, manifest: SkillManifest, body: str) -> list[str]:
    """Check that a skill declares every tool its body actually teaches — the other direction.

    `ToolScopedSkills` advertises a skill by its `tools:` list, so an under-declared skill is hidden
    from the agent that can do what it teaches. `taught_tool_names` sees the call form, a bare
    `snake_case` token and a whole backticked span; spans that are not tools are filtered by
    `declared_tool_names()`. Unknown names are the prose gate's to report, not this one's.
    """
    taught = taught_tool_names(body, declared_tool_names())
    undeclared = sorted(taught - set(manifest.tools))
    return [
        f"{skill_file}: teaches {tool!r} but does not declare it in `tools:` — an incomplete "
        "declaration hides the skill from an agent that can run it"
        for tool in undeclared
    ]


def _enable_list_problems(found_names: set[str]) -> list[str]:
    """Every name in `settings.skills_enabled` must be a skill some configured directory provides.

    `EnabledSkills` narrows rather than raising, so an unknown name silently advertises nothing.
    """
    unknown = sorted(set(settings.skills_enabled_list) - found_names)
    return [
        f"skills_enabled names unknown skill {name!r}; discovered: {sorted(found_names)}"
        for name in unknown
    ]


def _role_gate_problems(found_names: set[str]) -> list[str]:
    """Every key in `settings.skill_role_gates` must name a skill some directory provides.

    This map fails open: `RoleScopedSkills` treats an absent skill as ungated, so a typo'd key
    leaves the skill visible to every caller and nothing at run time can report it. Not an
    escalation
    (the tools are still gated by `authorize_tool`), but a control the operator believes is applied.
    """
    unknown = sorted(set(settings.skill_role_gates) - found_names)
    return [
        f"skill_role_gates names unknown skill {name!r}, so it gates nothing and the skill stays "
        f"visible to every caller; discovered: {sorted(found_names)}"
        for name in unknown
    ]


def _profile_skill_problems(found_names: set[str]) -> list[str]:
    """Every name in a registered profile's `skill_names` must be a skill some directory provides.

    `ProfileScopedSkills` narrows rather than raising, so a typo silently drops a skill. Profiles
    are
    loaded here (`load_profiles()`), since a CLI process would otherwise hold only `default`; a
    `ProfileError` is reported as a problem rather than a traceback.
    """
    try:
        load_profiles()
    except ProfileError as error:
        return [
            f"agent profiles could not be loaded, so no profile's skill_names was checked: {error}"
        ]
    problems: list[str] = []
    for name in registered_profile_names():
        declared = get_profile(name).skill_names
        if declared is None:
            continue
        for unknown in sorted(declared - found_names):
            problems.append(
                f"agent profile {name!r} names unknown skill {unknown!r} in skill_names, so that "
                f"profile silently offers one fewer skill than it declares; "
                f"discovered: {sorted(found_names)}"
            )
    return problems


def main(argv: Sequence[str] | None = None) -> int:
    """Validate every skill; print problems and exit non-zero if any (the CI gate).

    Parses arguments though it declares none, so a stray directory argument is refused rather than
    ignored; `CHEMCLAW_SKILLS_DIR` is the knob.
    """
    argparse.ArgumentParser(
        prog="python -m chemclaw.cli.validate_skills",
        description="Validate every discovered SKILL.md. Set CHEMCLAW_SKILLS_DIR "
        "(a PATH-style list) to point this at another tree.",
    ).parse_args(argv)
    # The configured tree plus every *discovered* bundle's `skills/`, so an opt-in bundle's skill is
    # validated whether or not this checkout enables it.
    problems = validate_skills([*settings.skills_dirs, *connector_skills_dirs()])
    if problems:
        print("SKILL.md validation failed:")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    print("SKILL.md validation passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
