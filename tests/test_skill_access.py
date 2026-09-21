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


def test_no_shipped_skill_declares_only_tools_no_manifest_advertises() -> None:
    """Every shipped skill teaches something this *tree* declares — a corpus check, not a turn's.

    The other side of capability scoping, and the one that would catch the real drift: a skill
    dropped here is not a filter bug, it is a skill whose whole subject has left the system — the
    stale-judgment case `make skill-validate` catches for a *renamed* tool and cannot catch for a
    capability that was simply disabled. Asserted against the default profile, which narrows
    nothing, so any drop is real.

    **The basis is deliberately the manifests, and the name now says so.** `_advertised_names` is
    what this tree *declares* — the in-process registry plus every enabled bundle's allow-list —
    which is the right question for "is a committed `SKILL.md` about capability this repository
    still ships". It is the wrong question for a turn, and `skills_backend` used it there until the
    2026-09-10 review measured two skills offered with no bound tool at all: a manifest does not
    move when a server is unreachable. That gate now reads the bound set
    (`tests/test_langgraph_agent.py::test_a_listed_skill_always_has_at_least_one_tool_this_turn_binds`);
    this stays as it was, because a tree-versus-manifest check that narrowed with the fleet would
    pass by going quiet exactly when a bundle is down.
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

    Deliberately pinned rather than left implicit. Hiding on *any* missing tool is the reading a
    future change is most likely to drift into, and it takes 20 of 28 skills off the shipped
    `property-lookup` profile — including `calculation-selection`, which that profile's own
    instructions tell the model to load.
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
    """The `requires:` rule, on exactly the input the `tools:` rule keeps — so it is non-vacuous.

    Deliberately the *same* declaration and the *same* surface as
    `test_one_reachable_tool_keeps_the_skill`: one of two declared tools is reachable, so the
    all-absent rule leaves the skill visible, and that test pins that reading against drift. This
    one adds `requires` naming the absent tool and asserts the opposite outcome from the same two
    arguments, which is the only way to show the second rule decides anything.

    Why the second rule exists is the measured case behind it: a skill whose *central* tools ship
    with an opt-in bundle keeps peripheral ones, survives the all-absent test, and is listed in
    every deployment's prefix as judgment about a path the turn cannot take.
    """
    declared = {"deep-research": frozenset({"gather_evidence", "sample_conformers"})}
    available = {"gather_evidence"}

    assert "deep-research" in _scoped_names(declared, available)
    assert "deep-research" not in _scoped_names(
        declared, available, required={"deep-research": frozenset({"sample_conformers"})}
    )


def test_every_required_tool_present_keeps_the_skill() -> None:
    """`requires` is all-of, not any-of, and the satisfied case must still be visible.

    The rule is `needed <= available`, so a skill naming two required tools is hidden until both
    are there — the opposite quantifier from `tools:`, and the reason the two keys cannot be one.
    Asserted in both directions off one declaration so the conjunction is pinned rather than
    implied by a single passing case.
    """
    declared = {"deep-research": frozenset({"gather_evidence", "sample_conformers", "predict_pka"})}
    required = {"deep-research": frozenset({"gather_evidence", "sample_conformers"})}

    both = _scoped_names(
        declared, available={"gather_evidence", "sample_conformers"}, required=required
    )
    one = _scoped_names(declared, available={"gather_evidence", "predict_pka"}, required=required)

    assert "deep-research" in both
    assert "deep-research" not in one


def test_a_corpus_that_only_requires_still_narrows() -> None:
    """The short-circuit reads both maps, because a `requires`-only corpus is still a narrowing.

    `ToolScopedSkills` skips the whole filter when nothing declares a dependency, and that guard
    used to ask about `declared` alone. A corpus reaching this class through `required` with every
    `declared` entry empty would then be waved through — visible everywhere, with the rule that
    should have hidden it never consulted. The validator makes `requires` a subset of `tools`, so
    the shipped corpus cannot take this shape; the guard is what keeps that a convention of the
    corpus rather than an assumption this class depends on.
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
    """`skill_names: []` means "no skills", and a chemist's own tier used to escape it.

    `data/evals/profiles/skills-removed.yaml` exists to be the clean control its own header demands
    — the arm that varies skills and nothing else. The personal tier is mounted by the backend
    rather than selected by `skill_names`, on a governance argument (`agent/local_skills.py`) that
    is right about a *named subset* and wrong about the empty set, which is a profile author writing
    down that this agent reaches no skill at all. Measured before this: the arm listed a personal
    skill while listing none of the 28 shared ones, so every A/B it reported carried personal
    judgment for any actor with a populated `/mine`.
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
