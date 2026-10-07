"""The self-confirmation guard, driven — including the arm where it changes the answer.

Nothing distilled may count evidence it produced itself: the guard reads
`turn_costs.skills_loaded` to discount sessions where the candidate skill was already loaded. A
guard asserted only where it is a no-op is a claim, so the first two tests differ only in whether
the skill was already loaded, and must disagree.
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
from chemclaw.agent.skill_fingerprint import skill_fingerprint
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
    """The same trajectory is not a candidate where the skill was already acting.

    A loaded skill shapes the trajectories that follow, so counting them would make the loop
    self-confirming by construction.
    """
    already = {session: frozenset({skill_fingerprint(_NAME)}) for session in ("s1", "s2")}

    found = candidates(_report(), {_TOOLS: ["s1", "s2", "s3"]}, already)

    assert found == [], (
        "the guard admitted evidence the skill it would propose had already produced; one "
        "independent session is below the bar the census itself sets"
    )


def test_the_guard_only_ever_removes_evidence() -> None:
    """The guard only ever removes evidence.

    Stated as a property, since the failure that matters is a guard that *adds* a session
    (mis-keying, defaulting a missing row to independent), which an example would not catch.
    """
    sessions = ["s1", "s2", "s3", "s4"]

    mine = skill_fingerprint(_NAME)
    for loaded in ({}, {"s1": frozenset({mine})}, {s: frozenset({mine}) for s in sessions}):
        independent, discounted = independent_sessions(sessions, loaded, _NAME)
        assert len(independent) + len(discounted) == len(sessions)
        assert set(independent) | set(discounted) == set(sessions)
        assert len(independent) <= len(sessions)


def test_a_skill_of_another_name_is_not_self_confirmation() -> None:
    """The guard is about *this* skill, not about having any skill loaded.

    A superset would discard evidence that is genuinely independent — a chemist who uses one skill
    heavily would have every other pattern they exhibit made invisible.
    """
    elsewhere = {
        "s1": frozenset({skill_fingerprint("something-else")}),
        "s2": frozenset({skill_fingerprint("another-thing")}),
    }

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

    `behaviour_proposals` keys on the content hash, so a timestamp or a reordered session list would
    make every run a new proposal and a rejection meaningless.
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
    """A miner cannot fill a queue past what a person could accept.

    Bounded by the cap every accepted skill is charged against, since an accepted skill sits in
    every later turn's prefix and the miner, not the person, should do the ranking.
    """
    monkeypatch.setattr(settings, "agent_local_skills_max", 2)
    many = [
        Candidate((f"tool_{index}", "expand_note"), ("s1", "s2", "s3"), 10 - index, ())
        for index in range(6)
    ]

    kept = bounded(many)

    assert len(kept) == 2
    assert [candidate.occurrences for candidate in kept] == [10, 9], "not the strongest first"


async def test_a_distilled_candidate_really_reaches_the_queue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A distilled candidate really reaches the queue.

    `propose` is the only join between miner and queue and decides the actor the row is filed under
    and its rationale; the wrong actor (or `""`) lands where its evidence did not come from.
    """
    from chemclaw.agent import behaviour_proposals
    from chemclaw.agent.behaviour_proposals import InMemoryProposalStore, content_hash
    from chemclaw.agent.distiller import propose, scaffold

    monkeypatch.setattr(settings, "session_store", "memory")
    store = InMemoryProposalStore()
    monkeypatch.setattr(behaviour_proposals, "_IN_MEMORY", store)

    candidate = Candidate(_TOOLS, ("s1", "s2"), 6, ("s3",))
    filed = await propose(candidate, "a-chemist")

    assert filed.state == "open"
    assert filed.actor == "a-chemist"
    assert filed.content_hash == content_hash(scaffold(candidate))
    assert "1 further conversation(s) discounted" in filed.rationale, (
        "the rationale dropped the discounted evidence, which is the guard's whole finding"
    )
    assert await store.one("a-chemist", "skill", candidate.name, filed.content_hash) is not None


def test_the_guard_s_finding_is_carried_and_not_only_counted() -> None:
    """The guard's finding is carried, not only counted.

    `Candidate.self_confirming` is the guard's output; an empty one reads as "nothing discounted",
    the reassuring direction, so it is asserted non-empty here.
    """
    from chemclaw.agent.skill_fingerprint import skill_fingerprint

    name = Candidate(_TOOLS, (), 0, ()).name
    found = candidates(
        _report(),
        {_TOOLS: ["s1", "s2", "s3"]},
        {"s3": frozenset({skill_fingerprint(name)})},
    )

    assert len(found) == 1
    assert found[0].sessions == ("s1", "s2")
    assert found[0].self_confirming == ("s3",), "the discounted session was not carried"


def test_the_strongest_candidate_is_the_one_with_the_most_independent_sessions() -> None:
    """The strongest candidate is the one with the most independent sessions.

    Session count is the primary key: a trajectory repeated ten times in one conversation is one
    person doing one thing; two conversations is the census's bar.
    """
    from chemclaw.agent.distiller import bounded

    many_occurrences = Candidate(("a", "b"), ("s1", "s2"), 99, ())
    many_sessions = Candidate(("c", "d"), ("s1", "s2", "s3"), 3, ())

    assert bounded([many_occurrences, many_sessions])[0] is many_sessions
