"""The reaction composite: balance, consistency, caching and honesty (xTB plan X4).

Real calculations again. The interesting assertions are not that a number comes out —
they are that an *unbalanced* equation does not, that the second reaction sharing a
species does not recompute it, and that the measured limits of the method are pinned
where a future change would trip over them.
"""

import asyncio
import math

import pytest

from chemclaw.core.config import settings
from chemclaw.science.calc.reaction import (
    check_balance,
    compare_solvent_effects,
    compute_reaction_energy,
)
from chemclaw.science.calc.store import InMemoryStore

# R T at the default temperature, in kcal/mol: the exact size of the free-energy shift one
# factor of e in a rotational symmetry number is worth.
_RT_KCAL = 1.987204259e-3 * settings.xtb_thermo_temperature_k


def test_esterification_returns_all_three_deltas() -> None:
    """The Fischer esterification of the green-chemistry eval case, computed end to end.

    Acetic acid + ethanol -> ethyl acetate + water sits near equilibrium in reality
    (K ~ 4, so ΔG ~ -0.8 kcal/mol). Asserting a window rather than a value: what is being
    pinned is that a balanced, four-species reaction produces ΔE, ΔH and ΔG that are
    consistent with each other and in the right neighbourhood.
    """

    async def _run() -> None:
        result = await compute_reaction_energy(
            InMemoryStore(),
            ["CC(=O)O", "CCO"],
            ["CCOC(C)=O", "O"],
            # Water is C2v; the other three have no rotational symmetry. ΔG is reported
            # only because this is stated — see the symmetry tests below.
            symmetry_numbers={"CC(=O)O": 1, "CCO": 1, "CCOC(C)=O": 1, "O": 2},
        )
        assert result.delta_h_kcal is not None
        assert result.delta_g_kcal is not None
        assert -8 < result.delta_g_kcal < 8  # a near-thermoneutral equilibrium
        assert len(result.species) == 4
        assert [entry.symmetry_number for entry in result.species] == [1, 1, 1, 2]
        assert {entry.role for entry in result.species} == {"reactant", "product"}
        assert all(entry.is_minimum for entry in result.species)
        assert result.uncertainty_kcal > 0
        assert result.conformer_treatment == "single"

    asyncio.run(_run())


def test_a_shared_species_is_computed_once_across_two_reactions() -> None:
    """The second reaction reuses the first's species from the store (D-011).

    Asserting cache *hits*, not wall clock: a timing assertion would pass on a machine
    that was merely fast and would flake on one that was busy.
    """

    async def _run() -> None:
        store = InMemoryStore()
        first = await compute_reaction_energy(
            store, ["CC(=O)O", "CCO"], ["CCOC(C)=O", "O"], level="quick"
        )
        assert first.cache_hits == 0
        second = await compute_reaction_energy(
            store, ["CC(=O)O", "CO"], ["COC(C)=O", "O"], level="quick"
        )
        # Acetic acid and water are shared with the first reaction; methanol and methyl
        # acetate are new.
        assert second.cache_hits == 2

    asyncio.run(_run())


@pytest.mark.parametrize(
    ("reactants", "products", "message"),
    [
        (["CCO"], ["CC(=O)O"], "not atom-balanced"),
        (["CC(=O)O"], ["CC(=O)[O-]"], "not atom-balanced"),
        (["CC(=O)[O-]", "[H+]"], ["CC(=O)O", "[Na+]"], "not atom-balanced"),
    ],
    ids=["missing atoms", "lost a proton", "charge carried by a foreign ion"],
)
def test_an_unbalanced_equation_is_rejected(
    reactants: list[str], products: list[str], message: str
) -> None:
    """An unbalanced reaction is refused, not computed (gate G4).

    This is the failure that matters most here: the difference of two unbalanced sides
    includes whatever atoms they do not share, so it is meaningless rather than
    imprecise — and it looks exactly like an ordinary number.
    """
    with pytest.raises(ValueError, match=message):
        check_balance(reactants, products)


def test_charge_imbalance_is_named_separately() -> None:
    """Same atoms, different total charge — the case an atom count cannot see.

    Chloride to a chlorine radical balances atom for atom and is an ionization, not a
    reaction; without the separate charge check it would return an "energy" for the
    electron that quietly went missing.
    """
    with pytest.raises(ValueError, match="not charge-balanced"):
        check_balance(["[Cl-]"], ["[Cl]"])


