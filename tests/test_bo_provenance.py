"""What the two BO writers are allowed to claim about *who* proposed an experiment.

`bo_campaigns.opened_by` and `bo_suggestions.actor` are the GxP answer to "who framed this
decision space" — `agent/leaver.py` retains them for exactly that reason, where it erases the
conversation around them. Two code paths write those columns, and until this module they wrote
them with equal confidence from unequal evidence:

- The **durable** campaign reads `requested_by` off the run's Temporal memo, which core sets from
  the validated front-door principal (`connectors/bo/workflows.py`, `connectors/bo/activities.py`).
  Nothing attacker-writable is between that value and an authenticated login.
- The **synchronous** MCP tool reads `X-Chemclaw-Actor` off the serving HTTP request. That header
  is unauthenticated by design (`connectors/caller.py`), and this bundle's manifest declares
  `auth: mode: none`, so the pod authenticates nobody at all — anything that can open a socket to
  it could name any chemist it liked, and the row it produced was byte-indistinguishable from the
  durable path's.

So these tests pin the asymmetry rather than a string: the synchronous path marks the name it could
not verify, the durable path does not, and an absent caller is recorded as absent rather than as an
unverified claim. Reverting the marker makes the first test below fail on the value it asserts
*against* — the bare forged oid — which is the only shape of this test that can catch the
regression.
"""

import asyncio
from typing import Any

import pytest

from chemclaw.connectors.bo.server.tools import suggest_next_experiment
from chemclaw.connectors.caller import bind_caller, reset_caller
from chemclaw.science.bo.campaign_record import (
    Campaign,
    InMemoryCampaignStore,
    Suggestion,
    campaign_store,
)
from chemclaw.science.bo.problem import (
    Candidate,
    CategoricalParameter,
    ContinuousParameter,
    Objective,
    Observation,
    OptimizationProblem,
)

# The oid an attacker would put on the header: a real chemist's, so the forged row would be filed
# under someone who exists and never asked for anything.
FORGED_ACTOR = "victim-oid-0000-1111"


def _problem() -> OptimizationProblem:
    """Maximize yield over a temperature range and a choice of two solvents."""
    return OptimizationProblem(
        parameters=[
            ContinuousParameter(name="temperature", lower=20.0, upper=120.0),
            CategoricalParameter(name="solvent", categories=["THF", "toluene"]),
        ],
        objectives=[Objective(name="yield", direction="maximize")],
    )


def _run(awaitable: Any) -> Any:
    """Drive one coroutine from a sync test (the in-memory store holds no loop state)."""
    return asyncio.run(awaitable)


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> InMemoryCampaignStore:
    """One fresh in-memory store, shared by the tool and the reader for the whole test."""
    fresh = InMemoryCampaignStore()
    monkeypatch.setattr("chemclaw.science.bo.campaign_record.campaign_store", lambda: fresh)
    campaign_store.cache_clear()
    return fresh


def _suggest_as(actor: str, session_id: str = "", correlation_id: str = "") -> str:
    """Call the synchronous tool with `actor` bound exactly as the request middleware would.

    `bind_caller` is the same entry point `connectors/server.py` uses per tool call, so binding it
    here reproduces a forged header without needing a live HTTP transport: the header is the *only*
    thing that reaches those contextvars.
    """
    tokens = bind_caller(actor, session_id, correlation_id)
    try:
        return str(_run(suggest_next_experiment(_problem(), None, count=1)).campaign_id)
    finally:
        reset_caller(tokens)


def _recorded(store: InMemoryCampaignStore, campaign_id: str) -> tuple[Campaign, Suggestion]:
    """The campaign row and its one suggestion row, as an auditor would read them back."""
    campaign = _run(store.read_campaign(campaign_id))
    assert campaign is not None, "the tool must have written the campaign it returned an id for"
    [suggestion] = _run(store.suggestions_for(campaign_id, 10))
    return campaign, suggestion


