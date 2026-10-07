"""The skill validator catches missing frontmatter, name/directory drift, and declaration drift.

Shipped skills pass; a skill missing `description` or whose `name` disagrees with its directory
is reported. The `tools:` declaration decides whether a skill is advertised, so a declared tool
must exist and a taught tool must be declared. An unknown key in `skill_role_gates` fails open and
is reported too.
"""

from pathlib import Path

import pytest

from chemclaw.agent.profiles import _REGISTRY, AgentProfile, registered_profile_names
from chemclaw.cli.validate_skills import main, validate_skills
from chemclaw.core.config import settings


def test_shipped_skills_are_valid(capsys: pytest.CaptureFixture[str]) -> None:
    """Every shipped SKILL.md passes the gate, driven through `main`.

    `main([])` assembles the corpus including connector skill directories; rebuilding that
    expression here would be a second copy to keep in step.
    """
    assert main([]) == 0, capsys.readouterr().out


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
    """A declared tool resolves wherever the capability lives.

    Declarations resolve against in-process tools, every connector's MCP tools and every declared
    job, so moving a tool across the process boundary breaks no skill.
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


def test_a_required_tool_outside_the_declaration_is_reported() -> None:
    """A `requires` entry outside the `tools` declaration is reported.

    A `requires` typo is caught by nothing else, and `ToolScopedSkills._permits` would then hide the
    skill everywhere.
    """
    from chemclaw.agent.skill_manifest import SkillManifest
    from chemclaw.cli.validate_skills import _requires_problems

    problems = _requires_problems(
        Path("probe/SKILL.md"),
        SkillManifest(
            name="probe",
            description="probe",
            tools=["gather_evidence"],
            requires=["gather_evidenc"],
        ),
    )

    assert len(problems) == 1
    assert "gather_evidenc" in problems[0] and "does not declare it" in problems[0]


def test_a_real_tool_still_has_to_be_declared_to_be_required() -> None:
    """A real tool must still be declared in `tools` to be required.

    `ToolScopedSkills` reads both lists for the same skill, so they must describe one capability.
    """
    from chemclaw.agent.skill_manifest import SkillManifest
    from chemclaw.cli.validate_skills import _requires_problems

    problems = _requires_problems(
        Path("probe/SKILL.md"),
        SkillManifest(
            name="probe",
            description="probe",
            tools=["gather_evidence"],
            # A real tool — `test_a_declared_tool_resolves_wherever_the_capability_lives` proves it.
            requires=["predict_pka"],
        ),
    )

    assert len(problems) == 1
    assert "predict_pka" in problems[0]


def test_a_requires_entry_inside_the_declaration_passes(tmp_path: Path) -> None:
    """A `requires` entry inside the declaration passes, end to end through `validate_skills`.

    `SkillManifest` forbids extra keys, so only the whole gate shows the field reaches the model.
    """
    root = _skill(
        tmp_path,
        "probe",
        "Call gather_evidence first, then read what it cites.",
        tools="tools:\n  - gather_evidence\nrequires:\n  - gather_evidence\n",
    )

    assert validate_skills([str(root)]) == []


def _skill(directory: Path, name: str, body: str, tools: str = "") -> Path:
    """Write one SKILL.md with an optional `tools:` block, and return the directory it lives in."""
    skill = directory / name / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text(f"---\nname: {name}\ndescription: probe\n{tools}---\n{body}\n", "utf-8")
    return directory


def test_a_taught_tool_that_is_not_declared_is_reported(tmp_path: Path) -> None:
    """A taught tool that is not declared is reported.

    Since the list decides visibility, an under-declared skill is hidden from the agent that can run
    it.
    """
    root = _skill(tmp_path, "probe", "Call gather_evidence first, then read what it cites.")

    problems = validate_skills([str(root)])

    assert any("gather_evidence" in p and "does not declare it" in p for p in problems)


def test_a_backticked_taught_tool_that_is_not_declared_is_reported(tmp_path: Path) -> None:
    """A backticked taught tool, the form skills actually use, counts as teaching it."""
    root = _skill(tmp_path, "probe", "Reach for `gather_evidence` before answering.")

    problems = validate_skills([str(root)])

    assert any("gather_evidence" in p and "does not declare it" in p for p in problems)


def test_a_backticked_name_that_is_not_a_tool_is_not_reported(tmp_path: Path) -> None:
    """A backticked name that is not a tool is not reported.

    The loose pattern is safe because matches are intersected with `available_tool_names()`, which
    filters out result fields such as `yield_percent`.
    """
    root = _skill(tmp_path, "probe", "Read `yield_percent` and `valid_from` off the record.")

    assert validate_skills([str(root)]) == []


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
    """A body naming no tool needs no declaration.

    Pure process guidance stays always visible; forcing a token declaration would make the
    visibility scope meaningless.
    """
    root = _skill(tmp_path, "probe", "Decompose the request, then keep evidence and analogy apart.")

    assert validate_skills([str(root)]) == []


def test_an_unknown_skill_role_gate_key_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unknown `skill_role_gates` key is reported.

    `RoleScopedSkills` reads an absent key as ungated, so a typo silently applies no restriction.
    """
    root = _skill(tmp_path, "probe", "Guidance.")
    monkeypatch.setattr(settings, "skill_role_gates", {"probe": ["chemist"], "porbe": ["chemist"]})

    problems = validate_skills([str(root)])

    # Exactly one: the correctly-spelled gate beside it is fine and must not be reported. Asserted
    # as a count rather than by searching for `'probe'`, which the typo's own message contains —
    # it lists the discovered names so the operator can see the spelling they meant.
    assert len(problems) == 1
    assert "porbe" in problems[0] and "gates nothing" in problems[0]


