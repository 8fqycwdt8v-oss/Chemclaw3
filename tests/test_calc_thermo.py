"""The statistical mechanics that stayed here, checked against measured values — no server needed.

`science/calc/thermo.py` is the half of thermochemistry that did *not* move to `Chemclaw3-mcp`: the
second derivatives cost minutes and are a cached primitive over there, while turning them into a
free energy is a page of partition functions costing milliseconds and depending on a temperature the
Hessian never saw. That split is only worth anything if the arithmetic came through it intact, and
"intact" here has a stronger meaning than "the code compiles": these numbers are compared against
experiment.

**The Hessians are recorded, not synthesized.** `tests/fixtures/calc_hessians.json` holds real
`compute_hessian` payloads for water, CO2 and H2, taken from the live calculation server on
2026-08-16 and stored exactly as they crossed the wire — base64 `.npy`, dipole derivatives and all.
That is what lets this file assert measured standard entropies with no quantum chemistry program
installed, and it also pins the *transport*: a change to the encoding on either side turns these
red rather than silently producing a spectrum of zeros.

The reference entropies are NIST standard molar entropies at 298.15 K, 1 atm, in cal/(mol K):
water 45.10, CO2 51.06, H2 31.23. The agreement below is to a few hundredths, which is the same
agreement this arithmetic had before the split.
"""

import json
from pathlib import Path

import pytest

from chemclaw.science.calc.models import (
    EnsembleMember,
    EnsemblePayload,
    HessianPayload,
    Structure,
    ThermochemistryResult,
    VibrationalMode,
)
from chemclaw.science.calc.thermo import (
    ThermoSettings,
    displaced_along,
    ensemble_from_members,
    thermochemistry_from_hessian,
)

_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "calc_hessians.json"
_RECORDED = json.loads(_FIXTURE.read_text(encoding="utf-8"))


def _recorded(name: str) -> tuple[Structure, HessianPayload]:
    """The recorded minimum and Hessian for one molecule."""
    entry = _RECORDED[name]
    return (
        Structure.model_validate(entry["structure"]),
        HessianPayload.model_validate(entry["hessian"]),
    )


# (fixture name, rotational symmetry number, measured standard entropy in cal/(mol K)).
_MEASURED = (
    ("water", 2, 45.10),
    ("carbon_dioxide", 2, 51.06),
    ("hydrogen", 2, 31.23),
)


@pytest.mark.parametrize(("name", "sigma", "reference"), _MEASURED)
def test_the_rrho_arithmetic_reproduces_a_measured_standard_entropy(
    name: str, sigma: int, reference: float
) -> None:
    """The claim the split rests on: the numbers did not change when the Hessian moved away.

    A translational, rotational and vibrational partition function over a real Hessian, against the
    NIST value. CO2 and H2 also exercise the linear-rotor branch, which is where this module's one
    historical arithmetic bug lived — a factor of 2 in the linear partition function that no
    `calc_version` could ever have moved, and the reason `CALCULATION_EPOCH` exists.
    """
    structure, hessian = _recorded(name)
    result = thermochemistry_from_hessian(ThermoSettings(symmetry_number=sigma), structure, hessian)
    assert result.entropy_cal_per_mol_k == pytest.approx(reference, abs=0.3)
    assert result.is_minimum


def test_the_symmetry_number_shifts_the_entropy_by_exactly_r_ln_sigma() -> None:
    """Sigma is an input, not a guess, and this is the size of getting it wrong.

    It does not cancel across a balanced reaction unless both sides carry the same symmetry, which
    for the chemistry worth computing they do not — every hydrogenation consumes H2. That is why a
    reaction with any species' sigma unstated withholds its free energy instead of reporting one.
    """
    import math

    structure, hessian = _recorded("water")
    at_one = thermochemistry_from_hessian(ThermoSettings(), structure, hessian)
    at_two = thermochemistry_from_hessian(ThermoSettings(symmetry_number=2), structure, hessian)
    shift = at_one.entropy_cal_per_mol_k - at_two.entropy_cal_per_mol_k
    assert shift == pytest.approx(1.987204258640832 * math.log(2), abs=1e-6)