def test_a_forged_actor_header_never_becomes_the_bare_recorded_identity(
    store: InMemoryCampaignStore,
) -> None:
    """The regression this module exists for, asserted against the value that used to be written.

    Measured before the fix: binding `X-Chemclaw-Actor: victim-oid-0000-1111` and calling the tool
    put that exact string into both columns. Nothing in the row said it had never been
    authenticated, so an auditor reading `bo_suggestions` could not tell a forged proposal from a
    real one — and `leaver.py` retains those columns precisely because they are supposed to answer
    that question.

    Asserting `!= FORGED_ACTOR` rather than `== "unverified:..."` is deliberate: it is the half that
    fails the moment the marking is removed, whatever shape a future marker takes.
    """
    campaign, suggestion = _recorded(store, _suggest_as(FORGED_ACTOR))

    assert suggestion.actor != FORGED_ACTOR, (
        "an unauthenticated header was written into bo_suggestions.actor as though the identity "
        "had been verified"
    )
    assert campaign.opened_by != FORGED_ACTOR, (
        "an unauthenticated header was written into bo_campaigns.opened_by as though the identity "
        "had been verified"
    )


def test_the_unverified_marker_still_carries_the_name_that_was_claimed(
    store: InMemoryCampaignStore,
) -> None:
    """Marked, not discarded — the claim is evidence even when the claimant is not authenticated.

    Dropping the actor entirely would have closed the same hole and cost the record the one thing
    it is for: `resume_campaign` returns `opened_by` so a later turn can say whose campaign this is,
    and a column that is always empty answers nobody. The marker keeps the trail and removes only
    the false confidence.
    """
    campaign, suggestion = _recorded(store, _suggest_as(FORGED_ACTOR))

    assert suggestion.actor == f"unverified:{FORGED_ACTOR}"
    assert campaign.opened_by == f"unverified:{FORGED_ACTOR}"


def test_the_join_keys_are_not_marked(store: InMemoryCampaignStore) -> None:
    """Session and correlation ids pass through untouched: they are joins, not attribution.

    They are how an auditor recovers the *validated* actor from core's own audit trail, which is
    the recovery that makes marking the actor affordable in the first place. Marking them would
    break the join and protect nothing.
    """
    campaign_id = _suggest_as(FORGED_ACTOR, session_id="sess-7", correlation_id="corr-9")
    _campaign, suggestion = _recorded(store, campaign_id)

    assert (suggestion.session_id, suggestion.correlation_id) == ("sess-7", "corr-9")


def test_an_absent_caller_is_recorded_as_absent_not_as_an_unverified_claim(
    store: InMemoryCampaignStore,
) -> None:
    """No header at all is "not recorded", and must not be dressed up as a claim nobody made.

    A tool exercised directly — a test, the CLI, a dev stack — has genuinely no caller. Stamping
    `unverified:` onto the empty string would invent an assertion, which is the same class of
    dishonesty as certifying a forged one.
    """
    campaign, suggestion = _recorded(store, _suggest_as(""))

    assert (campaign.opened_by, suggestion.actor) == ("", "")


def test_the_durable_path_records_its_validated_actor_unmarked(
    store: InMemoryCampaignStore,
) -> None:
    """The other half of the asymmetry, without which the marker would say nothing.

    A marker only carries information if the verified case is distinguishable from it. The durable
    activity is driven directly (it is a plain coroutine) with the actor the workflow reads off the
    run's memo — core's validated principal — and that one is written bare, so the column now says
    which writer could vouch for the name it holds.
    """
    from chemclaw.connectors.bo.activities import record_campaign_run

    problem = _problem()
    campaign_id = _run(
        record_campaign_run(
            problem,
            [Candidate(params={"temperature": 72.0, "solvent": "THF"})],
            [Observation(params={"temperature": 70.0, "solvent": "THF"}, value=0.81)],
            "alice@example.com",
            "corr-1",
            "bo-start_optimization_campaign-abc",
        )
    )
    campaign, suggestion = _recorded(store, campaign_id)

    assert (campaign.opened_by, suggestion.actor) == ("alice@example.com", "alice@example.com")
    assert not suggestion.actor.startswith("unverified:"), (
        "the memo-derived actor crossed no attacker-writable surface and must not be marked"
    )
