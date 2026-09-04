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
from chemclaw.science.bo.objectives import molecule_library_problem
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

    The same trade `agent/audit.py` and `kg/proposal.py` make. The campaign id is a pure function
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
# Generation 2: sorted, but with every name and label still hashed byte-exact.
_PRE_FOLDING_IDS = {
    "continuous-only": "campaign-a97f5dd910a2cc79",
    "mixed": "campaign-acfb471df76f2863",
    "with-structures": "campaign-59d74ed90e64b3f2",
}
# Generation 3, and current: names, category labels and objective names folded, bounds rounded
# (D-2026-08-21-a-geometry-is-an-address-not-a-payload).
#
# **The second deliberate move, and unlike the first it orphans nothing**:
# `chemclaw.cli.rekey_campaigns` recomputes each row's id from the `problem` migration 031 already
# stores, so a chemist resuming a campaign framed before this finds it. The first move had no such
# path and its cost was a `BACKLOG.md` row about orphaned rows; this one has one because the same
# review that asked for the fold asked what it would break.
#
# `continuous-only` is deliberately *unchanged*: its names are already lower-case and its bounds
# already round, so folding is the identity on it. That is the bound on the re-partition — the fold
# moves exactly the spaces that carry a capital letter or stray whitespace, which is exactly the set
# that was forking.
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

    Reconstructed rather than called, because the two historical claims below are statements about
    algorithms that are gone, and the current `_space_of` folds. `sort_categories` selects between
    generation 1 (the caller's order, the fork) and generation 2 (sorted).

    Faithful for the three baseline shapes only: none carries a descriptor map supplied without
    structures, so `_space_of`'s conditional `descriptors` key is not reproduced here.
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

    Rebuilt here rather than asserted about, because the claim this pins is a statement about the
    old algorithm and the old algorithm is gone: the id a shape carried then cannot be recovered by
    calling the current function, which sorts and folds whatever it is handed. The first assertion
    below keeps the reconstruction honest — it must reproduce the three ids captured from the parent
    commit, or this helper has drifted from the code it stands in for.

    Faithful for the three baseline shapes only: none carries a constraint or a second objective,
    so the two conditional keys of the identity payload are not reproduced here.
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

    Each shape above is written unsorted, so ordering canonicalization had to move it. What is
    asserted here is *where*: onto the id the same space already carried when written in sorted
    order, so an already-sorted campaign keeps its row and its unsorted twin merges into it. Rows
    written under the pre-canonicalization ids are orphaned — a one-time cost, recorded in
    `BACKLOG.md`, against a fork that would otherwise recur on every re-declaration.

    **The old algorithm has to appear in the test, and `_pre_canonicalization_id` is it.** The
    claim is about where an id computed the *old* way landed, so computing both sides with the
    current code cannot state it: today's hash sorts whatever it is given, which makes the unsorted
    and sorted spellings identical before hashing and reduces the whole assertion to two literals
    being equal. Mutations removing either sort survive that. A test of a migration needs the
    pre-migration function.
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

    Folding names and labels is what stops a re-typed decision space from minting a campaign with
    no history (D-2026-08-21) — measured, `THF` against `thf` was two rows, silently, because
    `record_suggestion` upserts. What is asserted here is the *extent*: a space already written in
    lower case keeps its id exactly, so the re-partition is confined to the spaces that were
    forking, and `chemclaw.cli.rekey_campaigns` moves the rest rather than leaving them orphaned.
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

    `molecule_library_problem` makes the canonical SMILES *itself* the category label, and SMILES
    spells an aromatic atom in lower case — `C1CCNCC1` is piperidine, `c1ccncc1` is pyridine, and
    `str.casefold` maps them onto one string. Measured before this: two chemists screening those two
    libraries got one campaign id, the second was told their campaign was not new, its decision
    space overwrote the first's, and `read_campaign_thread` then handed whoever resumed either one
    the other's observations. That is the "seeded with observations from a different campaign"
    failure `_space_of`'s descriptor rule exists to prevent, arriving through the label instead.
    """
    piperidine = molecule_library_problem(["C1CCNCC1", "CCO"])
    pyridine = molecule_library_problem(["c1ccncc1", "CCO"])
    first, second = piperidine.parameters[0], pyridine.parameters[0]
    assert isinstance(first, CategoricalParameter) and isinstance(second, CategoricalParameter)
    assert first.categories != second.categories
    assert campaign_id_for(piperidine) != campaign_id_for(pyridine)


def test_a_spelling_of_one_molecule_is_the_same_campaign_as_another() -> None:
    """And the fold's *purpose* survives on the same labels: one molecule is one campaign.

    A label that is a structure is reduced by RDKit rather than by `str.casefold`, which is the
    same act on the right data type — `OCC` and `CCO` are one molecule, so a space that names it
    either way is one campaign, exactly as `THF` and `thf` are.

    Built by hand rather than through `molecule_library_problem`, which canonicalizes its library on
    the way in: routing through it would assert RDKit's idempotence rather than this rule.
    """

    def library(ethanol: str) -> OptimizationProblem:
        return OptimizationProblem(
            parameters=[CategoricalParameter(name="molecule", categories=[ethanol, "c1ccncc1"])],
            objectives=[Objective(name="log_s", direction="maximize")],
        )

    assert campaign_id_for(library("OCC")) == campaign_id_for(library("CCO"))


def test_labels_that_fold_onto_each_other_keep_their_own_spellings() -> None:
    """A fold that merges two of one space's *own* labels is not a canonicalisation of it.

    `structures` is keyed by the category labels, so folding `L1` and `l1` onto one key does not
    merely lose a distinction — the dict comprehension building the identity payload **drops an
    entry**, and two genuinely different label→SMILES maps hash to the same shortened one. Measured
    before this: `{"L1": "CCO", "l1": "CCN"}` and `{"L1": "c1ccccc1", "l1": "CCN"}` were one
    campaign, one feature space silently standing in for the other.

    The labels are kept exact only where the fold would merge them, which is why this costs the
    ordinary spaces nothing — asserted directly above by the baseline ids.
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

    `_canonical` folds an exclusion's options because they are category labels a model re-emits —
    and they are category labels, so when they name molecules the fold is wrong there for the
    identical reason. Over a library holding both, "never piperidine in THF" and "never pyridine in
    THF" are different campaigns; the space alone cannot tell them apart, because it holds both.
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
    assert _run(store.read_campaign(campaign_id)).opened_by == "alice@example.com", (
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
    campaign_id = _run(record_suggestion(problem, [], [], [], ("a", "s", "c"))).campaign_id
    _run(record_suggestion(problem, [], [], [], ("a", "s", "c")))
    assert len(_run(store.suggestions_for(campaign_id, limit=10))) == 2


def test_a_suggestion_remembers_the_space_it_was_made_in(store: InMemoryCampaignStore) -> None:
    """The campaign row holds the *latest* problem, which is right for it and wrong for its history.

    A chemist who widens a bound is still working the same campaign, so the upsert refreshes the
    space — and a suggestion read back afterwards was then described by bounds that never applied
    to it. The candidates and observations were already snapshotted for exactly this reason.
    """
    problem = _problem()
    campaign_id = _run(record_suggestion(problem, [], [], [], ("a", "s", "c"))).campaign_id
    (recorded,) = _run(store.suggestions_for(campaign_id, limit=1))
    assert recorded.problem == problem.model_dump(mode="json")


def test_the_fork_flag_comes_from_the_write_and_not_from_a_read_before_it(
    store: InMemoryCampaignStore,
) -> None:
    """`opened_new_campaign` used to be a read taken just before the write, which raced it.

    Two turns opening the same decision space concurrently both read no campaign and both reported
    having opened one, while the upsert underneath serialized them — so exactly one was right and
    nothing could tell which. The write knows; the write says.

    Asserted as the sequence a race would break: the first record of a space reports opening it,
    every record after reports joining it. A read-before-write cannot promise the second line
    without a lock the store never took.
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
    """A blip is ignorance, and ignorance must not be announced as a fork.

    The read this replaced erred in the same direction deliberately ("assume known"), because
    raising a fork on the strength of a failed read sends a chemist looking for a problem a
    database outage invented. The candidates are still returned, which is the whole point of
    swallowing the failure at all.
    """

    class BrokenStore(InMemoryCampaignStore):
        async def record(self, campaign: Campaign, suggestion: Suggestion) -> tuple[int, bool]:
            raise ConnectionError("database down")

    monkeypatch.setattr("chemclaw.science.bo.campaign_record.campaign_store", BrokenStore)
    campaign_store.cache_clear()

    recorded = _run(record_suggestion(_problem(), [], [], [], ("a", "s", "c")))
    assert recorded.campaign_id == campaign_id_for(_problem())
    assert recorded.opened_new_campaign is False
