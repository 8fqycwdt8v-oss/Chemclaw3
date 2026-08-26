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


# --- pKa from macrostates ---------------------------------------------------------------------


def test_a_pka_is_two_searches_and_a_subtraction(monkeypatch: pytest.MonkeyPatch) -> None:
    """The whole composite: the neutral's conformers, its deprotomers, one difference.

    Two searches and no third — the count is what says this is a *composition* of cached primitives
    rather than a calculation of its own. A third would be a conformer search of the anion, which is
    a different pipeline and would need its own calibration.
    """
    server = install(monkeypatch, FakeCalcServer())
    store = InMemoryStore()

    result = _run(compose.microstate_pka(store, "Oc1ccccc1"))

    assert server.count("search_conformer_ensemble") == 2
    assert result.branch == "acid"
    assert result.site_smiles == "[O-]c1ccccc1", "which proton came off is half the answer"
    assert result.neutral.search == "conformers" and result.ionised.search == "deprotomers"


def test_the_ionised_side_is_computed_as_the_anion(monkeypatch: pytest.MonkeyPatch) -> None:
    """The defect this whole change exists for, asserted from the composite's own side.

    A deprotomer ensemble whose members carry the neutral's charge is not a slightly wrong label: it
    is a converged energy for a species that does not exist, and every pKa built on it would be a
    confident number about nothing. The ensembles this composite reports are the evidence for its
    pKa, so the charge has to be visible in them.
    """
    install(monkeypatch, FakeCalcServer())
    store = InMemoryStore()

    result = _run(compose.microstate_pka(store, "Oc1ccccc1"))

    assert all(member.structure.charge == -1 for member in result.ionised.conformers)
    assert all(member.structure.charge == 0 for member in result.neutral.conformers)


def test_asking_twice_pays_for_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Both halves are keyed primitives, so the second pKa is arithmetic over rows that exist.

    This is the economy the split is for: a CREST search is the most expensive single call in the
    system, and a composite that re-ran one per question would make the careful pKa unaffordable
    exactly where it is worth having.
    """
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

    And the number it reports is the *conjugate acid's* pKa — what is tabulated for amines and what
    an extraction pH is set against — which is why the branch travels on the result rather than
    being inferred by a reader from the molecule.
    """
    install(monkeypatch, FakeCalcServer())
    store = InMemoryStore()

    result = _run(compose.microstate_pka(store, "c1ccncc1"))

    assert result.branch == "base"
    assert result.ionised.search == "protomers"
    assert all(member.structure.charge == 1 for member in result.ionised.conformers)


def test_a_molecule_with_no_equilibrium_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """Benzene has no proton on a heteroatom and no nitrogen, so there is nothing to answer.

    Refused rather than computed: CREST would rank benzene's C-H deprotomers happily, and the
    calibration would turn that into a confident aqueous pKa for an equilibrium that does not exist
    in water. Two CREST searches is also an expensive way to produce a meaningless number.
    """
    install(monkeypatch, FakeCalcServer())
    store = InMemoryStore()

    with pytest.raises(CalculationDomainError, match="no acid/base equilibrium"):
        _run(compose.microstate_pka(store, "c1ccccc1"))


def test_an_aliphatic_amine_is_warned_about_rather_than_quietly_reported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The one limit CREST does not remove, because it is the solvent model rather than the search.

    Over 13 reference amines the computed basicity correlates with the measured pKa at Spearman
    -0.17. The sampling is not what fails — ALPB is — so a better ensemble produces a better-sampled
    number with no ranking information, which is worse than a bad number that looks bad.
    """
    install(monkeypatch, FakeCalcServer())
    store = InMemoryStore()

    result = _run(compose.microstate_pka(store, "CCN"))

    assert result.branch == "base"
    assert any("aliphatic nitrogen" in warning for warning in result.warnings)


def test_a_macrostate_is_more_stable_than_its_best_microstate() -> None:
    """The arithmetic the whole composite turns on, checked without a server.

    Two microstates within RT of each other make the macrostate more stable than either — by
    RT ln 2 when they are degenerate, which is 0.41 kcal/mol at 298 K and about 0.3 pKa units
    through a fitted slope. Taking the minimum instead of the sum silently loses exactly that.
    """
    degenerate = macrostate_free_energy_kcal([0.0, 0.0], [1, 1], 298.15)
    single = macrostate_free_energy_kcal([0.0], [1], 298.15)
    far_apart = macrostate_free_energy_kcal([0.0, 10.0], [1, 1], 298.15)

    assert single == 0.0
    assert abs(degenerate - (-0.4113)) < 1e-3, "RT ln 2 at 298 K"
    assert abs(far_apart) < 1e-6, "a microstate 10 kcal/mol up carries no population"


def test_a_deeper_search_than_the_calibration_says_so(monkeypatch: pytest.MonkeyPatch) -> None:
    """Paying for a better ensemble does not buy a calibration fitted on one.

    A deeper search finds lower members on *both* sides, so it moves the free-energy difference the
    slope was fitted against. The ensembles are genuinely better and the mapping is still the quick
    search's — which is a thing the reader has to be told, not a thing to quietly average over.
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

    The reference set is O-H and S-H only. CREST ranks every site, so an imide or a sulfonamide can
    legitimately come back with the proton off nitrogen — a correct answer to "which proton" and an
    unfitted one to "what is the pKa". Quoting the number without the warning is how a calibration
    silently acquires a domain nobody measured.
    """
    install(monkeypatch, FakeCalcServer())
    store = InMemoryStore()

    result = _run(compose.microstate_pka(store, "O=C(N)c1ccccc1", branch="acid"))

    assert result.site_smiles is not None
    assert any("came off nitrogen" in warning for warning in result.warnings)
