"""Skill visibility: the three narrowings over one discovered set, and that none of them widens.

Two seams, proven separately and then together:

- **Role scoping** (Phase 6): with no gates every skill is visible (today's behavior); a gated
  skill is hidden from a caller (the ambient identity) holding none of its roles and shown to one
  holding a role; ungated skills are unaffected. Roles come from `chemclaw.core.identity_context`,
  so the front door never threads identity through `build_agent`.
- **Capability scoping** (D-2026-08-05): a skill whose *every* declared tool is absent from the
  agent's surface is dropped, one surviving tool keeps it, and a skill declaring nothing is always
  visible. The boundary is what the tests pin, because both neighbouring rules are defensible in
  prose and only one of them leaves the shipped profiles usable.
"""

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

    `declared_tools`'s keys, because they *are* the discovered names — it walks the same tree the
    skills backend walks, and reading them from the one first-party reader is what keeps the tests
    from needing a second answer to "what skills exist".
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


def test_no_shipped_skill_is_orphaned_on_the_full_surface() -> None:
    """Every shipped skill teaches something the default agent can actually call.

    The other side of capability scoping, and the one that would catch the real drift: a skill
    dropped here is not a filter bug, it is a skill whose whole subject has left the system — the
    stale-judgment case `make skill-validate` catches for a *renamed* tool and cannot catch for a
    capability that was simply disabled. Asserted against the default profile, which narrows
    nothing, so any drop is real.
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
    advertised = {name for name in everything if permits(name)}

    assert advertised == _skill_names({}) | _bundled_skill_names()


def _bundle_dirs() -> list[str]:
    """Each enabled connector bundle's own `skills/` directory."""
    from chemclaw.connectors.registry import skills_dirs

    return list(skills_dirs())


def _bundled_skill_names() -> set[str]:
    """The skills that ship inside an enabled connector bundle rather than in `skills/`."""
    return set(declared_tools(_bundle_dirs()))


def _scoped_names(
    declared: dict[str, frozenset[str]], available: set[str], enabled: list[str] | None = None
) -> set[str]:
    """Names surviving capability scoping (optionally under an enable-list, to test composition)."""
    enablement = EnabledSkills(enabled)
    scoping = ToolScopedSkills(declared, available)
    return {name for name in _discovered() if enablement.permits(name) and scoping.permits(name)}


def test_a_skill_declaring_no_tools_is_always_visible() -> None:
    """An empty declaration is process guidance that depends on nothing — never scoped away.

    Asserted against an empty tool surface, which is the strongest form of the claim: not "it
    survives a narrow agent" but "there is no agent it can be hidden from."
    """
    assert _scoped_names({"deep-research": frozenset()}, available=set()) == _skill_names({})


def test_one_reachable_tool_keeps_the_skill() -> None:
    """A skill survives on any single surviving tool — the conservative half of the rule.

    Deliberately pinned rather than left implicit. Hiding on *any* missing tool is the reading a
    future change is most likely to drift into, and it takes 20 of 28 skills off the shipped
    `property-lookup` profile — including `calculation-selection`, which that profile's own
    instructions tell the model to load.
    """
    declared = {"deep-research": frozenset({"gather_evidence", "compute_dft_energy"})}
    assert "deep-research" in _scoped_names(declared, available={"gather_evidence"})


def test_a_skill_with_no_reachable_tool_is_dropped() -> None:
    """When every declared tool is gone, the judgment goes with it."""
    declared = {"deep-research": frozenset({"gather_evidence", "compute_dft_energy"})}
    scoped = _scoped_names(declared, available={"predict_pka"})

    assert "deep-research" not in scoped
    # Only the orphaned skill goes; the undeclared ones are untouched.
    assert scoped == _skill_names({}) - {"deep-research"}


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
        declared={"deep-research": frozenset({"compute_dft_energy"})},
        available={"predict_pka"},
        gates={"knowledge-graph-query": ["process-chemist"]},
    )

    token = set_current_identity("u-1", frozenset({"process-chemist"}))
    try:
        names = {name for name in _discovered() if permits(name)}
    finally:
        reset_current_identity(token)

    # Enabled two; capability dropped `deep-research`; the role let the other through.
    assert names == {"knowledge-graph-query"}
    assert names < every
