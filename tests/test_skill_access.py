"""Skill visibility: the narrowings over one discovered set, and that none of them widens.

- Role scoping: with no gates every skill is visible; a gated skill is shown only to a caller
  holding one of its roles, read from `chemclaw.core.identity_context`.
- Capability scoping: a skill whose every declared tool is absent is dropped, one surviving tool
  keeps it, and a skill declaring nothing is always visible; `requires:` tools must all be
  present.
"""

from typing import Any, cast

from chemclaw.agent.skill_access import (
    EnabledSkills,
    RoleScopedSkills,
    ToolScopedSkills,
    skill_permits,
)
from chemclaw.agent.skill_manifest import declared_tools
from chemclaw.core.config import settings
from chemclaw.core.identity_context import reset_current_identity, set_current_identity


def _discovered() -> set[str]:
    """Every skill name on disk, which is what the narrowings narrow.

    Read from `declared_tools`, the one first-party walker of the skills tree.
    """
    return set(declared_tools(settings.skills_dirs))


def _skill_names(
    gates: dict[str, list[str]] | None, roles: frozenset[str] | None = None
) -> set[str]:
    """Names advertised under a gate map, evaluated as a caller holding `roles` (None = no user)."""
    narrowing = RoleScopedSkills(gates)
    token = set_current_identity("u-1", roles) if roles is not None else None
    try:
        return {name for name in _discovered() if narrowing.permits(name)}
    finally:
        if token is not None:
            reset_current_identity(token)


def test_no_gates_advertises_every_skill() -> None:
    """The default (empty gate map) is unfiltered — all skills stay visible."""
    unfiltered = _skill_names({})
    assert "deep-research" in unfiltered
    assert len(unfiltered) > 1


def test_gated_skill_hidden_from_caller_lacking_the_role() -> None:
    """A gated skill is dropped for a caller (and an anonymous turn) holding none of its roles."""
    gates = {"deep-research": ["process-chemist"]}
    # Anonymous: no ambient identity at all.
    assert "deep-research" not in _skill_names(gates)
    # Authenticated but without the required role.
    assert "deep-research" not in _skill_names(gates, roles=frozenset({"viewer"}))


def test_gated_skill_shown_to_caller_holding_the_role() -> None:
    """A gated skill is advertised to a caller holding one of its allowed roles."""
    gates = {"deep-research": ["process-chemist"]}
    assert "deep-research" in _skill_names(gates, roles=frozenset({"process-chemist"}))


def test_ungated_skills_are_unaffected_by_gates() -> None:
    """Gating one skill never hides the others — only the gated name is scoped."""
    all_skills = _skill_names({})
    gated = _skill_names({"deep-research": ["process-chemist"]}, roles=frozenset({"viewer"}))
    assert gated == all_skills - {"deep-research"}


def test_no_shipped_skill_declares_only_tools_no_manifest_advertises() -> None:
    """Every shipped skill teaches something this tree's manifests declare.

    A corpus check, asserted against the default profile: a dropped skill's whole subject has left
    the system. It deliberately reads manifests, not the bound set, so it does not go quiet when a
    bundle is down; the per-turn gate reads the bound set
    (`tests/test_langgraph_agent.py::test_a_listed_skill_always_has_at_least_one_tool_this_turn_binds`).
    """
    from chemclaw.agent import chemclaw_agent
    from chemclaw.agent.profiles import get_profile

    profile = get_profile(None)
    permits = skill_permits(
        enabled=settings.skills_enabled_list,
        declared=declared_tools([*settings.skills_dirs, *_bundle_dirs()]),
        available=chemclaw_agent._advertised_names(
            profile, chemclaw_agent._capability_tools(profile)
        ),
        gates=settings.skill_role_gates,
    )
    everything = _discovered() | _bundled_skill_names()
    advertised = {name for name in everything if permits.filed(name)}

    assert advertised == _skill_names({}) | _bundled_skill_names()


def _bundle_dirs() -> list[str]:
    """Each enabled connector bundle's own `skills/` directory."""
    from chemclaw.connectors.registry import skills_dirs

    return list(skills_dirs())


def _bundled_skill_names() -> set[str]:
    """The skills that ship inside an enabled connector bundle rather than in `skills/`."""
    return set(declared_tools(_bundle_dirs()))


def _scoped_names(
    declared: dict[str, frozenset[str]],
    available: set[str],
    enabled: list[str] | None = None,
    required: dict[str, frozenset[str]] | None = None,
) -> set[str]:
    """Names surviving capability scoping (optionally under an enable-list, to test composition)."""
    enablement = EnabledSkills(enabled)
    scoping = ToolScopedSkills(declared, available, required)
    return {name for name in _discovered() if enablement.permits(name) and scoping.permits(name)}


def test_a_skill_declaring_no_tools_is_always_visible() -> None:
    """An empty declaration is process guidance that depends on nothing — never scoped away.

    Asserted against an empty tool surface, which is the strongest form of the claim: not "it
    survives a narrow agent" but "there is no agent it can be hidden from."
    """
    assert _scoped_names({"deep-research": frozenset()}, available=set()) == _skill_names({})