def test_quick_level_gives_electronic_energies_only() -> None:
    """`quick` skips the Hessian, so it reports ΔE and honestly nothing else."""

    async def _run() -> None:
        result = await compute_reaction_energy(InMemoryStore(), ["CCO"], ["COC"], level="quick")
        assert result.delta_h_kcal is None
        assert result.delta_g_kcal is None
        assert result.delta_e_kcal > 0  # ethanol is the more stable isomer
        assert all(entry.is_minimum is None for entry in result.species)

    asyncio.run(_run())


def test_homolysis_is_computable_because_multiplicity_comes_from_the_smiles() -> None:
    """A bond dissociation needs no hand-declared spin state: `[CH3]` says it itself.

    And the result carries the warning it must: GFN2 homolysis energies are far too
    large in absolute terms (ethane's C-C comes out well above its measured 90
    kcal/mol), so this is a ranking tool. The assertion is deliberately loose on the
    value and strict on the two things that are true — it is strongly endothermic, and
    the open-shell caveat is attached.
    """

    async def _run() -> None:
        result = await compute_reaction_energy(InMemoryStore(), ["CC"], ["[CH3]", "[CH3]"])
        assert [entry.multiplicity for entry in result.species] == [1, 2, 2]
        assert result.delta_h_kcal is not None
        assert result.delta_h_kcal > 50  # breaking a C-C bond costs a lot
        assert any("open-shell" in warning for warning in result.warnings)

    asyncio.run(_run())


def test_a_benzylic_c_h_is_weaker_than_a_methane_c_h() -> None:
    """The ordering BDEs are actually for: benzylic radicals are stabilized.

    Measured C-H bond strengths are 89.7 (toluene) vs 105 (methane) kcal/mol. GFN2
    overestimates both by a wide margin, so what is pinned is the *ordering* and that it
    is a substantial gap — the same "rank, do not measure" conclusion the pKa work
    reached, arrived at the same way.
    """

    async def _run() -> None:
        store = InMemoryStore()
        benzylic = await compute_reaction_energy(
            store, ["Cc1ccccc1"], ["[CH2]c1ccccc1", "[H]"], level="quick"
        )
        methane = await compute_reaction_energy(store, ["C"], ["[CH3]", "[H]"], level="quick")
        assert benzylic.delta_e_kcal < methane.delta_e_kcal - 10

    asyncio.run(_run())


def test_solvent_comparison_ranks_and_admits_when_it_cannot_distinguish() -> None:
    """The gas phase is included, the ordering is by ΔG, and a small spread is flagged.

    The warning is the point. An implicit continuum model resolving a fraction of a
    kcal/mol between two solvents is reading its own noise, and a result that presented
    that ordering without saying so would be the most confidently wrong output in this
    module.
    """

    async def _run() -> None:
        result = await compare_solvent_effects(
            InMemoryStore(),
            ["CC(=O)O", "CCO"],
            ["CCOC(C)=O", "O"],
            ["water", "toluene"],
            level="quick",
        )
        assert [effect.solvent for effect in result.effects].count(None) == 1  # gas phase
        assert len(result.effects) == 3
        ranked = [effect.delta_e_kcal for effect in result.effects]
        assert ranked == sorted(ranked)
        assert result.best_solvent == result.effects[0].solvent
        if result.spread_kcal <= result.uncertainty_kcal:
            assert any("does not distinguish" in warning for warning in result.warnings)

    asyncio.run(_run())


def test_no_solvents_is_an_error_not_an_empty_ranking() -> None:
    """An empty comparison would return a "best solvent" chosen from nothing."""

    async def _run() -> None:
        with pytest.raises(ValueError, match="at least one solvent"):
            await compare_solvent_effects(InMemoryStore(), ["CCO"], ["CCO"], [])

    asyncio.run(_run())


def test_the_open_shell_caveat_is_not_gated_on_one_level() -> None:
    """A homolysis carries its warning at every level, not only at `standard`.

    The bug: the condition read `level == "standard"`, so the caveat about unrestricted
    GFN2 energies vanished at `quick` and at `thorough` — dropping it from exactly the
    `thorough` run a user paid the most for. The warning is about the electronic energies,
    which every level differences, so no level is exempt.
    """

    async def _run() -> None:
        store = InMemoryStore()
        # H2 -> 2 H., the smallest homolysis: two doublets on the product side.
        result = await compute_reaction_energy(store, ["[H][H]"], ["[H]", "[H]"], level="quick")
        assert any("open-shell" in warning for warning in result.warnings)

    asyncio.run(_run())


