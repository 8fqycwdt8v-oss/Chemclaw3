"""A BO campaign is an entity, and the inline suggestion path records it.

`suggest_next_experiment` writes the campaign and its suggestions, so framing a problem is not
redone every turn. The campaign is identified by its problem, not minted per call, which turns a
sequence of turns into one history; most tests pin that identity. `InMemoryCampaignStore` is a
real backend, the contract its Postgres sibling must match.
"""

import asyncio
from typing import Any

import pytest

from chemclaw.core.ids import canonical_text, stable_hash
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
from tests.bo_harness import molecule_library_problem


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

    Descriptors computed from structures are a consequence of the space, not part of it.
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

    Parameter and category order are canonicalized in the identity payload only; the surrogate keeps
    the caller's order, since category order slightly affects ordinal encoding.
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
        ).campaign_id

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
    ).campaign_id

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

    The same trade `agent/audit.py` and `kg/record.py` make. The campaign id is a pure function
    of the problem, so it is still the right handle to return on the turn where the write failed.
    """

    class BrokenStore(InMemoryCampaignStore):
        async def record(self, campaign: Campaign, suggestion: Suggestion) -> tuple[int, bool]:
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
    assert returned.campaign_id == campaign_id_for(_problem())


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
    """Ask, observe, ask again — across sessions, not across one transcript.

    A second turn holding only the id gets the decision space and the runs back.
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
    ).campaign_id

    thread = _run(read_campaign_thread(campaign_id))

    assert thread.problem == problem
    assert [o.value for o in thread.observations] == [55.0, 78.0]
    assert [c.params["temperature"] for c in thread.last_candidates] == [95.0]
    assert (thread.objective, thread.direction) == ("yield", "maximize")
    # The thread deliberately carries no `opened_by` — see `CampaignThread`. The column still
    # holds it, and that is where an audit reads it from.
    assert not hasattr(thread, "opened_by")
    assert _run(store.read_campaign(campaign_id)).opened_by == "chemist-a"


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
    """An unknown id says the space changed rather than answering from nothing.

    A changed space yields a different id, so a miss means "new campaign", not "history lost"; an
    empty thread would answer with silence.
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
    """The bo connector serves and declares resuming.

    A served tool not in the manifest is never advertised, and a manifest entry with no tool fails
    at call time.
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
    ).campaign_id

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

