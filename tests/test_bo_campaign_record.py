"""A BO campaign is an entity, and the inline suggestion path stops discarding its own framing.

`suggest_next_experiment` is the path the conversational agent actually uses. It took a decision
space and a run history, fitted a surrogate, returned candidates, and wrote nothing — so the
expensive part of an optimization, a chemist and an agent jointly framing the problem out of
scattered history, was rebuilt from scratch on every question. Meanwhile
`knowledge/optimization-campaign/` notes came from retrospective DRFP clustering with no identity
link to any BO run: a word for a campaign, with no object behind it.

The load-bearing choice is that **the campaign is identified by its problem**, not minted per call.
That is what turns a sequence of turns into one campaign's history without anyone having to start
one, and it is what these tests mostly pin.

`InMemoryCampaignStore` is exercised directly because it is the backend a `session_store="memory"`
deployment really gets, not a double — so this is production behaviour on that path, and the
contract its Postgres sibling must match.
"""

import asyncio
from typing import Any

import pytest

from chemclaw.science.bo.campaign_record import (
    Campaign,
    InMemoryCampaignStore,
    Suggestion,
    campaign_id_for,
    campaign_store,
    record_suggestion,
)
from chemclaw.science.bo.problem import (
    CategoricalParameter,
    ContinuousParameter,
    Objective,
    Observation,
    OptimizationProblem,
)


def _problem(*, upper: float = 100.0, ligands: tuple[str, ...] = ("PPh3", "dppf")) -> Any:
    """A small two-parameter optimization: one continuous, one categorical over molecules."""
    return OptimizationProblem(
        parameters=[
            ContinuousParameter(name="temperature", lower=20.0, upper=upper),
            CategoricalParameter(
                name="ligand",
                categories=list(ligands),
                structures=dict.fromkeys(ligands, "c1ccccc1"),
            ),
        ],
        objective=Objective(name="yield", direction="maximize"),
    )


def _run(awaitable: Any) -> Any:
    """Drive one store coroutine from a sync test (the in-memory store holds no loop state)."""
    return asyncio.run(awaitable)


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> InMemoryCampaignStore:
    """One fresh in-memory store, shared by the recorder and the reader for the whole test."""
    fresh = InMemoryCampaignStore()
    monkeypatch.setattr("chemclaw.science.bo.campaign_record.campaign_store", lambda: fresh)
    campaign_store.cache_clear()
    return fresh


# --- the identity, which is the whole design ----------------------------------------------


def test_the_same_problem_is_the_same_campaign() -> None:
    """Asking twice about one optimization must reach one campaign, or nothing accumulates."""
    assert campaign_id_for(_problem()) == campaign_id_for(_problem())


def test_a_different_decision_space_is_a_different_campaign() -> None:
    """Widening the range or swapping a ligand is a different optimization, not the same one."""
    assert campaign_id_for(_problem()) != campaign_id_for(_problem(upper=140.0))
    assert campaign_id_for(_problem()) != campaign_id_for(_problem(ligands=("PPh3", "XPhos")))


def test_descriptors_do_not_change_a_campaign_s_identity() -> None:
    """A recomputed or upgraded descriptor must not fork the campaign.

    Descriptors are computed *from* the structures, so they are a consequence of the space rather
    than part of it. If they counted, a cache miss recomputing them — or a calculator upgrade
    shifting one in the sixth decimal — would silently start a second campaign over the same
    problem, and the history would split in two with nothing to say why.
    """
    bare = _problem()
    featurized = bare.model_copy(
        update={
            "parameters": [
                bare.parameters[0],
                bare.parameters[1].model_copy(
                    update={"descriptors": {"PPh3": {"homo_ev": -6.1}, "dppf": {"homo_ev": -5.8}}}
                ),
            ]
        }
    )
    assert campaign_id_for(featurized) == campaign_id_for(bare)


# --- what a campaign accumulates ------------------------------------------------------------


