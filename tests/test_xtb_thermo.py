"""Frequencies, IR intensities and RRHO thermochemistry against measured values (X3).

The physics here is easy to get subtly wrong — a mass-weighting factor, a partition
function, a symmetry number — and every mistake produces numbers that look perfectly
plausible. So the tests check against **experiment** where experiment exists: water's
standard entropy and the ordering of its IR bands are known to three figures, and
nothing that reproduces them by accident is likely.
"""

import asyncio

import numpy as np
import pytest

from chemclaw.core.config import settings
from chemclaw.science.calc.store import InMemoryStore
from chemclaw.science.calc.structure import Structure, structure_from_smiles
from chemclaw.science.calc.xtb_opt import OptSpec, optimize_structure
from chemclaw.science.calc.xtb_thermo import (
    ThermochemistryResult,
    ThermoSpec,
    VibrationalMode,
    compute_thermochemistry,
    relax_to_minimum,
)


def _minimum(smiles: str) -> Structure:
    """A converged GFN2 minimum for `smiles` — what a Hessian is only valid at."""
    return optimize_structure(OptSpec(), structure_from_smiles(smiles, optimize=True)).structure


def test_water_reproduces_its_measured_standard_entropy() -> None:
    """Water's S° is 45.10 cal/mol/K at 298.15 K and 1 atm. Reproduce it, with sigma=2.

    This is the load-bearing test of the whole module: the translational
    (Sackur-Tetrode), rotational (from the moments of inertia) and vibrational
    (quasi-RRHO) contributions all enter it, and an error in any of them moves the
    total by more than the tolerance here.
    """
    result = compute_thermochemistry(ThermoSpec(symmetry_number=2), _minimum("O"))
    assert result.entropy_cal_per_mol_k == pytest.approx(45.10, abs=1.0)


def test_the_symmetry_number_shifts_the_entropy_by_exactly_r_ln_sigma() -> None:
    """Getting sigma wrong is worth R·ln(sigma) — which is why it is an input, not a guess."""
    minimum = _minimum("O")
    one = compute_thermochemistry(ThermoSpec(symmetry_number=1), minimum)
    two = compute_thermochemistry(ThermoSpec(symmetry_number=2), minimum)
    expected = 1.987204 * np.log(2)  # R ln 2, in cal/(mol K)
    assert one.entropy_cal_per_mol_k - two.entropy_cal_per_mol_k == pytest.approx(
        expected, abs=0.01
    )


def test_water_has_three_real_modes_and_a_physical_zero_point_energy() -> None:
    """3N-6 = 3 vibrations, all real, and a ZPE near the measured 13.26 kcal/mol."""
    result = compute_thermochemistry(ThermoSpec(symmetry_number=2), _minimum("O"))
    assert result.is_minimum
    assert result.imaginary_frequencies_cm == []
    assert result.mode_count == 3
    assert result.zero_point_energy_kcal == pytest.approx(13.26, abs=1.5)
    # A bend near 1600 and two stretches above 3000: the shape of every water spectrum.
    bend, *stretches = [mode.wavenumber_cm for mode in result.modes]
    assert 1300 < bend < 1800
    assert all(3000 < value < 4000 for value in stretches)


def test_waters_bend_is_its_strongest_infrared_band() -> None:
    """Measured intensities are 53.6 (bend) vs 2.2 and 44.6 km/mol for the stretches.

    Semiempirical intensities are not quantitative, so this asserts the **ordering** a
    spectrum comparison actually uses: the bend dominates. That the intensities exist at
    all is the free dividend of reading the dipole from the displacements the Hessian
    already ran.
    """
    result = compute_thermochemistry(ThermoSpec(symmetry_number=2), _minimum("O"))
    intensities = [mode.ir_intensity_km_per_mol for mode in result.modes]
    assert intensities[0] == max(intensities)
    assert intensities[0] > 10


def test_a_saddle_point_reports_an_imaginary_frequency() -> None:
    """Planar ammonia is the inversion transition state, and must be identified as one.

    Without this the module would happily return a Gibbs energy for a structure that is
    not a minimum — the exact silent failure `is_minimum` exists to prevent.
    """
    pyramidal = _minimum("N")
    positions = np.array(pyramidal.positions)
    centre = positions.mean(axis=0)
    normal = np.linalg.svd(positions - centre)[2][2]
    flattened = positions - np.outer((positions - centre) @ normal, normal)
    planar = Structure(elements=pyramidal.elements, positions=flattened.tolist(), smiles="N")

    result = compute_thermochemistry(ThermoSpec(symmetry_number=3), planar)
    assert not result.is_minimum
    assert len(result.imaginary_frequencies_cm) == 1
    assert result.imaginary_frequencies_cm[0] < -500  # a real barrier, not numerical noise
    assert result.imaginary_displacement is not None


