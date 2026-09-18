"""`propose_skill`: what it refuses, what it tells the model, and what it still cannot do.

The tool's standing is the point — it writes a *proposal* and never a skill — so the first test
here is the absence one: `SkillsReadOnlyRefusal` is unchanged, and adding a proposer did not open a
way for a turn to write judgment into its own prompt.
"""

import asyncio

import pytest

from chemclaw.agent.authz import side_effecting_tools
from chemclaw.agent.behaviour_proposals import (
    InMemoryProposalStore,
    content_hash,
    default_proposal_store,
)
from chemclaw.agent.proposal_tools import propose_skill
from chemclaw.core.config import settings
from chemclaw.core.errors import ChemclawError
from chemclaw.core.identity_context import reset_current_identity, set_current_identity

_BODY = "---\nname: cold-quench\ndescription: how to quench this class cold\n---\n\nQuench cold.\n"


@pytest.fixture(autouse=True)
def _queue(monkeypatch: pytest.MonkeyPatch) -> None:
    """A fresh in-process queue per test."""
    from chemclaw.agent import behaviour_proposals

    monkeypatch.setattr(settings, "session_store", "memory")
    monkeypatch.setattr(behaviour_proposals, "_IN_MEMORY", InMemoryProposalStore())


def _call(**kwargs: str) -> str:
    """Call the tool as a turn for one chemist would."""
    tokens = set_current_identity("a-chemist", frozenset())
    try:
        return str(asyncio.run(propose_skill(**kwargs)))
    finally:
        reset_current_identity(tokens)


def test_proposing_is_state_changing_so_no_helper_holds_it() -> None:
    """Being in the partition is what takes it out of every helper, by arithmetic.

    A helper's surface is its caller's minus `side_effecting_tools()` on both halves
    (`D-2026-09-15-a-helper-shares-the-session-its-caller-already-opened`), so this one membership
    is the whole of the narrowing — no second list to keep in step. The reason is
    `ask_clarifying_question`'s exactly: a helper proposing behaviour changes from a context the
    chemist cannot see is worse than one that cannot propose.
    """
    assert "propose_skill" in side_effecting_tools()


def test_no_turn_can_write_a_skill_even_now_that_it_can_propose_one() -> None:
    """The absence this whole feature rests on, asserted rather than assumed.

    `agent/skill_backend.SkillsReadOnlyRefusal` refuses every write verb on the shared tree and
    `agent/local_skills.ReadOnlyStoreBackend` on the chemist's own. Adding a proposer must not have
    opened a third path, so what a turn can reach is checked here too: the proposer writes
    `behaviour_proposals` and touches neither tier.
    """
    import inspect

    from chemclaw.agent import proposal_tools

    source = inspect.getsource(proposal_tools)

    assert "save_local_skill" not in source, (
        "the proposer reaches the skills tier directly, which is the write a person's route exists "
        "to be the only one of"
    )
    assert "local_skills" not in source


def test_the_model_is_told_which_of_three_things_happened() -> None:
    """A proposer can learn what became of its proposal — a requirement, not a courtesy.

    Without it the only strategy after a decline is to propose again, which the queue's idempotence
    makes harmless and its counters make visible, but which wastes a turn every time. Three
    answers, because the three situations call for different next moves.
    """
    fresh = _call(name="cold-quench", body=_BODY, rationale="twice now")
    repeat = _call(name="cold-quench", body=_BODY, rationale="twice now")

    assert "proposed" in fresh and "waiting" in fresh
    assert "already waiting" in repeat, "a repeat reads as a fresh proposal"

    asyncio.run(
        default_proposal_store().decide(
            "a-chemist",
            "skill",
            "cold-quench",
            content_hash(_BODY),
            accepted=False,
            decided_by="a-chemist",
            reason="too narrow",
        )
    )
    decided = _call(name="cold-quench", body=_BODY, rationale="twice now")

    assert "rejected" in decided and "too narrow" in decided, (
        "the model is not told the verdict or the reason, so it cannot respond to the reason"
    )
    assert "cannot reopen" in decided


@pytest.mark.parametrize(
    ("kwargs", "because"),
    [
        ({"name": "x", "body": "no frontmatter", "rationale": "r"}, "not a skill at all"),
        (
            {"name": "x", "body": "---\nname: y\ndescription: d\n---\n\nb\n", "rationale": "r"},
            "two sources of one name can disagree",
        ),
        (
            {"name": "x", "body": "---\nname: x\n---\n\nb\n", "rationale": "r"},
            "a skill with no description is skipped by the loader",
        ),
        (
            {"name": "a/b", "body": "---\nname: a/b\ndescription: d\n---\n\nb\n", "rationale": "r"},
            "a '/' is the traversal shape",
        ),
    ],
)
def test_a_body_that_could_not_be_written_is_refused_at_the_proposal(
    kwargs: dict[str, str], because: str
) -> None:
    """Validated here rather than at acceptance, and that ordering is the point.

    `POST /skills/mine` refuses a malformed `SKILL.md`, so a proposal that skipped this check would
    be reviewed, accepted, and then fail at the write — the worst place to discover it, because the
    person has already decided and the failure looks like the system losing their decision.
    """
    with pytest.raises(ChemclawError):
        _call(**kwargs)

    assert not asyncio.run(default_proposal_store().list_for("a-chemist")), (
        f"{because}: a refused body was stored anyway"
    )


def test_a_body_over_the_tiers_bound_is_refused_with_the_number(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The same bound `POST /skills/mine` enforces, read from the same setting.

    A proposal the tier could not hold is one a person can accept and the system cannot honour.
    """
    monkeypatch.setattr(settings, "agent_local_skill_max_chars", 200)

    with pytest.raises(ChemclawError, match="200"):
        _call(name="cold-quench", body=_BODY + "x" * 300, rationale="r")
