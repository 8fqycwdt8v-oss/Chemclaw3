"""What the two BO writers are allowed to claim about *who* proposed an experiment.

`bo_campaigns.opened_by` and `bo_suggestions.actor` answer "who framed this decision space"
(`agent/leaver.py` retains them). The durable campaign reads `requested_by` from the run's memo,
set from the validated principal; the synchronous MCP tool reads the unauthenticated
`X-Chemclaw-Actor` header. So the synchronous path marks its name unverified, the durable path
does not, and an absent caller is recorded as absent.
`test_the_threat_model_this_module_states_is_the_one_its_manifest_declares` keeps the manifest's
auth mode in step.
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

    `bind_caller` is the entry point `connectors/server.py` uses per call, so this reproduces a
    forged header without a live HTTP transport.
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
    """A forged actor header never becomes the bare recorded identity.

    Asserting `!= FORGED_ACTOR` fails the moment the marking is removed, whatever shape a future
    marker takes.
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

    The marker removes only the false confidence. `CampaignThread` no longer carries `opened_by`,
    since a reader cannot tell marked from verified; the audit trail answers that.
    """
    campaign, suggestion = _recorded(store, _suggest_as(FORGED_ACTOR))

    assert suggestion.actor == f"unverified:{FORGED_ACTOR}"
    assert campaign.opened_by == f"unverified:{FORGED_ACTOR}"


def test_the_join_keys_are_not_marked(store: InMemoryCampaignStore) -> None:
    """Session and correlation ids pass through untouched: they are joins, not attribution.

    They let an auditor recover the validated actor from core's own trail.
    """
    campaign_id = _suggest_as(FORGED_ACTOR, session_id="sess-7", correlation_id="corr-9")
    _campaign, suggestion = _recorded(store, campaign_id)

    assert (suggestion.session_id, suggestion.correlation_id) == ("sess-7", "corr-9")


def test_an_absent_caller_is_recorded_as_absent_not_as_an_unverified_claim(
    store: InMemoryCampaignStore,
) -> None:
    """No header at all is "not recorded", and must not be dressed up as a claim nobody made."""
    campaign, suggestion = _recorded(store, _suggest_as(""))

    assert (campaign.opened_by, suggestion.actor) == ("", "")


def test_the_durable_path_records_its_validated_actor_unmarked(
    store: InMemoryCampaignStore,
) -> None:
    """The other half of the asymmetry, without which the marker would say nothing.

    The durable activity, driven directly with the memo's validated actor, writes it bare.
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
