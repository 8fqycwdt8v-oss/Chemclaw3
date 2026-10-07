"""The queue's rules, driven against both backends because a disagreement is the whole risk.

Every rule runs against the in-process backend (`session_store="memory"`) and the Postgres one
from one parametrised body:

1. An unchanged re-proposal cannot reopen a rejection (the key is the content).
2. A changed body supersedes an open sibling and never a decided one.
3. A decision is final.
4. A proposal is one person's.
5. A re-proposal of a superseded body is a proposal: `superseded` is not a decision, and leaving
   it there would strand the body where no route can move it.
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

    The table is append-only with no DELETE grant, so isolation comes from a fresh actor per test —
    content identity is per person by design (rule 4).
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

    The Postgres arm skips without a migrated database, and `tests/conftest.py` counts the skip.
    `migrated_db_or_skip` is a coroutine, so it is run with `asyncio.run`; un-awaited it would never
    skip.
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
    """Rule 2, both halves.

    Without the first, two open versions of one name render and a decision applies to both; without
    the second, superseding a decided version would erase the evidence rule 1 keeps.
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


def test_re_proposing_a_superseded_body_puts_it_back_where_a_decision_can_reach_it(
    store: ProposalStore, actor: str
) -> None:
    """Rule 5: re-proposing a superseded body puts it back where a decision can reach it.

    Propose V1, propose V2 (superseding V1), re-propose V1: the row must become open again, since
    `_DECIDE` only moves `open` rows. A revive is an arrival, so it also supersedes the open sibling
    (rule 2).
    """
    first = asyncio.run(store.propose(_proposal(actor=actor)))
    second = asyncio.run(store.propose(_proposal(_BODY.replace("cold.", "warm."), actor=actor)))
    stale = asyncio.run(store.one(actor, "skill", "cold-quench", first.content_hash))
    assert stale is not None and stale.state == "superseded", "the premise did not hold"

    again = asyncio.run(store.propose(_proposal(actor=actor)))

    assert again.state == "open", (
        "a re-proposed body stayed superseded, so the chemist's decision has nowhere to land"
    )
    assert again.content_hash == first.content_hash, "the revive returned some other version"
    displaced = asyncio.run(store.one(actor, "skill", "cold-quench", second.content_hash))
    assert displaced is not None and displaced.state == "superseded", (
        "the revived version did not displace the one that had superseded it"
    )
    assert [one.content_hash for one in asyncio.run(store.list_for(actor, states=["open"]))] == [
        first.content_hash
    ], "two open versions of one name: a reviewer cannot tell which a decision applies to"

    settled = asyncio.run(
        store.decide(
            actor,
            "skill",
            "cold-quench",
            first.content_hash,
            accepted=False,
            decided_by=actor,
            reason="still too narrow",
        )
    )
    assert settled is not None and settled.state == "rejected", (
        "`_DECIDE` is `AND state = 'open'`, so a revive that did not reach `open` is a revive "
        "that changed nothing a person can act on"
    )


def test_a_revive_cannot_reopen_a_decision(store: ProposalStore, actor: str) -> None:
    """Rule 5 stops exactly where rule 1 starts, and the two are one `WHERE` clause apart.

    `_REVIVE` carries `AND state = 'superseded'`; any wider clause would make a rejected body
    reopenable.
    """
    first = asyncio.run(store.propose(_proposal(actor=actor)))
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

    assert again.state == "rejected", "the revive reopened a decision"
    assert again.reason == "too narrow", "the reason a person gave did not survive the re-proposal"


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

    A repeat counted as a proposal would report the queue busier than it is.
    """
    first = asyncio.run(store.propose(_proposal(actor=actor)))
    repeat = asyncio.run(store.propose(_proposal(actor=actor)))

    assert (first.state, repeat.state) == ("open", "open")
    assert first.content_hash == repeat.content_hash
    assert len(asyncio.run(store.list_for(actor))) == 1, "a repeat created a second row"


def test_the_backend_follows_the_session_store(monkeypatch: pytest.MonkeyPatch) -> None:
    """The backend follows the session store, as the plan-approval store's does.

    Under `session_store="memory"` a person's whole context is a process, so a durable queue would
    outlive what it changes.
    """
    monkeypatch.setattr(settings, "session_store", "postgres")
    assert isinstance(default_proposal_store(), PostgresProposalStore)

    monkeypatch.setattr(settings, "session_store", "memory")
    assert isinstance(default_proposal_store(), InMemoryProposalStore)


async def test_the_counter_distinguishes_the_four_arrivals_it_declares(
    store: ProposalStore, actor: str
) -> None:
    """`_arrival`'s four outcomes reach the exposition, not just the tool's prose.

    `chemclaw_behaviour_proposals_total` tells an operator whether anybody reads the queue, so a
    repeat or a revive booked as a fresh proposal reports it busier than it is. `revived` is the
    fourth outcome.
    """
    from chemclaw.core.metrics import METRICS

    def counted() -> dict[str, float]:
        rendered = METRICS.render()
        return {
            outcome: float(line.rsplit(" ", 1)[1])
            for line in rendered.splitlines()
            if line.startswith("chemclaw_behaviour_proposals_total{")
            for outcome in [line.split('outcome="')[1].split('"')[0]]
        }

    before = counted()
    proposal = _proposal(actor=actor)
    newer = _proposal(_BODY.replace("cold.", "warm."), actor=actor)

    await store.propose(proposal)
    await store.propose(proposal)
    # A changed body supersedes the first, and re-proposing the first revives it — which then
    # supersedes the changed one, so this pair books two `superseded` as well as the two arrivals.
    await store.propose(newer)
    await store.propose(proposal)
    await store.decide(
        actor,
        "skill",
        "cold-quench",
        proposal.content_hash,
        accepted=False,
        decided_by=actor,
        reason="too narrow",
    )
    await store.propose(proposal)

    after = counted()
    moved = {key: after.get(key, 0.0) - before.get(key, 0.0) for key in after}

    assert moved.get("proposed") == 2.0, "a repeat or a settled re-propose booked as a proposal"
    assert moved.get("already_open") == 1.0, "the repeat did not book as a repeat"
    assert moved.get("revived") == 1.0, (
        "re-proposing a superseded body booked as a repeat, so the queue reads as being repeated "
        "at while it is being refilled"
    )
    assert moved.get("already_decided") == 1.0, "re-proposing into a rejection booked as fresh"
    assert moved.get("superseded") == 2.0, (
        "a revive that arrives must sweep the sibling it replaces"
    )


def test_a_listing_is_bounded_by_the_setting_and_keeps_the_newest(
    store: ProposalStore, actor: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`list_for` stops at `agent_proposals_list_max`, newest first, on both backends."""
    monkeypatch.setattr(settings, "agent_proposals_list_max", 2)
    names = ["first", "second", "third"]
    for name in names:
        asyncio.run(
            store.propose(_proposal(_BODY.replace("cold-quench", name), actor=actor, name=name))
        )

    listed = asyncio.run(store.list_for(actor))
    assert len(listed) == 2
    assert {one.name for one in listed} <= set(names)
    assert "first" not in {one.name for one in listed}, "the bound dropped a newer row"