def test_a_linear_molecule_has_one_more_vibration_than_a_bent_one() -> None:
    """CO2 is linear: 3N-5 = 4 modes, not 3.

    Regression guard on a real defect. The first implementation filtered the x/y/z
    rotation vectors by singular value, which looks equivalent and is not: an optimized
    CO2 is bent by a fraction of a degree, so its "null" rotation has a small but
    perfectly ordinary singular value, survives the cut, and eats a genuine vibration.
    Building the rotations about the principal axes and keeping them by moment of
    inertia — the same linearity test the entropy uses — is what makes this pass.
    """
    result = compute_thermochemistry(ThermoSpec(symmetry_number=2), _minimum("O=C=O"))
    assert result.mode_count == 4
    assert result.is_minimum


def test_ethanol_has_the_right_number_of_modes() -> None:
    """3N-6 for a nine-atom molecule is 21 — the projection is not eating real modes."""
    result = compute_thermochemistry(ThermoSpec(), _minimum("CCO"))
    assert result.mode_count == 21
    assert len(result.lowest_wavenumbers_cm) == 5


def test_free_energy_is_below_enthalpy_at_room_temperature() -> None:
    """G = H - TS with a positive entropy, so the ordering is fixed by construction."""
    result = compute_thermochemistry(ThermoSpec(), _minimum("CCO"))
    assert result.entropy_cal_per_mol_k > 0
    assert result.gibbs_free_energy_hartree < result.enthalpy_hartree
    assert result.electronic_energy_hartree < result.enthalpy_hartree


def test_relax_to_minimum_escapes_a_rotational_saddle() -> None:
    """Ethyl acetate optimizes onto a methyl-rotor saddle; the refinement gets off it.

    Not a contrived case — an ordinary ester. A force field hands over an eclipsed
    methyl and a Cartesian optimizer preserves that symmetry all the way down, leaving a
    -42 cm^-1 mode and a "free energy" that is not one. One displacement along the
    imaginary mode fixes it, for ~0.02 kcal/mol, which is what confirms the diagnosis.
    """

    async def _run() -> None:
        store = InMemoryStore()
        structure = structure_from_smiles("CCOC(C)=O", optimize=True)
        plain = compute_thermochemistry(
            ThermoSpec(), optimize_structure(OptSpec(), structure).structure
        )
        assert not plain.is_minimum  # the defect this exists to fix

        _, refined, _ = await relax_to_minimum(store, structure)
        assert refined.is_minimum
        assert refined.imaginary_frequencies_cm == []

    asyncio.run(_run())


def test_thermochemistry_refuses_a_molecule_too_large_to_be_inline() -> None:
    """Above the atom ceiling the Hessian is refused with a pointer, not attempted.

    The structure is assembled directly rather than embedded from a SMILES: the check is
    on the atom count, and embedding a molecule this size would spend a minute of test
    time proving nothing about it.
    """
    count = settings.xtb_hessian_max_atoms + 1
    oversized = Structure(
        elements=[6] * count,
        positions=[[float(index) * 2.0, 0.0, 0.0] for index in range(count)],
        multiplicity=1 if count % 2 == 0 else 3,
    )
    with pytest.raises(ValueError, match="exceeds the inline limit"):
        compute_thermochemistry(ThermoSpec(), oversized)


def test_cached_thermochemistry_keys_on_the_temperature() -> None:
    """298 K and 350 K are different calculations, so they are different cache entries."""
    minimum = _minimum("O")
    warm = ThermoSpec(temperature_k=350.0).cache_key(minimum)
    cold = ThermoSpec(temperature_k=298.15).cache_key(minimum)
    assert warm.params_hash != cold.params_hash
    assert warm.calc_type == "xtb.hess"


def test_strongest_bands_keeps_the_loud_ones_and_every_imaginary_one() -> None:
    """The IR truncation must never drop the mode that invalidates the result."""
    result = ThermochemistryResult(
        smiles=None,
        structure_id="st_x",
        method="GFN2-xTB",
        solvent=None,
        temperature_k=298.15,
        pressure_pa=101325.0,
        symmetry_number=1,
        is_minimum=False,
        imaginary_frequencies_cm=[-100.0],
        modes=[
            VibrationalMode(wavenumber_cm=-100.0, ir_intensity_km_per_mol=0.0),
            VibrationalMode(wavenumber_cm=500.0, ir_intensity_km_per_mol=1.0),
            VibrationalMode(wavenumber_cm=900.0, ir_intensity_km_per_mol=90.0),
            VibrationalMode(wavenumber_cm=1700.0, ir_intensity_km_per_mol=40.0),
        ],
        mode_count=4,
        lowest_wavenumbers_cm=[-100.0, 500.0, 900.0, 1700.0],
        electronic_energy_hartree=-1.0,
        zero_point_energy_kcal=1.0,
        thermal_enthalpy_correction_kcal=2.0,
        entropy_cal_per_mol_k=50.0,
        gibbs_correction_kcal=-1.0,
        enthalpy_hartree=-0.9,
        gibbs_free_energy_hartree=-1.1,
        uncertainty_kcal=3.0,
    )
    kept = [mode.wavenumber_cm for mode in result.strongest_bands(2)]
    assert kept == [-100.0, 900.0, 1700.0]
