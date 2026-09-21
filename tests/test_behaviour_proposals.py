"""The queue's four rules, driven against both backends because a disagreement is the whole risk.

`agent/behaviour_proposals.py` says a control with two implementations that disagree about whether
a rejection can be reopened is a control nobody can reason about. That is not a claim a reader can
check, so every rule here runs against **both** — the in-process backend a `session_store="memory"`
deployment gets (the CLI is one) and the Postgres one a fleet gets — from one parametrised body, so
a rule cannot be added to one and forgotten in the other.

The five rules, each stated as the thing that would be false without it:

1. **An unchanged re-proposal cannot reopen a rejection.** Without it the model retries until a
   person gives in, which is the whole reason the key is the content rather than the name.
2. **A changed body supersedes an open sibling and never a decided one.** Retired `note_proposals`
   shipped without the first half: migration 058 records a queue rendering versions nothing would
   deliver and one decision then applied to both.
3. **A decision is final.** A rejection that a later call can overwrite is not evidence.
4. **A proposal is one person's.** Two chemists may be offered the same procedure, and one
   rejecting it must not decide for the other.
5. **A re-proposal of a superseded body is a proposal.** Rule 1's idempotence is over a *decision*;
   `superseded` is a state this system produced and nobody answered. Applying rule 1 to it left the
   body in the one state no route can move — invisible to `GET /proposals?state=open`, 409 from
   `POST /proposals/{kind}/{name}`, and reported to the model as waiting for a chemist to decide.
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


def test_re_proposing_a_superseded_body_puts_it_back_where_a_decision_can_reach_it(
    store: ProposalStore, actor: str
) -> None:
    """Rule 5, and the state it was stuck in had no exit at all.

    Driven as the defect was: propose V1, propose V2 (which supersedes V1), re-propose V1. Before
    the revive the row stayed `superseded` — `_arrival` branched only on `stored.decided` and
    `superseded` is deliberately not a decision, so the call booked `already_open` for a row that
    `list_for(states=["open"])` does not return and that `POST /proposals/{kind}/{name}` answers
    409 for. `_DECIDE` is `AND state = 'open'`, so nothing else could move it either: a chemist's
    decision had nowhere to land and the model was told it was waiting for one.

    The second half is rule 2 holding *through* the revive. A revive is an arrival, so it sweeps
    the sibling an insert would — otherwise this fix trades one bad state for the two-open-rows
    state migration 058 exists to describe.
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

    `_REVIVE` carries `AND state = 'superseded'` for this: widening it to "any state that is not
    open" would have made a rejected body reopenable by re-proposing it, which is the behaviour the
    content key exists to prevent. Driven rather than argued, because the two clauses are adjacent
    and a reader cannot tell a deliberate narrow one from a typo.
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


async def test_the_counter_distinguishes_the_four_arrivals_it_declares(
    store: ProposalStore, actor: str
) -> None:
    """`_arrival`'s four outcomes reach the exposition, not just the tool's prose.

    `test_a_proposer_can_tell_a_fresh_proposal_from_a_repeat`'s docstring says "the three outcomes
    `_arrival` distinguishes, **which the counter labels** and the tool reports", and only the
    second half was held: `_arrival` returning `"proposed"` unconditionally, and `_book` returning
    without incrementing at all, both left this file and `tests/test_proposal_tools.py` at 19
    passed. The one thing holding `chemclaw_behaviour_proposals_total` anywhere was
    `test_every_declared_metric_is_named_somewhere_in_the_source` — the string's presence in a file.

    That matters because this series' whole purpose is telling an operator whether anybody is
    reading the queue, and a repeat booked as a fresh proposal reports it busier than it is — the
    reassuring direction, and the one `_book`'s own docstring says it exists to avoid.

    **`revived` is the fourth and it is the case that motivated counting four.** It used to book
    `already_open` — the queue reported as being repeated at while it was in fact being refilled,
    which is the same reassuring direction one state further on.
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
