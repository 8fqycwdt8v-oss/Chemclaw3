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

from chemclaw.agent.profiles import _REGISTRY, AgentProfile, registered_profile_names
from chemclaw.cli.validate_skills import main, validate_skills
from chemclaw.core.config import settings


def test_shipped_skills_are_valid(capsys: pytest.CaptureFixture[str]) -> None:
    """Every shipped SKILL.md passes the gate — driven through `main`, which assembles the corpus.

    **Through `main([])` rather than `validate_skills(settings.skills_dirs)`, and that is the whole
    fix.** `make skill-validate` validates `[*settings.skills_dirs, *connector_skills_dirs()]`, and
    three shipped skills live only in the second half (`connectors/bo`, `connectors/safety`,
    `connectors/calc`). Measured on 2026-09-06: breaking the `description:` key in
    `connectors/bo/skills/experiment-design/SKILL.md` left this file at 12 passed while
    `python -m chemclaw.cli.validate_skills` exited 1 — so `make lint type test`, the gate a step is
    declared done against, stayed green over a broken shipped skill.

    Calling the entry point rather than restating its argument is deliberate: a test that rebuilds
    the corpus expression is a second place to keep in step with it, which is the same defect one
    level up. `test_validate_connectors.py` and `test_templates.py` already assert the call `main`
    makes.
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


def test_a_required_tool_outside_the_declaration_is_reported() -> None:
    """`requires` is a subset of `tools`, and an entry outside it is held by nothing else.

    The asymmetry is the reason this rule exists at all. A `tools` typo is caught twice — against
    the live surface here, and by the taught-⇒-declared direction if the body names it — and its
    run-time effect is to leave the skill visible. A `requires` typo is caught by neither, and its
    run-time effect is the opposite: `ToolScopedSkills._permits` hides the skill wherever that name
    is absent from the surface, which for a name no tool has is *everywhere*. So the one that fails
    silently and removes a skill from every deployment is the one with no check, until this.
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
    """Existing is not enough: the two lists must describe one capability, not two.

    Stated separately from the typo case because the failure it prevents is not a misspelling. A
    `requires` naming a tool that really exists but is missing from `tools` passes every existence
    check in this module and still means the skill's declared surface and its required surface
    disagree — and `ToolScopedSkills` reads both, for the same skill, in the same call.
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
    """The satisfiable half, driven end to end through `validate_skills` rather than the helper.

    Through the whole gate because `requires` is a new frontmatter key: `SkillManifest` forbids
    extras, so a rule added to this module without the field reaching the model would fail every
    skill that uses it, and a helper-level test cannot see that.
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
    """The direction that makes the declaration mean something: teach it, declare it.

    Without this rule an incomplete `tools:` list is indistinguishable from an honest one — and
    since the list decides visibility, an under-declared skill is hidden from precisely the agent
    that can run what it teaches. Which is the fix failing, not the defect.
    """
    root = _skill(tmp_path, "probe", "Call gather_evidence first, then read what it cites.")

    problems = validate_skills([str(root)])

    assert any("gather_evidence" in p and "does not declare it" in p for p in problems)


def test_a_backticked_taught_tool_that_is_not_declared_is_reported(tmp_path: Path) -> None:
    """The form skills actually use — `` `predict_pka` `` — must count as teaching it.

    This is the whole rule's reach, not a variant of it: the shipped corpus names tools that way
    almost everywhere, and while the extractor could see only a call form and a bare token, 35
    taught tools across 11 skills were undeclared with the gate reporting none. An under-declared
    skill is hidden from precisely the agent that can run what it teaches, so a rule blind to the
    common spelling was the defect wearing the fix's clothes.
    """
    root = _skill(tmp_path, "probe", "Reach for `gather_evidence` before answering.")

    problems = validate_skills([str(root)])

    assert any("gather_evidence" in p and "does not declare it" in p for p in problems)


def test_a_backticked_name_that_is_not_a_tool_is_not_reported(tmp_path: Path) -> None:
    """And the loose pattern stays quiet, because `available_tool_names()` is the filter.

    The reason the widening is safe here and was rejected for the prose gate (D-2026-08-05):
    asking whether a *known* name is present tolerates a loose pattern, asking whether an unknown
    one is absent does not. Over the shipped corpus this pattern matches 75 spans that are not
    tools — result fields like `yield_percent` — and every one of them drops out here. Without
    that intersection this rule would demand a declaration for each.
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


def test_an_unknown_skill_name_in_a_profile_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The third map that names skills, failing the same quiet way the other two do.

    `ProfileScopedSkills` narrows rather than raising — a turn must not break because a profile
    file has a typo in it — so a misspelled name removes a skill the profile's author meant to keep
    and the profile simply offers one fewer than the file reads. A profile is discovered from disk,
    so this is a deployment's typo as readily as a shipped one.
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
    """The check loads profiles itself, so a *deployment's* profile file is what it sees.

    This is the arm the two above cannot be: they patch the registry, so a version of
    `_profile_skill_problems` that never called `load_profiles()` would satisfy both and still be
    green against every real tree forever — `validate_skills` runs in a CLI process where nothing
    else has registered a profile, so the registry holds `default` alone and `default` declares no
    `skill_names`. Driving it from a file is what distinguishes "looked and found nothing wrong"
    from "did not look".
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
