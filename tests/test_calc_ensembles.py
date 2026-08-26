"""The multi-step composites: did the fan-out ask for the right parts, and stop asking on a repeat?

Same discipline as `tests/test_calc_compose.py`, and for the same reason: these functions have no
cache row of their own — their key would name an output — so the only thing that makes them
correct is that they reach parts which *are* keyed, once each. "Was this recomputed?" is a question
about call counts and nothing else (D-011), and a fan-out is where a wrong answer to it costs the
most: a species ranking that misses the cache runs a CREST search per species, and a 33-atom search
was measured at 1142 s.

Driven against `tests/calc_server_fake.py`. Its ensemble members are three *distinct* geometries,
which is what makes a per-member refinement three calls rather than one.
"""

import asyncio
from typing import Any

import pytest

from chemclaw.connectors.calc import compose
from chemclaw.core.config import settings as calc_settings
from chemclaw.science.calc.store import InMemoryStore
from tests.calc_server_fake import FakeCalcServer, install


def _run(coroutine: Any) -> Any:
    """Run one coroutine to completion, the shape every test here uses."""
    return asyncio.run(coroutine)


# --- refined ensembles ------------------------------------------------------------------------


def test_a_refined_ensemble_optimizes_and_takes_a_hessian_per_member(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The cost D-101 declined to pay, paid deliberately and bounded.

    One search, then one optimization and one Hessian for each member kept. The count is the point:
    it is what makes free-energy weighting a different *price* as well as a different treatment, and
    it is what `ensemble_refine_top_n` bounds.
    """
    server = install(monkeypatch, FakeCalcServer())
    store = InMemoryStore()

    refined = _run(compose.refined_ensemble(store, "CCO"))

    assert server.count("search_conformer_ensemble") == 1
    assert server.count("compute_hessian") == 3, "a Hessian per member is the whole cost"
    assert refined.refined_count == 3
    assert refined.treatment == "free-energy-weighted-top-n"
    assert abs(sum(member.population for member in refined.conformers) - 1.0) < 1e-3


def test_refining_the_same_ensemble_twice_pays_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """The measurement the whole split turns on, at fan-out scale.

    Every part is separately keyed, so a repeat is a lookup per part and no calculation. If this
    ever regresses, a chemist asking the same question at a second temperature pays for a second
    conformer search — the most expensive single thing in the system.
    """
    server = install(monkeypatch, FakeCalcServer())
    store = InMemoryStore()

    _run(compose.refined_ensemble(store, "CCO"))
    before = {tool: server.count(tool) for tool in ("search_conformer_ensemble", "compute_hessian")}
    _run(compose.refined_ensemble(store, "CCO"))

    assert {
        tool: server.count(tool) for tool in ("search_conformer_ensemble", "compute_hessian")
    } == before, "a repeat recomputed something"


def test_a_truncated_refinement_says_what_share_of_the_ensemble_it_covers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A refinement over part of an ensemble must not read as one over the whole.

    This is the error `ensemble_from_members` already refuses for `max_members`, and it is worse
    here because a free energy looks more careful than an electronic one. The coverage is reported
    as a number and warned about below the threshold, so a reader has to look away deliberately.
    """
    install(monkeypatch, FakeCalcServer())
    store = InMemoryStore()

    refined = _run(compose.refined_ensemble(store, "CCO", top_n=1))

    assert refined.refined_count == 1
    assert refined.total_found == 3
    assert refined.refined_population_covered < 1.0
    assert any("population" in warning for warning in refined.warnings)


def test_a_refinement_over_the_whole_ensemble_warns_about_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The other half of the same rule: a complete refinement must not cry wolf."""
    install(monkeypatch, FakeCalcServer())

    refined = _run(compose.refined_ensemble(InMemoryStore(), "CCO"))

    assert refined.refined_population_covered == pytest.approx(1.0, abs=1e-3)
    assert not any("population" in warning for warning in refined.warnings)


# --- averaged properties ----------------------------------------------------------------------


def test_a_property_average_evaluates_at_every_member_and_reports_the_spread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The caveat under every other number here — "this describes one conformer" — lifted.

    One properties call per member, at that member's own geometry, and the answer carries the range
    as well as the mean. A property whose ensemble spread exceeds the difference it is being used to
    argue is not a single number, and the spread is what lets a reader see that.
    """
    server = install(monkeypatch, FakeCalcServer())

    averaged = _run(compose.ensemble_property(InMemoryStore(), "CCO", prop="dipole_debye"))

    assert server.count("compute_properties_at") == 3
    assert averaged.members_averaged == 3
    assert averaged.value is not None
    assert averaged.value.minimum < averaged.value.mean < averaged.value.maximum
    assert averaged.value.spread > 0, "three distinct geometries must not average to a point"
    assert averaged.value.spread == pytest.approx(averaged.value.maximum - averaged.value.minimum)


def test_an_averaged_fukui_ranking_reaches_the_geometry_taking_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The regioselectivity question over a whole ensemble rather than over one embedding.

    It must reach `compute_fukui_at` — the primitive whose absence was a `DEFERRED.md` row — and
    average per atom rather than per molecule, because "which site" is a per-atom question.
    """
    server = install(monkeypatch, FakeCalcServer())

    averaged = _run(compose.ensemble_property(InMemoryStore(), "CCO", prop="fukui"))

    assert server.count("compute_fukui_at") == 3
    assert server.count("predict_site_reactivity") == 0
    assert averaged.value is None, "a per-atom property has no single scalar"
    assert len(averaged.per_atom) > 1
    assert all(atom.value.minimum <= atom.value.mean for atom in averaged.per_atom)
    # Every atom appears once, and the indices are the molecule's rather than a rank order.
    indices = [atom.index for atom in averaged.per_atom]
    assert indices == sorted(set(indices)), "atoms must be reported once each, by index"


def test_an_averaged_fukui_pairs_atoms_by_index_not_by_rank() -> None:
    """Conformers rank their atoms differently, and averaging by list position mixes them up.

    `SiteReactivityResult.sites` is ordered *most-susceptible first* and truncated to `top_n`, so
    position k is a different atom in different conformers — which is the normal case, not the
    exotic one: if the ranking did not move with geometry there would be no reason to average over
    an ensemble at all. The first version of `_averaged` did `member[position]` and labelled the
    result with the first conformer's index, so it averaged one atom's index with another's and
    reported it under a third name.

    Asserted against a hand-built input rather than through the fake, because the arithmetic is
    what is being pinned: atom 0 is 0.1 in every conformer and atom 1 is 0.9 in every conformer, so
    *any* correct pairing gives 0.1 and 0.9 with zero spread. Position-pairing gives 0.5 and 0.5
    with a spread of 0.8 — the reordering is the only difference between the two members.
    """
    ranked_high_first = {0: ("C", 0.1), 1: ("O", 0.9)}
    ranked_low_first = {1: ("O", 0.9), 0: ("C", 0.1)}

    per_atom, dropped = compose._per_atom([ranked_high_first, ranked_low_first], [0.5, 0.5])

    assert dropped == 0, "both conformers carry both atoms"

    by_index = {atom.index: atom for atom in per_atom}
    assert by_index[0].value.mean == pytest.approx(0.1)
    assert by_index[1].value.mean == pytest.approx(0.9)
    assert by_index[0].element == "C" and by_index[1].element == "O"
    assert by_index[0].value.spread == pytest.approx(0.0), (
        "one atom's value is identical in both conformers; a spread here means two atoms were "
        "averaged together"
    )


def test_an_atom_missing_from_one_conformer_is_dropped_rather_than_part_averaged() -> None:
    """Truncation means conformers can carry different atom *sets*, not merely different orders.

    A Fukui result is cut to `top_n`, so a marginal atom can be inside one conformer's list and
    outside another's. Position-pairing raised `IndexError` on the short list; averaging over
    whichever members happen to carry the atom would be worse, because the result would look like a
    population-weighted mean over the ensemble and be a mean over a subset, with nothing saying so.
    """
    both = {0: ("C", 0.2), 1: ("O", 0.4)}
    truncated = {0: ("C", 0.6)}

    per_atom, dropped = compose._per_atom([both, truncated], [0.5, 0.5])

    assert [atom.index for atom in per_atom] == [0], "an atom absent from a member must be dropped"
    assert per_atom[0].value.mean == pytest.approx(0.4)
    assert dropped == 1, "the dropped atom must be counted so the caller can say so"


def test_a_property_no_conformer_defines_is_refused_rather_than_averaged() -> None:
    """A missing LUMO is not a zero, and averaging it as one is a number about nothing."""
    with pytest.raises(ValueError, match="not defined for every conformer"):
        compose._averaged(
            "lumo_ev",
            [
                {
                    "calc_version": "v",
                    "calc_key": None,
                    "smiles": "CCO",
                    "structure_id": "st_x",
                    "method": "GFN2-xTB",
                    "solvent": None,
                    "total_energy_hartree": -1.0,
                    "homo_ev": -9.0,
                    "lumo_ev": None,
                    "gap_ev": None,
                    "dipole_debye": 1.0,
                    "atom_charges": [],
                    "bond_orders": [],
                }
            ],
            [1.0],
        )


# --- species distributions --------------------------------------------------------------------


def test_a_species_ranking_computes_each_form_once_and_normalizes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tautomers, microstates and stereoisomers are one composite, and this is its contract.

    Every species goes through `_species_energy` — the engine the reaction composites already use —
    so a species computed for a ranking is a cache hit for a reaction and the two cannot disagree
    about what one form's free energy is.
    """
    server = install(monkeypatch, FakeCalcServer())
    store = InMemoryStore()

    distribution = _run(
        compose.species_ranking(
            store,
            [("CC(=O)CC(C)=O", "keto"), ("CC(O)=CC(C)=O", "enol")],
            kind="tautomers",
        )
    )

    assert server.count("relax_structure") == 2, "one relaxation per species"
    assert distribution.kind == "tautomers"
    assert len(distribution.species) == 2
    assert abs(sum(entry.population for entry in distribution.species) - 1.0) < 1e-3
    assert distribution.species[0].relative_kcal == 0.0, "the lowest form is the reference"
    assert distribution.dominant.population >= 0.5


def test_a_quick_ranking_says_it_ignored_the_entropy(monkeypatch: pytest.MonkeyPatch) -> None:
    """At `quick` there is no free energy, so the populations are not free-energy populations.

    Saying so is the difference between a cheap answer and a wrong one: two tautomers can differ by
    more in zero-point energy than in electronic energy, and a reader given "populations" with no
    qualifier has no way to know which was computed.
    """
    install(monkeypatch, FakeCalcServer())

    distribution = _run(
        compose.species_ranking(
            InMemoryStore(), [("CCO", "a"), ("COC", "b")], kind="tautomers", level="quick"
        )
    )

    assert any("electronic energy" in warning for warning in distribution.warnings)
    assert all(entry.gibbs_free_energy_hartree is None for entry in distribution.species)


def test_a_ranking_past_the_ceiling_reports_what_it_left_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A distribution over a truncated set is confident about the wrong universe unless it says so.

    `enumerated` carries what the caller started from, so the gap between it and the ranked count is
    visible without reading a warning — and the warning is there too.
    """
    install(monkeypatch, FakeCalcServer())
    monkeypatch.setattr(calc_settings, "species_ranking_max", 2)

    distribution = _run(
        compose.species_ranking(
            InMemoryStore(),
            [("CCO", "a"), ("COC", "b"), ("CCC", "c")],
            kind="tautomers",
            level="quick",
        )
    )

    assert distribution.enumerated == 3
    assert len(distribution.species) == 2
    # The *number*, not just the substring. Asserting `"were enumerated" in warning` is what let
    # this ship reading "3 species were enumerated and the -1 lowest-priority were not computed":
    # the branch runs only when the set exceeds the ceiling, so `ceiling - len(species)` was always
    # negative. A chemist-facing count is worth pinning as a count.
    truncation = next(w for w in distribution.warnings if "were enumerated" in w)
    assert "1 that were dropped" in truncation, truncation
    assert "-" not in truncation.replace("lowest-", ""), (
        f"a negative count reached a chemist-facing warning: {truncation}"
    )


def test_an_empty_species_set_is_refused() -> None:
    """A distribution over nothing is not an empty distribution; it is a caller bug."""
    with pytest.raises(ValueError, match="at least one species"):
        _run(compose.species_ranking(InMemoryStore(), []))


# --- bond dissociation ------------------------------------------------------------------------


def test_a_bond_survey_runs_one_reaction_per_bond_and_ranks_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each bond is one balanced reaction, so the open-shell handling is the one already in use.

    The weakest bond is flagged rather than left to be read off the ordering, because "which bond
    breaks first" is the question and the magnitudes are not trustworthy enough to be the answer.
    """
    install(monkeypatch, FakeCalcServer())

    survey = _run(
        compose.bond_dissociation_survey(
            InMemoryStore(),
            "CCc1ccccc1",
            [
                ((0, 1), "C-C", ["[CH2]c1ccccc1", "[CH3]"]),
                ((1, 2), "C-C", ["[CH2]C", "[c]1ccccc1"]),
            ],
        )
    )

    assert survey.considered == 2
    assert len(survey.bonds) == 2
    assert sum(bond.is_weakest for bond in survey.bonds) == 1
    assert survey.bonds[0].is_weakest, "the ranking must put the weakest bond first"
    assert survey.uncertainty_kcal > 0


def test_a_survey_with_no_breakable_bond_is_refused() -> None:
    """Benzene has no breakable C-C, and an empty survey would read as "nothing breaks"."""
    with pytest.raises(ValueError, match="no breakable bond"):
        _run(compose.bond_dissociation_survey(InMemoryStore(), "c1ccccc1", []))


# --- the budget preflight ---------------------------------------------------------------------


def test_a_fan_out_over_the_ceiling_refuses_before_it_computes_anything(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The fence that matters: refuse in the first second, not after three hours.

    A timeout that fires at the end has already spent the time. The refusal names the count, because
    the caller's next move depends on whether it is eleven species or two hundred.
    """
    server = install(monkeypatch, FakeCalcServer())
    monkeypatch.setattr(calc_settings, "calc_max_primitive_calls", 1)
    monkeypatch.setattr(calc_settings, "species_ranking_max", 8)

    with pytest.raises(ValueError, match=r"would run \d+ calculations"):
        _run(
            compose.species_ranking(
                InMemoryStore(), [("CCO", "a"), ("COC", "b"), ("CCC", "c")], level="thorough"
            )
        )

    assert server.count("relax_structure") == 0, "the refusal came after work had started"
    assert server.count("search_conformer_ensemble") == 0


@pytest.mark.parametrize(
    "composite",
    [
        pytest.param(lambda store: compose.refined_ensemble(store, "CCO"), id="refined_ensemble"),
        pytest.param(
            lambda store: compose.ensemble_property(store, "CCO", prop="dipole_debye"),
            id="ensemble_property",
        ),
    ],
)
def test_the_ensemble_composites_also_refuse_before_the_search(
    monkeypatch: pytest.MonkeyPatch, composite: Any
) -> None:
    """Both of these awaited the CREST search and *then* checked the budget.

    That is the exact inversion `budget.py` exists to prevent — "a timeout that fires after three
    hours has already spent three hours" — and it left the most expensive call in the bundle
    outside the fence. Only `species_ranking` was covered by the test above, so the two that had
    the defect were the two nobody asserted.

    The count assertion is the whole test: a refusal that arrives after `search_conformer_ensemble`
    ran is not a preflight, however correct its message.
    """
    server = install(monkeypatch, FakeCalcServer())
    monkeypatch.setattr(calc_settings, "calc_max_primitive_calls", 1)

    with pytest.raises(ValueError, match=r"would run \d+ calculations"):
        _run(composite(InMemoryStore()))

    assert server.count("search_conformer_ensemble") == 0, (
        "the conformer search ran before the budget was checked"
    )
    assert server.count("relax_structure") == 0


def test_a_published_survey_names_the_method_the_server_ran(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`BondDissociationSurvey.method` must come off the result, never off local config.

    `settings.xtb_method` describes a calculation this process no longer runs — the physics is
    `Chemclaw3-mcp`'s since `D-2026-08-16-the-physics-leaves-the-cache-stays`. A deployment whose
    env says one method while the server runs another would publish a Temporal wire type, PR-gated
    into the knowledge graph, asserting the wrong level of theory. `reaction_energy` carries the
    argument in a comment and reads it off the result; this composite did not, alone among the
    three added beside it.

    The setting is moved rather than the server's answer, so the test fails for the right reason:
    with the defect present the survey reports "WRONG-METHOD" because that is what the env said.
    """
    install(monkeypatch, FakeCalcServer())
    monkeypatch.setattr(calc_settings, "xtb_method", "WRONG-METHOD")

    survey = _run(
        compose.bond_dissociation_survey(
            InMemoryStore(),
            "CCO",
            [((0, 1), "C-C", ["[CH3]", "[CH2]O"])],
        )
    )

    assert survey.method == "GFN2-xTB", (
        f"the survey published {survey.method!r} rather than what the server ran"
    )


# --- species distributions across media ---------------------------------------------------------


def test_a_species_screen_ranks_every_form_in_every_medium_and_includes_the_gas_phase(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The fan-out's contract: N species x (M solvents + 1 reference), each relaxed once.

    The gas phase is prepended rather than asked for, for the reason `solvent_comparison` prepends
    it: "the medium barely matters here" is a real answer and is invisible without a reference
    point. So two tautomers in two solvents is six relaxations, not four.
    """
    server = install(monkeypatch, FakeCalcServer())
    store = InMemoryStore()

    comparison = _run(
        compose.species_solvent_comparison(
            store,
            [("CC(=O)CC(C)=O", "keto"), ("CC(O)=CC(C)=O", "enol")],
            ["water", "toluene"],
            kind="tautomers",
        )
    )

    assert server.count("relax_structure") == 6, "2 species x (2 solvents + gas phase)"
    assert [d.solvent for d in comparison.distributions] == [None, "water", "toluene"]
    assert comparison.kind == "tautomers"
    # Every species appears in every medium's standings, in media order.
    assert {response.label for response in comparison.responses} == {"keto", "enol"}
    for response in comparison.responses:
        assert [standing.solvent for standing in response.standings] == [None, "water", "toluene"]


def test_a_screen_that_reorders_the_ranking_says_so_rather_than_only_shifting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`dominance_changes` is the finding, and it has to survive the sort.

    `species_ranking` returns its species sorted by relative energy, so the same index is a
    different form in two media exactly when the ranking reorders — which is why the transpose is
    keyed by SMILES. Here the enol is stabilised in water enough to overtake the keto form, and the
    result must report a changed dominant rather than two distributions a reader has to diff.
    """
    # Keyed on the **canonical** SMILES, which is what reaches the server:
    # `require_canonical_smiles` rewrites the enol `CC(O)=CC(C)=O` to `CC(=O)C=C(C)O` before
    # any structure is embedded.
    keto, enol = "CC(=O)CC(C)=O", "CC(=O)C=C(C)O"
    server = install(
        monkeypatch,
        FakeCalcServer(
            solvent_shifts={
                # Keto 2 kcal/mol lower in the gas phase, enol 3 kcal/mol lower again in water:
                # the ordering is unambiguous in both media rather than resting on a tie-break.
                (keto, ""): -0.0032,
                (enol, "water"): -0.0080,
            }
        ),
    )

    comparison = _run(
        compose.species_solvent_comparison(
            InMemoryStore(),
            [(keto, "keto"), (enol, "enol")],
            ["water"],
            kind="tautomers",
            level="quick",
        )
    )

    assert server.count("relax_structure") == 4
    assert comparison.dominance_changes is True
    assert comparison.distributions[0].dominant.label == "keto", "gas phase"
    assert comparison.distributions[1].dominant.label == "enol", "water"
    assert comparison.largest_swing_kcal > 1.0
    assert any("dominant form is not the same" in warning for warning in comparison.warnings)


def test_a_screen_the_method_cannot_resolve_refuses_to_report_a_difference(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A continuum model resolving tenths of a kcal/mol between media is reading its own noise.

    The fake's energy is a function of atom count, so with no shift every medium is identical and
    the swing is exactly zero — the strongest form of the case this warning exists for.
    """
    install(monkeypatch, FakeCalcServer())

    comparison = _run(
        compose.species_solvent_comparison(
            InMemoryStore(),
            [("CCO", "a"), ("COC", "b")],
            ["water", "thf"],
            level="quick",
        )
    )

    assert comparison.largest_swing_kcal == 0.0
    assert comparison.dominance_changes is False
    assert any("does not distinguish them" in warning for warning in comparison.warnings)


def test_screening_the_same_species_in_a_medium_already_computed_pays_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every part is keyed on its solvent, so a screen that adds a medium reuses the rest.

    This is the D-011 property the whole split turns on, at the shape where it pays most: a chemist
    who ranked tautomers in water and then asks for toluene should pay for toluene alone.
    """
    server = install(monkeypatch, FakeCalcServer())
    store = InMemoryStore()
    species = [("CCO", "a"), ("COC", "b")]

    _run(compose.species_solvent_comparison(store, species, ["water"], level="quick"))
    first = server.count("relax_structure")
    _run(compose.species_solvent_comparison(store, species, ["water", "toluene"], level="quick"))

    assert first == 4, "2 species x (water + gas phase)"
    assert server.count("relax_structure") == first + 2, "only toluene is new"


def test_a_screen_counts_its_whole_fan_out_against_the_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`species x media` is the number that surprises, so it is the number the fence checks.

    Counting one medium would let five solvents through a ceiling sized for one, which is the
    direction a preflight must never be wrong in.
    """
    install(monkeypatch, FakeCalcServer())
    monkeypatch.setattr(calc_settings, "calc_max_primitive_calls", 10)

    with pytest.raises(ValueError, match="would run 16 calculations"):
        _run(
            compose.species_solvent_comparison(
                InMemoryStore(),
                [("CCO", "a"), ("COC", "b")],
                ["water", "toluene", "thf"],
                level="quick",
            )
        )
