"""The statistical mechanics that stays here: RRHO over a Hessian, Boltzmann over an ensemble.

The Hessian and the conformer search are cached primitives computed on the server; turning them into
a free energy or populations is cheap arithmetic that depends on a temperature the server never saw,
so a second temperature is a cache hit plus milliseconds. Only numpy, no binaries.

**Quasi-RRHO entropy (Grimme 2012).** Each mode's entropy is a Head-Gordon-damped mix of harmonic
and free-rotor expressions, weighted 1/(1 + (w0/w)^4), so near-zero modes cannot contribute nonsense
to G. `rrho_cutoff_cm` (w0, default 50 cm^-1 as in xtb's `--sthr`) is where the two contribute
equally; the choice is worth about a kcal/mol on a flexible molecule (`tests/test_calc_thermo.py`).

**The standard state follows the phase.** 1 atm in the gas phase, 1 mol/L in an implicit solvent
(`HessianPayload.solvent`): they differ by RT ln(RT c0/P0) = 1.894 kcal/mol per mole of species at
298.15 K, which cancels only when Δn = 0.

**The rotational symmetry number is an input**, shifting entropy by R ln(sigma). It defaults to 1
and an unstated value is reported, since it does not cancel across most reactions (H2 has sigma 2,
benzene 12).
"""

import base64
import io
import math
from collections.abc import Sequence

import numpy as np
from pydantic import BaseModel, Field
from rdkit import Chem
from scipy.linalg import null_space

from chemclaw.core.config import settings
from chemclaw.core.units import HARTREE_TO_KCAL as HARTREE_TO_KCAL
from chemclaw.core.units import JOULE_PER_CALORIE
from chemclaw.science.calc.models import (
    Conformer,
    ConformerEnsemble,
    EnsemblePayload,
    EnsembleSearch,
    HessianPayload,
    Interconversion,
    StandardState,
    Structure,
    ThermochemistryResult,
    VibrationalMode,
    WeightedValue,
)

# Constants. `HARTREE_TO_KCAL` is re-exported from `core.units` (its one definition) because
# `connectors/calc/compose.py` imports it from here. The rest are SI (CODATA 2018 / SI 2019);
# everything internal is SI, and h, kB and N_A are exact, so R is derived below.
_PLANCK = 6.62607015e-34  # J s
_BOLTZMANN = 1.380649e-23  # J/K
_AVOGADRO = 6.02214076e23  # 1/mol
_LIGHT_CM = 2.99792458e10  # cm/s
_HARTREE_J = 4.3597447222071e-18
_AMU_KG = 1.66053906660e-27
_J_PER_MOL_TO_KCAL = 1.0 / (JOULE_PER_CALORIE * 1000.0)

# The molar gas constant in J/(mol K) and in cal/(mol K), each derived once rather than typed.
_GAS_CONSTANT = _BOLTZMANN * _AVOGADRO
_GAS_CONSTANT_CAL = _GAS_CONSTANT / JOULE_PER_CALORIE

# Grimme's free-rotor moment of inertia, the value that keeps the free-rotor entropy finite as the
# frequency goes to zero (kg m^2).
_FREE_ROTOR_INERTIA = 1e-44

# (Debye/Angstrom)^2/amu -> km/mol, the standard IR intensity conversion.
_IR_TO_KM_PER_MOL = 42.2561

# A principal moment of inertia below this fraction of the largest one means the molecule is linear
# and has one rotational degree of freedom fewer.
_LINEAR_INERTIA_RATIO = 1e-4

# The 1 mol/L solution standard state, in mol/m^3; a definition, not a setting.
_MOLAR_STANDARD_CONCENTRATION = 1000.0


class ThermoSettings(BaseModel):
    """The state variables an RRHO free energy is computed at — and nothing the Hessian depends on.

    Deliberately **not** a cache key, and that is the whole point of the split. Before the move
    these lived on a `ThermoSpec` that was hashed into a `xtb.hess` row; now the second derivatives
    are keyed by the server on what can actually move the matrix (geometry, method, solvent), and
    the temperature, pressure, symmetry number and RRHO cutoff move only the arithmetic below. So a
    second question about the same geometry at another temperature costs one cache hit and a
    millisecond, where a shipped composite would have recomputed the Hessian.
    """

    temperature_k: float = Field(default_factory=lambda: settings.xtb_thermo_temperature_k, gt=0)
    # The **gas-phase** reference pressure only; in solution the pressure is c0·R·T, derived in
    # `_reference_pressure`.
    pressure_pa: float = Field(default_factory=lambda: settings.xtb_thermo_pressure_pa, gt=0)
    # Rotational symmetry number. 1 unless the caller knows better; see the module docstring for
    # why it is not derived.
    symmetry_number: int = Field(default=1, ge=1)
    rrho_cutoff_cm: float = Field(default_factory=lambda: settings.xtb_rrho_cutoff_cm, gt=0)


