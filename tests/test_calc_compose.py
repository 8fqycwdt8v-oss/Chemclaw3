"""The composites: what a calculation is when its parts live in another repository.

`D-2026-08-16-the-physics-leaves-the-cache-stays` split `calc` by **composability**. A primitive
moved and is cached under the server's key; a composite — anything whose key would name an output —
was not shipped at all and is assembled in `connectors/calc/compose.py` from parts that *are* keyed.
That decision is only correct if the composites ask for the right parts and stop asking on a repeat,
so almost everything here is a **call count**: "was this recomputed?" is a question about call
counts and nothing else (D-011).

Driven against `tests/calc_server_fake.py` rather than a running server, and the fake reproduces
three key properties measured against the real one — a Fukui key that does not name the mode, an
`xtb.opt` key that does not name who asked, and a `predict_logd` with no key at all. A fake that got
those wrong would let these tests pass on a design that fails in production.
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


# --- thermochemistry -------------------------------------------------------------------------


def test_thermochemistry_is_two_cached_parts_and_a_local_arithmetic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The measurement the whole split turns on: a repeat pays no calculation at all.

    `compute_thermochemistry` never had a cache row of its own — its key would have to name the
    geometry the refinement loop settles on, which is an output — so its economy is entirely the
    nested optimization and Hessian entries. Shipping it whole would have swallowed both. Here the
    second run computes **nothing**: two `calculation_key` round trips, two store hits, and the RRHO
    arithmetic again, which is milliseconds and is the part that depends on the temperature.
    """
    server = install(monkeypatch, FakeCalcServer())
    store = InMemoryStore()

    async def _both() -> tuple[Any, Any]:
        structure = await compose.embed("CCO")
        first = await compose.relax_to_minimum(store, structure, None)
        second = await compose.relax_to_minimum(store, structure, None)
        return first, second

    (_, cold, cold_cached), (_, warm, warm_cached) = _run(_both())

    assert cold_cached is False and warm_cached is True
    assert server.count("relax_structure") == 1, "a persisted optimization was recomputed"
    assert server.count("compute_hessian") == 1, "a persisted Hessian was recomputed"
    # The arithmetic is redone and must agree to the last digit, or the cache is serving a
    # different answer than the one it stored.
    assert warm.gibbs_free_energy_hartree == cold.gibbs_free_energy_hartree
    assert warm.entropy_cal_per_mol_k == cold.entropy_cal_per_mol_k


def test_a_second_temperature_reuses_the_hessian_instead_of_recomputing_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The reason the state variables are not in any key any more.

    A Hessian does not depend on the temperature, so the server keys it on what can move the matrix
    and nothing else. Asking the same minimum at 310 K after 298 K is therefore a cache hit plus a
    page of partition functions — where a shipped composite would have taken the second derivatives
    again.
    """
    from chemclaw.science.calc.thermo import ThermoSettings

    server = install(monkeypatch, FakeCalcServer())
    store = InMemoryStore()

    async def _two_temperatures() -> tuple[float, float]:
        structure = await compose.embed("CCO")
        _, cold, _ = await compose.relax_to_minimum(
            store, structure, None, ThermoSettings(temperature_k=298.15)
        )
        _, hot, _ = await compose.relax_to_minimum(
            store, structure, None, ThermoSettings(temperature_k=400.0)
        )
        return cold.gibbs_free_energy_hartree, hot.gibbs_free_energy_hartree

    cold, hot = _run(_two_temperatures())

    assert server.count("compute_hessian") == 1
    assert hot != cold, "the temperature must still change the free energy"


def test_the_refinement_loop_escapes_a_saddle_point(monkeypatch: pytest.MonkeyPatch) -> None:
    """A stationary point is not always a minimum, and the escape is this repository's.

    A force field hands over an eclipsed methyl and a Cartesian optimizer preserves that symmetry
    all the way down onto the rotational saddle — measured on ethyl acetate, an ordinary ester, at
    -42 cm^-1, where the free energy computed there is not a free energy. Displace along the
    imaginary mode and re-optimize; here the fake reports a saddle once and a minimum after, so the
    loop has to take a second pass and land on a *different* geometry.
    """
    server = install(monkeypatch, FakeCalcServer(saddle_first=True))
    store = InMemoryStore()

    async def _refine() -> Any:
        return await compose.relax_to_minimum(store, await compose.embed("CCO"), None)

    optimization, result, cached = _run(_refine())

    assert result.is_minimum is True
    assert cached is False
    assert server.count("relax_structure") == 2, "the saddle was not escaped"
    assert server.count("compute_hessian") == 2
    # The second pass ran on a displaced geometry, not on the one that was a saddle.
    first, second = server.arguments("relax_structure")
    assert first["structure"]["positions"] != second["structure"]["positions"]
    assert optimization.structure.structure_id == result.structure_id


def test_a_structure_that_will_not_settle_is_returned_as_it_stands(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bounded refinement: a molecule that keeps reporting a saddle is reporting something real.

    Looping on it is not the fix, so the result comes back with `is_minimum=False` intact rather
    than after an unbounded number of remote optimizations.
    """
    from chemclaw.core.config import settings

    server = install(monkeypatch, FakeCalcServer())
    server.overrides["compute_hessian"] = lambda arguments: _always_a_saddle(arguments)
    store = InMemoryStore()

    async def _refine() -> Any:
        return await compose.relax_to_minimum(store, await compose.embed("CCO"), None)

    _, result, _ = _run(_refine())

    assert result.is_minimum is False
    assert server.count("relax_structure") == settings.xtb_minimum_refinement_attempts + 1


