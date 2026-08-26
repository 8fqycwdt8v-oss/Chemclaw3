"""The statistical mechanics that stayed behind: RRHO over a Hessian, Boltzmann over an ensemble.

Two pieces of arithmetic, one argument. `D-2026-08-16-the-physics-leaves-the-cache-stays` split
`calc` by **composability**, and the line it drew runs straight through thermochemistry: the second
derivatives cost minutes and are a *primitive* the server caches under its own key, while turning
them into a free energy is a page of partition functions costing milliseconds and depending on the
temperature. Ship the composite and every repeat recomputes a Hessian to answer a question about
298 K versus 310 K; ship the primitive and compose here, and the second question is a cache hit
plus a millisecond. The same shape holds for a CREST search: sampling conformational space is the
expensive, cached half, and weighting the members it found is arithmetic that depends on a
temperature the search never saw.

So neither of these is physics that failed to move. They are the halves that had to stay for the
cache to be worth keeping, and they need no binary, no tblite and no crest — only numpy over what
came back over the wire.

Two deliberate choices about the physics, carried over unchanged because they are the numbers'
meaning rather than their implementation:

**Quasi-RRHO entropy (Grimme 2012).** A harmonic oscillator's entropy diverges as its frequency
goes to zero, and the lowest modes are exactly where the harmonic approximation is worst — so a
5 cm^-1 mode from a floppy molecule can contribute several kcal/mol of nonsense to G. Below
`rrho_cutoff_cm` a mode is interpolated toward a free rotor, which is what `xtb` itself does.

**The rotational symmetry number is an input, not a guess.** It shifts the entropy by exactly
R ln(sigma) — 1.4 cal/mol/K for a C2 axis, 4.9 for benzene — and deriving it needs point-group
detection this layer does not do. It defaults to 1, and a caller that leaves it unstated is told so
rather than served a free energy that is wrong by RT ln(sigma). The error does **not** cancel within
a balanced reaction unless both sides carry the same symmetry, and for the chemistry that matters
they do not: every hydrogenation has H2 (sigma 2) on one side only, and anything aromatic carries
benzene's sigma 12.
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
from chemclaw.science.calc.models import (
    Conformer,
    ConformerEnsemble,
    EnsemblePayload,
    EnsembleSearch,
    HessianPayload,
    Interconversion,
    Structure,
    ThermochemistryResult,
    VibrationalMode,
    WeightedValue,
)

# Hartree to kcal/mol. Every energy the server returns is in Hartree; every difference a chemist
# reads is in kcal/mol, so this is the one conversion every composite here and in
# `connectors/calc/compose.py` needs.
HARTREE_TO_KCAL = 627.5094740631

# SI constants (CODATA), and the conversions this module needs. Everything internal is SI; only the
# reported fields are in the units a chemist reads.
_PLANCK = 6.62607015e-34  # J s
_BOLTZMANN = 1.380649e-23  # J/K
_AVOGADRO = 6.02214076e23  # 1/mol
_GAS_CONSTANT = 8.314462618  # J/(mol K)
_LIGHT_CM = 2.99792458e10  # cm/s
_HARTREE_J = 4.3597447222071e-18
_AMU_KG = 1.66053906660e-27
_J_PER_MOL_TO_KCAL = 1.0 / 4184.0

# The same gas constant in cal/(mol K), which is the unit a conformational entropy is reported in.
_GAS_CONSTANT_CAL = 1.987204258640832

# Grimme's free-rotor moment of inertia, the value that keeps the free-rotor entropy finite as the
# frequency goes to zero (kg m^2).
_FREE_ROTOR_INERTIA = 1e-44

# (Debye/Angstrom)^2/amu -> km/mol, the standard IR intensity conversion.
_IR_TO_KM_PER_MOL = 42.2561

# A principal moment of inertia below this fraction of the largest one means the molecule is linear
# and has one rotational degree of freedom fewer.
_LINEAR_INERTIA_RATIO = 1e-4


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
    pressure_pa: float = Field(default_factory=lambda: settings.xtb_thermo_pressure_pa, gt=0)
    # Rotational symmetry number. 1 unless the caller knows better; see the module docstring for
    # why it is not derived.
    symmetry_number: int = Field(default=1, ge=1)
    rrho_cutoff_cm: float = Field(default_factory=lambda: settings.xtb_rrho_cutoff_cm, gt=0)


def unpack_npy(encoded: str) -> np.ndarray:
    """Decode one base64 `.npy` blob from a `HessianPayload` into an array.

    `.npy` rather than a JSON list because a drug-sized Hessian is megabytes — 33 atoms is 99x99
    doubles — and because it is self-describing, so the (3N, 3N) shape cannot be lost in transit and
    silently reshaped into something that still diagonalizes.

    `allow_pickle=False` because these bytes crossed a network and may have come out of a database:
    pickle deserialization is arbitrary code execution, and nothing here needs it.
    """
    return np.asarray(np.load(io.BytesIO(base64.b64decode(encoded)), allow_pickle=False))


def _atomic_masses(elements: list[int]) -> np.ndarray:
    """Standard atomic weights in amu, one per atom."""
    table = Chem.GetPeriodicTable()
    return np.array([table.GetAtomicWeight(number) for number in elements])


def _align_intensities(intensities: np.ndarray, modes: int, structure: Structure) -> np.ndarray:
    """Drop xtb's projected-out external modes so intensities pair with our own modes.

    xtb lists all 3N entries with the translations and rotations first; the projection below reports
    only the vibrations. Reconciling by count is the point — if the two projections disagree about
    how many external modes a molecule has, every intensity would shift by one mode, so a mismatch
    fails loudly instead (gate G4).
    """
    external = intensities.size - modes
    if external < 0:
        raise ValueError(
            f"the server reported {intensities.size} modes but the projection found {modes} "
            f"for {structure.smiles or structure.structure_id}"
        )
    return intensities[external:]


def _inertia(masses: np.ndarray, positions: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Principal moments of inertia (amu Angstrom^2, ascending) and their axes as columns.

    One definition for the two places the molecule's rotations matter — the entropy and the
    projection that separates rotation from vibration — so "is this molecule linear" cannot be
    answered one way in one place and another way in the other.
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

    Builds the mass-weighted translations and rotations and returns their orthogonal complement.
    Diagonalizing the Hessian inside that complement is what makes every eigenvalue a vibration —
    the alternative, discarding the six smallest eigenvalues afterwards, silently discards a real
    low-frequency mode whenever one is smaller than a translational residual.

    Rotations are built about the **principal axes** and kept by their moment of inertia, which is
    what makes a linear molecule come out with 3N-5 modes instead of 3N-6. Filtering the raw x/y/z
    rotations by singular value instead does not work: an optimized "linear" molecule is bent by a
    fraction of a degree, so its null rotation has a small but perfectly ordinary singular value and
    survives the cut — measured on CO2, which lost a real mode that way.
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

    A negative wavenumber encodes an imaginary frequency (a negative Hessian eigenvalue). Modes come
    out sorted by wavenumber, so imaginary modes are first.
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

    The mode's Cartesian displacement per unit normal coordinate is the mass-weighted eigenvector
    divided by the square root of the mass, so the dipole derivative with respect to the normal
    coordinate follows directly from the Cartesian one.
    """
    if vectors.shape[1] == 0:
        return np.zeros(0)
    scaled = vectors / np.repeat(np.sqrt(masses), 3)[:, None]
    per_mode = scaled.T @ dipole_derivatives  # (modes, 3), in Debye/(Angstrom sqrt(amu))
    return np.asarray(_IR_TO_KM_PER_MOL * np.sum(per_mode**2, axis=1))


