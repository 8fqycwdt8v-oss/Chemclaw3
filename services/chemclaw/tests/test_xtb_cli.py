"""The binary backends: dispatch, the security boundary, and cross-backend agreement.

The tests that need `xtb` or `crest` skip without them, because both are optional — a
deployment can run entirely on the in-process library. The tests that do *not* need them
are the ones that matter most: the argv boundary, and that the backend never reaches a
cache key as "auto".
"""

import pytest

from calc import crest_cli, xtb_cli
from calc.conformers import ConformerSpec, _conformational_entropy, _populations
from calc.structure import structure_from_smiles
from calc.xtb_opt import OptSpec, optimize_structure
from calc.xtb_spec import XtbSpec, backend_version, resolve_backend
from calc.xtb_thermo import ThermoSpec, compute_thermochemistry

needs_xtb = pytest.mark.skipif(not xtb_cli.is_available(), reason="the xtb binary is not installed")
needs_crest = pytest.mark.skipif(
    not crest_cli.is_available(), reason="the crest binary is not installed"
)


def test_a_value_that_could_be_read_as_a_flag_is_rejected() -> None:
    """The one way a data string becomes an option in an argv-based tool.

    There is no shell, so quotes and semicolons are inert; a leading dash is not. This is
    the whole injection surface of the subprocess backends, and it is closed here rather
    than trusted to every call site.
    """
    with pytest.raises(ValueError, match="may not start with"):
        xtb_cli._safe("--define-a-flag", "solvent")
    assert xtb_cli._safe("water", "solvent") == "water"


def test_auto_never_reaches_a_cache_key() -> None:
    """A key containing "auto" would mean different things on two deployments.

    Both would then share entries computed by different programs, which is the one
    failure a versioned cache exists to prevent (D-011).
    """
    assert resolve_backend("auto") in ("tblite", "xtb")
    assert resolve_backend("tblite") == "tblite"
    key = XtbSpec(task="sp").cache_key(structure_from_smiles("O"))
    assert "auto" not in key.calc_version
    assert XtbSpec(task="sp").engine in key.calc_version


def test_the_two_backends_do_not_share_cache_entries() -> None:
    """They produce different numbers, so they are different calculator versions."""
    structure = structure_from_smiles("O")
    library = XtbSpec(task="sp", engine="tblite").cache_key(structure)
    binary = XtbSpec(task="sp", engine="xtb").cache_key(structure)
    assert library.calc_version != binary.calc_version
    # ...but the *parameters* are identical: the engine is a version, not a knob.
    assert library.params_hash == binary.params_hash


def test_backend_version_names_the_build() -> None:
    """An upgrade of either binary must be a cache miss, not a stale hit."""
    assert "tblite" in backend_version("tblite")
    assert backend_version("xtb").startswith("xtb-")


def test_populations_weight_by_rotamer_degeneracy() -> None:
    """n-butane's gauche stands for two rotamers, and ignoring that is worth 14 points.

    Hand-computed against CREST's own reported figures for the same ensemble: with
    degeneracies 9 and 17 at a 0.596 kcal/mol gap, the anti comes out at 59.2% and the
    ensemble entropy at 6.23 cal/(mol K) — CREST reports 59.14% and 6.227. Ignoring
    degeneracy gives 73%, which is simply the wrong answer.
    """
    energies = [0.0, 0.596]
    weighted = _populations(energies, [9, 17], 298.15)
    unweighted = _populations(energies, [1, 1], 298.15)
    assert weighted[0] == pytest.approx(0.592, abs=0.01)
    assert unweighted[0] == pytest.approx(0.73, abs=0.01)
    assert _conformational_entropy(weighted, [9, 17]) == pytest.approx(6.227, abs=0.05)


def test_an_ensemble_spec_keys_on_the_search_and_the_effort() -> None:
    """A quick search and an extensive one are different calculations."""
    structure = structure_from_smiles("CCCC")
    quick = ConformerSpec(effort="quick").cache_key(structure)
    extensive = ConformerSpec(effort="extensive").cache_key(structure)
    tautomers = ConformerSpec(search="tautomers").cache_key(structure)
    assert quick.params_hash != extensive.params_hash
    assert quick.params_hash != tautomers.params_hash
    assert quick.calc_type == "xtb.conformers"