def test_one_reachable_tool_keeps_the_skill() -> None:
    """A skill survives on any single surviving tool — the conservative half of the rule.

    Hiding on any missing tool would strip most skills from restricted profiles, including ones
    their instructions tell the model to load.
    """
    declared = {"deep-research": frozenset({"gather_evidence", "sample_conformers"})}
    assert "deep-research" in _scoped_names(declared, available={"gather_evidence"})


def test_a_skill_with_no_reachable_tool_is_dropped() -> None:
    """When every declared tool is gone, the judgment goes with it."""
    declared = {"deep-research": frozenset({"gather_evidence", "sample_conformers"})}
    scoped = _scoped_names(declared, available={"predict_pka"})

    assert "deep-research" not in scoped
    # Only the orphaned skill goes; the undeclared ones are untouched.
    assert scoped == _skill_names({}) - {"deep-research"}


def test_an_absent_required_tool_hides_a_skill_the_declared_rule_would_keep() -> None:
    """An absent `requires:` tool hides a skill the `tools:` rule alone would keep.

    Same declaration and surface as `test_one_reachable_tool_keeps_the_skill`, with the opposite
    outcome, so the second rule is shown to decide something.
    """
    declared = {"deep-research": frozenset({"gather_evidence", "sample_conformers"})}
    available = {"gather_evidence"}

    assert "deep-research" in _scoped_names(declared, available)
    assert "deep-research" not in _scoped_names(
        declared, available, required={"deep-research": frozenset({"sample_conformers"})}
    )


def test_every_required_tool_present_keeps_the_skill() -> None:
    """`requires` is all-of, not any-of, asserted in both directions off one declaration."""
    declared = {"deep-research": frozenset({"gather_evidence", "sample_conformers", "predict_pka"})}
    required = {"deep-research": frozenset({"gather_evidence", "sample_conformers"})}

    both = _scoped_names(
        declared, available={"gather_evidence", "sample_conformers"}, required=required
    )
    one = _scoped_names(declared, available={"gather_evidence", "predict_pka"}, required=required)

    assert "deep-research" in both
    assert "deep-research" not in one


def test_a_corpus_that_only_requires_still_narrows() -> None:
    """A corpus that only declares `requires` still narrows.

    `ToolScopedSkills` must consult both maps before short-circuiting; the validator keeps
    `requires` a subset of `tools`, but this class must not depend on that.
    """
    scoped = _scoped_names(
        {"deep-research": frozenset()},
        available={"predict_pka"},
        required={"deep-research": frozenset({"sample_conformers"})},
    )

    assert "deep-research" not in scoped


def test_capability_scoping_is_a_no_op_when_nothing_declares_a_dependency() -> None:
    """No declarations ⇒ no narrowing, whatever the tool surface is (the short-circuit)."""
    assert _scoped_names({}, available=set()) == _skill_names({})


def test_the_narrowings_compose_and_only_ever_remove() -> None:
    """Chaining enablement, capability and role scoping intersects — it can never add a skill.

    The property that matters for the safety rubric: a profile or a bundle can attenuate the
    advertised judgment and no combination of the three can widen it past what discovery found.
    """
    every = _skill_names({})
    permits = skill_permits(
        enabled=["deep-research", "knowledge-graph-query"],
        declared={"deep-research": frozenset({"sample_conformers"})},
        available={"predict_pka"},
        gates={"knowledge-graph-query": ["process-chemist"]},
    )

    token = set_current_identity("u-1", frozenset({"process-chemist"}))
    try:
        names = {name for name in _discovered() if permits.filed(name)}
    finally:
        reset_current_identity(token)

    # Enabled two; capability dropped `deep-research`; the role let the other through.
    assert names == {"knowledge-graph-query"}
    assert names < every


def test_the_control_arm_that_removes_skills_removes_both_tiers() -> None:
    """`skill_names: []` removes both the shared and the personal skills tier.

    The skills-removed eval arm must vary skills and nothing else, so an empty list means no skill
    at all, including a chemist's own.
    """
    from chemclaw.agent.langgraph_agent import _skills_middleware
    from chemclaw.agent.local_skills import LOCAL_SKILLS_ROOT
    from chemclaw.agent.profiles import AgentProfile

    class _Backend:
        routes = {LOCAL_SKILLS_ROOT: object()}

    def _sources(profile: AgentProfile) -> list[str]:
        backend = cast("Any", _Backend())
        return [str(source) for source in _skills_middleware(backend, [], profile).sources]

    assert LOCAL_SKILLS_ROOT.rstrip("/") in _sources(AgentProfile(name="default"))
    assert LOCAL_SKILLS_ROOT.rstrip("/") in _sources(
        AgentProfile(name="some", skill_names=frozenset({"a"}))
    )
    assert LOCAL_SKILLS_ROOT.rstrip("/") not in _sources(
        AgentProfile(name="arm", skill_names=frozenset())
    )