def _translational(mass_amu: float, temperature: float, pressure: float) -> tuple[float, float]:
    """(energy, entropy) of translation per mole, in J/mol and J/(mol K).

    Entropy is Sackur-Tetrode at the given pressure; the energy is equipartition.
    """
    mass = mass_amu * _AMU_KG
    partition = (2 * math.pi * mass * _BOLTZMANN * temperature / _PLANCK**2) ** 1.5 * (
        _BOLTZMANN * temperature / pressure
    )
    return 1.5 * _GAS_CONSTANT * temperature, _GAS_CONSTANT * (math.log(partition) + 2.5)


def _rotational(
    masses: np.ndarray, positions: np.ndarray, temperature: float, symmetry: int
) -> tuple[float, float]:
    """(energy, entropy) of rotation per mole, in J/mol and J/(mol K).

    Handles the monatomic (no rotation) and linear (two degrees of freedom) cases from the principal
    moments themselves rather than from a separate structural test.
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

    Imaginary modes are skipped — they contribute nothing physical, and including one would be a way
    of pretending a saddle point is a minimum. Entropy uses Grimme's quasi-RRHO interpolation toward
    a free rotor below `cutoff_cm`; energy and ZPE stay harmonic, which is the published form of the
    approximation.
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

    Every caller goes through here, so the quasi-RRHO treatment and the symmetry handling have
    exactly one implementation regardless of which backend produced the matrix. `structure` should
    be the geometry the Hessian was taken at; if it is not a converged minimum the result says so
    through `is_minimum` rather than refusing, because "this geometry is a saddle point" is a useful
    answer and often the question.

    Synchronous and CPU-bound (an eigendecomposition of a 3N x 3N matrix), so a caller on an event
    loop hands it to `asyncio.to_thread` — the same treatment the embedding used to get.
    """
    masses = _atomic_masses(structure.elements)
    _, positions = structure.arrays()
    matrix = unpack_npy(hessian.hessian_npy)
    wavenumbers, vectors = _normal_modes(matrix, masses, positions)
    electronic = hessian.electronic_energy_hartree
    if hessian.ir_intensities is not None:
        intensities = _align_intensities(
            np.asarray(hessian.ir_intensities), wavenumbers.size, structure
        )
    elif hessian.dipole_derivatives_npy is not None:
        intensities = _ir_intensities(unpack_npy(hessian.dipole_derivatives_npy), vectors, masses)
    else:
        # Unreachable through the server, which always populates one of the two. Stated rather than
        # assumed, because a Hessian with neither would silently produce a spectrum of
        # zero-intensity bands instead of failing.
        raise ValueError(
            f"the Hessian for {structure.smiles or structure.structure_id} carries neither IR "
            "intensities nor dipole derivatives, so no spectrum can be derived from it"
        )

    temperature = spec.temperature_k
    translation_energy, translation_entropy = _translational(
        float(masses.sum()), temperature, spec.pressure_pa
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
    # The *most negative* imaginary mode is the first, since modes are sorted ascending and an
    # imaginary frequency is reported as negative — so index 0 is the steepest downhill direction,
    # not the softest one. That is the direction to escape along.
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
        pressure_pa=spec.pressure_pa,
        symmetry_number=spec.symmetry_number,
        is_minimum=not imaginary,
        imaginary_frequencies_cm=imaginary,
        modes=[
            VibrationalMode(
                wavenumber_cm=round(float(wavenumber), 1),
                ir_intensity_km_per_mol=round(float(intensity), 2),
            )
            for wavenumber, intensity in zip(wavenumbers, intensities, strict=True)
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

    The escape from a saddle point, and the only geometry this repository still *builds*: a plain
    gradient optimization converges to the nearest stationary point, which is not always a minimum
    — the common case is a force field handing over an eclipsed methyl and a Cartesian optimizer
    preserving that symmetry all the way down onto the rotational saddle. Measured on ethyl
    acetate, an ordinary ester, the optimizer settles at a -42 cm^-1 mode.

    Normalizing on the largest single-atom motion rather than on the vector norm keeps the kick the
    same physical size whether the mode is localized on one methyl or spread over the whole
    molecule.
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


def rate_from_barrier(barrier_kcal: float, temperature_k: float) -> float:
    """The Eyring rate constant, in s^-1, for a free-energy barrier in kcal/mol.

    `k = (kB T / h) exp(-dG‡ / RT)`, with the transmission coefficient at 1 — the convention every
    tabulated barrier in the literature is quoted under, so a computed number and a measured one
    can be compared without a conversion nobody states.

    Arithmetic over a result rather than a calculation, which is why it lives here beside the RRHO
    and Boltzmann halves rather than behind the wire: it needs no binary, and it is what the model
    would otherwise be asked to do in its head at the exact point where one kcal/mol is a factor
    of five.
    """
    exponent = -barrier_kcal * 1000.0 / (_GAS_CONSTANT_CAL * temperature_k)
    return (_BOLTZMANN * temperature_k / _PLANCK) * math.exp(exponent)


def half_life_from_barrier(
    barrier_kcal: float, temperature_k: float, uncertainty_kcal: float | None = None
) -> Interconversion:
    """How long a rotamer survives at `temperature_k`, with the band the method's error implies.

    `t½ = ln2 / k` for a first-order process, which an interconversion is. The band is the same
    arithmetic at `barrier ± uncertainty`: a *lower* barrier is a *shorter* half-life, so the
    fastest end comes from the minus side.

    Args:
        barrier_kcal: The free-energy barrier out of the populated well, in kcal/mol.
        temperature_k: The temperature the lifetime is quoted at — the process temperature, not
            298 K, when the question is whether something racemizes during manufacture.
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


def barrier_from_half_life(half_life_seconds: float, temperature_k: float) -> float:
    """The barrier a required lifetime implies — Eyring read backwards, in kcal/mol.

    The question a formulation or a specification actually asks: *what barrier would this compound
    need for a two-year shelf life?* Answering it turns a computed barrier from a number into a
    comparison against a requirement, which is what decides whether an experiment is worth running.

    The exact inverse of `rate_from_barrier`, so the two cannot drift into disagreeing about the
    prefactor.
    """
    if half_life_seconds <= 0:
        raise ValueError(f"a half-life is positive; got {half_life_seconds}")
    rate = math.log(2.0) / half_life_seconds
    prefactor = _BOLTZMANN * temperature_k / _PLANCK
    return -_GAS_CONSTANT_CAL * temperature_k * math.log(rate / prefactor) / 1000.0


def boltzmann_populations(
    relative_kcal: Sequence[float], degeneracies: Sequence[int], temperature_k: float
) -> list[float]:
    """Normalized populations from relative energies in kcal/mol, weighted by degeneracy.

    Extracted from `ensemble_from_members`, where it was inline, once a third caller appeared: a
    free-energy-weighted ensemble and a Boltzmann-averaged property both need exactly this and must
    agree with it to the last digit — an ensemble whose populations sum to one under one convention,
    averaged over by a property using another, is two answers to one question.

    **Degeneracy multiplies the weight, and it is not bookkeeping.** Each conformer stands for `g`
    rotamers that are equally populated, so it carries `g` times the statistical weight. Measured on
    n-butane, ignoring it puts the anti conformer at 73% against CREST's own reported 59.1%; with
    it, 59.2%.
    """
    rt = _GAS_CONSTANT_CAL * temperature_k / 1000.0  # kcal/mol
    smallest = min(relative_kcal)
    weights = [
        degeneracy * math.exp(-(value - smallest) / rt)
        for value, degeneracy in zip(relative_kcal, degeneracies, strict=True)
    ]
    total = sum(weights)
    return [weight / total for weight in weights]


def ensemble_entropy(populations: Sequence[float], degeneracies: Sequence[int]) -> float:
    """Conformational entropy in cal/(mol K) from a population distribution.

    `S = -R sum p ln(p/g)`: each conformer stands for `g` equally populated rotamers, so the sum
    runs over *states* rather than over conformers. Reproduces CREST's own reported ensemble entropy
    for n-butane to three figures, which is the check that the two count the same thing.
    """
    return -_GAS_CONSTANT_CAL * sum(
        population * math.log(population / degeneracy)
        for population, degeneracy in zip(populations, degeneracies, strict=True)
        if population > 0
    )


def free_energy_populations(
    gibbs_hartree: Sequence[float], degeneracies: Sequence[int], temperature_k: float
) -> list[float]:
    """Populations from Gibbs free energies rather than from electronic energies.

    **A different treatment, not a better one, and the result must say which ran.** Weighting by G
    carries the zero-point, thermal and entropic differences between conformers, which is the right
    distribution when those differ — a conformer with a low electronic energy and a stiff, ordered
    geometry is over-weighted by E alone. It costs one Hessian per member, which is why
    `ensemble_from_members` weights by E and this stands beside it rather than replacing it; D-101
    states the trade as "one Hessian per member, half an hour each at 76 atoms".

    Same convention as `boltzmann_populations` by construction — it *is* that function over a
    different energy — so the two cannot drift into disagreeing about degeneracy or about the
    reference state.
    """
    lowest = min(gibbs_hartree)
    relative = [(value - lowest) * HARTREE_TO_KCAL for value in gibbs_hartree]
    return boltzmann_populations(relative, degeneracies, temperature_k)


def weighted_average(values: Sequence[float], populations: Sequence[float]) -> WeightedValue:
    """One scalar property, averaged over an ensemble at its populations.

    Takes plain sequences rather than a result model because a dipole, a HOMO-LUMO gap and one
    atom's Fukui index are the same arithmetic: a per-atom property is this function called once per
    atom, and giving each its own function is how three of them come to disagree.
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

    The Boltzmann half of the same split the RRHO arithmetic above is the harmonic half of. Three
    things it does, and each is a reason it is *not* baked into the cached payload:

    - **Populations depend on the temperature**, which does not move the search. Recomputing them
      here is what lets a second question at another temperature be a cache hit on the expensive
      half rather than a second CREST run — the most expensive single calculation in the system.
    - **Degeneracy multiplies the population.** Measured on n-butane, ignoring it puts the anti
      conformer at 73% against CREST's own reported 59.1%; with it, 59.2%.
    - **`max_members` truncates a finished answer**, so asking to see more of one already computed
      is free. `total_found`, the populations and the entropy are properties of the *whole*
      ensemble and are deliberately left alone — truncating them would turn "here are the 10 that
      matter out of 47" into a quietly wrong claim that there were 10.
    """
    members = payload.members
    if not members:
        raise ValueError("the conformer search returned no structures")
    lowest = min(member.energy_hartree for member in members)
    relative = [(member.energy_hartree - lowest) * HARTREE_TO_KCAL for member in members]
    degeneracies = [member.degeneracy for member in members]
    populations = boltzmann_populations(relative, degeneracies, temperature_k)
    entropy = ensemble_entropy(populations, degeneracies)
    # **Sorted here, so "lowest-first" is a property of this function rather than of the server.**
    # `ConformerEnsemble.lowest_structure_id` and `compose.refined_ensemble`'s "top N by electronic
    # energy" both index `conformers[0:]` and both documented the order as given. Nothing in this
    # repository established it: `EnsemblePayload` has no ordering validator, and the order held
    # only because `Chemclaw3-mcp`'s `crest_cli` sorts on the way out. That is a real control in
    # another repository, which is exactly the kind this tree declines to depend on silently — a
    # backend that returned members unsorted would spend the Hessians on arbitrary conformers and
    # report a lowest-energy geometry that was not one, with every number still internally
    # consistent. One sort costs nothing and makes the claim local.
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