def test_three_turns_on_one_problem_build_one_campaign_with_three_suggestions(
    store: InMemoryCampaignStore,
) -> None:
    """The behaviour the entity exists for: the sequence of proposals is the campaign's history."""
    problem = _problem()
    for round_index in range(3):
        history = [
            Observation(params={"temperature": 40.0 + round_index, "ligand": "PPh3"}, value=55.0)
        ]
        campaign_id = _run(
            record_suggestion(
                problem=problem,
                candidates=[],
                observations=history,
                calc_refs=["xtb@v1:aaa:bbb"],
                provenance=("chemist-a", "session-1", "corr-1"),
            )
        )

    assert _run(store.read_campaign(campaign_id)) is not None
    suggestions = _run(store.suggestions_for(campaign_id, 10))
    assert len(suggestions) == 3
    # Newest first, and each carries the evidence it rested on — the same candidate proposed from
    # three runs and from thirty means different things.
    assert [s.observations[0].params["temperature"] for s in suggestions] == [42.0, 41.0, 40.0]


def test_a_suggestion_records_who_asked_and_in_which_conversation(
    store: InMemoryCampaignStore,
) -> None:
    """The join the advisory `X-Chemclaw-*` headers were sent for (D-141).

    Without it a persisted suggestion is a row nobody can trace to a chemist or a turn, which is
    most of what made recording it worth doing.
    """
    campaign_id = _run(
        record_suggestion(
            problem=_problem(),
            candidates=[],
            observations=[],
            calc_refs=["xtb@v1:aaa:bbb"],
            provenance=("chemist-a", "session-7", "corr-9"),
        )
    )

    [suggestion] = _run(store.suggestions_for(campaign_id, 10))
    assert (suggestion.actor, suggestion.session_id, suggestion.correlation_id) == (
        "chemist-a",
        "session-7",
        "corr-9",
    )
    assert suggestion.calc_refs == ["xtb@v1:aaa:bbb"]


def test_a_later_asker_does_not_become_the_campaign_s_author(
    store: InMemoryCampaignStore,
) -> None:
    """Whoever framed the campaign framed it; `last_asked_at` is what tracks activity."""
    problem = _problem()
    _run(store.upsert_campaign(_campaign(problem, "chemist-a")))
    first = _run(store.read_campaign(campaign_id_for(problem)))
    _run(store.upsert_campaign(_campaign(problem, "chemist-b")))
    second = _run(store.read_campaign(campaign_id_for(problem)))

    assert first is not None and second is not None
    assert second.opened_by == "chemist-a"
    assert second.created_at == first.created_at
    assert second.last_asked_at is not None and first.last_asked_at is not None
    assert second.last_asked_at >= first.last_asked_at


def test_recording_never_costs_the_suggestion(monkeypatch: pytest.MonkeyPatch) -> None:
    """The chemist asked for candidates; a database blip must not turn that into an error.

    The same trade `agent/audit.py` and `kg/proposal.py` make. The campaign id is a pure function
    of the problem, so it is still the right handle to return on the turn where the write failed.
    """

    class BrokenStore(InMemoryCampaignStore):
        async def upsert_campaign(self, campaign: Campaign) -> None:
            raise ConnectionError("database down")

    monkeypatch.setattr("chemclaw.science.bo.campaign_record.campaign_store", BrokenStore)

    returned = _run(
        record_suggestion(
            problem=_problem(),
            candidates=[],
            observations=[],
            calc_refs=[],
            provenance=("chemist-a", "", ""),
        )
    )
    assert returned == campaign_id_for(_problem())


def _campaign(problem: Any, opened_by: str) -> Campaign:
    """The campaign row `record_suggestion` would write for `problem`."""
    return Campaign(
        campaign_id=campaign_id_for(problem),
        objective=problem.objective.name,
        direction=problem.objective.direction,
        problem=problem.model_dump(mode="json"),
        opened_by=opened_by,
    )


def test_suggestions_are_append_only(store: InMemoryCampaignStore) -> None:
    """A second ask with more data is a new proposal, not an edit of the old one.

    Overwriting would destroy the only record of what was proposed *before* the latest data
    arrived, which is exactly the comparison a campaign's history is for.
    """
    problem = _problem()
    _run(store.upsert_campaign(_campaign(problem, "chemist-a")))
    identical = Suggestion(campaign_id=campaign_id_for(problem), candidates=[], observations=[])

    first = _run(store.add_suggestion(identical))
    second = _run(store.add_suggestion(identical))

    assert first != second
    assert len(_run(store.suggestions_for(campaign_id_for(problem), 10))) == 2
