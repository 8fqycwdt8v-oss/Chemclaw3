"""The reaction composite: balance, consistency, caching and honesty (xTB plan X4).

Real calculations again. The interesting assertions are not that a number comes out —
they are that an *unbalanced* equation does not, that the second reaction sharing a
species does not recompute it, and that the measured limits of the method are pinned
where a future change would trip over them.
"""

import asyncio

import pytest

from calc.reaction import (
    check_balance,
    compare_solvent_effects,
    compute_reaction_energy,
)
from calc.store import InMemoryStore
from chemclaw.config import settings


def test_esterification_returns_all_three_deltas() -> None:
    """The Fischer esterification of the green-chemistry eval case, computed end to end.

    Acetic acid + ethanol -> ethyl acetate + water sits near equilibrium in reality
    (K ~ 4, so ΔG ~ -0.8 kcal/mol). Asserting a window rather than a value: what is being
    pinned is that a balanced, four-species reaction produces ΔE, ΔH and ΔG that are
    consistent with each other and in the right neighbourhood.
    """

    async def _run() -> None:
        result = await compute_reaction_energy(
            InMemoryStore(), ["CC(=O)O", "CCO"], ["CCOC(C)=O", "O"]
        )
        assert result.delta_h_kcal is not None
        assert result.delta_g_kcal is not None
        assert -8 < result.delta_g_kcal < 8  # a near-thermoneutral equilibrium
        assert len(result.species) == 4
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

    `calc.reaction_energy` was deleted rather than kept alongside this module, so its
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
