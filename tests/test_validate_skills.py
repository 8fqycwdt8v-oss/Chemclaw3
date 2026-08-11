"""The skill validator catches missing frontmatter, name/directory drift, and declaration drift.

Proves the `make skill-validate` gate: the shipped skills pass, and a skill missing its
`description` or whose declared `name` disagrees with its directory is reported (so a broken
SKILL.md fails CI rather than silently disappearing from the agent's skill surface).

Two of the rules are about the `tools:` declaration in both directions, and they matter more since
D-2026-08-05 made that list decide whether the skill is advertised at all: a declared tool must
exist (or the skill teaches a capability that is gone), and a taught tool must be declared (or the
skill is hidden from the very agent that can run it). A third checks the config map that fails
*open* — an unknown key in `skill_role_gates` gates nothing at all.
"""

from pathlib import Path

import pytest

from chemclaw.cli.validate_skills import validate_skills
from chemclaw.core.config import settings


def test_shipped_skills_are_valid() -> None:
    """Every real SKILL.md under the configured skills dir passes validation."""
    assert validate_skills(settings.skills_dirs) == []


def test_missing_description_is_reported(tmp_path: Path) -> None:
    """A skill without a `description` frontmatter field is flagged."""
    skill = tmp_path / "broken-skill" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("---\nname: broken-skill\n---\nBody only.\n", encoding="utf-8")
    problems = validate_skills([str(tmp_path)])
    assert any("description" in p for p in problems)


def test_name_directory_mismatch_is_reported(tmp_path: Path) -> None:
    """A declared `name` that disagrees with the directory is flagged (breaks discovery)."""
    skill = tmp_path / "actual-dir" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text(
        "---\nname: different-name\ndescription: does a thing\n---\nBody.\n", encoding="utf-8"
    )
    problems = validate_skills([str(tmp_path)])
    assert any("does not match directory" in p for p in problems)


def test_empty_skills_dir_is_reported(tmp_path: Path) -> None:
    """A skills dir with no SKILL.md is a problem (misconfiguration, not silent success)."""
    assert validate_skills([str(tmp_path)]) != []


def test_skill_dir_without_skill_md_is_reported(tmp_path: Path) -> None:
    """A skill directory whose SKILL.md is missing or misnamed is flagged, not glob-invisible."""
    good = tmp_path / "good-skill" / "SKILL.md"
    good.parent.mkdir(parents=True)
    good.write_text("---\nname: good-skill\ndescription: works\n---\nBody.\n", encoding="utf-8")
    hidden = tmp_path / "renamed-skill" / "skill.md"  # lowercase: invisible to discovery
    hidden.parent.mkdir(parents=True)
    hidden.write_text("---\nname: renamed-skill\ndescription: lost\n---\nBody.\n", encoding="utf-8")
    problems = validate_skills([str(tmp_path)])
    assert any("renamed-skill" in p and "missing SKILL.md" in p for p in problems)
    assert not any("good-skill" in p for p in problems)


def test_nonexistent_configured_dir_is_reported(tmp_path: Path) -> None:
    """A typo'd skills dir is flagged even when another configured dir has valid skills."""
    good = tmp_path / "real" / "good-skill" / "SKILL.md"
    good.parent.mkdir(parents=True)
    good.write_text("---\nname: good-skill\ndescription: works\n---\nBody.\n", encoding="utf-8")
    problems = validate_skills([str(tmp_path / "real"), str(tmp_path / "typo")])
    assert any("typo" in p and "does not exist" in p for p in problems)