def unpack_npy(encoded: str) -> np.ndarray:
    """Decode one base64 `.npy` blob from a `HessianPayload` into an array.

    `.npy` is compact and self-describing, so the (3N, 3N) shape survives transit.
    `allow_pickle=False` because the bytes crossed a network and pickle is code execution.
    """
    return np.asarray(np.load(io.BytesIO(base64.b64decode(encoded)), allow_pickle=False))


def _atomic_masses(elements: list[int]) -> np.ndarray:
    """Standard atomic weights in amu, one per atom."""
    table = Chem.GetPeriodicTable()
    return np.array([table.GetAtomicWeight(number) for number in elements])


#: Below this wavenumber (cm^-1) the server has projected the mode out as a translation or
#: rotation (written as exact zeros); the tolerance covers float formatting only.
_EXTERNAL_MODE_CM = 0.01


class IntensityAlignmentError(ValueError):
    """The server's intensities cannot be paired with this projection's modes.

    Separate so the failure is confined to the spectrum: thermochemistry reads no intensity.
    `ThermochemistryResult.spectrum_unavailable` carries the message and every band's intensity is
    `None`. Still a `ValueError`, so direct callers are unchanged.
    """


def _align_intensities(
    intensities: np.ndarray,
    modes: int,
    structure: Structure,
    wavenumbers_cm: list[float] | None = None,
) -> np.ndarray:
    """Pair the server's intensities with this projection's modes, by wavenumber where possible.

    xtb lists all 3N entries, translations and rotations first. Counting alone is unsafe: xtb and
    `_is_linear` judge linearity by different criteria and disagree near 180 degrees, which would
    shift every band silently. So when the server sends wavenumbers, the zeroed external rows are
    dropped by value and the remainder must match this projection's mode count or it raises. Older
    cached rows without wavenumbers fall back to subtraction.
    """
    if wavenumbers_cm is not None:
        if len(wavenumbers_cm) != intensities.size:
            raise IntensityAlignmentError(
                f"the server sent {len(wavenumbers_cm)} wavenumbers for {intensities.size} "
                f"intensities for {structure.smiles or structure.structure_id}"
            )
        internal = np.abs(np.asarray(wavenumbers_cm)) >= _EXTERNAL_MODE_CM
        paired = np.asarray(intensities[internal])
        if paired.size != modes:
            raise IntensityAlignmentError(
                f"the server projected out {intensities.size - paired.size} external mode(s) "
                f"leaving {paired.size}, and this projection found {modes} "
                f"for {structure.smiles or structure.structure_id}; pairing them would shift "
                "every band"
            )
        return paired
    external = intensities.size - modes
    if external < 0:
        raise IntensityAlignmentError(
            f"the server reported {intensities.size} modes but the projection found {modes} "
            f"for {structure.smiles or structure.structure_id}"
        )
    return intensities[external:]


