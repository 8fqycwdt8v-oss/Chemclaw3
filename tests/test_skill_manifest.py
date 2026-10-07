"""The validated skill manifest and the explicit enable-list.

A `SKILL.md` frontmatter is a typed contract whose declared capabilities are checked against the
live registries, and a deployment can narrow which discovered skills are advertised. Both only
attenuate; the role gate still runs on top. Offline, over the shipped `skills/` tree.
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
    """A skill teaching a tool no connector serves fails the gate.

    The check spans in-process and connector tools, so a renamed bundle tool is caught too.
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


def test_every_template_tool_a_skill_names_is_a_real_template_tool() -> None:
    """Every `run_*` tool a skill names is a real template tool.

    `tool_name` is `run_` plus the template stem with dashes as underscores. `skill-validate`'s
    `_BARE` does not extract backticked names without parens, so this scans the raw text rather
    than widening `_BARE` for every skill.
    """
    import re

    from chemclaw.templates.registry import enabled, tool_name

    real = {tool_name(template) for template in enabled()}
    assert real, "no templates discovered; this test would pass vacuously"

    roots = [Path("skills"), Path("src/chemclaw/connectors")]
    files = sorted(path for root in roots for path in root.rglob("SKILL.md") if path.is_file())
    assert files, "no SKILL.md found; this test would pass vacuously"

    wrong: dict[str, set[str]] = {}
    for path in files:
        named = set(re.findall(r"\brun_[a-z0-9_-]+", path.read_text()))
        if unknown := {name for name in named if name not in real}:
            wrong[str(path)] = unknown

    assert not wrong, (
        f"skills name template tools that do not exist: {wrong}. "
        f"The real names are {sorted(real)} — `run_` plus the file stem with dashes replaced by "
        "underscores, never the stem as written."
    )


def test_a_frontmatter_defect_cannot_widen_what_a_skill_is_scoped_to(tmp_path: Path) -> None:
    """A frontmatter defect cannot widen what a skill is scoped to.

    `ToolScopedSkills` reads a missing entry as "declares nothing", i.e. visible to everyone, so a
    read error must never drop the entry.
    """
    from chemclaw.agent.skill_manifest import MAX_SKILL_DESCRIPTION_CHARS, _declared_tools

    for name, extra in (
        ("over-long", f"description: {'x' * (MAX_SKILL_DESCRIPTION_CHARS + 1)}"),
        ("typo-key", "description: fine\ndescriptions: a misspelled key extra=forbid refuses"),
    ):
        (tmp_path / name).mkdir()
        (tmp_path / name / "SKILL.md").write_text(
            f"---\nname: {name}\n{extra}\ntools: [predict_pka]\n---\n\nbody\n"
        )

    _declared_tools.cache_clear()
    declared = declared_tools([str(tmp_path)])

    assert declared == {
        "over-long": frozenset({"predict_pka"}),
        "typo-key": frozenset({"predict_pka"}),
    }, "a frontmatter defect erased the tools declaration, leaving the skill unscoped"

    _declared_tools.cache_clear()


def test_a_skill_with_no_readable_name_is_scoped_to_nothing(tmp_path: Path) -> None:
    """A skill with no readable name is scoped to nothing.

    It is keyed by its directory name with a declaration nothing can satisfy, since a missing entry
    would be visible to every caller. `skill-validate` requires directory and frontmatter names to
    agree.
    """
    from chemclaw.agent.skill_access import ToolScopedSkills
    from chemclaw.agent.skill_manifest import UNREADABLE_DECLARATION, _declared_tools

    for name, body in (
        ("nameless", "---\ndescription: no name\n---\n\nbody\n"),
        ("tools-scalar", "---\nname: tools-scalar\ndescription: d\ntools: predict_pka\n---\n"),
        ("bad-yaml", "---\nname: bad-yaml\ndescription: [unclosed\n---\n\nbody\n"),
    ):
        (tmp_path / name).mkdir()
        (tmp_path / name / "SKILL.md").write_text(body)

    _declared_tools.cache_clear()
    declared = declared_tools([str(tmp_path)])

    assert declared == {
        "nameless": UNREADABLE_DECLARATION,
        "tools-scalar": UNREADABLE_DECLARATION,
        "bad-yaml": UNREADABLE_DECLARATION,
    }, "an unreadable manifest stayed out of the map, which leaves the skill visible to everyone"

    # Ask the consumer whether the sentinel is satisfiable, over realistic non-empty surfaces up to
    # everything this deployment serves; an empty surface alone cannot tell a sentinel from a real
    # tool name.
    from tests.surface import surface

    served = surface().tool_names
    assert served, (
        "the shipped profile advertises no tools, so the widest arm below asserts nothing"
    )
    for available in (frozenset(), frozenset({"predict_pka"}), frozenset({"find_notes"}), served):
        narrowing = ToolScopedSkills(declared=declared, available=available)
        visible = [name for name in declared if narrowing._permits(name)]
        assert visible == [], (
            f"{visible} has an unreadable manifest and is visible to a profile holding "
            f"{sorted(available)[:4]}{'…' if len(available) > 4 else ''}"
        )

    # The sentinel is unsatisfiable because its name carries a NUL, which no served tool name can.
    assert any("\x00" in name for name in UNREADABLE_DECLARATION), (
        f"the unreadable-manifest sentinel {sorted(UNREADABLE_DECLARATION)} holds no character a "
        "tool name cannot, so nothing stops a real profile satisfying it"
    )
    assert not [name for name in served if "\x00" in name], (
        "a served tool name carries a NUL, so the sentinel is no longer unsatisfiable by "
        "construction and needs a different basis"
    )

    _declared_tools.cache_clear()


def test_a_skill_manifest_read_is_always_a_whole_triple(tmp_path: Path) -> None:
    """`_declared_pair` always returns a whole `(name, tools, requires)` triple.

    Driven over six ways a manifest can fail to read. `tools` and `requires` are consulted with
    opposite quantifiers, so sentinelling only one would fail open on the other; the triple is
    compared whole.
    """
    from chemclaw.agent.skill_manifest import UNREADABLE_DECLARATION, _declared_pair

    cases = {
        "gone": None,
        "empty-name": "---\nname: ''\n---\nbody\n",
        "ws-name": "---\nname: '   '\n---\nbody\n",
        "scalar-tools": "---\nname: scalar-tools\ntools: nope\n---\nbody\n",
        "scalar-requires": "---\nname: scalar-requires\nrequires: nope\n---\nbody\n",
    }
    for directory, body in cases.items():
        (tmp_path / directory).mkdir()
        if body is not None:
            (tmp_path / directory / "SKILL.md").write_text(body)
    (tmp_path / "bad-bytes").mkdir()
    (tmp_path / "bad-bytes" / "SKILL.md").write_bytes(b"---\nname: \xff\xfe\n---\nbody\n")

    for directory in (*cases, "bad-bytes"):
        read = _declared_pair(tmp_path / directory / "SKILL.md")
        assert read == (directory, UNREADABLE_DECLARATION, UNREADABLE_DECLARATION), (
            f"{directory} answered {read!r}; an unreadable manifest must be a whole triple keyed "
            "by its directory and scoped to nothing — `None` drops the entry, a missing entry "
            "reads "
            "as 'declares nothing', which leaves the skill visible to every profile, and an empty "
            "`requires` is the same fail-open answer for the all-of rule"
        )
