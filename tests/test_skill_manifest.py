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


def test_every_template_tool_a_skill_names_is_a_real_template_tool() -> None:
    """A skill's routing table named seven tools that do not exist, and no gate saw it.

    `templates/registry.py::tool_name` is `f"run_{name.replace('-', '_')}"`, so the tool for
    `data/templates/tautomer-resolution.yaml` is `run_tautomer_resolution`. Both shipped skills
    wrote `run_tautomer-resolution` — the file stem, dashes and all — in every row of the "which
    workflow" table the whole feature exists to provide.

    `make skill-validate` could not catch it. Its "taught implies declared" half extracts tool names
    with `validate_prose_contract._BARE`, whose lookbehind excludes a backticked name with no
    parens — so it extracted *nothing* from `ensemble-workflows/SKILL.md` and the bidirectional
    check was vacuous on exactly the file that needed it. This closes that specific hole rather than
    widening `_BARE`, which would change what every other skill is checked against.

    Scanned over the raw text, not the frontmatter, because the wrong names were in prose: a
    `run_*` token anywhere in a skill is a claim that a template by that name is launchable.
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
    """A read error must cost a skill its visibility, never buy it back.

    `declared_tools` fed `ToolScopedSkills`, which reads a missing entry as "declares nothing" and
    therefore leaves the skill **visible to every caller**. So while this walked the whole
    `SkillManifest`, any frontmatter defect — a description one character over
    `MAX_SKILL_DESCRIPTION_CHARS`, a misspelled `tags:` key — silently unscoped the skill. Neither
    is evidence about which tools the skill teaches, and the filter's whole contract is one-way.
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
    """The residue of the rule above, and it used to be asserted the other way round.

    **This test asserted `declared_tools(...) == {}` and called staying out of the map "the
    conservative answer". It is the fail-open answer**, and the test directly above says why: the
    consumer is `ToolScopedSkills._permits`, which reads a *missing* entry as "declares nothing" and
    therefore leaves the skill **visible to every caller**. So the pair contradicted each other on
    one rule — "a read error must cost a skill its visibility, never buy it back" — with this half
    buying it back for every defect the sibling's two cases do not cover. Measured against a profile
    holding **zero** callable tools: a scalar `tools:`, a mapping `tools:` and an unparseable
    frontmatter were all visible, where the over-long `description` the sibling covers was not.

    So the unreadable case is keyed too, with a declaration nothing can satisfy. The key is the
    *directory* name, because the frontmatter is the thing that could not be read — and
    `make skill-validate` requires the directory and the frontmatter `name` to agree
    (`cli/validate_skills.py`), so in any tree CI has walked this is the same string the readable
    path would have produced. In a tree it has not walked, a skill scoped to nothing is the safe
    answer rather than a guess, which is the direction the sibling's contract asks for.
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

    # **The half that makes the assertion above mean something.** A key in the map is only
    # conservative if the value cannot be satisfied, so this asks the consumer rather than trusting
    # the sentinel's name.
    #
    # **`available=frozenset()` alone is the one value that cannot tell the two apart**, and it was
    # the only one asserted. An empty surface satisfies no declaration whatever it names, so setting
    # the sentinel to a real tool name (`frozenset({"predict_pka"})`) left 91 tests green — while a
    # profile holding `predict_pka`, which `data/profiles/` ships, saw every skill with an
    # unreadable manifest. So the surfaces below include realistic non-empty ones, and the widest
    # of them is every name this deployment can actually serve.
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

    # **And the sentinel's unsatisfiability is derived, not asserted about its wording.** The reason
    # `required & available` is always empty is that the name carries a NUL, which no tool name in
    # this deployment can — so that is what is checked, against the served surface rather than
    # against a sentence. A sentinel changed to any name a real profile could hold fails here as
    # well as in the loop above, which is what makes the two independent.
    assert any("\x00" in name for name in UNREADABLE_DECLARATION), (
        f"the unreadable-manifest sentinel {sorted(UNREADABLE_DECLARATION)} holds no character a "
        "tool name cannot, so nothing stops a real profile satisfying it"
    )
    assert not [name for name in served if "\x00" in name], (
        "a served tool name carries a NUL, so the sentinel is no longer unsatisfiable by "
        "construction and needs a different basis"
    )

    _declared_tools.cache_clear()


def test_a_skill_manifest_pair_is_always_a_pair(tmp_path: Path) -> None:
    """`_declared_pair` is total, over every way a manifest can fail to be read.

    **The dead code this replaces was invisible to `mypy --strict`.** The function was annotated
    `-> tuple[str, frozenset[str]] | None`, its summary line said "or None (logged) if the file
    cannot be read at all", and `_declared_tools` guarded on `if pair is not None:` — all three long
    after the `except` arm was changed to return `(directory name, UNREADABLE_DECLARATION)`. Nothing
    can return `None` any more, so the guard was a branch no test could cover and the annotation was
    a licence: a future `return None` would type-check, pass the guard, drop the entry, and leave
    the skill **visible**, which is the fail-open answer that arm exists to refuse.

    So the annotation is narrowed and this is what holds it. Driven over five shapes, which is the
    set that reaches the `except` for five different reasons — a file that is not there at all, a
    name that is empty, a name that is only whitespace, bytes no decoder accepts, and a `tools:` key
    of the wrong type.
    """
    from chemclaw.agent.skill_manifest import UNREADABLE_DECLARATION, _declared_pair

    cases = {
        "gone": None,
        "empty-name": "---\nname: ''\n---\nbody\n",
        "ws-name": "---\nname: '   '\n---\nbody\n",
        "scalar-tools": "---\nname: scalar-tools\ntools: nope\n---\nbody\n",
    }
    for directory, body in cases.items():
        (tmp_path / directory).mkdir()
        if body is not None:
            (tmp_path / directory / "SKILL.md").write_text(body)
    (tmp_path / "bad-bytes").mkdir()
    (tmp_path / "bad-bytes" / "SKILL.md").write_bytes(b"---\nname: \xff\xfe\n---\nbody\n")

    for directory in (*cases, "bad-bytes"):
        pair = _declared_pair(tmp_path / directory / "SKILL.md")
        assert pair == (directory, UNREADABLE_DECLARATION), (
            f"{directory} answered {pair!r}; an unreadable manifest must be a pair keyed by its "
            "directory and scoped to nothing — `None` drops the entry, and a missing entry reads "
            "as 'declares nothing', which leaves the skill visible to every profile"
        )