def _inertia(masses: np.ndarray, positions: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Principal moments of inertia (amu Angstrom^2, ascending) and their axes as columns.

    One definition for the entropy and the projection, so linearity is judged the same way in both.
    """
    relative = positions - np.average(positions, axis=0, weights=masses)
    tensor = np.zeros((3, 3))
    for mass, vector in zip(masses, relative, strict=True):
        tensor += mass * (np.dot(vector, vector) * np.eye(3) - np.outer(vector, vector))
    moments, axes = np.linalg.eigh(tensor)
    return moments, axes


def _is_linear(moments: np.ndarray) -> bool:
    """Whether a molecule's smallest principal moment is effectively zero."""
    return bool(moments[0] < moments[2] * _LINEAR_INERTIA_RATIO)


def _vibrational_basis(masses: np.ndarray, positions: np.ndarray) -> np.ndarray:
    """An orthonormal basis of the *vibrational* subspace, in mass-weighted coordinates.

    The orthogonal complement of the mass-weighted translations and rotations; diagonalizing inside
    it makes every eigenvalue a vibration, rather than discarding the six smallest and risking a
    real soft mode. Rotations are built about the principal axes and kept by moment of inertia, so a
    near-linear molecule gets 3N-5 modes.
    """
    count = len(masses)
    root_mass = np.sqrt(masses)
    relative = positions - np.average(positions, axis=0, weights=masses)
    moments, axes = _inertia(masses, positions)
    rotational_axes = axes.T[1:] if _is_linear(moments) else axes.T

    columns = []
    for axis in range(3):
        translation = np.zeros((count, 3))
        translation[:, axis] = root_mass
        columns.append(translation.ravel())
    for unit in rotational_axes:
        columns.append((np.cross(unit, relative) * root_mass[:, None]).ravel())

    # Orthonormalize the external subspace, then return its complement.
    left, _, _ = np.linalg.svd(np.column_stack(columns), full_matrices=False)
    return np.asarray(null_space(left.T))


def _normal_modes(
    hessian: np.ndarray, masses: np.ndarray, positions: np.ndarray
) -> tuple[np.ndarray, np.ndarray]:
    """Return (wavenumbers in cm^-1, mass-weighted eigenvectors as columns).

    A negative wavenumber encodes an imaginary frequency; modes are sorted ascending, imaginary
    first.
    """
    if len(masses) == 1:
        return np.zeros(0), np.zeros((3, 0))
    root_mass = np.repeat(np.sqrt(masses * _AMU_KG), 3)
    # Hartree/Angstrom^2 -> J/m^2, then mass-weight: eigenvalues come out in s^-2.
    si_hessian = hessian * _HARTREE_J * 1e20
    mass_weighted = si_hessian / np.outer(root_mass, root_mass)

    basis = _vibrational_basis(masses, positions)
    eigenvalues, vectors = np.linalg.eigh(basis.T @ mass_weighted @ basis)
    # sqrt of a negative eigenvalue is an imaginary frequency, reported as negative.
    wavenumbers = np.sign(eigenvalues) * np.sqrt(np.abs(eigenvalues)) / (2 * np.pi * _LIGHT_CM)
    order = np.argsort(wavenumbers)
    return wavenumbers[order], (basis @ vectors)[:, order]


def _ir_intensities(
    dipole_derivatives: np.ndarray, vectors: np.ndarray, masses: np.ndarray
) -> np.ndarray:
    """IR intensities in km/mol for each normal mode.

    A mode's Cartesian displacement per unit normal coordinate is the mass-weighted eigenvector over
    the square root of the mass, which converts the Cartesian dipole derivative.
    """
    if vectors.shape[1] == 0:
        return np.zeros(0)
    scaled = vectors / np.repeat(np.sqrt(masses), 3)[:, None]
    per_mode = scaled.T @ dipole_derivatives  # (modes, 3), in Debye/(Angstrom sqrt(amu))
    return np.asarray(_IR_TO_KM_PER_MOL * np.sum(per_mode**2, axis=1))


def _translational(mass_amu: float, temperature: float, pressure: float) -> tuple[float, float]:
    """(energy, entropy) of translation per mole, in J/mol and J/(mol K).

    Sackur-Tetrode at the given pressure; equipartition energy.
    """
    mass = mass_amu * _AMU_KG
    partition = (2 * math.pi * mass * _BOLTZMANN * temperature / _PLANCK**2) ** 1.5 * (
        _BOLTZMANN * temperature / pressure
    )
    return 1.5 * _GAS_CONSTANT * temperature, _GAS_CONSTANT * (math.log(partition) + 2.5)


def standard_state_for(solvent: str | None) -> StandardState:
    """Which reference state a free energy computed in this medium is quoted at.

    One definition shared by the RRHO arithmetic and `connectors/calc/compose.py`, so a label and
    the number it labels cannot disagree.
    """
    return "gas-1atm" if solvent is None else "solution-1M"


def _reference_pressure(spec: ThermoSettings, solvent: str | None) -> tuple[float, StandardState]:
    """The pressure the translational partition function is evaluated at, and what to call it.

    The only pressure-dependent term is Sackur-Tetrode's. Gas phase keeps the configured 1 atm; an
    implicit solvent uses 1 mol/L, i.e. c0·R·T (2.479 MPa at 298.15 K), raising G by 1.894 kcal/mol
    per species. Derived from `hessian.solvent`, since the phase is a property of the calculation.
    """
    state = standard_state_for(solvent)
    if state == "gas-1atm":
        return spec.pressure_pa, state
    return _GAS_CONSTANT * spec.temperature_k * _MOLAR_STANDARD_CONCENTRATION, state


def _rotational(
    masses: np.ndarray, positions: np.ndarray, temperature: float, symmetry: int
) -> tuple[float, float]:
    """(energy, entropy) of rotation per mole, in J/mol and J/(mol K).

    Monatomic and linear cases are read from the principal moments themselves.
    """
    if len(masses) == 1:
        return 0.0, 0.0
    amu_angstrom2, _ = _inertia(masses, positions)
    moments = amu_angstrom2 * _AMU_KG * 1e-20  # amu Angstrom^2 -> kg m^2
    linear = _is_linear(moments)
    factor = 8 * math.pi**2 * _BOLTZMANN * temperature / _PLANCK**2
    if linear:
        partition = factor * moments[2] / symmetry
        return _GAS_CONSTANT * temperature, _GAS_CONSTANT * (math.log(partition) + 1.0)
    partition = math.sqrt(math.pi * factor**3 * moments.prod()) / symmetry
    return 1.5 * _GAS_CONSTANT * temperature, _GAS_CONSTANT * (math.log(partition) + 1.5)


def _vibrational(
    wavenumbers: np.ndarray, temperature: float, cutoff_cm: float
) -> tuple[float, float, float]:
    """(zero-point energy, thermal energy, entropy) per mole from the real modes.

    Imaginary modes are skipped. Entropy uses Grimme's quasi-RRHO interpolation below `cutoff_cm`;
    energy and ZPE stay harmonic, as published.
    """
    zero_point = thermal = entropy = 0.0
    for wavenumber in wavenumbers:
        if wavenumber <= 0:
            continue
        frequency = wavenumber * _LIGHT_CM
        theta = _PLANCK * frequency / _BOLTZMANN
        ratio = theta / temperature
        zero_point += 0.5 * _GAS_CONSTANT * theta
        thermal += _GAS_CONSTANT * theta / math.expm1(ratio)
        harmonic = _GAS_CONSTANT * (ratio / math.expm1(ratio) - math.log(-math.expm1(-ratio)))
        inertia = _PLANCK / (8 * math.pi**2 * frequency)
        effective = inertia * _FREE_ROTOR_INERTIA / (inertia + _FREE_ROTOR_INERTIA)
        free_rotor = _GAS_CONSTANT * (
            0.5
            + math.log(
                math.sqrt(
                    8 * math.pi**3 * effective * _BOLTZMANN * temperature / _PLANCK**2,
                )
            )
        )
        weight = 1.0 / (1.0 + (cutoff_cm / wavenumber) ** 4)
        entropy += weight * harmonic + (1 - weight) * free_rotor
    return zero_point, thermal, entropy


def thermochemistry_from_hessian(
    spec: ThermoSettings, structure: Structure, hessian: HessianPayload
) -> ThermochemistryResult:
    """RRHO thermochemistry over a Hessian the server computed — the arithmetic, and only that.

    The one implementation of the quasi-RRHO and symmetry handling. `structure` should be the
    geometry the Hessian was taken at; a non-minimum is reported through `is_minimum` rather than
    refused. The gradient the server sends is checked against `xtb_stationary_gradient_tolerance`
    (`is_stationary`) and folded into `is_minimum`, because a non-stationary geometry can have no
    imaginary mode yet a ZPE that is too low.

    Synchronous and CPU-bound (a 3N x 3N eigendecomposition); call via `asyncio.to_thread` from an
    event loop.
    """
    masses = _atomic_masses(structure.elements)
    _, positions = structure.arrays()
    matrix = unpack_npy(hessian.hessian_npy)
    wavenumbers, vectors = _normal_modes(matrix, masses, positions)
    electronic = hessian.electronic_energy_hartree
    # `None` means this result carries no spectrum (see `IntensityAlignmentError`); nothing else
    # depends on intensities.
    intensities: np.ndarray | None
    spectrum_unavailable: str | None = None
    if hessian.ir_intensities is not None:
        try:
            intensities = _align_intensities(
                np.asarray(hessian.ir_intensities),
                wavenumbers.size,
                structure,
                hessian.ir_wavenumbers_cm,
            )
        except IntensityAlignmentError as mismatch:
            intensities = None
            spectrum_unavailable = str(mismatch)
    elif hessian.dipole_derivatives_npy is not None:
        intensities = _ir_intensities(unpack_npy(hessian.dipole_derivatives_npy), vectors, masses)
    else:
        # Unreachable through the server, but a Hessian with neither would otherwise yield a
        # spectrum of
        # zero-intensity bands.
        raise ValueError(
            f"the Hessian for {structure.smiles or structure.structure_id} carries neither IR "
            "intensities nor dipole derivatives, so no spectrum can be derived from it"
        )

    # One entry per mode either way, so the zip below stays strict.
    per_mode: list[float | None] = (
        [None] * int(wavenumbers.size) if intensities is None else list(intensities)
    )

    temperature = spec.temperature_k
    # The reference state is the medium's (see `_reference_pressure`); in solution every species
    # shifts by RT ln(RT c0/P0), which does not cancel when Δn != 0.
    pressure, standard_state = _reference_pressure(spec, hessian.solvent)
    translation_energy, translation_entropy = _translational(
        float(masses.sum()), temperature, pressure
    )
    rotation_energy, rotation_entropy = _rotational(
        masses, positions, temperature, spec.symmetry_number
    )
    zero_point, vibration_energy, vibration_entropy = _vibrational(
        wavenumbers, temperature, spec.rrho_cutoff_cm
    )
    # Spin degeneracy: R ln(2S+1), zero for the closed-shell case and the reason an open-shell
    # species is not simply "the same thermochemistry with a different SCF".
    electronic_entropy = _GAS_CONSTANT * math.log(structure.multiplicity)

    # H = E + ZPE + thermal(vib+rot+trans) + RT (the pV term of an ideal gas).
    enthalpy_correction = (
        zero_point
        + vibration_energy
        + rotation_energy
        + translation_energy
        + _GAS_CONSTANT * temperature
    )
    entropy = translation_entropy + rotation_entropy + vibration_entropy + electronic_entropy
    gibbs_correction = enthalpy_correction - temperature * entropy
    hartree_per_j_mol = 1.0 / (_HARTREE_J * _AVOGADRO)

    imaginary = [
        round(float(value), 1)
        for value in wavenumbers
        if value < -settings.xtb_imaginary_threshold_cm
    ]
    # The other way of not being a minimum: away from a stationary point the spurious modes are
    # often
    # not imaginary, so the frequencies cannot show it. `None` means not assessed (no gradient
    # reported), which stays distinct from stationary.
    gradient = hessian.max_gradient_hartree_per_angstrom
    stationary = (
        None if gradient is None else gradient <= settings.xtb_stationary_gradient_tolerance
    )
    # Index 0 is the most negative imaginary mode (modes sort ascending): the steepest downhill
    # direction to escape along.
    displacement = (
        (vectors[:, 0] / np.repeat(np.sqrt(masses), 3)).reshape(-1, 3).tolist()
        if imaginary
        else None
    )
    return ThermochemistryResult(
        smiles=structure.smiles,
        structure_id=structure.structure_id,
        method=hessian.method,
        solvent=hessian.solvent,
        temperature_k=temperature,
        pressure_pa=pressure,
        standard_state=standard_state,
        symmetry_number=spec.symmetry_number,
        is_minimum=not imaginary and stationary is not False,
        imaginary_frequencies_cm=imaginary,
        is_stationary=stationary,
        max_gradient_hartree_per_angstrom=gradient,
        spectrum_unavailable=spectrum_unavailable,
        modes=[
            VibrationalMode(
                wavenumber_cm=round(float(wavenumber), 1),
                ir_intensity_km_per_mol=None if band is None else round(float(band), 2),
            )
            # `strict=True`: an intensity array longer than the mode set must fail, not be silently
            # truncated.
            for wavenumber, band in zip(wavenumbers, per_mode, strict=True)
        ],
        mode_count=len(wavenumbers),
        lowest_wavenumbers_cm=[round(float(value), 1) for value in wavenumbers[:5]],
        electronic_energy_hartree=electronic,
        zero_point_energy_kcal=zero_point * _J_PER_MOL_TO_KCAL,
        thermal_enthalpy_correction_kcal=enthalpy_correction * _J_PER_MOL_TO_KCAL,
        entropy_cal_per_mol_k=entropy * _J_PER_MOL_TO_KCAL * 1000.0,
        gibbs_correction_kcal=gibbs_correction * _J_PER_MOL_TO_KCAL,
        enthalpy_hartree=electronic + enthalpy_correction * hartree_per_j_mol,
        gibbs_free_energy_hartree=electronic + gibbs_correction * hartree_per_j_mol,
        uncertainty_kcal=settings.xtb_reaction_uncertainty_kcal,
        imaginary_displacement=displacement,
    )


def displaced_along(structure: Structure, direction: list[list[float]]) -> Structure:
    """Push `structure` along `direction`, scaled so the largest atom moves a fixed step.

    The escape from a saddle point: a gradient optimization can converge onto a symmetric saddle
    (e.g. an eclipsed methyl). Normalizing on the largest single-atom motion keeps the kick the same
    physical size whether the mode is localized or delocalized.
    """
    step = np.asarray(direction)
    step = settings.xtb_imaginary_kick_angstrom * step / np.abs(step).max()
    _, positions = structure.arrays()
    return Structure(
        elements=structure.elements,
        positions=(positions + step).tolist(),
        charge=structure.charge,
        multiplicity=structure.multiplicity,
        smiles=structure.smiles,
    )


def rt_kcal(temperature_k: float) -> float:
    """`RT` in kcal/mol — the energy scale every Boltzmann question here is asked in.

    Public so callers never hard-code RT at 298.15 K for a temperature the caller chose.
    """
    return _GAS_CONSTANT_CAL * temperature_k / 1000.0


def rate_from_barrier(barrier_kcal: float, temperature_k: float) -> float:
    """The Eyring rate constant, in s^-1, for a free-energy barrier in kcal/mol.

    `k = (kB T / h) exp(-dG‡ / RT)` with transmission coefficient 1, the convention tabulated
    barriers use, so the model need not do the exponential in its head.
    """
    exponent = -barrier_kcal * 1000.0 / (_GAS_CONSTANT_CAL * temperature_k)
    return (_BOLTZMANN * temperature_k / _PLANCK) * math.exp(exponent)


def half_life_from_barrier(
    barrier_kcal: float, temperature_k: float, uncertainty_kcal: float | None = None
) -> Interconversion:
    """How long a rotamer survives at `temperature_k`, with the band the method's error implies.

    `t½ = ln2 / k` for a first-order interconversion; the band is the same at `barrier ±
    uncertainty` (a lower barrier gives the shorter half-life).

    Args:
        barrier_kcal: The free-energy barrier out of the populated well, in kcal/mol.
        temperature_k: The temperature the lifetime is quoted at — the process temperature when the
        question is racemization during manufacture.
        uncertainty_kcal: The method's uncertainty; the configured semiempirical value by default.

    Returns:
        The rate, the half-life, and the shortest and longest half-life the barrier's uncertainty
        allows.
    """
    band = settings.xtb_reaction_uncertainty_kcal if uncertainty_kcal is None else uncertainty_kcal
    rate = rate_from_barrier(barrier_kcal, temperature_k)
    return Interconversion(
        barrier_kcal=barrier_kcal,
        temperature_k=temperature_k,
        rate_per_second=rate,
        half_life_seconds=math.log(2.0) / rate,
        half_life_seconds_fastest=math.log(2.0)
        / rate_from_barrier(barrier_kcal - band, temperature_k),
        half_life_seconds_slowest=math.log(2.0)
        / rate_from_barrier(barrier_kcal + band, temperature_k),
        uncertainty_kcal=band,
    )


def boltzmann_populations(
    relative_kcal: Sequence[float], degeneracies: Sequence[int], temperature_k: float
) -> list[float]:
    """Normalized populations from relative energies in kcal/mol, weighted by degeneracy.

    Shared by every ensemble weighting and averaging so they agree exactly. Each conformer stands
    for `g` equally populated rotamers and carries `g` times the weight (n-butane anti: 59.2%,
    matching CREST).
    """
    rt = rt_kcal(temperature_k)
    smallest = min(relative_kcal)
    weights = [
        degeneracy * math.exp(-(value - smallest) / rt)
        for value, degeneracy in zip(relative_kcal, degeneracies, strict=True)
    ]
    total = sum(weights)
    return [weight / total for weight in weights]


def ensemble_entropy(populations: Sequence[float], degeneracies: Sequence[int]) -> float:
    """Conformational entropy in cal/(mol K) from a population distribution.

    `S = -R sum p ln(p/g)`: the sum runs over states, each conformer standing for `g` rotamers.
    Matches CREST's reported ensemble entropy for n-butane.
    """
    return -_GAS_CONSTANT_CAL * sum(
        population * math.log(population / degeneracy)
        for population, degeneracy in zip(populations, degeneracies, strict=True)
        if population > 0
    )


def macrostate_free_energy_kcal(
    relative_kcal: Sequence[float], degeneracies: Sequence[int], temperature_k: float
) -> float:
    """The ensemble's free energy relative to its lowest member: `-RT ln sum_i g_i exp(-dE_i/RT)`.

    Always <= 0, and an identity rather than a correction: equilibria between macrostates are ratios
    of these partition functions, which is why a pKa uses it. Unlike
    `ConformerEnsemble.ensemble_correction_kcal` (`-T*S_conf` added to the lowest member), this also
    includes the Boltzmann-averaged energy above the lowest member; the two agree only when one
    conformer holds all the population. Two sites within RT shift a pKa by up to RT ln 2. Takes
    relative energies in kcal/mol, the same convention as `boltzmann_populations`.
    """
    rt = rt_kcal(temperature_k)
    smallest = min(relative_kcal)
    partition = sum(
        degeneracy * math.exp(-(value - smallest) / rt)
        for value, degeneracy in zip(relative_kcal, degeneracies, strict=True)
    )
    return smallest - rt * math.log(partition)


def free_energy_populations(
    gibbs_hartree: Sequence[float], degeneracies: Sequence[int], temperature_k: float
) -> list[float]:
    """Populations from Gibbs free energies rather than from electronic energies.

    A different treatment, not a better one, and the result must say which ran: weighting by G
    carries ZPE, thermal and entropic differences but costs one Hessian per member. It is
    `boltzmann_populations` over a different energy.
    """
    lowest = min(gibbs_hartree)
    relative = [(value - lowest) * HARTREE_TO_KCAL for value in gibbs_hartree]
    return boltzmann_populations(relative, degeneracies, temperature_k)


def weighted_average(values: Sequence[float], populations: Sequence[float]) -> WeightedValue:
    """One scalar property, averaged over an ensemble at its populations.

    Plain sequences so dipoles, gaps and per-atom indices share one implementation.
    """
    if not values:
        raise ValueError("nothing to average")
    mean = sum(value * population for value, population in zip(values, populations, strict=True))
    return WeightedValue(
        mean=mean, minimum=min(values), maximum=max(values), spread=max(values) - min(values)
    )


def ensemble_from_members(
    payload: EnsemblePayload,
    *,
    smiles: str | None,
    search: EnsembleSearch,
    temperature_k: float,
    max_members: int,
) -> ConformerEnsemble:
    """Weight a cached search's members into an ensemble at `temperature_k`.

    Not baked into the cached payload because: populations depend on temperature, so another
    temperature is a cache hit rather than a new CREST run; degeneracy multiplies each population;
    and `max_members` truncates only the listing — `total_found`, the populations and the entropy
    describe the whole ensemble.
    """
    members = payload.members
    if not members:
        raise ValueError("the conformer search returned no structures")
    lowest = min(member.energy_hartree for member in members)
    relative = [(member.energy_hartree - lowest) * HARTREE_TO_KCAL for member in members]
    degeneracies = [member.degeneracy for member in members]
    populations = boltzmann_populations(relative, degeneracies, temperature_k)
    entropy = ensemble_entropy(populations, degeneracies)
    # Sorted here, so lowest-first is this function's guarantee rather than the server's:
    # `ConformerEnsemble.lowest_structure_id` and `compose.refined_ensemble` both rely on it.
    ordered = sorted(zip(relative, populations, members, strict=True), key=lambda entry: entry[0])
    conformers = [
        Conformer(
            relative_kcal=round(energy, 3),
            population=round(population, 4),
            degeneracy=member.degeneracy,
            structure=member.structure,
        )
        for energy, population, member in ordered
    ]
    return ConformerEnsemble(
        smiles=smiles,
        method=payload.method,
        search=search,
        effort=payload.effort,
        solvent=payload.solvent,
        temperature_k=temperature_k,
        conformers=conformers[:max_members],
        total_found=payload.total_found,
        conformational_entropy_cal_per_mol_k=round(entropy, 3),
        ensemble_correction_kcal=round(-temperature_k * entropy / 1000.0, 3),
    )