def test_a_bent_and_a_linear_molecule_get_different_mode_counts() -> None:
    """3N-6 against 3N-5, decided by the moments of inertia rather than by a structural guess.

    Filtering the raw rotations by singular value instead does not work: an optimized "linear"
    molecule is bent by a fraction of a degree, so its null rotation has a small but perfectly
    ordinary singular value and survives the cut — measured on CO2, which lost a real mode that way.
    """
    water = thermochemistry_from_hessian(ThermoSettings(), *_recorded("water"))
    carbon_dioxide = thermochemistry_from_hessian(ThermoSettings(), *_recorded("carbon_dioxide"))
    assert water.mode_count == 3  # 3N-6 for three atoms
    assert carbon_dioxide.mode_count == 4  # 3N-5


def test_a_hessian_with_neither_intensities_nor_dipole_derivatives_is_refused() -> None:
    """A spectrum of zero-intensity bands would look ordinary; refusing does not.

    Unreachable through the server, which always populates one of the two — stated rather than
    assumed, because this is the shape a future backend could arrive in.
    """
    structure, hessian = _recorded("water")
    stripped = hessian.model_copy(update={"dipole_derivatives_npy": None, "ir_intensities": None})
    with pytest.raises(ValueError, match="neither IR"):
        thermochemistry_from_hessian(ThermoSettings(), structure, stripped)


def test_free_energy_is_below_enthalpy_at_room_temperature() -> None:
    """G = H - TS with a positive entropy, so the ordering is a property, not a coincidence."""
    result = thermochemistry_from_hessian(ThermoSettings(), *_recorded("water"))
    assert result.gibbs_free_energy_hartree < result.enthalpy_hartree
    assert result.entropy_cal_per_mol_k > 0


def test_strongest_bands_keeps_the_loud_ones_and_every_imaginary_one() -> None:
    """Truncating a spectrum for a context budget must never drop the reason to distrust it."""
    result = ThermochemistryResult(
        smiles="X",
        structure_id="st_x",
        method="GFN2-xTB",
        solvent=None,
        temperature_k=298.15,
        pressure_pa=101325.0,
        symmetry_number=1,
        is_minimum=False,
        imaginary_frequencies_cm=[-42.0],
        modes=[
            VibrationalMode(wavenumber_cm=-42.0, ir_intensity_km_per_mol=0.0),
            VibrationalMode(wavenumber_cm=800.0, ir_intensity_km_per_mol=1.0),
            VibrationalMode(wavenumber_cm=1200.0, ir_intensity_km_per_mol=90.0),
        ],
        mode_count=3,
        lowest_wavenumbers_cm=[-42.0, 800.0, 1200.0],
        electronic_energy_hartree=-1.0,
        zero_point_energy_kcal=1.0,
        thermal_enthalpy_correction_kcal=2.0,
        entropy_cal_per_mol_k=50.0,
        gibbs_correction_kcal=-1.0,
        enthalpy_hartree=-0.9,
        gibbs_free_energy_hartree=-1.1,
        uncertainty_kcal=2.0,
    )
    kept = [mode.wavenumber_cm for mode in result.strongest_bands(1)]
    assert kept == [-42.0, 1200.0]


def test_a_displacement_moves_the_largest_atom_by_the_configured_step() -> None:
    """The saddle escape is normalized on the largest single-atom motion, not on the vector norm.

    Which keeps the kick the same physical size whether the imaginary mode is localized on one
    methyl or spread over the whole molecule.
    """
    from chemclaw.core.config import settings

    structure, _ = _recorded("water")
    direction = [[1.0, 0.0, 0.0], [0.1, 0.0, 0.0], [0.0, 0.0, 0.0]]
    moved = displaced_along(structure, direction)
    shifts = [
        abs(after[0] - before[0])
        for before, after in zip(structure.positions, moved.positions, strict=True)
    ]
    assert max(shifts) == pytest.approx(settings.xtb_imaginary_kick_angstrom, abs=1e-4)
    assert moved.structure_id != structure.structure_id


