"""The queue's four rules, driven against both backends because a disagreement is the whole risk.

`agent/behaviour_proposals.py` says a control with two implementations that disagree about whether
a rejection can be reopened is a control nobody can reason about. That is not a claim a reader can
check, so every rule here runs against **both** — the in-process backend a `session_store="memory"`
deployment gets (the CLI is one) and the Postgres one a fleet gets — from one parametrised body, so
a rule cannot be added to one and forgotten in the other.

The four rules, each stated as the thing that would be false without it:

1. **An unchanged re-proposal cannot reopen a rejection.** Without it the model retries until a
   person gives in, which is the whole reason the key is the content rather than the name.
2. **A changed body supersedes an open sibling and never a decided one.** Retired `note_proposals`
   shipped without the first half: migration 058 records a queue rendering versions nothing would
   deliver and one decision then applied to both.
3. **A decision is final.** A rejection that a later call can overwrite is not evidence.
4. **A proposal is one person's.** Two chemists may be offered the same procedure, and one
   rejecting it must not decide for the other.
"""

import asyncio
import uuid
from collections.abc import Iterator

import pytest

from chemclaw.agent.behaviour_proposals import (
    InMemoryProposalStore,
    PostgresProposalStore,
    Proposal,
    ProposalStore,
    content_hash,
    default_proposal_store,
)
from chemclaw.core.config import settings
from tests.pg import migrated_db_or_skip

_BODY = "---\nname: cold-quench\ndescription: how to quench this class cold\n---\n\nQuench cold.\n"


@pytest.fixture
def actor() -> str:
    """A person nobody else's test has proposed for.

    **The Postgres arm shares one database with every other test and with whatever a session drove
    by hand**, and this table is deliberately append-only with no DELETE grant — so isolation
    cannot come from truncating it. A fresh actor per test is the isolation the schema already
    provides, since content identity is per person by design: that is the same property rule 4
    asserts, spent here rather than worked around.
    """
    return f"chemist-{uuid.uuid4().hex[:12]}"


def _proposal(content: str = _BODY, *, actor: str, name: str = "cold-quench") -> Proposal:
    """One proposal as `propose_skill` would build it."""
    return Proposal(
        kind="skill",
        name=name,
        content_hash=content_hash(content),
        content=content,
        rationale="this went wrong the same way twice",
        actor=actor,
        session_id="session-1",
        correlation_id="correlation-1",
    )