def test_a_reaction_reports_which_conformational_treatment_produced_it() -> None:
    """`conformer_treatment` was hard-coded to "single" and so was wrong wherever it mattered.

    At `thorough` an ensemble is searched and its conformational entropy folded into every
    ΔG — the one level where a reader needs to know the treatment was *not* single, and
    the one level the field misreported. Asserted at `quick` here (no crest needed); the
    thorough branch is covered by the ensemble tests.
    """

    async def _run() -> None:
        result = await compute_reaction_energy(InMemoryStore(), ["CCO"], ["CCO"], level="quick")
        assert result.conformer_treatment == "single"
        assert all(entry.conformational_entropy_kcal is None for entry in result.species)

    asyncio.run(_run())


def test_the_exotherm_flag_survived_the_consolidation() -> None:
    """The one capability the removed exotherm screen had, now on the composite (D-108).

    `chemclaw.science.calc.reaction_energy` was deleted rather than kept alongside this module, so
    its
    thermal-hazard flag had to move rather than be dropped — that is the difference between
    consolidating and losing a feature. It reads ΔE against the same configured threshold.

    Asserted on a combustion-like oxidation, which is unambiguously strongly exothermic at
    any level of theory, and on a thermoneutral identity so the flag is not simply always on.
    """

    async def _run() -> None:
        store = InMemoryStore()
        burn = await compute_reaction_energy(
            store, ["C", "O=O", "O=O"], ["O=C=O", "O", "O"], level="quick"
        )
        assert burn.is_strongly_exothermic is True
        assert burn.delta_e_kcal <= burn.exotherm_threshold_kcal
        assert burn.exotherm_threshold_kcal == settings.reaction_energy_exotherm_threshold_kcal

        nothing = await compute_reaction_energy(store, ["CCO"], ["CCO"], level="quick")
        assert nothing.is_strongly_exothermic is False

    asyncio.run(_run())


def test_the_hydrogens_symmetry_number_reaches_the_free_energy() -> None:
    """Methanol synthesis, CO + 2 H2 -> CH3OH, with sigma stated correctly and stated as 1.

    The bug this pins: `compute_reaction_energy` built one `ThermoSpec` with sigma at its
    default of 1 for every species and had no parameter to change it, so every ΔG carried
    the rotational entropy of a molecule with no symmetry — whatever the molecule was.

    Two H2 (sigma=2) are consumed and nothing symmetric is produced, so the error does not
    cancel across the arrow: the ΔG must move by exactly -2 RT ln 2 = -0.82 kcal/mol when
    the symmetry numbers are told the truth. A run that ignores them moves by 0.00.
    """

    async def _run() -> None:
        store = InMemoryStore()
        reactants, products = ["[C-]#[O+]", "[H][H]", "[H][H]"], ["CO"]
        assumed = await compute_reaction_energy(
            store, reactants, products, symmetry_numbers=dict.fromkeys([*reactants, *products], 1)
        )
        stated = await compute_reaction_energy(
            store,
            reactants,
            products,
            symmetry_numbers={"[C-]#[O+]": 1, "[H][H]": 2, "CO": 1},
        )
        assert assumed.delta_g_kcal is not None
        assert stated.delta_g_kcal is not None
        assert stated.delta_g_kcal - assumed.delta_g_kcal == pytest.approx(
            -2 * _RT_KCAL * math.log(2), abs=0.02
        )
        # Sigma is an entropy term only: it must leave ΔE and ΔH exactly where they were.
        assert stated.delta_e_kcal == assumed.delta_e_kcal
        assert stated.delta_h_kcal == assumed.delta_h_kcal

    asyncio.run(_run())