def _ensemble(degeneracies: tuple[int, ...]) -> EnsemblePayload:
    """A three-member ensemble at fixed relative energies, with the given rotamer counts."""
    structure, _ = _recorded("water")
    return EnsemblePayload(
        structure_id=structure.structure_id,
        method="GFN2-xTB",
        solvent=None,
        search="conformers",
        effort="quick",
        members=[
            EnsembleMember(energy_hartree=energy, degeneracy=degeneracy, structure=structure)
            for energy, degeneracy in zip((-10.0, -9.999, -9.998), degeneracies, strict=True)
        ],
        total_found=3,
    )


def test_degeneracy_multiplies_the_population() -> None:
    """Not bookkeeping: ignoring it puts n-butane's anti at 73% against CREST's reported 59%.

    Here the same effect in miniature — the lowest member loses population to a doubly degenerate
    one above it, which cannot happen if the weighting counts conformers instead of states.
    """
    plain = ensemble_from_members(
        _ensemble((1, 1, 1)),
        smiles="O",
        search="conformers",
        temperature_k=298.15,
        max_members=10,
    )
    weighted = ensemble_from_members(
        _ensemble((1, 4, 1)),
        smiles="O",
        search="conformers",
        temperature_k=298.15,
        max_members=10,
    )
    assert weighted.conformers[0].population < plain.conformers[0].population
    assert weighted.conformers[1].population > plain.conformers[1].population


def test_truncation_keeps_the_ensembles_own_account_of_itself() -> None:
    """Truncation keeps the ensemble's own account of itself.

    "Here are the 2 that matter out of 3" must not become a claim that there were 2.

    `total_found`, the populations and the conformational entropy are properties of the *whole*
    ensemble; only the returned list is cut. This is what makes asking for a wider view of a cached
    search free rather than a second CREST run.
    """
    full = ensemble_from_members(
        _ensemble((1, 1, 1)),
        smiles="O",
        search="conformers",
        temperature_k=298.15,
        max_members=10,
    )
    cut = ensemble_from_members(
        _ensemble((1, 1, 1)),
        smiles="O",
        search="conformers",
        temperature_k=298.15,
        max_members=2,
    )
    assert len(cut.conformers) == 2
    assert cut.total_found == full.total_found == 3
    assert cut.conformational_entropy_cal_per_mol_k == pytest.approx(
        full.conformational_entropy_cal_per_mol_k
    )


def test_the_populations_depend_on_the_temperature_the_search_never_saw() -> None:
    """Why the weighting stayed here: it is the part a second question actually changes.

    A hotter ensemble is flatter. If this were baked into the cached payload, asking the same
    molecule at another temperature would cost a second search — the most expensive single
    calculation in the system — instead of a cache hit and a millisecond.
    """
    cold = ensemble_from_members(
        _ensemble((1, 1, 1)), smiles="O", search="conformers", temperature_k=200.0, max_members=10
    )
    hot = ensemble_from_members(
        _ensemble((1, 1, 1)), smiles="O", search="conformers", temperature_k=500.0, max_members=10
    )
    assert hot.conformers[0].population < cold.conformers[0].population
    assert hot.conformational_entropy_cal_per_mol_k > cold.conformational_entropy_cal_per_mol_k


def test_an_empty_ensemble_is_refused_rather_than_weighted() -> None:
    """A search that found nothing is a failure, not an ensemble of zero conformers."""
    empty = _ensemble((1, 1, 1)).model_copy(update={"members": [], "total_found": 0})
    with pytest.raises(ValueError, match="no structures"):
        ensemble_from_members(
            empty, smiles="O", search="conformers", temperature_k=298.15, max_members=10
        )