@needs_xtb
def test_the_binary_backend_reproduces_the_measured_entropy_of_water() -> None:
    """The load-bearing cross-backend check: one RRHO implementation, two Hessians.

    Water's measured standard entropy is 45.10 cal/(mol K). The in-process path
    reproduces it, and so must the binary path — because the binary supplies only the
    Hessian, and the thermochemistry over it is the same validated code. If the two ever
    disagreed, a reaction mixing species computed on different backends would be silently
    inconsistent.
    """
    structure = structure_from_smiles("O", optimize=True)
    minimum = optimize_structure(OptSpec(engine="xtb"), structure).structure
    result = compute_thermochemistry(ThermoSpec(engine="xtb", symmetry_number=2), minimum)
    assert result.entropy_cal_per_mol_k == pytest.approx(45.10, abs=1.0)
    assert result.mode_count == 3
    assert result.is_minimum
    # The bend is still the strongest band — the intensities came from xtb this time.
    assert result.modes[0].ir_intensity_km_per_mol == max(
        mode.ir_intensity_km_per_mol for mode in result.modes
    )


@needs_xtb
def test_both_backends_reach_the_same_minimum() -> None:
    """Dispatch happens, and the two optimizers agree on where the minimum is.

    This test used to assert that ANCopt took fewer *iterations* than the Cartesian
    optimizer, and the X9 preconditioner disproved it: the preconditioned in-process path
    now converges in 24 iterations on ibuprofen against xtb's 43 ANC cycles — while xtb
    remains ~3x faster in wall clock, because each of its cycles is far cheaper than a
    Python-mediated tblite gradient. Iteration count was measuring the wrong thing.

    Wall clock is what the binary is for and is too machine-dependent to assert, so what
    is pinned here is what must hold on any machine: the dispatch, and that a change of
    optimizer is not a change of answer. The speed claim lives in `calc.xtb_cli`'s
    docstring with the numbers it was measured from.
    """
    structure = structure_from_smiles("CCCCO", optimize=True)
    binary = optimize_structure(OptSpec(engine="xtb"), structure)
    library = optimize_structure(OptSpec(engine="tblite"), structure)
    assert binary.engine == "xtb"
    assert library.engine == "tblite"
    assert binary.energy_hartree == pytest.approx(library.energy_hartree, abs=2e-3)


@needs_xtb
def test_an_unsupported_method_is_rejected_before_the_subprocess() -> None:
    """A typo in a method name fails as a validation error, not a subprocess crash."""
    with pytest.raises(ValueError, match="does not support method"):
        xtb_cli.run(structure_from_smiles("O"), task="sp", method="B3LYP")


@needs_xtb
def test_gfnff_optimizes_a_molecule_no_quantum_method_would_be_worth_on() -> None:
    """GFN-FF is the large-system escape valve: a force field with xTB's parameterization."""
    outcome = xtb_cli.run(
        structure_from_smiles("CCCCCCCCCCCCCCCCCC", optimize=True), task="opt", method="GFN-FF"
    )
    assert outcome.structure is not None
    assert outcome.energy_hartree < 0


@needs_crest
def test_a_conformer_search_finds_butanes_rotamers() -> None:
    """n-butane: anti lowest, a gauche within ~1 kcal/mol, and a real ensemble entropy.

    A lesson from writing it. The first version asserted `total_found == 2` — n-butane has
    two conformers, and two consecutive runs agreed. The third returned 4, because CREST
    splits methyl-rotor variants differently depending on what the metadynamics happened
    to visit. That is the documented stochasticity behaving exactly as documented, and a
    test that pins a sampled count is a flake waiting to fire in CI.

    So this asserts what is stable across runs: the anti is the reference, something
    gauche-like sits within a kcal/mol of it, the anti is the most populated single
    conformer, and the ensemble contributes a positive entropy — never a count.
    """
    from calc.conformers import compute_ensemble

    ensemble = compute_ensemble(ConformerSpec(), structure_from_smiles("CCCC", optimize=True))
    assert ensemble.total_found >= 2
    assert ensemble.conformers[0].relative_kcal == 0.0
    assert any(0.2 < member.relative_kcal < 1.2 for member in ensemble.conformers[1:])
    assert ensemble.conformers[0].population == max(
        member.population for member in ensemble.conformers
    )
    assert sum(member.population for member in ensemble.conformers) == pytest.approx(1.0, abs=1e-3)
    # The term a single-conformer free energy is missing, and it is never negative.
    assert ensemble.conformational_entropy_cal_per_mol_k > 0
    assert ensemble.ensemble_correction_kcal < 0
    assert ensemble.sampled is True
