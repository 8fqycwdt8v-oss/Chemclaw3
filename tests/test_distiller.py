"""The self-confirmation guard, driven — including the arm where it changes the answer.

`tasks/todo.md` has carried this as a requirement since the plan was written: *nothing distilled may
count evidence it itself produced*, and *the guard ships with its caller*. Building it found a third
thing that had to ship with both, and this file is where that shows: the guard reads
`turn_costs.skills_loaded`, which did not exist, because `chemclaw_skill_loads_total{skill}` says a
skill was read and never in which turn.

**The test that matters is the one where the guard changes the verdict.** A guard asserted only on
a corpus where it is a no-op is `map_to_hpc_identity` with extra steps — a claim that a control
exists. So the first two tests here differ in nothing but whether the candidate skill was already
loaded, and they must disagree.
"""

import pytest

from chemclaw.agent.distiller import (
    MIN_INDEPENDENT_SESSIONS,
    Candidate,
    bounded,
    candidates,
    independent_sessions,
    scaffold,
)
from chemclaw.core.config import settings

_TOOLS = ("find_notes", "expand_note", "gather_evidence")
_NAME = Candidate(_TOOLS, (), 0, ()).name


def _report(tools: tuple[str, ...] = _TOOLS, occurrences: int = 6) -> dict[str, object]:
    """One census report with a single recurring class — the census's own output shape."""
    return {
        "recurring_classes": [
            {
                "tools": list(tools),
                "occurrences": occurrences,
                "sessions": 3,
                "would_have_helped": True,
                "multi_tool": True,
            }
        ]
    }


def test_a_trajectory_that_recurs_independently_is_a_candidate() -> None:
    """The arm where the guard is a no-op: three sessions, none of them already taught."""
    found = candidates(_report(), {_TOOLS: ["s1", "s2", "s3"]}, {})

    assert [candidate.name for candidate in found] == [_NAME]
    assert found[0].sessions == ("s1", "s2", "s3")
    assert found[0].self_confirming == ()


def test_the_same_trajectory_is_not_a_candidate_where_the_skill_was_already_acting() -> None:
    """The arm that makes the guard a control rather than a claim.

    Identical corpus, identical census, one difference: the sessions had already loaded a skill of
    the name this candidate would take. A skill is injected into the prompt and *shapes the
    trajectories that follow*, so counting those would make the loop self-confirming by
    construction — propose, accept, observe the behaviour the acceptance caused, propose again with
    a larger count.
    """
    already = {session: frozenset({_NAME}) for session in ("s1", "s2")}

    found = candidates(_report(), {_TOOLS: ["s1", "s2", "s3"]}, already)

    assert found == [], (
        "the guard admitted evidence the skill it would propose had already produced; one "
        "independent session is below the bar the census itself sets"
    )


def test_the_guard_only_ever_removes_evidence() -> None:
    """It can make a proposal harder to justify and never easier — a one-way property.

    Stated as a property rather than as an example, because the failure that would matter is a
    guard that *added* a session (by mis-keying, by defaulting a missing row to "independent" in a
    way that outvoted a present one), and an example would not catch it.
    """
    sessions = ["s1", "s2", "s3", "s4"]

    for loaded in ({}, {"s1": frozenset({_NAME})}, {s: frozenset({_NAME}) for s in sessions}):
        independent, discounted = independent_sessions(sessions, loaded, _NAME)
        assert len(independent) + len(discounted) == len(sessions)
        assert set(independent) | set(discounted) == set(sessions)
        assert len(independent) <= len(sessions)


def test_a_skill_of_another_name_is_not_self_confirmation() -> None:
    """The guard is about *this* skill, not about having any skill loaded.

    A superset would discard evidence that is genuinely independent — a chemist who uses one skill
    heavily would have every other pattern they exhibit made invisible.
    """
    elsewhere = {"s1": frozenset({"something-else"}), "s2": frozenset({"another-thing"})}

    found = candidates(_report(), {_TOOLS: ["s1", "s2"]}, elsewhere)

    assert [candidate.name for candidate in found] == [_NAME]
    assert found[0].self_confirming == ()


def test_the_bar_is_the_census_s_own_bar_on_the_surviving_evidence() -> None:
    """Two independent sessions, which is what the census already requires before it reports one."""
    assert MIN_INDEPENDENT_SESSIONS == 2

    assert candidates(_report(), {_TOOLS: ["s1"]}, {}) == []
    assert len(candidates(_report(), {_TOOLS: ["s1", "s2"]}, {})) == 1


def test_a_scaffold_is_deterministic_and_says_it_is_unfinished() -> None:
    """Two runs over one corpus must propose the same bytes.

    `behaviour_proposals` keys on the content hash, so a body carrying a timestamp or a re-ordered
    session list would turn one idempotent proposal into a new one every run — and a rejection
    would stop meaning anything, which is the property the whole queue is built on.
    """
    candidate = Candidate(_TOOLS, ("s2", "s1"), 6, ())
    other = Candidate(_TOOLS, ("s1", "s2"), 6, ())

    assert scaffold(candidate) == scaffold(other), "session order changed the bytes"
    assert "scaffold, not finished judgment" in scaffold(candidate)
    assert "Not derivable from the trajectory" in scaffold(candidate)


def test_a_scaffold_is_a_valid_skill_the_queue_will_accept() -> None:
    """A proposal a person accepts must be a document that can actually be written.

    The same check `propose_skill` makes on a model's draft, applied to the miner's — otherwise the
    distiller files bodies that fail at the write, which is the worst place to discover it.
    """
    import frontmatter

    from chemclaw.agent.skill_manifest import SkillManifest

    body = scaffold(Candidate(_TOOLS, ("s1", "s2"), 6, ()))
    manifest = SkillManifest.model_validate(frontmatter.loads(body).metadata)

    assert manifest.name == _NAME
    assert len(body) <= settings.agent_local_skill_max_chars


def test_a_miner_cannot_fill_a_queue_past_what_a_person_could_accept(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bounded by the cap every accepted skill is charged against.

    Proposing more than a person could accept is asking them to do the ranking the miner should
    have done — and the cap exists because every accepted skill sits in the prefix of every later
    turn.
    """
    monkeypatch.setattr(settings, "agent_local_skills_max", 2)
    many = [
        Candidate((f"tool_{index}", "expand_note"), ("s1", "s2", "s3"), 10 - index, ())
        for index in range(6)
    ]

    kept = bounded(many)

    assert len(kept) == 2
    assert [candidate.occurrences for candidate in kept] == [10, 9], "not the strongest first"