def test_benzenes_twelvefold_symmetry_is_worth_one_and_a_half_kcal() -> None:
    """The case the "it cancels within a balanced reaction" defence never covered.

    Benzene is D6h, sigma=12, and a hydrogenation of it produces nothing remotely as
    symmetric — 1,3-cyclohexadiene is C2v, sigma=2. Defaulting both to 1 leaves benzene's
    free energy too low by RT ln 12 and the diene's by RT ln 2, and since benzene is the
    reactant those do not cancel: ΔG is too high by RT ln(12/2)... plus the H2's own
    RT ln 2, which is exactly RT ln 12 = 1.47 kcal/mol in total. Half the method's whole
    quoted uncertainty, from bookkeeping rather than from physics.
    """

    async def _run() -> None:
        store = InMemoryStore()
        reactants, products = ["c1ccccc1", "[H][H]"], ["C1=CCCC=C1"]
        assumed = await compute_reaction_energy(
            store, reactants, products, symmetry_numbers=dict.fromkeys([*reactants, *products], 1)
        )
        stated = await compute_reaction_energy(
            store,
            reactants,
            products,
            symmetry_numbers={"c1ccccc1": 12, "[H][H]": 2, "C1=CCCC=C1": 2},
        )
        assert assumed.delta_g_kcal is not None
        assert stated.delta_g_kcal is not None
        assert stated.delta_g_kcal - assumed.delta_g_kcal == pytest.approx(
            -_RT_KCAL * math.log(12), abs=0.02
        )

    asyncio.run(_run())


def test_an_unstated_symmetry_number_withholds_the_free_energy() -> None:
    """No sigma, no ΔG — and the result says which species and why.

    The alternative that was there before is the one thing that must not happen: a ΔG
    computed at sigma=1 for a molecule that has symmetry, reported as an ordinary number.
    ΔE and ΔH do not depend on sigma, so they are still reported — the refusal is exactly
    as wide as the damage.
    """

    async def _run() -> None:
        result = await compute_reaction_energy(
            InMemoryStore(), ["[C-]#[O+]", "[H][H]", "[H][H]"], ["CO"]
        )
        assert result.delta_g_kcal is None
        assert result.delta_h_kcal is not None
        assert result.delta_e_kcal != 0.0
        assert all(entry.symmetry_number is None for entry in result.species)
        (symmetry_warning,) = [w for w in result.warnings if "symmetry number" in w]
        assert "[H][H]" in symmetry_warning and "[C-]#[O+]" in symmetry_warning

    asyncio.run(_run())


def test_a_partially_stated_equation_still_withholds_the_free_energy() -> None:
    """One unstated species is enough: its own R ln(sigma) is in the sum unopposed.

    The symmetric species here is the one that was stated; CO and methanol are the ones
    left out, and both really are sigma=1. The ΔG is withheld anyway, because "sigma is 1"
    and "sigma was not considered" are different claims and only the caller can tell them
    apart. That is the intended trade: obvious is still a statement.
    """

    async def _run() -> None:
        result = await compute_reaction_energy(
            InMemoryStore(),
            ["[C-]#[O+]", "[H][H]", "[H][H]"],
            ["CO"],
            symmetry_numbers={"[H][H]": 2},
        )
        assert result.delta_g_kcal is None
        assert [entry.symmetry_number for entry in result.species] == [None, 2, 2, None]

    asyncio.run(_run())


def test_a_quick_reaction_says_nothing_about_symmetry() -> None:
    """`quick` computes no entropy, so it has no sigma to report and nothing to withhold.

    Its ΔG is None for the reason it always was, and the symmetry warning would be noise —
    a caller reading the warning would go and supply numbers that change nothing.
    """

    async def _run() -> None:
        result = await compute_reaction_energy(
            InMemoryStore(), ["[C-]#[O+]", "[H][H]", "[H][H]"], ["CO"], level="quick"
        )
        assert result.delta_g_kcal is None
        assert all(entry.symmetry_number is None for entry in result.species)
        assert not [w for w in result.warnings if "symmetry number" in w]

    asyncio.run(_run())


def test_a_symmetry_number_for_a_species_that_is_not_there_is_an_error() -> None:
    """A key that matches nothing is a typo, and it would read as an omission otherwise.

    `c1ccccc1` and `C1=CC=CC=C1` are the same molecule to a chemist and different strings
    here. Silently ignoring the second would tell the caller their symmetry number is
    missing while they are looking at the line where they passed it.
    """

    async def _run() -> None:
        with pytest.raises(ValueError, match="does not contain"):
            await compute_reaction_energy(
                InMemoryStore(),
                ["c1ccccc1", "[H][H]"],
                ["C1=CCCC=C1"],
                symmetry_numbers={"C1=CC=CC=C1": 12},
            )
        with pytest.raises(ValueError, match="at least 1"):
            await compute_reaction_energy(
                InMemoryStore(), ["CCO"], ["CCO"], symmetry_numbers={"CCO": 0}
            )

    asyncio.run(_run())
