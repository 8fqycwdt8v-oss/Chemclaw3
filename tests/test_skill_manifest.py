"""The validated skill manifest + explicit enable-list (config-extensibility item 5).

Proves the two halves of "discovery is not enablement": a `SKILL.md` frontmatter is a typed
contract whose declared capabilities are checked against the live registries (so a skill teaching a
renamed tool fails CI instead of surviving as stale prose), and a deployment can narrow which
discovered skills are advertised without deleting folders. Both only ever *attenuate* — neither can
advertise a skill no directory provides, and the role gate still runs on top. Offline; the shipped
`skills/` tree is the fixture. See `docs/archive/audit/10-config-extensibility.md` §9 item 5.
"""

from pathlib import Path
from typing import Any

import pytest

import chemclaw.cli.validate_skills as validate_skills
from chemclaw.agent.skill_access import EnabledSkills, RoleScopedSkills
from chemclaw.agent.skill_manifest import SkillManifest, declared_tools
from chemclaw.core.config import Settings, settings


def _write_skill(root: Path, name: str) -> None:
    """Create a minimal valid skill directory — real files, so the real file source reads them."""
    skill_dir = root / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {name} judgment\n---\n\nbody\n", encoding="utf-8"
    )


def _skills_dir(tmp_path: Path, *names: str) -> str:
    """A skills directory populated with `names`."""
    for name in names:
        _write_skill(tmp_path, name)
    return str(tmp_path)


def _names(directory: str, narrowing: Any) -> set[str]:
    """The skill names in `directory` that survive `narrowing` (no identity, so no role gating)."""
    return {name for name in declared_tools([directory]) if narrowing.permits(name)}


def test_manifest_requires_name_and_description() -> None:
    """The two fields discovery depends on stay required."""
    with pytest.raises(ValueError):
        SkillManifest.model_validate({"name": "x"})
    with pytest.raises(ValueError):
        SkillManifest.model_validate({"name": "x", "description": "  "})


def test_manifest_rejects_an_unknown_key() -> None:
    """`extra="forbid"`: a typo'd frontmatter key fails instead of being silently ignored."""
    with pytest.raises(ValueError):
        SkillManifest.model_validate({"name": "x", "description": "d", "descriptions": "typo"})


def test_manifest_declarations_default_to_empty() -> None:
    """A skill that is pure process guidance declares nothing — the deps are optional."""
    manifest = SkillManifest.model_validate({"name": "x", "description": "d"})
    assert manifest.tools == [] and manifest.tags == []


def test_shipped_skills_all_have_valid_manifests() -> None:
    """Every SKILL.md in the repo parses as a manifest and declares only real capabilities."""
    assert validate_skills.validate_skills(settings.skills_dirs) == []


def test_gate_catches_a_skill_declaring_a_vanished_tool(tmp_path: Path) -> None:
    """The payoff: a skill teaching a tool that does not exist is a validation failure.

    This is the drift the frontmatter shape-check cannot see — a renamed or deleted capability
    leaves the skill's prose plausible but wrong.
    """
    skill_dir = tmp_path / "ghost-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\nname: ghost-skill\ndescription: teaches a tool that is gone\n"
        "tools:\n  - predict_pKa\n---\n\nbody\n",
        encoding="utf-8",
    )
    problems = validate_skills.validate_skills([str(tmp_path)])
    assert any("declares unknown tool 'predict_pKa'" in p for p in problems)


def test_gate_catches_a_skill_declaring_an_unknown_connector_tool(tmp_path: Path) -> None:
    """A skill teaching a tool no connector serves fails the gate — the cross-process drift case.

    The declared-tool check spans both halves of the surface, so a skill may legitimately name a
    tool
    that lives behind a connector rather than in this process. The failure mode this guards is the
    same one it guards in-process: a bundle renamed or removed its tool, and the skill still teaches
    it.
    """
    skill_dir = tmp_path / "ghost-connector-tool"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\nname: ghost-connector-tool\ndescription: teaches a connector tool that is gone\n"
        "tools:\n  - similar_unicorns\n---\n\nbody\n",
        encoding="utf-8",
    )
    problems = validate_skills.validate_skills([str(tmp_path)])
    assert any("declares unknown tool 'similar_unicorns'" in p for p in problems)


def test_a_real_connector_tool_satisfies_the_gate(tmp_path: Path) -> None:
    """The other direction: a tool an enabled connector serves resolves, though out of process."""
    skill_dir = tmp_path / "structure-judgment"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\nname: structure-judgment\ndescription: when a similarity hit counts as precedent\n"
        "tools:\n  - similar_molecules\n---\n\nbody\n",
        encoding="utf-8",
    )
    assert validate_skills.validate_skills([str(tmp_path)]) == []


def test_gate_catches_an_enabled_skill_that_does_not_exist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A typo in `skills_enabled` would silently advertise nothing — so the gate fails loud."""
    skill_dir = tmp_path / "real-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\nname: real-skill\ndescription: a real one\n---\n\nbody\n", encoding="utf-8"
    )
    monkeypatch.setattr(settings, "skills_enabled", "real-skill:typo-skill")
    problems = validate_skills.validate_skills([str(tmp_path)])
    assert any("skills_enabled names unknown skill 'typo-skill'" in p for p in problems)


def test_empty_enable_list_advertises_every_discovered_skill(tmp_path: Path) -> None:
    """The default is a no-op: no enable-list means today's behavior, every skill visible."""
    assert _names(_skills_dir(tmp_path, "a", "b", "c"), EnabledSkills([])) == {"a", "b", "c"}


def test_enable_list_narrows_to_the_named_subset(tmp_path: Path) -> None:
    """A configured enable-list advertises exactly those skills."""
    assert _names(_skills_dir(tmp_path, "a", "b", "c"), EnabledSkills(["a", "c"])) == {"a", "c"}


def test_enable_list_cannot_invent_a_skill(tmp_path: Path) -> None:
    """It attenuates only: naming an undiscovered skill adds nothing to the advertised set."""
    assert _names(_skills_dir(tmp_path, "a"), EnabledSkills(["a", "not-discovered"])) == {"a"}


def test_role_gate_still_applies_on_top_of_the_enable_list(tmp_path: Path) -> None:
    """Enablement does not bypass RBAC — a gated skill stays hidden from a caller without roles."""
    enablement = EnabledSkills(["open", "gated"])
    gate = RoleScopedSkills({"gated": ["process-chemist"]})
    directory = _skills_dir(tmp_path, "open", "gated")
    names = {
        name
        for name in declared_tools([directory])
        if enablement.permits(name) and gate.permits(name)
    }
    assert names == {"open"}  # no ambient roles in this test context


def test_skills_enabled_list_parses_the_pathsep_token() -> None:
    """`skills_enabled` uses the delimited-string idiom (a bare-key set), like `skills_dir`."""
    import os

    assert Settings(_env_file=None).skills_enabled_list == []  # type: ignore[call-arg]
    configured: Any = Settings(  # type: ignore[call-arg]
        _env_file=None, skills_enabled=os.pathsep.join(["deep-research", "reaction-search"])
    )
    assert configured.skills_enabled_list == ["deep-research", "reaction-search"]
