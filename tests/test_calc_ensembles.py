"""The multi-step composites: did the fan-out ask for the right parts, and stop asking on a repeat?

These have no cache row (their key would name an output), so correctness is reaching keyed parts
once each (D-011); a missed cache in a fan-out costs a CREST search per species. Driven against
`tests/calc_server_fake.py`, whose ensemble members are distinct geometries.
"""

import asyncio
import math
from typing import Any

import pytest

from chemclaw.connectors.calc import compose
from chemclaw.core.config import settings as calc_settings
from chemclaw.science.calc.store import InMemoryStore
from chemclaw.science.calc.thermo import macrostate_free_energy_kcal
from chemclaw.science.calc.uncertainty import CalculationDomainError
from tests.calc_server_fake import FakeCalcServer, install


def _run(coroutine: Any) -> Any:
    """Run one coroutine to completion, the shape every test here uses."""
    return asyncio.run(coroutine)


# --- refined ensembles ------------------------------------------------------------------------


def test_a_refined_ensemble_optimizes_and_takes_a_hessian_per_member(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A refined ensemble optimizes and takes a Hessian per member.

    One search, then one optimization and one Hessian per kept member, bounded by
    `ensemble_refine_top_n`.
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
    """Refining the same ensemble twice pays nothing.

    Every part is keyed, so a repeat is a lookup per part.
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

    Coverage is reported as a number and warned about below the threshold, as
    `ensemble_from_members` does for `max_members`.
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
    """A property average evaluates at every member and reports the spread.

    One properties call per member at its own geometry; the range shows whether the property is a
    single number.
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

    It must reach `compute_fukui_at` — the primitive whose absence was a `BACKLOG.md` row — and
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
    """An averaged Fukui pairs atoms by index, not by rank.

    `SiteReactivityResult.sites` is ordered most-susceptible first and truncated, so a position is a
    different atom in different conformers. Atom 0 is 0.1 and atom 1 is 0.9 in every conformer, so a
    correct pairing gives zero spread; position pairing would give 0.5 and 0.5.
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
    """An atom missing from one conformer is dropped rather than part-averaged.

    Truncation to `top_n` means conformers carry different atom sets; averaging over the members
    that happen to carry an atom would masquerade as an ensemble mean.
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

    Every species goes through `_species_energy`, as the reaction composites do, so the two share
    cache entries and cannot disagree.
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

    Two tautomers can differ more in zero-point energy than in electronic energy, so the ranking
    says which was computed.
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
    # The number, not just the substring: the truncation count is chemist-facing and must be right.
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


def test_a_bond_survey_does_not_assert_a_symmetry_number_it_cannot_know(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The survey does no point-group detection, so it may not mark sigma *stated*.

    A literal sigma=1 marked as stated would disarm `reaction_energy`'s withhold-and-warn, and is
    wrong for most homolysis products (benzene 12, phenyl 2, methyl 6). For benzene's C-H the free
    energy would be off by RT ln(2/12) ≈ -1.06 kcal/mol.
    """
    install(monkeypatch, FakeCalcServer())
    gas_constant_kcal = 1.987204258640832e-3
    hidden = gas_constant_kcal * 298.15 * math.log(2 / 12)
    print(f"withheld sigma term for benzene C-H = {hidden!r} kcal/mol")

    survey = _run(
        compose.bond_dissociation_survey(
            InMemoryStore(),
            "c1ccccc1",
            [((0, 1), "C-H", ["[c]1ccccc1", "[H]"])],
            level="standard",
        )
    )

    assert hidden == pytest.approx(-1.0615, abs=1e-3)
    (unstated,) = [line for line in survey.warnings if "symmetry number" in line]
    assert "c1ccccc1" in unstated, unstated
    # ΔH is sigma-independent, so the survey's energy must survive. Compared against
    # `reaction_energy`, since only agreement between the two is meaningful with the fake's
    # placeholder energies.
    reaction = _run(
        compose.reaction_energy(
            InMemoryStore(), ["c1ccccc1"], ["[c]1ccccc1", "[H]"], level="standard"
        )
    )
    assert reaction.delta_g_kcal is None, "an unstated sigma must still withhold the free energy"
    assert reaction.delta_h_kcal is not None
    assert survey.bonds[0].dissociation_energy_kcal == pytest.approx(
        round(reaction.delta_h_kcal, 1), abs=1e-9
    )


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
    """The ensemble composites also refuse before the search.

    A budget checked after the CREST search has already spent it. The call count is the test.
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
    """A published survey names the method the server ran.

    `BondDissociationSurvey.method` comes off the result, never local config, since the server's
    method may differ from `settings.xtb_method`. The setting is moved, so a regression reports
    "WRONG-METHOD".
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

    The gas phase is prepended as the reference, so two tautomers in two solvents is six
    relaxations.
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

    Each medium's species are sorted by energy, so the transpose is keyed by SMILES. Here the enol
    overtakes the keto form in water and the result reports the changed dominant.
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


# --- pKa from macrostates ---------------------------------------------------------------------


def test_a_pka_is_two_searches_and_a_subtraction(monkeypatch: pytest.MonkeyPatch) -> None:
    """The whole composite: the neutral's conformers, its deprotomers, one difference.

    Exactly two searches; a third (the anion's conformers) would be a different, uncalibrated
    pipeline.
    """
    server = install(monkeypatch, FakeCalcServer())
    store = InMemoryStore()

    result = _run(compose.microstate_pka(store, "Oc1ccccc1"))

    assert server.count("search_conformer_ensemble") == 2
    assert result.branch == "acid"
    assert result.site_smiles == "[O-]c1ccccc1", "which proton came off is half the answer"
    assert result.neutral.search == "conformers" and result.ionised.search == "deprotomers"


def test_the_ionised_side_is_computed_as_the_anion(monkeypatch: pytest.MonkeyPatch) -> None:
    """The ionised side is computed as the anion.

    A deprotomer ensemble at the neutral's charge is a species that does not exist; the reported
    ensembles are the pKa's evidence, so the charge must be visible in them.
    """
    install(monkeypatch, FakeCalcServer())
    store = InMemoryStore()

    result = _run(compose.microstate_pka(store, "Oc1ccccc1"))

    assert all(member.structure.charge == -1 for member in result.ionised.conformers)
    assert all(member.structure.charge == 0 for member in result.neutral.conformers)


def test_asking_twice_pays_for_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Both halves are keyed primitives, so the second pKa is arithmetic over rows that exist."""
    server = install(monkeypatch, FakeCalcServer())
    store = InMemoryStore()

    first = _run(compose.microstate_pka(store, "Oc1ccccc1"))
    second = _run(compose.microstate_pka(store, "Oc1ccccc1"))

    assert server.count("search_conformer_ensemble") == 2, "two searches in total, not four"
    assert first.pka == second.pka


def test_a_second_temperature_is_free(monkeypatch: pytest.MonkeyPatch) -> None:
    """Populations depend on a temperature the search never saw, so re-weighting is arithmetic.

    The same property `conformer_ensemble` has, and it has to survive composition: a pKa at 310 K
    after one at 298 K must not be a second pair of searches.
    """
    server = install(monkeypatch, FakeCalcServer())
    store = InMemoryStore()

    _run(compose.microstate_pka(store, "Oc1ccccc1"))
    warmer = _run(compose.microstate_pka(store, "Oc1ccccc1", temperature_k=310.0))

    assert server.count("search_conformer_ensemble") == 2
    assert warmer.temperature_k == 310.0


def test_a_base_is_the_other_search_and_the_other_calibration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pyridine has no proton to lose, so `auto` asks the protonation question instead.

    It reports the conjugate acid's pKa (what is tabulated for amines), and the branch travels on
    the result.
    """
    install(monkeypatch, FakeCalcServer())
    store = InMemoryStore()

    result = _run(compose.microstate_pka(store, "c1ccncc1"))

    assert result.branch == "base"
    assert result.ionised.search == "protomers"
    assert all(member.structure.charge == 1 for member in result.ionised.conformers)


def test_a_molecule_with_no_equilibrium_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """Benzene has no proton on a heteroatom and no nitrogen, so there is nothing to answer.

    Refused rather than computed: CREST would rank C-H deprotomers and the calibration would report
    a confident pKa for an equilibrium that does not exist in water.
    """
    install(monkeypatch, FakeCalcServer())
    store = InMemoryStore()

    with pytest.raises(CalculationDomainError, match="no acid/base equilibrium"):
        _run(compose.microstate_pka(store, "c1ccccc1"))


def test_an_aliphatic_amine_is_warned_about_rather_than_quietly_reported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The one limit CREST does not remove, because it is the solvent model rather than the search.

    For aliphatic amines the computed basicity does not rank with measured pKa (ALPB fails, not
    sampling), so the result is warned about.
    """
    install(monkeypatch, FakeCalcServer())
    store = InMemoryStore()

    result = _run(compose.microstate_pka(store, "CCN"))

    assert result.branch == "base"
    assert any("aliphatic nitrogen" in warning for warning in result.warnings)


def test_a_macrostate_is_more_stable_than_its_best_microstate() -> None:
    """A macrostate is more stable than its best microstate.

    Two degenerate microstates stabilize the macrostate by RT ln 2 (0.41 kcal/mol at 298 K, about
    0.3 pKa units); taking the minimum loses that.
    """
    degenerate = macrostate_free_energy_kcal([0.0, 0.0], [1, 1], 298.15)
    single = macrostate_free_energy_kcal([0.0], [1], 298.15)
    far_apart = macrostate_free_energy_kcal([0.0, 10.0], [1, 1], 298.15)

    assert single == 0.0
    assert abs(degenerate - (-0.4113)) < 1e-3, "RT ln 2 at 298 K"
    assert abs(far_apart) < 1e-6, "a microstate 10 kcal/mol up carries no population"


def test_a_deeper_search_than_the_calibration_says_so(monkeypatch: pytest.MonkeyPatch) -> None:
    """Paying for a better ensemble does not buy a calibration fitted on one.

    A deeper search shifts the free-energy difference the slope was fitted against, so the result
    says the mapping is still the quick search's.
    """
    install(monkeypatch, FakeCalcServer())
    store = InMemoryStore()

    result = _run(compose.microstate_pka(store, "Oc1ccccc1", effort="extensive"))

    assert any("calibration was fitted at 'quick'" in warning for warning in result.warnings)


def test_a_solvent_the_calibration_was_not_fitted_in_says_so(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The free energy is for the medium asked for; the pKa mapping is not.

    Both calibrations are fitted in water, and a pKa is an aqueous quantity by definition — so a
    number computed in acetonitrile is a real free energy wearing the wrong units.
    """
    install(monkeypatch, FakeCalcServer())
    store = InMemoryStore()

    result = _run(compose.microstate_pka(store, "Oc1ccccc1", solvent="acetonitrile"))

    assert any("fitted in water" in warning for warning in result.warnings)


def test_a_deprotonation_off_the_fitted_domain_says_so(monkeypatch: pytest.MonkeyPatch) -> None:
    """An N-H acid is a real winner and an extrapolation, and the result has to say which.

    The reference set is O-H and S-H only, so a proton lost from nitrogen gets a warning.
    """
    install(monkeypatch, FakeCalcServer())
    store = InMemoryStore()

    result = _run(compose.microstate_pka(store, "O=C(N)c1ccccc1", branch="acid"))

    assert result.site_smiles is not None
    assert any("came off nitrogen" in warning for warning in result.warnings)


def test_a_search_is_given_the_samplers_budget_not_a_hessians(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The client may not abandon a calculation the server is still running.

    A search is given the sampler's read bound, not a Hessian's; a shorter client bound discards an
    answer the server computes anyway.
    """
    server = install(monkeypatch, FakeCalcServer())
    store = InMemoryStore()

    _run(compose.conformer_ensemble(store, "CCO"))

    # Two sessions: the embed takes the default, the search takes the sampler's. Both halves are
    # the point — widening the bound for *every* call would drop the one that catches a mute host.
    assert server.timeouts == [None, calc_settings.calc_sampling_timeout_seconds]
    assert calc_settings.calc_sampling_timeout_seconds > calc_settings.calc_server_timeout_seconds


# --- a species set with a charged member: the pc-03 defect as a ranking -------------------------

# Glycine's protonation microstates as `enumerate_protonation_states` hands them over: cation,
# neutral, anion. Net charges +1, 0, -1 — the mixed-charge set `ranking="microstates"` exists for,
# and the worst case of a gas-phase difference, since every gap between them is unscreened charge.
_GLYCINE_MICROSTATES = [
    ("[NH3+]CC(=O)O", "cation"),
    ("NCC(=O)O", "neutral"),
    ("NCC(=O)[O-]", "anion"),
]


def test_a_gas_phase_ranking_of_mixed_charge_microstates_is_refused_before_anything_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`species_ranking(solvent=None)` over microstates is refused, naming the charged members."""
    server = install(monkeypatch, FakeCalcServer())
    with pytest.raises(ValueError, match="not physically meaningful") as refused:
        _run(
            compose.species_ranking(
                InMemoryStore(), _GLYCINE_MICROSTATES, kind="microstates", level="quick"
            )
        )
    assert "[NH3+]CC(=O)O" in str(refused.value) and "NCC(=O)[O-]" in str(refused.value)
    assert server.calls == [], "the refusal came after work had started"


def test_the_same_microstates_in_water_rank_and_carry_the_ion_caveat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With a continuum the ranking runs, and its result says what an ion's energy there is."""
    install(monkeypatch, FakeCalcServer())
    ranking = _run(
        compose.species_ranking(
            InMemoryStore(),
            _GLYCINE_MICROSTATES,
            kind="microstates",
            solvent="water",
            level="quick",
        )
    )
    assert len(ranking.species) == 3
    assert any("charged species present" in line for line in ranking.warnings)


def test_a_neutral_tautomer_ranking_still_runs_in_the_gas_phase(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The refusal's boundary: a same-charge neutral set is unaffected and carries no caveat."""
    install(monkeypatch, FakeCalcServer())
    ranking = _run(
        compose.species_ranking(
            InMemoryStore(),
            [("CC(=O)CC(C)=O", "keto"), ("CC(O)=CC(C)=O", "enol")],
            kind="tautomers",
            level="quick",
        )
    )
    assert not any("charged species" in line for line in ranking.warnings)


def test_a_screen_over_charged_microstates_drops_the_gas_reference_and_says_so(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The across-solvents screen's automatic gas row is the refused number, so it is left out."""
    install(monkeypatch, FakeCalcServer())
    comparison = _run(
        compose.species_solvent_comparison(
            InMemoryStore(),
            _GLYCINE_MICROSTATES,
            ["water", "dmso"],
            kind="microstates",
            level="quick",
        )
    )
    assert [d.solvent for d in comparison.distributions] == ["water", "dmso"]
    assert any("no gas-phase reference" in line for line in comparison.warnings)
    assert any("charged species present" in line for line in comparison.warnings)