def test_a_declared_tool_resolves_wherever_the_capability_lives() -> None:
    """A skill names a capability; which process delivers it is a deployment decision.

    Moving the calculators out to the `calc` connector — and the expensive ones on to its
    durable jobs — changed no skill, because a declaration resolves against the whole
    surface: in-process tools, every connector's MCP tools, and every declared job. If it
    did not, moving a tool across the boundary would break every skill that teaches it,
    which would make the deployment shape a property of the judgment layer.
    """
    from chemclaw.agent.skill_manifest import SkillManifest
    from chemclaw.cli.validate_skills import _dependency_problems
    from chemclaw.connectors.registry import connector_tool_names

    out_of_process = set(connector_tool_names())
    assert "predict_pka" in out_of_process  # a connector MCP tool
    assert "compute_reaction_energy" in out_of_process  # a connector *job*

    manifest = SkillManifest(
        name="probe",
        description="probe",
        # One connector MCP tool, one connector job, one in-process tool.
        tools=["predict_pka", "compute_reaction_energy", "gather_evidence"],
    )
    assert _dependency_problems(Path("probe/SKILL.md"), manifest) == []


def test_an_invented_tool_is_still_rejected() -> None:
    """Widening the lookup must not weaken it: an unknown name is still a failure."""
    from chemclaw.agent.skill_manifest import SkillManifest
    from chemclaw.cli.validate_skills import _dependency_problems

    problems = _dependency_problems(
        Path("probe/SKILL.md"),
        SkillManifest(name="probe", description="probe", tools=["no_such_tool"]),
    )
    assert len(problems) == 1
    assert "no_such_tool" in problems[0]


def _skill(directory: Path, name: str, body: str, tools: str = "") -> Path:
    """Write one SKILL.md with an optional `tools:` block, and return the directory it lives in."""
    skill = directory / name / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text(f"---\nname: {name}\ndescription: probe\n{tools}---\n{body}\n", "utf-8")
    return directory


def test_a_taught_tool_that_is_not_declared_is_reported(tmp_path: Path) -> None:
    """The direction that makes the declaration mean something: teach it, declare it.

    Without this rule an incomplete `tools:` list is indistinguishable from an honest one — and
    since the list decides visibility, an under-declared skill is hidden from precisely the agent
    that can run what it teaches. Which is the fix failing, not the defect.
    """
    root = _skill(tmp_path, "probe", "Call gather_evidence first, then read what it cites.")

    problems = validate_skills([str(root)])

    assert any("gather_evidence" in p and "does not declare it" in p for p in problems)


def test_a_taught_tool_that_is_declared_passes(tmp_path: Path) -> None:
    """The same skill with the declaration filled in is clean — the rule is satisfiable."""
    root = _skill(
        tmp_path,
        "probe",
        "Call gather_evidence first, then read what it cites.",
        tools="tools:\n  - gather_evidence\n",
    )

    assert validate_skills([str(root)]) == []


def test_a_body_naming_no_tool_needs_no_declaration(tmp_path: Path) -> None:
    """Pure process guidance depends on nothing, so an empty `tools:` is correct, not lazy.

    The rule must not push every skill into declaring something: an always-visible skill is the
    right outcome for judgment that names no capability, and forcing a token declaration would
    make the visibility scope meaningless.
    """
    root = _skill(tmp_path, "probe", "Decompose the request, then keep evidence and analogy apart.")

    assert validate_skills([str(root)]) == []


def test_an_unknown_skill_role_gate_key_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A typo'd gate key silently gates nothing — the one config map that fails open.

    `RoleScopedSkills` reads "absent from the map" as "ungated", so the restriction an
    operator wrote is simply never applied and nothing at run time can say so. Its twin,
    `skills_enabled`, fails the other way (the skill vanishes, and someone notices), which is why
    only this one needed a test written against the *direction* of the failure.
    """
    root = _skill(tmp_path, "probe", "Guidance.")
    monkeypatch.setattr(settings, "skill_role_gates", {"probe": ["chemist"], "porbe": ["chemist"]})

    problems = validate_skills([str(root)])

    # Exactly one: the correctly-spelled gate beside it is fine and must not be reported. Asserted
    # as a count rather than by searching for `'probe'`, which the typo's own message contains —
    # it lists the discovered names so the operator can see the spelling they meant.
    assert len(problems) == 1
    assert "porbe" in problems[0] and "gates nothing" in problems[0]
