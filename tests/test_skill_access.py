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

import asyncio
from typing import cast

from agent_framework import FileSkillsSource, SkillsSourceContext
from agent_framework._agents import SupportsAgentRun

from chemclaw.agent.skill_access import (
    EnabledSkillsSource,
    RoleScopedSkillsSource,
    ToolScopedSkillsSource,
)
from chemclaw.core.config import settings
from chemclaw.core.identity_context import reset_current_identity, set_current_identity


def _skill_names(
    gates: dict[str, list[str]] | None, roles: frozenset[str] | None = None
) -> set[str]:
    """Names advertised under a gate map, evaluated as a caller holding `roles` (None = no user)."""
    source = RoleScopedSkillsSource(FileSkillsSource(settings.skills_dirs), gates)
    # The file source ignores the context's agent; a cast keeps the stand-in strictly typed.
    context = SkillsSourceContext(agent=cast(SupportsAgentRun, None))
    token = set_current_identity("u-1", roles) if roles is not None else None
    try:
        skills = asyncio.run(source.get_skills(context))
    finally:
        if token is not None:
            reset_current_identity(token)
    return {skill.frontmatter.name for skill in skills}


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
    from chemclaw.agent.chemclaw_agent import _capability_tools, skills_source
    from chemclaw.agent.profiles import get_profile

    profile = get_profile(None)
    source = skills_source(profile, _capability_tools(profile))
    context = SkillsSourceContext(agent=cast(SupportsAgentRun, None))
    advertised = {s.frontmatter.name for s in asyncio.run(source.get_skills(context))}

    assert advertised == _skill_names({}) | _bundled_skill_names()


def _bundled_skill_names() -> set[str]:
    """The skills that ship inside an enabled connector bundle rather than in `skills/`."""
    from chemclaw.connectors.registry import skills_dirs

    context = SkillsSourceContext(agent=cast(SupportsAgentRun, None))
    source = FileSkillsSource(skills_dirs())
    return {skill.frontmatter.name for skill in asyncio.run(source.get_skills(context))}


def _scoped_names(
    declared: dict[str, frozenset[str]], available: set[str], enabled: list[str] | None = None
) -> set[str]:
    """Names surviving capability scoping (optionally under an enable-list, to test composition)."""
    inner = EnabledSkillsSource(FileSkillsSource(settings.skills_dirs), enabled)
    source = ToolScopedSkillsSource(inner, declared, available)
    context = SkillsSourceContext(agent=cast(SupportsAgentRun, None))
    return {skill.frontmatter.name for skill in asyncio.run(source.get_skills(context))}


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
    enabled = ["deep-research", "knowledge-graph-query"]
    declared = {"deep-research": frozenset({"compute_dft_energy"})}
    inner = EnabledSkillsSource(FileSkillsSource(settings.skills_dirs), enabled)
    source = RoleScopedSkillsSource(
        ToolScopedSkillsSource(inner, declared, {"predict_pka"}),
        {"knowledge-graph-query": ["process-chemist"]},
    )
    context = SkillsSourceContext(agent=cast(SupportsAgentRun, None))

    token = set_current_identity("u-1", frozenset({"process-chemist"}))
    try:
        names = {skill.frontmatter.name for skill in asyncio.run(source.get_skills(context))}
    finally:
        reset_current_identity(token)

    # Enabled two; capability dropped `deep-research`; the role let the other through.
    assert names == {"knowledge-graph-query"}
    assert names < every
