"""Validate the SKILL.md files: every skill has the frontmatter the agent needs.

A skill is discovered by its `SKILL.md` frontmatter (`name`, `description`) — the model sees
those to decide when to load a skill (progressive disclosure). A skill missing either, or a
directory name that disagrees with the declared `name`, silently breaks discovery. This is the
`make skill-validate` gate: it walks every `skills/*/SKILL.md`, checks the required fields, and
exits non-zero (listing the problems) so CI catches skill drift like `kg-validate` catches note
drift. Read-only; touches nothing.
"""

import sys
from pathlib import Path

import frontmatter

from chemclaw.config import settings

_REQUIRED = ("name", "description")


def validate_skills(skills_dirs: list[str]) -> list[str]:
    """Return a list of problems across every skill under `skills_dirs` (empty = all good).

    Walks the skill *directories* rather than globbing `*/SKILL.md`, because the failures
    this gate exists to catch are invisible to the glob: a skill directory whose SKILL.md
    is missing or misnamed, and a configured skills dir that does not exist at all. Each
    configured dir is checked on its own, so one healthy dir cannot mask another's typo.
    """
    problems: list[str] = []
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
            problems.extend(_problems_for(skill_file))
    return problems


def _problems_for(skill_file: Path) -> list[str]:
    """Check one SKILL.md: required frontmatter present and `name` matches its directory."""
    try:
        post = frontmatter.load(skill_file)
    except Exception as exc:  # a malformed file is a problem to report, not a crash
        return [f"{skill_file}: could not parse frontmatter ({exc})"]
    found: list[str] = []
    for field in _REQUIRED:
        value = post.metadata.get(field)
        if not (isinstance(value, str) and value.strip()):
            found.append(f"{skill_file}: missing or empty frontmatter field {field!r}")
    directory_name = skill_file.parent.name
    declared = post.metadata.get("name")
    if isinstance(declared, str) and declared != directory_name:
        found.append(
            f"{skill_file}: frontmatter name {declared!r} "
            f"does not match directory {directory_name!r}"
        )
    return found


def main() -> None:
    """Validate every skill; print problems and exit non-zero if any (the CI gate)."""
    problems = validate_skills(settings.skills_dirs)
    if problems:
        print("SKILL.md validation failed:")
        for problem in problems:
            print(f"  - {problem}")
        sys.exit(1)
    print("SKILL.md validation passed.")


if __name__ == "__main__":
    main()