# The ids these three shapes hashed to before parameter and category order were canonicalized
# (D-2026-08-08-a-partial-answer-must-say-so). A moved id silently makes every running campaign
# look new, so each move is recorded. Each shape moved onto the id its sorted spelling already had,
# so an already-sorted campaign kept its id.
_PRE_CANONICALIZATION_IDS = {
    "continuous-only": "campaign-6958b7edaa261c83",
    "mixed": "campaign-55e5f929fe83a9a5",
    "with-structures": "campaign-109f34eac28892ab",
}
# Generation 2: sorted, but with every name and label still hashed byte-exact.
_PRE_FOLDING_IDS = {
    "continuous-only": "campaign-a97f5dd910a2cc79",
    "mixed": "campaign-acfb471df76f2863",
    "with-structures": "campaign-59d74ed90e64b3f2",
}
# Generation 3, and current: names, category labels and objective names folded, bounds rounded
# (D-2026-08-21-a-geometry-is-an-address-not-a-payload). `chemclaw.cli.rekey_campaigns` re-keys
# stored rows, so nothing is orphaned. `continuous-only` is unchanged because folding is the
# identity on it, which bounds the re-partition to spaces carrying capitals or stray whitespace.
_BASELINE_IDS = {
    "continuous-only": "campaign-a97f5dd910a2cc79",
    "mixed": "campaign-d1c269048981a830",
    "with-structures": "campaign-fca719589962491a",
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


def _pre_folding_space(parameter: Any, *, sort_categories: bool) -> dict[str, Any]:
    """`_space_of` as it dumped one parameter *before* names and labels were folded.

    Reconstructed because the old algorithms are gone; `sort_categories` selects generation 1
    (caller's order) or 2 (sorted). Faithful for the three baseline shapes only (no descriptor map
    without structures).
    """
    # Annotated because `parameter` is `Any` (three unrelated parameter classes reach here), so
    # `model_dump` returns `Any` and the declared return type would be unchecked.
    dumped: dict[str, Any] = parameter.model_dump(
        mode="json", include={"kind", "name", "lower", "upper", "categories", "structures"}
    )
    if isinstance(parameter, CategoricalParameter):
        dumped["categories"] = (
            sorted(parameter.categories) if sort_categories else list(parameter.categories)
        )
    return dumped


def _pre_canonicalization_id(problem: OptimizationProblem) -> str:
    """`campaign_id_for` as it hashed *before* parameter and category order were canonicalized.

    The current function sorts and folds, so the old id cannot be recovered by calling it; the first
    assertion below checks this reconstruction reproduces the captured ids. Faithful for the three
    baseline shapes only (no constraint or second objective).
    """
    space = [
        _pre_folding_space(parameter, sort_categories=False) for parameter in problem.parameters
    ]  # and the caller's parameter order, unsorted
    identity = {"space": space, "objective": problem.objective.model_dump(mode="json")}
    return f"campaign-{stable_hash(identity)}"


def _pre_folding_id(problem: OptimizationProblem) -> str:
    """`campaign_id_for` as it hashed after the ordering fix and before the folding one."""
    space = sorted(
        (_pre_folding_space(parameter, sort_categories=True) for parameter in problem.parameters),
        key=lambda dumped: str(dumped["name"]),
    )
    identity = {"space": space, "objective": problem.objective.model_dump(mode="json")}
    return f"campaign-{stable_hash(identity)}"


def test_canonicalization_moved_each_legacy_id_onto_its_sorted_twin() -> None:
    """The one deliberate id move, pinned in both directions so it can never happen quietly.

    Each unsorted shape moved onto the id its sorted spelling already carried. The old algorithm
    (`_pre_canonicalization_id`) must appear here, since computing both sides with today's sorting
    code would compare two equal literals.
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
        # As written, the shape used to hash here — the row now orphaned.
        assert _pre_canonicalization_id(problem) == _PRE_CANONICALIZATION_IDS[label], label
        # And its sorted spelling already carried the id it took next: the move is a merge onto an
        # existing row, not a newly minted one. This is the whole claim.
        assert _pre_canonicalization_id(sorted_spelling) == _PRE_FOLDING_IDS[label], label


def test_folding_moved_only_the_spaces_that_carry_a_capital_letter() -> None:
    """The second deliberate id move, pinned in both directions and bounded.

    Folding stops a re-typed space (`THF` vs `thf`) minting a history-less campaign. A space already
    in lower case keeps its id exactly; `chemclaw.cli.rekey_campaigns` moves the rest.
    """
    for label, problem in _baseline_problems().items():
        previous = _pre_folding_id(problem)
        assert previous == _PRE_FOLDING_IDS[label], label
        current = campaign_id_for(problem)
        assert current == _BASELINE_IDS[label], label
        already_folded = all(
            name == canonical_text(name)
            for parameter in problem.parameters
            for name in [parameter.name, *getattr(parameter, "categories", [])]
        ) and problem.objective.name == canonical_text(problem.objective.name)
        assert (current == previous) is already_folded, label


def test_a_recased_or_padded_spelling_is_the_same_campaign() -> None:
    """The defect itself: the ways a model re-emits a space it just read must not fork it."""
    problem = _baseline_problems()["mixed"]
    reference = campaign_id_for(problem)
    perturbed = problem.model_copy(
        update={
            "parameters": [
                ContinuousParameter(name="Temperature", lower=20, upper=120.0000001),
                CategoricalParameter(name="solvent", categories=["thf ", " Toluene"]),
            ],
            "objectives": [Objective(name="Yield", direction="maximize")],
        }
    )
    assert campaign_id_for(perturbed) == reference


def test_two_libraries_whose_smiles_differ_only_in_case_are_two_campaigns() -> None:
    """The bound on the fold: case is chemistry in a SMILES, so the fold must not reach one.

    `C1CCNCC1` (piperidine) and `c1ccncc1` (pyridine) casefold to one string, which would merge two
    libraries' campaigns and hand each the other's observations.
    """
    piperidine = molecule_library_problem(["C1CCNCC1", "CCO"])
    pyridine = molecule_library_problem(["c1ccncc1", "CCO"])
    first, second = piperidine.parameters[0], pyridine.parameters[0]
    assert isinstance(first, CategoricalParameter) and isinstance(second, CategoricalParameter)
    assert first.categories != second.categories
    assert campaign_id_for(piperidine) != campaign_id_for(pyridine)


def test_a_spelling_of_one_molecule_is_the_same_campaign_as_another() -> None:
    """And the fold's *purpose* survives on the same labels: one molecule is one campaign.

    A structure label is reduced by RDKit (`OCC` and `CCO` are one molecule). Built by hand, since
    `molecule_library_problem` already canonicalizes.
    """

    def library(ethanol: str) -> OptimizationProblem:
        return OptimizationProblem(
            parameters=[CategoricalParameter(name="molecule", categories=[ethanol, "c1ccncc1"])],
            objectives=[Objective(name="log_s", direction="maximize")],
        )

    assert campaign_id_for(library("OCC")) == campaign_id_for(library("CCO"))


def test_labels_that_fold_onto_each_other_keep_their_own_spellings() -> None:
    """A fold that merges two of one space's *own* labels is not a canonicalisation of it.

    `structures` is keyed by label, so folding `L1` and `l1` together would drop an entry and make
    different maps hash alike. Labels stay exact only where the fold would merge them.
    """

    def ligands(structures: dict[str, str]) -> OptimizationProblem:
        return OptimizationProblem(
            parameters=[
                CategoricalParameter(
                    name="ligand", categories=sorted(structures), structures=structures
                )
            ],
            objectives=[Objective(name="yield", direction="maximize")],
        )

    assert campaign_id_for(ligands({"L1": "CCO", "l1": "CCN"})) != campaign_id_for(
        ligands({"L1": "c1ccccc1", "l1": "CCN"})
    )


def test_an_exclusion_naming_one_molecule_is_not_the_exclusion_naming_another() -> None:
    """The same rule on the other half of the identity, where the labels are re-typed too.

    An exclusion naming molecules must not be casefolded: over a library holding both, "never
    piperidine in THF" and "never pyridine in THF" are different campaigns.
    """

    def excluding(molecule: str) -> OptimizationProblem:
        return OptimizationProblem(
            parameters=[
                CategoricalParameter(name="molecule", categories=["C1CCNCC1", "c1ccncc1"]),
                CategoricalParameter(name="solvent", categories=["THF", "DMF"]),
            ],
            objectives=[Objective(name="yield", direction="maximize")],
            constraints=[
                ExcludeConstraint(parameters=["molecule", "solvent"], options=[[molecule], ["THF"]])
            ],
        )

    assert campaign_id_for(excluding("C1CCNCC1")) != campaign_id_for(excluding("c1ccncc1"))


def test_a_lab_code_that_happens_to_parse_as_a_molecule_does_not_fork_its_space() -> None:
    """The reduction is decided per *space*, because a one-label decision cannot see the space.

    Many short labels parse as SMILES (`CO`, `CN`, `B`), so a per-label rule would fork mixed
    spaces. A label list is chemistry only when all of it parses; otherwise the whole list folds as
    text. Piperidine and pyridine still stay two campaigns.
    """

    def screen(categories: list[str]) -> OptimizationProblem:
        return OptimizationProblem(
            parameters=[CategoricalParameter(name="atmosphere", categories=categories)],
            objectives=[Objective(name="yield", direction="maximize")],
        )

    assert campaign_id_for(screen(["CO", "N2", "H2"])) == campaign_id_for(
        screen(["co", "n2", "h2"])
    ), "a gas atmosphere that is also a legal SMILES re-cased into a second campaign"
    assert campaign_id_for(screen(["A", "B", "C"])) == campaign_id_for(screen(["a", "b", "c"])), (
        "an opaque catalyst code re-cased into a second campaign"
    )
    assert campaign_id_for(screen(["C1CCNCC1", "CCO"])) != campaign_id_for(
        screen(["c1ccncc1", "CCO"])
    ), "a space whose every label is a structure must still keep piperidine from pyridine"


def test_an_exclusion_reduces_its_options_the_way_its_own_parameter_does() -> None:
    """One label set, one reduction — the rule `_space_of` already states for `structures`.

    An exclusion's options are re-keyed through their parameter's own reduction map, so a subset
    cannot answer the per-space question differently.
    """

    def excluding(atmosphere: str, solvent: str, categories: list[str]) -> OptimizationProblem:
        return OptimizationProblem(
            parameters=[
                CategoricalParameter(name="atmosphere", categories=categories),
                CategoricalParameter(name="solvent", categories=["THF", "DMF"]),
            ],
            objectives=[Objective(name="yield", direction="maximize")],
            constraints=[
                ExcludeConstraint(
                    parameters=["atmosphere", "solvent"], options=[[atmosphere], [solvent]]
                )
            ],
        )

    assert campaign_id_for(excluding("CO", "THF", ["CO", "N2", "H2"])) == campaign_id_for(
        excluding("co", "THF", ["co", "n2", "h2"])
    ), "the excluded option re-cased into a second campaign after the space stopped doing so"


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
    """Caller-supplied descriptors identify the space.

    With no structures, the descriptors are the only statement of what the surrogate sees, so spaces
    featurized differently must get different ids.
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
    """The identity allowlist still covers every parameter field.

    A field added later would not be hashed and two spaces would share a history; failing here
    forces a decision about the new field.
    """
    declared = set(ContinuousParameter.model_fields) | set(CategoricalParameter.model_fields)
    assert declared == _SPACE_FIELDS | _IDENTIFYING_EXCLUSIONS


def test_a_programming_error_in_the_write_is_not_swallowed_as_a_database_blip() -> None:
    """A programming error in the write is not swallowed as a database blip.

    Only the database's own failures are tolerated; a `ValidationError` or `TypeError` must surface.
    """

    class BrokenStore(InMemoryCampaignStore):
        async def record(self, campaign: Campaign, suggestion: Suggestion) -> tuple[int, bool]:
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
    """A durable campaign run is recorded and resumable.

    Both paths mint ids from `campaign_id_for`, so `resume_campaign` finds durable work too. The
    activity is driven directly; reading the actor off the memo is pinned in
    `tests/test_connector_job_workflow.py`.
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
    assert _run(store.read_campaign(campaign_id)).opened_by == "alice@example.com", (
        "the actor is the real one off the run's memo, never a fabricated service identity"
    )


def test_a_retried_durable_write_does_not_append_a_second_identical_suggestion(
    store: InMemoryCampaignStore,
) -> None:
    """A retried durable write does not append a second identical suggestion.

    Activities are retried, so the write is keyed on the run id, never on content: two identical
    asks are two history entries.
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
    """The idempotency key is the run, so the path that has no run keeps every entry.

    `job_id` defaults to empty and the unique index is partial on `job_id <> ''`.
    """
    problem = _problem()
    campaign_id = _run(record_suggestion(problem, [], [], [], ("a", "s", "c"))).campaign_id
    _run(record_suggestion(problem, [], [], [], ("a", "s", "c")))
    assert len(_run(store.suggestions_for(campaign_id, limit=10))) == 2


def test_a_suggestion_remembers_the_space_it_was_made_in(store: InMemoryCampaignStore) -> None:
    """A suggestion remembers the space it was made in.

    The campaign row holds the latest problem (a widened bound is the same campaign), so each
    suggestion snapshots its own space, as candidates and observations already are.
    """
    problem = _problem()
    campaign_id = _run(record_suggestion(problem, [], [], [], ("a", "s", "c"))).campaign_id
    (recorded,) = _run(store.suggestions_for(campaign_id, limit=1))
    assert recorded.problem == problem.model_dump(mode="json")


def test_the_fork_flag_comes_from_the_write_and_not_from_a_read_before_it(
    store: InMemoryCampaignStore,
) -> None:
    """The fork flag comes from the write, not from a read before it.

    Two concurrent turns would both read "no campaign"; the upsert knows which one opened it. The
    first record of a space reports opening it, every later one joining.
    """
    problem = _problem()
    first = _run(record_suggestion(problem, [], [], [], ("a", "s", "c")))
    second = _run(record_suggestion(problem, [], [], [], ("a", "s", "c")))

    assert first.opened_new_campaign is True
    assert second.opened_new_campaign is False
    assert first.campaign_id == second.campaign_id


def test_a_failed_write_reports_no_fork_rather_than_guessing_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed write reports no fork rather than guessing one.

    A database blip must not send a chemist hunting a fork; the candidates are still returned.
    """

    class BrokenStore(InMemoryCampaignStore):
        async def record(self, campaign: Campaign, suggestion: Suggestion) -> tuple[int, bool]:
            raise ConnectionError("database down")

    monkeypatch.setattr("chemclaw.science.bo.campaign_record.campaign_store", BrokenStore)
    campaign_store.cache_clear()

    recorded = _run(record_suggestion(_problem(), [], [], [], ("a", "s", "c")))
    assert recorded.campaign_id == campaign_id_for(_problem())
    assert recorded.opened_new_campaign is False


def test_an_inline_suggestion_replayed_with_the_same_ask_records_one_row(
    store: InMemoryCampaignStore,
) -> None:
    """An inline suggestion replayed with the same ask records one row.

    A tool killed mid-call is re-run with its original arguments, so the inline path derives an
    idempotency key from the ask (the dedupe index is partial on `job_id <> ''`). A different ask is
    still a second row.
    """
    problem = _problem()
    history = [Observation(params={"temperature": 40.0, "ligand": "PPh3"}, value=55.0)]

    first = _run(
        record_suggestion(
            problem=problem,
            candidates=[],
            observations=history,
            calc_refs=["xtb@v1:aaa:bbb"],
            provenance=("chemist-a", "session-1", "corr-1"),
            job_id="inline-same",
        )
    )
    _run(
        record_suggestion(
            problem=problem,
            candidates=[],
            observations=history,
            calc_refs=["xtb@v1:aaa:bbb"],
            provenance=("chemist-a", "session-1", "corr-1"),
            job_id="inline-same",
        )
    )
    assert len(_run(store.suggestions_for(first.campaign_id, 10))) == 1, (
        "a replay of one ask must not add a second suggestion to the campaign's history"
    )

    _run(
        record_suggestion(
            problem=problem,
            candidates=[],
            observations=[Observation(params={"temperature": 41.0, "ligand": "PPh3"}, value=57.0)],
            calc_refs=["xtb@v1:aaa:bbb"],
            provenance=("chemist-a", "session-1", "corr-1"),
            job_id="inline-different",
        )
    )
    assert len(_run(store.suggestions_for(first.campaign_id, 10))) == 2, (
        "a genuinely different ask is a second suggestion and must still be recorded"
    )
