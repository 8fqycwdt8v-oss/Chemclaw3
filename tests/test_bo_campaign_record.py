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
    _IDENTIFYING_EXCLUSIONS,
    _SPACE_FIELDS,
    Campaign,
    InMemoryCampaignStore,
    Suggestion,
    campaign_id_for,
    campaign_store,
    read_campaign_thread,
    record_suggestion,
)
from chemclaw.science.bo.problem import (
    Candidate,
    CategoricalParameter,
    ContinuousParameter,
    ExcludeConstraint,
    LinearConstraint,
    Objective,
    Observation,
    OptimizationProblem,
    Parameter,
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
        objectives=[Objective(name="yield", direction="maximize")],
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


def _ordering_problem(
    parameters: list[ContinuousParameter | CategoricalParameter],
) -> OptimizationProblem:
    """One problem over exactly `parameters`, in the order given."""
    return OptimizationProblem(
        parameters=parameters, objectives=[Objective(name="yield", direction="maximize")]
    )


def test_the_order_the_space_was_written_in_does_not_fork_the_campaign() -> None:
    """The same decision space is one campaign however the caller happened to list it.

    Constraint *terms* were already canonicalized against precisely this failure (`_canonical`),
    and the two lists beside them were not: `[temperature, solvent]` and `[solvent, temperature]`
    hashed to two ids, and reversing the categories gave a third — three empty histories over one
    optimization, which is the fork `read_campaign_thread` exists to prevent.

    Parameter order is provably inert: measured with a fixed seed, `[T, E, S]` and `[S, E, T]`
    propose byte-identical candidates. Category order moves the acquisition optimizer slightly
    (a bare `CategoricalInput` is ordinally encoded), which is why only the *identity* payload is
    sorted — the problem the surrogate sees keeps the caller's order.
    """
    temperature = ContinuousParameter(name="temperature", lower=20.0, upper=120.0)
    solvent = CategoricalParameter(name="solvent", categories=["THF", "toluene"])
    reversed_solvent = CategoricalParameter(name="solvent", categories=["toluene", "THF"])

    canonical = campaign_id_for(_ordering_problem([solvent, temperature]))
    assert campaign_id_for(_ordering_problem([temperature, solvent])) == canonical
    assert campaign_id_for(_ordering_problem([temperature, reversed_solvent])) == canonical
    # And it must still tell two genuinely different spaces apart.
    assert (
        campaign_id_for(
            _ordering_problem(
                [CategoricalParameter(name="solvent", categories=["THF", "DMF"]), temperature]
            )
        )
        != canonical
    )


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
    blank = Suggestion(campaign_id=campaign_id_for(problem), candidates=[], observations=[])
    _run(store.record(_campaign(problem, "chemist-a"), blank))
    first = _run(store.read_campaign(campaign_id_for(problem)))
    _run(store.record(_campaign(problem, "chemist-b"), blank))
    second = _run(store.read_campaign(campaign_id_for(problem)))

    assert first is not None and second is not None
    assert second.opened_by == "chemist-a"
    assert second.created_at == first.created_at
    assert second.last_asked_at is not None and first.last_asked_at is not None
    assert second.last_asked_at > first.last_asked_at


def test_recording_never_costs_the_suggestion(monkeypatch: pytest.MonkeyPatch) -> None:
    """The chemist asked for candidates; a database blip must not turn that into an error.

    The same trade `agent/audit.py` and `kg/proposal.py` make. The campaign id is a pure function
    of the problem, so it is still the right handle to return on the turn where the write failed.
    """

    class BrokenStore(InMemoryCampaignStore):
        async def record(self, campaign: Campaign, suggestion: Suggestion) -> int:
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
    identical = Suggestion(campaign_id=campaign_id_for(problem), candidates=[], observations=[])

    first = _run(store.record(_campaign(problem, "chemist-a"), identical))
    second = _run(store.record(_campaign(problem, "chemist-a"), identical))

    assert first != second
    assert len(_run(store.suggestions_for(campaign_id_for(problem), 10))) == 2


# --- reading the campaign back, which is what makes writing it worth anything -----------------


def test_a_later_session_recovers_the_space_and_the_runs_it_never_saw(
    store: InMemoryCampaignStore,
) -> None:
    """User story 3.2: ask, observe, ask again — across sessions, not across one transcript.

    The record was written on every suggestion and had no reader, so turn N+1 could recover turn
    N's observations only from the chat transcript. This is the whole loop: one turn records, a
    second turn holding *nothing but the id* gets the decision space and the runs back.
    """
    problem = _problem()
    observations = [
        Observation(params={"temperature": 40.0, "ligand": "PPh3"}, value=55.0),
        Observation(params={"temperature": 80.0, "ligand": "dppf"}, value=78.0),
    ]
    campaign_id = _run(
        record_suggestion(
            problem=problem,
            candidates=[Candidate(params={"temperature": 95.0, "ligand": "dppf"})],
            observations=observations,
            calc_refs=[],
            provenance=("chemist-a", "session-1", "corr-1"),
        )
    )

    thread = _run(read_campaign_thread(campaign_id))

    assert thread.problem == problem
    assert [o.value for o in thread.observations] == [55.0, 78.0]
    assert [c.params["temperature"] for c in thread.last_candidates] == [95.0]
    assert (thread.objective, thread.direction) == ("yield", "maximize")
    assert thread.opened_by == "chemist-a"


def test_resuming_returns_the_latest_turn_s_evidence_not_the_first(
    store: InMemoryCampaignStore,
) -> None:
    """A resumed campaign must continue from what is known now, not from where it started.

    Each turn passes the campaign's whole history, so the newest suggestion holds all of it —
    reading the oldest, or merging every turn's list, would resume from stale or duplicated runs.
    """
    problem = _problem()
    for count in (1, 2, 3):
        _run(
            record_suggestion(
                problem=problem,
                candidates=[],
                observations=[
                    Observation(params={"temperature": 40.0 + i, "ligand": "PPh3"}, value=50.0 + i)
                    for i in range(count)
                ],
                calc_refs=[],
                provenance=("chemist-a", "session-1", "corr-1"),
            )
        )

    thread = _run(read_campaign_thread(campaign_id_for(problem)))

    assert [o.value for o in thread.observations] == [50.0, 51.0, 52.0]


def test_an_unknown_id_says_the_space_changed_rather_than_answering_from_nothing(
    store: InMemoryCampaignStore,
) -> None:
    """The failure mode that decided the design: a hashed id cannot conflict, only miss.

    A changed decision space yields a *different* id, so an unresolvable id means "this is a new
    campaign" — not "your history is lost". Returning an empty thread would answer a question about
    a real campaign with silence, and merging into a suggestion call would seed a new campaign with
    another one's runs invisibly.
    """
    _run(
        record_suggestion(
            problem=_problem(),
            candidates=[],
            observations=[],
            calc_refs=[],
            provenance=("chemist-a", "", ""),
        )
    )
    widened = campaign_id_for(_problem(upper=140.0))

    with pytest.raises(ValueError, match="decision space"):
        _run(read_campaign_thread(widened))


def test_the_bo_connector_serves_and_declares_resuming(store: InMemoryCampaignStore) -> None:
    """Reachability is the item: the store had both backends, a migration, and no caller at all.

    Both halves, because either alone leaves it unreachable — a tool the MCP server serves but the
    manifest does not allow-list is never advertised to the agent, and a manifest entry with no
    tool behind it fails the first time a chemist asks.
    """
    from chemclaw.connectors.bo.server.tools import resume_campaign, server
    from chemclaw.connectors.registry import discovered

    campaign_id = _run(
        record_suggestion(
            problem=_problem(),
            candidates=[],
            observations=[Observation(params={"temperature": 40.0, "ligand": "PPh3"}, value=55.0)],
            calc_refs=[],
            provenance=("chemist-a", "", ""),
        )
    )

    thread = _run(resume_campaign(campaign_id))
    assert [o.value for o in thread.observations] == [55.0]

    assert "resume_campaign" in {tool.name for tool in _run(server.list_tools())}
    endpoint = discovered()["bo"][1].endpoint
    # The `bo` bundle declares an endpoint; asserting it rather than narrowing with a cast keeps
    # the failure legible if a future manifest drops it.
    assert endpoint is not None
    assert "resume_campaign" in endpoint.tools
    assert "resume_campaign" in endpoint.read_only


# --- the campaign-id compatibility pins (W3) ---------------------------------------------------

# The ids these three shapes hash to. A change that moves one does not break a test somewhere — it
# tells every chemist with a running campaign that their campaign is new, silently, because
# `read_campaign_thread` cannot find a row it never wrote. So each move is a decision, recorded.
#
# **They moved once, on purpose**: `campaign_id_for` now canonicalizes the parameter and category
# order (D-2026-08-08-a-partial-answer-must-say-so), and all three of these shapes happen to be
# written unsorted. Each landed *on the id its sorted spelling already carried* — the values below
# were captured from the pre-canonicalization code by hashing each shape rewritten in sorted order,
# so nothing new was minted: the unsorted spelling joined the sorted one's row. That is also the
# pin that an already-sorted campaign keeps its id, which is what bounds the re-partition.
_PRE_CANONICALIZATION_IDS = {
    "continuous-only": "campaign-6958b7edaa261c83",
    "mixed": "campaign-55e5f929fe83a9a5",
    "with-structures": "campaign-109f34eac28892ab",
}
_BASELINE_IDS = {
    "continuous-only": "campaign-a97f5dd910a2cc79",
    "mixed": "campaign-acfb471df76f2863",
    "with-structures": "campaign-59d74ed90e64b3f2",
}


def _baseline_problems() -> dict[str, OptimizationProblem]:
    """The three shapes M-2 hashed, rebuilt exactly."""
    return {
        "continuous-only": OptimizationProblem(
            parameters=[
                ContinuousParameter(name="temperature", lower=20.0, upper=120.0),
                ContinuousParameter(name="equiv", lower=1.0, upper=3.0),
            ],
            objectives=[Objective(name="yield", direction="maximize")],
        ),
        "mixed": OptimizationProblem(
            parameters=[
                ContinuousParameter(name="temperature", lower=20.0, upper=120.0),
                CategoricalParameter(name="solvent", categories=["THF", "toluene"]),
            ],
            objectives=[Objective(name="yield", direction="maximize")],
        ),
        "with-structures": OptimizationProblem(
            parameters=[
                CategoricalParameter(
                    name="ligand",
                    categories=["PPh3", "PCy3"],
                    structures={"PPh3": "c1ccccc1P(c1ccccc1)c1ccccc1", "PCy3": "C1CCCCC1"},
                )
            ],
            objectives=[Objective(name="impurity", direction="minimize")],
        ),
    }


def test_a_single_objective_problem_keeps_the_id_it_had_before_the_migration() -> None:
    """The hard-coded ids are the whole safety net for `objectives` and for the allowlist."""
    for label, problem in _baseline_problems().items():
        assert campaign_id_for(problem) == _BASELINE_IDS[label], label


def test_canonicalization_moved_each_legacy_id_onto_its_sorted_twin() -> None:
    """The one deliberate id move, pinned in both directions so it can never happen quietly.

    Each shape above is written unsorted, so ordering canonicalization had to move it. What is
    asserted here is *where*: onto the id the same space already carried when written in sorted
    order, so an already-sorted campaign keeps its row and its unsorted twin merges into it. Rows
    written under the pre-canonicalization ids are orphaned — a one-time cost, recorded in
    `BACKLOG.md`, against a fork that would otherwise recur on every re-declaration.
    """
    for label, problem in _baseline_problems().items():
        rewritten = [
            p.model_copy(update={"categories": sorted(p.categories)})
            if isinstance(p, CategoricalParameter)
            else p
            for p in problem.parameters
        ]
        sorted_spelling = problem.model_copy(
            update={"parameters": sorted(rewritten, key=lambda p: p.name)}
        )
        assert campaign_id_for(sorted_spelling) == _BASELINE_IDS[label], label
        assert _BASELINE_IDS[label] != _PRE_CANONICALIZATION_IDS[label], label


def test_the_legacy_spelling_hashes_to_the_same_id_as_the_new_one() -> None:
    """A row on disk says `objective`; the problem in memory says `objectives`. One campaign."""
    for label, problem in _baseline_problems().items():
        legacy = problem.model_dump(mode="json")
        legacy["objective"] = legacy.pop("objectives")[0]
        assert campaign_id_for(OptimizationProblem.model_validate(legacy)) == _BASELINE_IDS[label]


def test_a_legacy_payload_validates_and_round_trips() -> None:
    """Permanent compatibility, not a migration window — this shape is in every existing row."""
    legacy = {
        "parameters": [{"kind": "continuous", "name": "t", "lower": 0.0, "upper": 1.0}],
        "objective": {"name": "yield", "direction": "maximize"},
    }
    problem = OptimizationProblem.model_validate(legacy)
    assert [objective.name for objective in problem.objectives] == ["yield"]
    assert problem.objective.direction == "maximize"
    # The wire shape going back out is the new one; the property is not serialized.
    dumped = problem.model_dump(mode="json")
    assert "objectives" in dumped
    assert "objective" not in dumped


def test_giving_both_spellings_is_refused() -> None:
    """Two answers to "which objectives" is a caller error, not a compatibility case."""
    with pytest.raises(ValueError, match="not both"):
        OptimizationProblem.model_validate(
            {
                "parameters": [{"kind": "continuous", "name": "t", "lower": 0.0, "upper": 1.0}],
                "objective": {"name": "yield"},
                "objectives": [{"name": "yield"}],
            }
        )


def test_a_second_objective_is_a_different_campaign() -> None:
    """Adding an objective changes the question, so it must not join the old campaign's history."""
    single = _baseline_problems()["mixed"]
    both = single.model_copy(
        update={
            "objectives": [*single.objectives, Objective(name="impurity", direction="minimize")]
        }
    )
    assert campaign_id_for(both) != campaign_id_for(single)


# --- what identifies a decision space (review follow-up) ----------------------------------------


def test_caller_supplied_descriptors_identify_the_space() -> None:
    """Three different feature spaces used to collide on one campaign id.

    `descriptors` was excluded outright, on the reasoning that they are computed *from*
    `structures`. That holds only when structures are set. With none, the caller stated the
    descriptors directly and they are the **only** statement of what the surrogate sees — so a bare
    categorical, one featurized on `{A: 1, B: 2}` and one on `{A: 99, B: -99}` all hashed alike,
    and `record_suggestion`'s upsert let one overwrite another's decision space on the shared row.
    """
    objectives = [Objective(name="yield", direction="maximize")]
    ids = {
        campaign_id_for(
            OptimizationProblem(
                parameters=[
                    CategoricalParameter(name="lig", categories=["A", "B"], descriptors=values)
                ],
                objectives=objectives,
            )
        )
        for values in (
            None,
            {"A": {"x": 1.0}, "B": {"x": 2.0}},
            {"A": {"x": 99.0}, "B": {"x": -99.0}},
        )
    }
    assert len(ids) == 3


def test_descriptors_computed_from_structures_still_do_not_fork_the_campaign() -> None:
    """The other half, which the original exclusion got right and must keep getting right.

    With `structures` set the descriptors are derived, so a cache miss recomputing them, or a
    calculator upgrade shifting the sixth decimal, is the same optimization problem.
    """
    objectives = [Objective(name="yield", direction="maximize")]
    structures = {"A": "CC", "B": "CCC"}
    ids = {
        campaign_id_for(
            OptimizationProblem(
                parameters=[
                    CategoricalParameter(
                        name="lig",
                        categories=["A", "B"],
                        structures=structures,
                        descriptors=values,
                    )
                ],
                objectives=objectives,
            )
        )
        for values in (None, {"A": {"x": 1.0}, "B": {"x": 2.0}}, {"A": {"x": 7.0}, "B": {"x": 8.0}})
    }
    assert len(ids) == 1


def test_the_order_a_constraint_was_written_in_does_not_fork_the_campaign() -> None:
    """`base + acid <= 3` and `acid + base <= 3` are one polytope and must be one campaign.

    Hashing the dump directly made them two, each with an empty history — the silent fork the
    identity's allowlist exists to prevent, on the field that reasoning did not cover.
    """
    parameters: list[Parameter] = [
        ContinuousParameter(name="acid", lower=0.0, upper=3.0),
        ContinuousParameter(name="base", lower=0.0, upper=3.0),
    ]
    objectives = [Objective(name="yield", direction="maximize")]
    written: list[tuple[list[str], list[float]]] = [
        (["acid", "base"], [1.0, 2.0]),
        (["base", "acid"], [2.0, 1.0]),
    ]
    ids = {
        campaign_id_for(
            OptimizationProblem(
                parameters=parameters,
                objectives=objectives,
                constraints=[
                    LinearConstraint(parameters=names, coefficients=coefficients, rhs=3.0)
                ],
            )
        )
        for names, coefficients in written
    }
    assert len(ids) == 1


def test_an_exclusion_written_either_way_round_is_one_campaign() -> None:
    """`forbids()` is symmetric in the two parameters, so the identity must be too."""
    parameters: list[Parameter] = [
        CategoricalParameter(name="catalyst", categories=["Pd(OAc)2", "Pd2dba3"]),
        CategoricalParameter(name="solvent", categories=["DMSO", "toluene"]),
    ]
    objectives = [Objective(name="yield", direction="maximize")]
    written: list[tuple[list[str], list[list[str]]]] = [
        (["catalyst", "solvent"], [["Pd(OAc)2"], ["DMSO"]]),
        (["solvent", "catalyst"], [["DMSO"], ["Pd(OAc)2"]]),
    ]
    ids = {
        campaign_id_for(
            OptimizationProblem(
                parameters=parameters,
                objectives=objectives,
                constraints=[ExcludeConstraint(parameters=names, options=options)],
            )
        )
        for names, options in written
    }
    assert len(ids) == 1


def test_the_identity_allowlist_still_covers_every_parameter_field() -> None:
    """An allowlist inverts the denylist's failure; this is what stops it inverting silently.

    A field added to a parameter later would not be hashed, so two different decision spaces would
    share one id and one history — the same invisible fork, one direction over. Failing here forces
    an explicit decision about whether the new field identifies the space.
    """
    declared = set(ContinuousParameter.model_fields) | set(CategoricalParameter.model_fields)
    assert declared == _SPACE_FIELDS | _IDENTIFYING_EXCLUSIONS


def test_a_programming_error_in_the_write_is_not_swallowed_as_a_database_blip() -> None:
    """`except Exception` made a deployment where every write fails look like one where none do.

    The models are constructed inside the same `try`, so a `ValidationError`, a `TypeError` or a
    non-finite float refused by the store all produced the one WARNING line a dropped connection
    produces — and the tool still answered successfully. Only the database's own failures are the
    database's fault; ours must surface.
    """

    class BrokenStore(InMemoryCampaignStore):
        async def record(self, campaign: Campaign, suggestion: Suggestion) -> int:
            raise TypeError("a defect in this code, not a blip")

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr("chemclaw.science.bo.campaign_record.campaign_store", BrokenStore)
    try:
        with pytest.raises(TypeError, match="a defect in this code"):
            _run(
                record_suggestion(
                    problem=_problem(),
                    candidates=[],
                    observations=[],
                    calc_refs=[],
                    provenance=("a", "s", "c"),
                )
            )
    finally:
        monkeypatch.undo()


# --- the durable campaign, which used to write nothing at all -------------------------------


def test_a_durable_campaign_run_is_recorded_and_resumable(store: InMemoryCampaignStore) -> None:
    """The gap the BO deep review found: hours of evaluation that `resume_campaign` denied existed.

    Both paths mint ids from one `campaign_id_for` space, and only the inline tool ever wrote — so
    `resume_campaign` on a campaign that had run durably reported no such campaign about work that
    was actually done. The activity is driven directly here because it is a plain coroutine; what
    the workflow adds on top (reading the actor off the run's memo) is pinned by
    `tests/test_connector_job_workflow.py` for every bundle.
    """
    from chemclaw.connectors.bo.activities import record_campaign_run

    problem = _problem()
    history = [Observation(params={"ligand": "L1", "temperature": 70.0}, value=0.81)]
    campaign_id = _run(
        record_campaign_run(
            problem,
            [Candidate(params={"ligand": "L1", "temperature": 72.0})],
            history,
            "alice@example.com",
            "corr-1",
            "bo-start_optimization_campaign-abc",
        )
    )
    assert campaign_id == campaign_id_for(problem)
    thread = _run(read_campaign_thread(campaign_id))
    assert thread.observations == history, "the evidence a resume seeds from"
    assert thread.last_candidates == [Candidate(params={"ligand": "L1", "temperature": 72.0})]
    assert thread.opened_by == "alice@example.com", (
        "the actor is the real one off the run's memo, never a fabricated service identity"
    )


def test_a_retried_durable_write_does_not_append_a_second_identical_suggestion(
    store: InMemoryCampaignStore,
) -> None:
    """A Temporal activity is retried by design, and history is meant to record what was proposed.

    The inline path never needed this — it wrote once per turn, and a duplicate was harmless
    because the read takes the latest. The durable path made the duplicate routine. Keyed on the
    run id, never on the content: two genuinely identical *asks* are two history entries, which is
    what "the sequence is the campaign's history" means.
    """
    from chemclaw.connectors.bo.activities import record_campaign_run

    problem = _problem()
    history = [Observation(params={"ligand": "L1", "temperature": 70.0}, value=0.81)]
    args = (problem, [Candidate(params={"ligand": "L1", "temperature": 72.0})], history)
    first = _run(record_campaign_run(*args, "alice@example.com", "c", "bo-job-1"))
    _run(record_campaign_run(*args, "alice@example.com", "c", "bo-job-1"))
    assert len(_run(store.suggestions_for(first, limit=10))) == 1

    # A different run against the same campaign is a real second entry, not a retry.
    _run(record_campaign_run(*args, "alice@example.com", "c", "bo-job-2"))
    assert len(_run(store.suggestions_for(first, limit=10))) == 2


def test_two_inline_suggestions_are_still_two_entries(store: InMemoryCampaignStore) -> None:
    """The idempotency key is the *run*, so the path that has no run is left exactly as it was.

    `job_id` defaults to empty for the inline tool, and the unique index is partial on `job_id <>
    ''` for the same reason: a shared default would collapse a campaign's whole inline history into
    one row.
    """
    problem = _problem()
    campaign_id = _run(record_suggestion(problem, [], [], [], ("a", "s", "c")))
    _run(record_suggestion(problem, [], [], [], ("a", "s", "c")))
    assert len(_run(store.suggestions_for(campaign_id, limit=10))) == 2


def test_a_suggestion_remembers_the_space_it_was_made_in(store: InMemoryCampaignStore) -> None:
    """The campaign row holds the *latest* problem, which is right for it and wrong for its history.

    A chemist who widens a bound is still working the same campaign, so the upsert refreshes the
    space — and a suggestion read back afterwards was then described by bounds that never applied
    to it. The candidates and observations were already snapshotted for exactly this reason.
    """
    problem = _problem()
    campaign_id = _run(record_suggestion(problem, [], [], [], ("a", "s", "c")))
    (recorded,) = _run(store.suggestions_for(campaign_id, limit=1))
    assert recorded.problem == problem.model_dump(mode="json")
