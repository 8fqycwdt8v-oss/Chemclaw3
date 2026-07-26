"""The validated skill manifest + explicit enable-list (config-extensibility item 5).

Proves the two halves of "discovery is not enablement": a `SKILL.md` frontmatter is a typed
contract whose declared capabilities are checked against the live registries (so a skill teaching a
renamed tool fails CI instead of surviving as stale prose), and a deployment can narrow which
discovered skills are advertised without deleting folders. Both only ever *attenuate* — neither can
advertise a skill no directory provides, and the role gate still runs on top. Offline; the shipped
`skills/` tree is the fixture. See `docs/audit/10-config-extensibility.md` §9 item 5.
"""

import asyncio
from pathlib import Path
from typing import Any, cast

import pytest
from agent_framework import FileSkillsSource, SkillsSource, SkillsSourceContext
from agent_framework._agents import SupportsAgentRun

import scripts.validate_skills as validate_skills
from agents.skill_access import EnabledSkillsSource, RoleScopedSkillsSource
from agents.skill_manifest import SkillManifest
from chemclaw.config import Settings, settings


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


def _names(source: SkillsSource) -> set[str]:
    """The skill names a source advertises (no ambient identity, so no role gating applies)."""
    # The file source ignores the context's agent; a cast keeps the stand-in strictly typed.
    context = SkillsSourceContext(agent=cast(SupportsAgentRun, None))
    skills = asyncio.run(source.get_skills(context))
    return {s.frontmatter.name for s in skills}


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
    assert manifest.tools == [] and manifest.mcp_servers == [] and manifest.tags == []


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


def test_gate_catches_a_skill_declaring_an_unknown_mcp_server(tmp_path: Path) -> None:
    """The same check covers the MCP half of a skill's declared capabilities."""
    skill_dir = tmp_path / "ghost-mcp"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\nname: ghost-mcp\ndescription: teaches a server that is gone\n"
        "mcp_servers:\n  - mcp-nope\n---\n\nbody\n",
        encoding="utf-8",
    )
    problems = validate_skills.validate_skills([str(tmp_path)])
    assert any("declares unknown MCP server 'mcp-nope'" in p for p in problems)


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
    source = EnabledSkillsSource(FileSkillsSource([_skills_dir(tmp_path, "a", "b", "c")]), [])
    assert _names(source) == {"a", "b", "c"}


def test_enable_list_narrows_to_the_named_subset(tmp_path: Path) -> None:
    """A configured enable-list advertises exactly those skills."""
    source = EnabledSkillsSource(
        FileSkillsSource([_skills_dir(tmp_path, "a", "b", "c")]), ["a", "c"]
    )
    assert _names(source) == {"a", "c"}


def test_enable_list_cannot_invent_a_skill(tmp_path: Path) -> None:
    """It attenuates only: naming an undiscovered skill adds nothing to the advertised set."""
    source = EnabledSkillsSource(
        FileSkillsSource([_skills_dir(tmp_path, "a")]), ["a", "not-discovered"]
    )
    assert _names(source) == {"a"}


def test_role_gate_still_applies_on_top_of_the_enable_list(tmp_path: Path) -> None:
    """Enablement does not bypass RBAC — a gated skill stays hidden from a caller without roles."""
    chained = RoleScopedSkillsSource(
        EnabledSkillsSource(
            FileSkillsSource([_skills_dir(tmp_path, "open", "gated")]), ["open", "gated"]
        ),
        {"gated": ["process-chemist"]},
    )
    assert _names(chained) == {"open"}  # no ambient roles in this test context


def test_skills_enabled_list_parses_the_pathsep_token() -> None:
    """`skills_enabled` uses the delimited-string idiom (a bare-key set), like `skills_dir`."""
    import os

    assert Settings(_env_file=None).skills_enabled_list == []  # type: ignore[call-arg]
    configured: Any = Settings(  # type: ignore[call-arg]
        _env_file=None, skills_enabled=os.pathsep.join(["deep-research", "reaction-search"])
    )
    assert configured.skills_enabled_list == ["deep-research", "reaction-search"]