def test_an_unknown_skill_name_in_a_profile_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unknown skill name in a profile is reported.

    `ProfileScopedSkills` narrows rather than raising, so a typo silently drops a skill the author
    meant to keep.
    """
    root = _skill(tmp_path, "probe", "Guidance.")
    # `load_profiles` too, not only the two readers: it registers the six shipped profiles into a
    # module-global registry and this test has no cleanup, which is the leak
    # `tests/test_profile_discovery.py`'s own fixture exists to prevent.
    monkeypatch.setattr("chemclaw.cli.validate_skills.load_profiles", lambda: None)
    monkeypatch.setattr("chemclaw.cli.validate_skills.registered_profile_names", lambda: ["narrow"])
    monkeypatch.setattr(
        "chemclaw.cli.validate_skills.get_profile",
        lambda _name: AgentProfile(name="narrow", skill_names=frozenset({"probe", "porbe"})),
    )

    problems = validate_skills([str(root)])

    assert len(problems) == 1
    assert "porbe" in problems[0] and "narrow" in problems[0]


def test_a_profile_naming_only_real_skills_is_clean(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The negative arm: the check must not fire on the configuration it exists to permit."""
    root = _skill(tmp_path, "probe", "Guidance.")
    monkeypatch.setattr("chemclaw.cli.validate_skills.load_profiles", lambda: None)
    monkeypatch.setattr("chemclaw.cli.validate_skills.registered_profile_names", lambda: ["narrow"])
    monkeypatch.setattr(
        "chemclaw.cli.validate_skills.get_profile",
        lambda _name: AgentProfile(name="narrow", skill_names=frozenset({"probe"})),
    )

    assert validate_skills([str(root)]) == []


def test_a_profile_file_on_disk_is_read_rather_than_assumed_registered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A profile file on disk is read rather than assumed registered.

    The tests above patch the registry; a CLI process has only `default` registered, so only driving
    it from a file shows the check calls `load_profiles()`.
    """
    root = _skill(tmp_path / "tree", "probe", "Guidance.")
    profiles = tmp_path / "profiles"
    profiles.mkdir()
    (profiles / "narrow.yaml").write_text(
        "instructions: narrow agent\nskill_names:\n  - porbe\n", encoding="utf-8"
    )
    monkeypatch.setattr("chemclaw.core.config.settings.profiles_dir", str(profiles))
    before = set(registered_profile_names())
    try:
        problems = validate_skills([str(root)])
    finally:
        for name in set(registered_profile_names()) - before:
            _REGISTRY.pop(name, None)

    assert len(problems) == 1
    assert "porbe" in problems[0] and "narrow" in problems[0]


def test_a_malformed_profile_is_reported_rather_than_raised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The gate lists problems; it does not traceback about a profile out of the skill validator.

    Same treatment `_problems_for` gives a malformed `SKILL.md`, and for the same reason: CI goes
    red either way, and what differs is whether the operator is told what to fix.
    """
    root = _skill(tmp_path / "tree", "probe", "Guidance.")
    profiles = tmp_path / "profiles"
    profiles.mkdir()
    # `extra="forbid"`, so a singular `instruction:` is a validation error rather than a no-op.
    (profiles / "broken.yaml").write_text("instruction: oops\n", encoding="utf-8")
    monkeypatch.setattr("chemclaw.core.config.settings.profiles_dir", str(profiles))
    before = set(registered_profile_names())
    try:
        problems = validate_skills([str(root)])
    finally:
        for name in set(registered_profile_names()) - before:
            _REGISTRY.pop(name, None)

    assert len(problems) == 1
    assert "could not be loaded" in problems[0] and "broken.yaml" in problems[0]