def _always_a_saddle(arguments: dict[str, Any]) -> dict[str, Any]:
    """A Hessian that always carries an imaginary mode, whatever geometry it is handed."""
    from tests.calc_server_fake import harmonic_hessian

    return harmonic_hessian(arguments["structure"], imaginary=True)


# --- relaxed scan ----------------------------------------------------------------------------


def test_a_scan_is_a_series_of_separately_keyed_points(monkeypatch: pytest.MonkeyPatch) -> None:
    """One `scan_point` per value, each cached on its own — so re-running with two more is cheap.

    The whole profile used to be one cache entry, on the reasoning that its constrained points were
    of no use to anyone else. They are of use to the *same* scan asked again, which is the common
    case, and the server keys each point anyway.
    """
    server = install(monkeypatch, FakeCalcServer())
    store = InMemoryStore()

    async def _twice() -> tuple[Any, Any]:
        first = await compose.scan_profile(store, "CCCC", (0, 1, 2, 3), (0.0, 60.0, 120.0), None)
        second = await compose.scan_profile(store, "CCCC", (0, 1, 2, 3), (0.0, 60.0, 120.0), None)
        return first, second

    first, second = _run(_twice())

    assert server.count("scan_point") == 3, "a persisted scan point was recomputed"
    assert first.coordinate == "dihedral"
    assert first.unit == "degree"
    assert [point.value for point in first.points] == [0.0, 60.0, 120.0]
    assert min(point.relative_kcal for point in first.points) == 0.0
    assert second.minimum_value == first.minimum_value
    assert second.maximum_relative_kcal == first.maximum_relative_kcal