@pytest.fixture(params=["memory", "postgres"])
def store(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> Iterator[ProposalStore]:
    """Both backends, from one body, so a rule cannot hold in one and not the other.

    The Postgres arm skips without a migrated database and says so, rather than quietly running
    half the file — `tests/conftest.py` counts that skip and names what the run is therefore not
    evidence about.

    **`asyncio.run` around the skip helper, and it is not decoration.** `migrated_db_or_skip` is a
    coroutine, and the first spelling of this fixture called it without awaiting: the coroutine was
    created and dropped, so the arm never skipped and only passed because this machine happened to
    have a migrated database. `mypy --strict` caught it where no pytest rule would have — the same
    hole `D-2026-09-16`'s review wrote down about a forgotten `await` inside an *async* test, which
    is invisible because the warning is raised by the garbage collector after the test returns.
    """
    if request.param == "postgres":
        asyncio.run(migrated_db_or_skip())
        monkeypatch.setattr(settings, "session_store", "postgres")
        yield PostgresProposalStore()
    else:
        monkeypatch.setattr(settings, "session_store", "memory")
        yield InMemoryProposalStore()


def test_an_unchanged_re_proposal_cannot_reopen_a_rejection(
    store: ProposalStore, actor: str
) -> None:
    """Rule 1, and the reason the key is the content rather than the name.

    Without it the model's only strategy after a decline is to propose again, and a queue that
    reopens on repetition is a queue a person has to keep saying no to.
    """
    first = asyncio.run(store.propose(_proposal(actor=actor)))
    assert first.state == "open"

    asyncio.run(
        store.decide(
            actor,
            "skill",
            "cold-quench",
            first.content_hash,
            accepted=False,
            decided_by=actor,
            reason="too narrow",
        )
    )
    again = asyncio.run(store.propose(_proposal(actor=actor)))

    assert again.state == "rejected", "the same text reopened a decision"
    assert again.reason == "too narrow", "the reason a person gave did not survive the re-proposal"


def test_a_changed_body_supersedes_an_open_sibling_and_never_a_decided_one(
    store: ProposalStore, actor: str
) -> None:
    """Rule 2, both halves, because retired `note_proposals` shipped with only the second.

    Migration 058 records what the missing half costs: the queue renders two open versions of one
    name, a reviewer cannot tell which a decision applies to, and the decision is then applied to
    both. The other half matters as much — superseding a *decided* version would erase the evidence
    rule 1 exists to keep.
    """
    first = asyncio.run(store.propose(_proposal(actor=actor)))
    second = asyncio.run(store.propose(_proposal(_BODY.replace("cold.", "warm."), actor=actor)))

    stale = asyncio.run(store.one(actor, "skill", "cold-quench", first.content_hash))
    assert stale is not None and stale.state == "superseded"
    assert second.state == "open"
    assert len(asyncio.run(store.list_for(actor, states=["open"]))) == 1, (
        "two open versions of one name: a reviewer cannot tell which a decision applies to"
    )

    asyncio.run(
        store.decide(
            actor,
            "skill",
            "cold-quench",
            second.content_hash,
            accepted=False,
            decided_by=actor,
            reason="no",
        )
    )
    asyncio.run(store.propose(_proposal(_BODY.replace("cold.", "tepid."), actor=actor)))
    decided = asyncio.run(store.one(actor, "skill", "cold-quench", second.content_hash))

    assert decided is not None and decided.state == "rejected", (
        "a newer version superseded a decided one, which erases the decision rule 1 keeps"
    )


def test_a_decision_is_final(store: ProposalStore, actor: str) -> None:
    """Rule 3. A rejection a later call can overwrite is not evidence about anything.

    The person's route out is not this queue: `POST /skills/mine` writes the skill directly, which
    is the same act with one fewer indirection and no pretence that the agent proposed it twice.
    """
    proposal = asyncio.run(store.propose(_proposal(actor=actor)))
    asyncio.run(
        store.decide(
            actor,
            "skill",
            "cold-quench",
            proposal.content_hash,
            accepted=False,
            decided_by=actor,
            reason="too narrow",
        )
    )

    second = asyncio.run(
        store.decide(
            actor,
            "skill",
            "cold-quench",
            proposal.content_hash,
            accepted=True,
            decided_by=actor,
            reason="changed my mind",
        )
    )

    assert second is not None
    assert (second.state, second.reason) == ("rejected", "too narrow"), (
        "a second decision replaced the first, so the trail no longer records what was decided"
    )


def test_a_proposal_is_one_persons(store: ProposalStore, actor: str) -> None:
    """Rule 4. Content identity is per actor, so one chemist cannot decide for another.

    Two people may independently be offered the same procedure — the proposer is a model reading
    their own separate work — and the tier an accepted one lands in is per person too.
    """
    mine = asyncio.run(store.propose(_proposal(actor=actor)))
    asyncio.run(
        store.decide(
            actor,
            "skill",
            "cold-quench",
            mine.content_hash,
            accepted=False,
            decided_by=actor,
            reason="not for me",
        )
    )

    theirs = asyncio.run(store.propose(_proposal(actor=f"{actor}-other")))

    assert theirs.state == "open", "one chemist's rejection decided another's proposal"
    assert asyncio.run(store.list_for(f"{actor}-other", states=["open"]))
    assert not asyncio.run(store.list_for(f"{actor}-other", states=["rejected"]))


def test_a_proposer_can_tell_a_fresh_proposal_from_a_repeat(
    store: ProposalStore, actor: str
) -> None:
    """The three outcomes `_arrival` distinguishes, which the counter labels and the tool reports.

    A queue's usefulness is the gap between `proposed` and `accepted`/`rejected`, and a repeat
    counted as a proposal reports a queue busier than it is — in the one series whose purpose is
    telling an operator whether anybody is reading it.
    """
    first = asyncio.run(store.propose(_proposal(actor=actor)))
    repeat = asyncio.run(store.propose(_proposal(actor=actor)))

    assert (first.state, repeat.state) == ("open", "open")
    assert first.content_hash == repeat.content_hash
    assert len(asyncio.run(store.list_for(actor))) == 1, "a repeat created a second row"


def test_the_backend_follows_the_session_store(monkeypatch: pytest.MonkeyPatch) -> None:
    """Chosen the way the plan-approval store chooses one, and for the same reason.

    A proposal authorizes a change to what the agent does for one person, and under
    `session_store="memory"` that person's whole context is a process — so a durable queue would
    outlive the thing it changes, and the two must not disagree about which backend is live.
    """
    monkeypatch.setattr(settings, "session_store", "postgres")
    assert isinstance(default_proposal_store(), PostgresProposalStore)

    monkeypatch.setattr(settings, "session_store", "memory")
    assert isinstance(default_proposal_store(), InMemoryProposalStore)
