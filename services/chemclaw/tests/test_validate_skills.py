"""The skill validator catches missing frontmatter and name/directory drift.

Proves the `make skill-validate` gate: the shipped skills pass, and a skill missing its
`description` or whose declared `name` disagrees with its directory is reported (so a broken
SKILL.md fails CI rather than silently disappearing from the agent's skill surface).
"""

from pathlib import Path

from chemclaw.config import settings
from scripts.validate_skills import validate_skills


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