def test_a_scan_longer_than_the_cap_is_rejected_before_any_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every point is a full constrained optimization, so the length of `values` *is* the cost.

    The values come from the model, which makes an uncapped scan an unbounded compute request the
    agent can issue by naming more of them. Checked before the embed, so a refused request costs no
    round trip at all.
    """
    from chemclaw.core.config import settings

    server = install(monkeypatch, FakeCalcServer())
    values = tuple(float(index) for index in range(settings.xtb_scan_max_points + 1))
    with pytest.raises(ValueError, match="capped at"):
        _run(compose.scan_profile(InMemoryStore(), "CCCC", (0, 1), values, None))
    assert server.calls == []


def test_an_out_of_range_scan_atom_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """An index past the molecule would drive a coordinate that does not exist."""
    install(monkeypatch, FakeCalcServer())
    with pytest.raises(ValueError, match="out of range"):
        _run(compose.scan_profile(InMemoryStore(), "O", (0, 99), (1.0, 1.2), None))


# --- conformer ensembles ---------------------------------------------------------------------


def test_a_wider_view_of_a_cached_ensemble_costs_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """`max_members` truncates a finished answer; it must never reach the search.

    A CREST search is the most expensive single calculation in the system, and "show me 20 instead
    of 10" is a presentation choice. The stored payload is the whole ensemble the search found.
    """
    from chemclaw.core.config import settings

    server = install(monkeypatch, FakeCalcServer())
    store = InMemoryStore()

    async def _twice() -> tuple[Any, Any]:
        narrow = await compose.conformer_ensemble(store, "CCCC")
        monkeypatch.setattr(settings, "crest_max_members", 2)
        wide = await compose.conformer_ensemble(store, "CCCC")
        return narrow, wide

    (narrow, first_cached), (wide, second_cached) = _run(_twice())

    assert server.count("search_conformer_ensemble") == 1
    assert (first_cached, second_cached) == (False, True)
    assert len(wide.conformers) == 2
    assert wide.total_found == narrow.total_found == 3


def test_the_search_kind_and_effort_still_move_the_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """A quick pass and an extensive one are different calculations that must not share an entry."""
    server = install(monkeypatch, FakeCalcServer())
    store = InMemoryStore()

    async def _three() -> None:
        await compose.conformer_ensemble(store, "CCCC", search="conformers", effort="quick")
        await compose.conformer_ensemble(store, "CCCC", search="conformers", effort="extensive")
        await compose.conformer_ensemble(store, "CCCC", search="tautomers", effort="quick")

    _run(_three())
    assert server.count("search_conformer_ensemble") == 3


# --- non-covalent complexes --------------------------------------------------------------------


def test_the_pair_is_canonically_ordered_so_either_direction_is_one_calculation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A-with-B and B-with-A are one physical quantity but not one starting geometry.

    `combine_structures` holds the first monomer at the origin and offsets the second along +x, so
    swapping the arguments negates the intermolecular vector and would key to a different entry —
    paying twice, at minutes per search, for the same answer.
    """
    server = install(monkeypatch, FakeCalcServer())
    store = InMemoryStore()

    async def _both_ways() -> tuple[Any, Any]:
        forward = await compose.interaction(store, "O", "CO")
        reverse = await compose.interaction(store, "CO", "O")
        return forward, reverse

    forward, reverse = _run(_both_ways())

    assert server.count("search_binding_modes") == 1, "the reversed pair ran a second search"
    assert (forward.smiles_a, forward.smiles_b) == (reverse.smiles_a, reverse.smiles_b)
    assert forward.interaction_energy_kcal == reverse.interaction_energy_kcal


