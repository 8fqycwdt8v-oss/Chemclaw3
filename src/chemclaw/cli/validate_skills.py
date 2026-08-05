"""Validate the SKILL.md files: valid frontmatter, and declared capabilities that really exist.

A skill is discovered by its `SKILL.md` frontmatter (`name`, `description`) — the model sees
those to decide when to load a skill (progressive disclosure). A skill missing either, or a
directory name that disagrees with the declared `name`, silently breaks discovery.

Beyond that shape check, this gate closes the loop between a skill's *judgment* and the
*capabilities* it is written about, in both directions:

- **Declared ⇒ exists.** A skill's `tools:` are checked against the live tool surface
  (`chemclaw.core.tool_registry`, plus every tool an enabled connector advertises). That catches
  the drift the frontmatter check cannot see — a skill still instructing the model to call a tool
  that was renamed or removed — which otherwise survives as plausible, stale prose.
- **Taught ⇒ declared.** Every tool a skill's *body* names must appear in `tools:`. This half was
  missing, and it is what makes the declaration mean anything: an incomplete `tools:` list is
  indistinguishable from an honest one, and since D-2026-08-05 the list decides whether the skill
  is advertised at all (`chemclaw.agent.skill_access.ToolScopedSkillsSource`) — an under-declared
  skill would be hidden from an agent that can do exactly what it teaches. The body is read with
  `chemclaw.cli.validate_prose_contract.referenced_tool_names`, the same extractor the prose gate
  uses, so the two cannot disagree about what a skill says.

Two configured maps are checked the same way, because both name skills and neither fails loudly at
run time:

- `settings.skills_enabled` — an unknown name silently advertises nothing.
- `settings.skill_role_gates` — an unknown name silently gates **nothing**, which is the direction
  that matters: `RoleScopedSkillsSource` reads "absent from the map" as "ungated", so a typo'd key
  leaves the skill it was meant to restrict visible to every caller. A gate that fails open is
  worth a CI failure.

This is the `make skill-validate` gate: it exits non-zero listing the problems, so CI catches skill
drift like `kg-validate` catches note drift. Read-only; touches nothing.
"""

from pathlib import Path

import frontmatter
from pydantic import ValidationError

# Importing the agent package's tool modules is what populates the tool registry (the same
# registration side effect `build_agent` relies on), so the declared-tool check sees the real set.
from chemclaw.agent import chemclaw_agent as _agent  # noqa: F401 — imported for tool registration
from chemclaw.agent.chemclaw_agent import available_tool_names
from chemclaw.agent.skill_manifest import SKILL_FILENAME, SkillManifest
from chemclaw.cli.validate_prose_contract import referenced_tool_names
from chemclaw.connectors.registry import skills_dirs as connector_skills_dirs
from chemclaw.core.config import settings


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
            skill_file = skill_dir / SKILL_FILENAME
            if not skill_file.is_file():
                problems.append(f"{skill_dir}: missing SKILL.md — skill invisible to discovery")
                continue
            found_names.add(skill_dir.name)
            problems.extend(_problems_for(skill_file))
    problems.extend(_enable_list_problems(found_names))
    problems.extend(_role_gate_problems(found_names))
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
    problems.extend(_undeclared_problems(skill_file, manifest, post.content))
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
    known_tools = available_tool_names()
    return [
        f"{skill_file}: declares unknown tool {tool!r}; available tools: {sorted(known_tools)}"
        for tool in sorted(set(manifest.tools) - known_tools)
    ]


def _undeclared_problems(skill_file: Path, manifest: SkillManifest, body: str) -> list[str]:
    """Check that a skill declares every tool its body actually teaches — the other direction.

    `_dependency_problems` asks "does everything declared exist?"; this asks "is everything taught
    declared?", and without it the declaration means very little: an incomplete `tools:` list looks
    exactly like an honest one. That was tolerable while the list was documentation. It stopped
    being tolerable when the list started deciding whether the skill is advertised at all
    (`chemclaw.agent.skill_access.ToolScopedSkillsSource`, D-2026-08-05) — an under-declared skill
    is hidden from precisely the agent that can do what it teaches, which is the failure mode of
    the fix rather than of the defect.

    The body is read with `referenced_tool_names`, the same extractor `make prose-validate` uses,
    so the two gates cannot disagree about what a skill says. That extractor is deliberately narrow
    — it sees a call form (`` `gather_evidence(` ``) and a bare `snake_case` token, but not a
    backticked name with no parentheses, because in this corpus that form is mostly result-field
    names and matching it produced far more false positives than findings. So this rule is a floor,
    not a proof: it catches the names the prose gate can already resolve, and the rest is caught by
    `_dependency_problems` the moment a declared tool disappears.

    Names the extractor finds that are not tools at all are ignored here rather than reported —
    that is the prose gate's rule 1/2, and reporting it twice would make one typo two CI failures.
    """
    taught = referenced_tool_names(body) & available_tool_names()
    undeclared = sorted(taught - set(manifest.tools))
    return [
        f"{skill_file}: teaches {tool!r} but does not declare it in `tools:` — an incomplete "
        "declaration hides the skill from an agent that can run it"
        for tool in undeclared
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


def _role_gate_problems(found_names: set[str]) -> list[str]:
    """Every key in `settings.skill_role_gates` must name a skill some directory provides.

    **The two config maps fail in opposite directions, and this is the one that fails open.** A
    typo in `skills_enabled` advertises *nothing* under that name — loud, and the operator notices
    the missing skill. A typo in `skill_role_gates` gates *nothing*: `RoleScopedSkillsSource`
    treats a skill absent from the map as ungated, so the restriction an operator wrote is simply
    not applied and the skill stays visible to every caller. Nothing at run time can report that,
    because from the source's point of view nothing happened.

    It is not a privilege escalation — the tools the skill teaches are still gated by
    `authorize_tool`, and skill visibility has never been an access-control boundary on its own —
    but it is a control an operator believes they configured and did not, which is exactly the kind
    of claim this repository refuses to let stand unchecked.
    """
    unknown = sorted(set(settings.skill_role_gates) - found_names)
    return [
        f"skill_role_gates names unknown skill {name!r}, so it gates nothing and the skill stays "
        f"visible to every caller; discovered: {sorted(found_names)}"
        for name in unknown
    ]


def main() -> int:
    """Validate every skill; print problems and exit non-zero if any (the CI gate)."""
    # The same dirs `build_agent` discovers from: the configured tree plus every enabled connector
    # bundle's own `skills/`, so a bundled skill is validated exactly like a shipped one.
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