def test_an_interaction_energy_differences_relaxed_species(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The complex at its optimized binding mode, minus each monomer optimized on its own.

    Which deliberately includes the deformation cost of binding — the part a "rigid monomer"
    definition leaves out. Three relaxations plus one search, and every one of them shared with any
    other question about those molecules.
    """
    server = install(monkeypatch, FakeCalcServer())

    result = _run(compose.interaction(InMemoryStore(), "O", "CO"))

    assert server.count("relax_structure") == 3  # two monomers and the chosen binding mode
    assert server.count("search_binding_modes") == 1
    assert len(result.monomer_energies_hartree) == 2
    assert result.binding_modes == 3
    assert result.interaction_energy_kcal == pytest.approx(
        (result.complex_energy_hartree - sum(result.monomer_energies_hartree)) * 627.5094740631,
        abs=0.01,
    )


# --- reaction energetics -----------------------------------------------------------------------


_ESTERIFICATION = (["CC(=O)O", "CCO"], ["CC(=O)OCC", "O"])
# Two ethenes into cyclobutane: balanced, and Δn = -1, which is the class the standard state
# actually moves. Esterification above is 2 -> 2, so the term cancels there exactly.
_DIMERISATION = (["C=C", "C=C"], ["C1CCC1"])
_SIGMAS = {"C=C": 4, "C1CCC1": 8}
# RT ln(RT c0 / P0) at 298.15 K in kcal/mol, from CODATA and nothing in `src/` — see
# `tests/test_calc_thermo.py`, which derives it twice and pins it to 1e-12.
_STANDARD_STATE_KCAL = 1.8943284454483122


def test_an_unbalanced_equation_is_rejected_before_anything_is_computed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unbalanced equation is rejected before anything crosses the wire.

    The difference would include whatever atoms the two sides do not share — meaningless, and it
    looks entirely ordinary. The message names the element and the count, because the usual cause is
    a forgotten water and that is immediately fixable once stated.
    """
    server = install(monkeypatch, FakeCalcServer())
    with pytest.raises(ValueError, match="not atom-balanced"):
        _run(compose.reaction_energy(InMemoryStore(), ["CC(=O)O", "CCO"], ["CC(=O)OCC"]))
    assert server.calls == []


def test_charge_imbalance_is_named_separately(monkeypatch: pytest.MonkeyPatch) -> None:
    """Atoms can balance while charge does not, and the fix is a different one."""
    install(monkeypatch, FakeCalcServer())
    with pytest.raises(ValueError, match="not charge-balanced"):
        _run(compose.reaction_energy(InMemoryStore(), ["[Na+]"], ["[Na]"]))


def test_a_shared_species_is_computed_once_across_two_reactions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """There is deliberately no reaction-level cache entry, and this is why it needs none.

    The expensive parts are one optimization and one Hessian per species, each keyed individually,
    so a second reaction sharing a species reuses it and a reaction is a subtraction over values
    already held. A reaction-level row could never be hit by anything the per-species rows miss.
    """
    server = install(monkeypatch, FakeCalcServer())
    store = InMemoryStore()

    async def _two_reactions() -> tuple[Any, Any]:
        first = await compose.reaction_energy(
            store,
            *_ESTERIFICATION,
            symmetry_numbers={"CC(=O)O": 1, "CCO": 1, "CC(=O)OCC": 1, "O": 2},
        )
        # Shares acetic acid and water with the first; only methanol and methyl acetate are new.
        second = await compose.reaction_energy(
            store,
            ["CC(=O)O", "CO"],
            ["CC(=O)OC", "O"],
            symmetry_numbers={"CC(=O)O": 1, "CO": 1, "CC(=O)OC": 1, "O": 2},
        )
        return first, second

    first, second = _run(_two_reactions())

    assert first.cache_hits == 0
    assert second.cache_hits == 2, "acetic acid and water were recomputed for the second reaction"
    # Six distinct species across the two reactions, one relaxation and one Hessian each.
    assert server.count("relax_structure") == 6
    assert server.count("compute_hessian") == 6


def test_a_reaction_without_symmetry_numbers_withholds_the_free_energy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sigma shifts a species' entropy by exactly R ln(sigma) and does not cancel across the arrow.

    So the honest answer is ΔE and ΔH with no ΔG and a warning naming the species — never a third
    state, a ΔG computed at sigma=1 for symmetric species and reported as an ordinary number.
    """
    install(monkeypatch, FakeCalcServer())
    result = _run(compose.reaction_energy(InMemoryStore(), *_ESTERIFICATION))

    assert result.delta_g_kcal is None
    assert result.delta_h_kcal is not None
    assert result.delta_e_kcal is not None
    assert all(species.symmetry_number is None for species in result.species)
    (warning,) = [line for line in result.warnings if "symmetry number" in line]
    assert "O" in warning


def test_stating_the_symmetry_numbers_yields_a_free_energy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stating 1 is a real statement: "no rotational symmetry" and "not considered" differ here."""
    install(monkeypatch, FakeCalcServer())
    result = _run(
        compose.reaction_energy(
            InMemoryStore(),
            *_ESTERIFICATION,
            symmetry_numbers={"CC(=O)O": 1, "CCO": 1, "CC(=O)OCC": 1, "O": 2},
        )
    )
    assert result.delta_g_kcal is not None
    assert [species.symmetry_number for species in result.species] == [1, 1, 1, 2]
    assert not [line for line in result.warnings if "symmetry number" in line]


def test_a_symmetry_number_for_a_species_that_is_not_in_the_equation_is_a_typo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A sigma keyed to a species the equation does not contain is a typo, not an omission.

    Left unchecked the two look identical, and the caller is told their symmetry number is missing
    while staring at the line where they passed it.
    """
    install(monkeypatch, FakeCalcServer())
    with pytest.raises(ValueError, match="does not contain"):
        _run(
            compose.reaction_energy(
                InMemoryStore(), *_ESTERIFICATION, symmetry_numbers={"c1ccccc1": 12}
            )
        )


def test_quick_level_takes_no_hessian_at_all(monkeypatch: pytest.MonkeyPatch) -> None:
    """`quick` differences electronic energies, so there is no entropy to be missing a sigma for."""
    server = install(monkeypatch, FakeCalcServer())
    result = _run(compose.reaction_energy(InMemoryStore(), *_ESTERIFICATION, level="quick"))

    assert server.count("compute_hessian") == 0
    assert result.delta_h_kcal is None
    assert result.delta_g_kcal is None
    assert not [line for line in result.warnings if "symmetry number" in line]


def test_an_open_shell_species_is_multiplicity_two_and_says_so(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A homolysis is the reaction whose whole point is that one side is open-shell.

    Its multiplicity comes from the SMILES' own radical electrons, derived here because the server
    reads `multiplicity=None` as closed-shell singlet and would refuse the species outright —
    measured. The warning is attached at every level, because the caveat is about the energies and
    every level differences those.
    """
    install(monkeypatch, FakeCalcServer())
    result = _run(
        compose.reaction_energy(InMemoryStore(), ["CC"], ["[CH3]", "[CH3]"], level="quick")
    )
    assert [species.multiplicity for species in result.species] == [1, 2, 2]
    assert any("open-shell" in line for line in result.warnings)


def test_a_solution_reaction_that_changes_the_molecule_count_is_corrected_to_one_molar(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Δn != 0 in solution: the reported ΔG must be the 1 mol/L one, not the 1 atm one.

    The electronic energy comes back from an ALPB implicit-solvent SCF while the entropy is the
    ideal-gas one, so a solution ΔG quoted at 1 atm is wrong by RT ln(RT c0/P0) = 1.894 kcal/mol
    per mole the reaction creates or destroys — a factor of 24.47 in K per unit of Δn. The fake
    returns the same energies in every medium (which is what the solvent screen's own test rests
    on), so the entire gas-to-solution difference here *is* the standard-state term and nothing
    else.

    An association (Δn = -1) must become **more** favourable in the 1 M state, because 1 M is the
    more concentrated reference: the sign is asserted rather than only the magnitude.
    """
    install(monkeypatch, FakeCalcServer())
    store = InMemoryStore()

    gas = _run(
        compose.reaction_energy(store, *_DIMERISATION, solvent=None, symmetry_numbers=_SIGMAS)
    )
    solution = _run(
        compose.reaction_energy(store, *_DIMERISATION, solvent="thf", symmetry_numbers=_SIGMAS)
    )
    assert gas.delta_g_kcal is not None and solution.delta_g_kcal is not None
    shift = solution.delta_g_kcal - gas.delta_g_kcal
    print(
        f"association: ΔG(1 M) {solution.delta_g_kcal} - ΔG(1 atm) {gas.delta_g_kcal} = {shift} "
        f"against Δn·RT ln(RT c0/P0) = {-_STANDARD_STATE_KCAL}"
    )
    assert shift == pytest.approx(-_STANDARD_STATE_KCAL, abs=0.011)
    assert gas.standard_state == "gas-1atm"
    assert solution.standard_state == "solution-1M"
    # ΔH does not depend on the reference pressure, so it must be untouched by all of this.
    assert solution.delta_h_kcal == gas.delta_h_kcal


def test_the_standard_state_term_follows_the_sign_of_delta_n(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The same reaction read backwards moves the other way, by the same 1.894 kcal/mol.

    A dissociation makes a mole of species and is therefore **less** favourable at 1 M; checking
    both directions is what distinguishes a correct correction from a constant offset applied
    wherever a solvent is named.
    """
    install(monkeypatch, FakeCalcServer())
    store = InMemoryStore()
    reactants, products = _DIMERISATION

    gas = _run(
        compose.reaction_energy(store, products, reactants, solvent=None, symmetry_numbers=_SIGMAS)
    )
    solution = _run(
        compose.reaction_energy(store, products, reactants, solvent="thf", symmetry_numbers=_SIGMAS)
    )
    assert gas.delta_g_kcal is not None and solution.delta_g_kcal is not None
    shift = solution.delta_g_kcal - gas.delta_g_kcal
    print(
        f"dissociation: ΔG(1 M) {solution.delta_g_kcal} - ΔG(1 atm) {gas.delta_g_kcal} = {shift} "
        f"against Δn·RT ln(RT c0/P0) = {_STANDARD_STATE_KCAL}"
    )
    assert shift == pytest.approx(_STANDARD_STATE_KCAL, abs=0.011)


def test_a_reaction_that_conserves_the_molecule_count_is_unmoved_by_the_solvent_reference(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Δn = 0 cancels the term exactly, which is why this defect never showed as a wrong number.

    Esterification is 2 -> 2. If the correction were applied per *species* rather than per mole of
    change, this would shift by 4 x 1.894 and every tautomer ranking in the tree with it.
    """
    install(monkeypatch, FakeCalcServer())
    store = InMemoryStore()
    sigmas = {"CC(=O)O": 1, "CCO": 1, "CC(=O)OCC": 1, "O": 2}

    gas = _run(
        compose.reaction_energy(store, *_ESTERIFICATION, solvent=None, symmetry_numbers=sigmas)
    )
    solution = _run(
        compose.reaction_energy(store, *_ESTERIFICATION, solvent="thf", symmetry_numbers=sigmas)
    )
    assert gas.delta_g_kcal == solution.delta_g_kcal
    assert solution.standard_state == "solution-1M"


def test_a_solvent_screen_says_which_standard_state_each_number_is_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The screen's gas reference is quoted at 1 atm and its solutions at 1 M, so it must say so.

    Solvent-against-solvent — the comparison the tool exists for — is unaffected: every solution
    entry is in the same state. The gas-to-solution gap is not, and for Δn != 0 it carries the
    1.894·Δn term on top of the solvation. A reader differencing the two columns without being
    told is the failure this warning prevents.
    """
    install(monkeypatch, FakeCalcServer())
    result = _run(
        compose.solvent_comparison(
            InMemoryStore(), *_DIMERISATION, ["water", "toluene"], symmetry_numbers=_SIGMAS
        )
    )
    states = {effect.solvent: effect.standard_state for effect in result.effects}
    print(states)
    assert states[None] == "gas-1atm"
    assert states["water"] == "solution-1M" and states["toluene"] == "solution-1M"
    (mixed,) = [line for line in result.warnings if "standard state" in line]
    assert "1.89" in mixed, mixed


def test_a_solvent_screen_ranks_the_media_and_includes_the_gas_phase(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A screen ranks its media and includes the gas phase as a reference.

    "The solvent barely matters here" is a real answer and is invisible without one.

    The screen also shares every species across media that key the same way, which is the reason it
    is a fan-out over one cache rather than N independent runs.
    """
    install(monkeypatch, FakeCalcServer())
    result = _run(
        compose.solvent_comparison(
            InMemoryStore(),
            *_ESTERIFICATION,
            ["water", "toluene"],
            symmetry_numbers={"CC(=O)O": 1, "CCO": 1, "CC(=O)OCC": 1, "O": 2},
        )
    )
    assert [effect.solvent for effect in result.effects].count(None) == 1
    assert len(result.effects) == 3
    assert all(effect.delta_g_kcal is not None for effect in result.effects)


def test_a_screen_that_cannot_distinguish_its_solvents_says_so(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An implicit continuum model resolving less than its own uncertainty is reading its noise.

    The fake returns the same energies in every medium, which is the extreme of that case: a spread
    of exactly zero must produce the warning rather than a confident ranking.
    """
    install(monkeypatch, FakeCalcServer())
    result = _run(
        compose.solvent_comparison(InMemoryStore(), *_ESTERIFICATION, ["water"], level="quick")
    )
    assert result.spread_kcal == 0.0
    assert any("does not distinguish" in line for line in result.warnings)


def test_a_reaction_energy_over_the_ceiling_refuses_before_it_computes_anything(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`reaction_energy` fans out over every reactant and product with no ceiling of its own.

    Unlike every sibling composite in this module (species_ranking, refined_ensemble, ...), it had
    no `require_within_budget` call at all — an unprivileged, non-`expensive` job could name an
    arbitrarily long reactants/products list. The refusal must land before any species is computed.
    """
    server = install(monkeypatch, FakeCalcServer())
    monkeypatch.setattr(calc_settings, "calc_max_primitive_calls", 1)

    with pytest.raises(ValueError, match=r"would run \d+ calculations"):
        _run(compose.reaction_energy(InMemoryStore(), *_ESTERIFICATION))

    assert server.calls == [], "the refusal came after work had started"


def test_a_solvent_screen_counts_species_times_media_against_the_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`solvent_comparison` bounded only *parallelism*; every solvent still ran eventually.

    `calc_screen_max_parallel` throttles concurrency, not the total count — the same gap
    `rank_species_across_solvents` closes by multiplying species by media before checking the
    budget. A modest reaction (`_ESTERIFICATION`, 4 species) run across enough solvents must still
    refuse, and refuse before any solvent's reaction energy is computed.
    """
    server = install(monkeypatch, FakeCalcServer())
    monkeypatch.setattr(calc_settings, "calc_max_primitive_calls", 1)

    with pytest.raises(ValueError, match=r"would run \d+ calculations"):
        _run(
            compose.solvent_comparison(
                InMemoryStore(), *_ESTERIFICATION, ["water", "dmso", "acetonitrile"]
            )
        )

    assert server.calls == [], "the refusal came after work had started"


# --- what this system offers when a Hessian is out of reach --------------------------------


def test_an_oversized_hessian_is_refused_here_with_the_routes_this_system_actually_has(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The molecule too big for a Hessian is refused before the wire, naming a real way forward.

    The calculation server has its own atom ceiling and refuses above it — and its refusal used to
    tell the model to "submit it through Chemclaw3's durable QM job path instead", a route
    `D-2026-08-26-semiempirical-is-the-whole-tier` deleted. There is no such path: every durable
    job here composes the *same* `compute_hessian` primitive under the same ceiling, so escalating
    changes nothing.

    So the fence is a preflight on this side, in the `require_within_budget` family, and it names
    the two things that do work: `level="quick"`, which skips every Hessian, and a smaller model
    system. The count assertion is the whole test — a refusal that arrives after the call is not a
    preflight, however true its message.
    """
    server = install(monkeypatch, FakeCalcServer())
    monkeypatch.setattr(calc_settings, "calc_hessian_max_atoms", 4)

    async def _go() -> Any:
        return await compose.hessian(InMemoryStore(), await compose.embed("CCO"), None)

    with pytest.raises(ValueError, match=r'level="quick"'):
        _run(_go())

    assert server.count("compute_hessian") == 0, "the refusal came after the calculation was asked"


def test_a_hessian_carries_the_gradient_that_says_it_was_a_stationary_point(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`max_gradient_hartree_per_angstrom` survives the wire into the payload this repository reads.

    The server returns it beside every Hessian precisely because `compute_hessian` differentiates
    whatever geometry it is handed. Dropping it here — which a model that does not declare the
    field does silently — is what leaves a zero-point energy computed at a non-stationary geometry
    indistinguishable from one computed at a minimum.
    """
    install(monkeypatch, FakeCalcServer())

    async def _go() -> Any:
        return await compose.hessian(InMemoryStore(), await compose.embed("CCO"), None)

    payload, _ = _run(_go())
    assert payload.max_gradient_hartree_per_angstrom == pytest.approx(1e-4)
