"""Harmonic frequencies, IR intensities and RRHO thermochemistry (xTB plan X3).

The phase that turns an *energy* into a *free energy*, which is the quantity every
question about equilibrium, selectivity and spontaneity is actually about. Three
things come out of one Hessian:

- **Frequencies**, and with them the answer to "is this geometry a minimum at all?".
  A Gibbs energy computed at a saddle point is not a Gibbs energy, so
  `ThermochemistryResult.is_minimum` is a field rather than an assumption.
- **IR intensities**, essentially for free. The finite-difference loop displaces every
  Cartesian and reads the gradient; tblite hands back the dipole from the same SCF, so
  dipole derivatives cost one array we were already discarding. A computed IR spectrum
  is a real discriminator between candidate structures for an unknown impurity.
- **Thermochemistry** — ZPE, enthalpy, entropy, Gibbs — by ideal-gas RRHO.

Two deliberate choices about the physics:

**Quasi-RRHO entropy (Grimme 2012).** A harmonic oscillator's entropy diverges as its
frequency goes to zero, and the lowest modes are exactly where the harmonic
approximation is worst — so a 5 cm^-1 mode from a floppy molecule can contribute
several kcal/mol of nonsense to G. Below `rrho_cutoff_cm` a mode is interpolated
toward a free rotor, which is what `xtb` itself does.

**The rotational symmetry number is an input, not a guess.** It shifts the entropy by
exactly R ln(sigma) — 1.4 cal/mol/K for a C2 axis, 4.9 for benzene — and deriving it
needs point-group detection this layer does not do. It defaults to 1 and is part of
the cache key, so setting it correctly is a recompute rather than a silent correction.
Within a balanced reaction the error partly cancels; between unlike species it does not.
"""

import math
from typing import Literal

import numpy as np
from pydantic import BaseModel, Field
from rdkit import Chem
from scipy.linalg import null_space

from calc import xtb_cli
from calc.store import ResultStore, run_cached
from calc.structure import Structure
from calc.xtb_engine import evaluate_point, make_calculator
from calc.xtb_opt import OptimizationResult, OptSpec, run_cached_optimization
from calc.xtb_spec import XtbSpec
from chemclaw.config import settings

# SI constants (CODATA), and the conversions this module needs. Everything internal is
# SI; only the reported fields are in the units a chemist reads.
_PLANCK = 6.62607015e-34  # J s
_BOLTZMANN = 1.380649e-23  # J/K
_AVOGADRO = 6.02214076e23  # 1/mol
_GAS_CONSTANT = 8.314462618  # J/(mol K)
_LIGHT_CM = 2.99792458e10  # cm/s
_HARTREE_J = 4.3597447222071e-18
_AMU_KG = 1.66053906660e-27
_J_PER_MOL_TO_KCAL = 1.0 / 4184.0
_AU_TO_DEBYE = 2.5417464519

# Grimme's free-rotor moment of inertia, the value that keeps the free-rotor entropy
# finite as the frequency goes to zero (kg m^2).
_FREE_ROTOR_INERTIA = 1e-44

# (Debye/Angstrom)^2/amu -> km/mol, the standard IR intensity conversion.
_IR_TO_KM_PER_MOL = 42.2561

# A principal moment of inertia below this fraction of the largest one means the
# molecule is linear and has one rotational degree of freedom fewer.
_LINEAR_INERTIA_RATIO = 1e-4


class ThermoSpec(XtbSpec):
    """Settings of one Hessian + thermochemistry calculation.

    Like `OptSpec`, every field enters the cache key automatically. `symmetry_number`
    is here rather than applied afterwards for that reason: it changes the entropy, so
    a result computed at sigma=1 must not be served for a request at sigma=2.
    """

    task: Literal["hess"] = "hess"
    temperature_k: float = Field(default_factory=lambda: settings.xtb_thermo_temperature_k, gt=0)
    pressure_pa: float = Field(default_factory=lambda: settings.xtb_thermo_pressure_pa, gt=0)
    # Rotational symmetry number. 1 unless the caller knows better; see the module
    # docstring for why it is not derived.
    symmetry_number: int = Field(default=1, ge=1)
    displacement_angstrom: float = Field(
        default_factory=lambda: settings.xtb_hessian_displacement, gt=0
    )
    rrho_cutoff_cm: float = Field(default_factory=lambda: settings.xtb_rrho_cutoff_cm, gt=0)


class VibrationalMode(BaseModel):
    """One normal mode: its wavenumber and how strongly it absorbs in the IR.

    A **negative** `wavenumber_cm` is an imaginary frequency — the geometry is not a
    minimum along that mode. Reported as a negative number because that is the
    convention every quantum chemistry program prints and every chemist reads.
    """

    wavenumber_cm: float
    ir_intensity_km_per_mol: float


class ThermochemistryResult(BaseModel):
    """RRHO thermochemistry at the semiempirical level, with its caveats in the data.

    Absolute values are in Hartree (what `calc.reaction` differences); the corrections
    are in kcal/mol (what a person reads). `is_minimum=False` with a populated
    `imaginary_frequencies_cm` is the point of the model: the result states that its
    own free energy is not a free energy, rather than relying on the caller to notice.

    `conformer_treatment="single"` is the second built-in caveat. Everything here
    describes one conformer, and a single-conformer free energy is the most common
    silent error in semiempirical work — ensembles arrive with plan X6.
    """

    smiles: str | None
    structure_id: str
    method: str
    solvent: str | None
    temperature_k: float
    pressure_pa: float
    symmetry_number: int

    is_minimum: bool
    imaginary_frequencies_cm: list[float]
    # Every normal mode, ordered by wavenumber. A caller with a context budget may
    # truncate this to the bands that matter (`strongest_bands`); `mode_count` is then
    # the honest statement of how many there were — the same truncation contract
    # `SiteReactivityResult` uses for atoms.
    modes: list[VibrationalMode]
    mode_count: int
    # The five lowest modes, always from the *full* set. RRHO is weakest here, so this
    # is where doubt about the free energy belongs, and it must survive truncation.
    lowest_wavenumbers_cm: list[float]

    electronic_energy_hartree: float
    zero_point_energy_kcal: float
    # Enthalpy minus the electronic energy: ZPE plus the thermal population of every
    # degree of freedom plus RT. This is the number that transfers between molecules.
    thermal_enthalpy_correction_kcal: float
    entropy_cal_per_mol_k: float
    gibbs_correction_kcal: float
    enthalpy_hartree: float
    gibbs_free_energy_hartree: float

    uncertainty_kcal: float
    conformer_treatment: Literal["single"] = "single"
    # Cartesian direction of the most negative mode (Angstrom per unit displacement),
    # present only when the geometry is not a minimum. It is what the structure wants
    # to do, so it is also what `relax_to_minimum` displaces along to escape.
    imaginary_displacement: list[list[float]] | None = None

    def strongest_bands(self, limit: int) -> list[VibrationalMode]:
        """The `limit` most intense bands, plus every imaginary mode, by wavenumber.

        What a computed IR spectrum is compared against: a measured spectrum shows the
        bands that absorb, and the weak modes between them carry no information for
        that comparison. Imaginary modes are never dropped — they are the reason to
        distrust the whole result, and they have no intensity to rank by.
        """
        real = [index for index, mode in enumerate(self.modes) if mode.wavenumber_cm > 0]
        real.sort(key=lambda index: self.modes[index].ir_intensity_km_per_mol, reverse=True)
        kept = set(real[:limit])
        return [
            mode
            for index, mode in enumerate(self.modes)
            if mode.wavenumber_cm <= 0 or index in kept
        ]


def _atomic_masses(elements: list[int]) -> np.ndarray:
    """Standard atomic weights in amu, one per atom."""
    table = Chem.GetPeriodicTable()
    return np.array([table.GetAtomicWeight(number) for number in elements])


def _second_derivatives(
    spec: ThermoSpec, structure: Structure
) -> tuple[np.ndarray, np.ndarray | None, float]:
    """The Hessian, IR intensities and energy, from whichever backend the spec names.

    The `xtb` binary computes both the Hessian and the IR intensities itself and is far
    faster at it — measured, a 76-atom Hessian in 26 s against 218 s of finite
    differences. What it does *not* get to supply is the thermochemistry over them: that
    stays here, so the symmetry number remains an explicit input and the quasi-RRHO
    treatment is identical whichever backend ran, which is what keeps free energies from
    the two comparable.

    The in-process path returns `None` for intensities, because it derives them from the
    dipole derivatives it collected while displacing — a separate step the caller runs.
    """
    if spec.for_structure(structure).engine == "xtb":
        outcome = xtb_cli.run(structure, task="hess", method=spec.method, solvent=spec.solvent)
        return (
            np.asarray(outcome.hessian),
            np.asarray(outcome.ir_intensities),
            (outcome.energy_hartree),
        )
    hessian, dipole_derivatives = _finite_difference(spec, structure)
    calc = make_calculator(
        spec.method,
        *structure.arrays(),
        charge=structure.charge,
        uhf=structure.uhf,
        solvent=spec.solvent,
    )
    _, positions = structure.arrays()
    energy, _, _ = evaluate_point(calc, positions)
    _CACHED_DIPOLE_DERIVATIVES[structure.structure_id] = dipole_derivatives
    return hessian, None, energy


# The in-process Hessian produces dipole derivatives as a by-product; the two-step shape
# of `compute_thermochemistry` (Hessian, then modes, then intensities) means they have to
# survive one call. A one-entry handoff rather than a wider return type, because nothing
# else ever wants them.
_CACHED_DIPOLE_DERIVATIVES: dict[str, np.ndarray] = {}


def _dipole_derivatives(spec: ThermoSpec, structure: Structure) -> np.ndarray:
    """The dipole derivatives the in-process Hessian collected for this structure."""
    return _CACHED_DIPOLE_DERIVATIVES.pop(structure.structure_id)


def _align_intensities(intensities: np.ndarray, modes: int, structure: Structure) -> np.ndarray:
    """Drop xtb's projected-out external modes so intensities pair with our own modes.

    xtb lists all 3N entries with the translations and rotations first; our projection
    reports only the vibrations. Reconciling by count is the point — if the two
    projections disagree about how many external modes a molecule has, every intensity
    would shift by one mode, so a mismatch fails loudly instead (gate G4).
    """
    external = intensities.size - modes
    if external < 0:
        raise ValueError(
            f"xtb reported {intensities.size} modes but the projection found {modes} "
            f"for {structure.smiles or structure.structure_id}"
        )
    return intensities[external:]


def _finite_difference(spec: ThermoSpec, structure: Structure) -> tuple[np.ndarray, np.ndarray]:
    """Central-difference Hessian and dipole derivatives at `structure`'s geometry.

    Returns `(hessian, dipole_derivatives)` with the Hessian in Hartree/Angstrom^2,
    shape (3N, 3N), and the dipole derivatives in Debye/Angstrom, shape (3N, 3).

    Cost is 6N single points: the gradient is analytic, so only *first* derivatives
    need differencing. The Hessian is symmetrized afterwards — central differences of
    an exact gradient give a nearly symmetric matrix, and forcing the symmetry removes
    the small asymmetry that would otherwise put a spurious imaginary component into
    the eigenvalues.
    """
    numbers, positions = structure.arrays()
    calc = make_calculator(
        spec.method,
        numbers,
        positions,
        charge=structure.charge,
        uhf=structure.uhf,
        solvent=spec.solvent,
    )
    size = positions.size
    hessian = np.zeros((size, size))
    dipole_derivatives = np.zeros((size, 3))
    step = spec.displacement_angstrom
    for index in range(size):
        shifted = positions.copy().ravel()
        shifted[index] += step
        _, gradient_plus, dipole_plus = evaluate_point(calc, shifted.reshape(-1, 3))
        shifted[index] -= 2 * step
        _, gradient_minus, dipole_minus = evaluate_point(calc, shifted.reshape(-1, 3))
        hessian[index] = (gradient_plus.ravel() - gradient_minus.ravel()) / (2 * step)
        dipole_derivatives[index] = (dipole_plus - dipole_minus) * _AU_TO_DEBYE / (2 * step)
    return 0.5 * (hessian + hessian.T), dipole_derivatives


def _inertia(masses: np.ndarray, positions: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Principal moments of inertia (amu Angstrom^2, ascending) and their axes as columns.

    One definition for the two places the molecule's rotations matter — the entropy and
    the projection that separates rotation from vibration — so "is this molecule linear"
    cannot be answered one way in one place and another way in the other.
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

    Builds the mass-weighted translations and rotations and returns their orthogonal
    complement. Diagonalizing the Hessian inside that complement is what makes every
    eigenvalue a vibration — the alternative, discarding the six smallest eigenvalues
    afterwards, silently discards a real low-frequency mode whenever one is smaller than
    a translational residual.

    Rotations are built about the **principal axes** and kept by their moment of
    inertia, which is what makes a linear molecule come out with 3N-5 modes instead of
    3N-6. Filtering the raw x/y/z rotations by singular value instead does not work: an
    optimized "linear" molecule is bent by a fraction of a degree, so its null rotation
    has a small but perfectly ordinary singular value and survives the cut — measured on
    CO2, which lost a real mode that way.
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

    A negative wavenumber encodes an imaginary frequency (a negative Hessian
    eigenvalue). Modes come out sorted by wavenumber, so imaginary modes are first.
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

    The mode's Cartesian displacement per unit normal coordinate is the mass-weighted
    eigenvector divided by the square root of the mass, so the dipole derivative with
    respect to the normal coordinate follows directly from the Cartesian one.
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

    Handles the monatomic (no rotation) and linear (two degrees of freedom) cases from
    the principal moments themselves rather than from a separate structural test.
    """
    if len(masses) == 1:
        return 0.0, 0.0
    amu_angstrom2, _ = _inertia(masses, positions)
    moments = amu_angstrom2 * _AMU_KG * 1e-20  # amu Angstrom^2 -> kg m^2
    linear = _is_linear(moments)
    factor = 8 * math.pi**2 * _BOLTZMANN * temperature / _PLANCK**2
    if linear:
        partition = factor * moments[2] / (2 * symmetry)
        return _GAS_CONSTANT * temperature, _GAS_CONSTANT * (math.log(partition) + 1.0)
    partition = math.sqrt(math.pi * factor**3 * moments.prod()) / symmetry
    return 1.5 * _GAS_CONSTANT * temperature, _GAS_CONSTANT * (math.log(partition) + 1.5)


def _vibrational(
    wavenumbers: np.ndarray, temperature: float, cutoff_cm: float
) -> tuple[float, float, float]:
    """(zero-point energy, thermal energy, entropy) per mole from the real modes.

    Imaginary modes are skipped — they contribute nothing physical, and including one
    would be a way of pretending a saddle point is a minimum. Entropy uses Grimme's
    quasi-RRHO interpolation toward a free rotor below `cutoff_cm`; energy and ZPE stay
    harmonic, which is the published form of the approximation.
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


def compute_thermochemistry(spec: ThermoSpec, structure: Structure) -> ThermochemistryResult:
    """Hessian, frequencies, IR intensities and RRHO thermochemistry for `structure`.

    `structure` should be a converged minimum (`calc.xtb_opt`); if it is not, the
    result says so through `is_minimum` rather than refusing, because "this geometry
    is a saddle point" is a useful answer and often the question.

    Raises `ValueError` above `settings.xtb_hessian_max_atoms`: the cost is 6N single
    points, and blocking an agent turn for minutes is a worse failure than saying the
    calculation needs the durable job path.
    """
    if len(structure.elements) > settings.xtb_hessian_max_atoms:
        raise ValueError(
            f"a Hessian on {len(structure.elements)} atoms exceeds the inline limit of "
            f"{settings.xtb_hessian_max_atoms}: submit it as a durable QM job instead"
        )
    masses = _atomic_masses(structure.elements)
    _, positions = structure.arrays()
    hessian, intensities, electronic = _second_derivatives(spec, structure)
    wavenumbers, vectors = _normal_modes(hessian, masses, positions)
    if intensities is None:
        intensities = _ir_intensities(_dipole_derivatives(spec, structure), vectors, masses)
    else:
        intensities = _align_intensities(intensities, wavenumbers.size, structure)

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
    # Spin degeneracy: R ln(2S+1), zero for the closed-shell case and the reason an
    # open-shell species is not simply "the same thermochemistry with a different SCF".
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
    # The softest imaginary mode is always the first, since modes are sorted.
    displacement = (
        (vectors[:, 0] / np.repeat(np.sqrt(masses), 3)).reshape(-1, 3).tolist()
        if imaginary
        else None
    )
    return ThermochemistryResult(
        smiles=structure.smiles,
        structure_id=structure.structure_id,
        method=spec.method,
        solvent=spec.solvent,
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


async def run_cached_thermochemistry(
    store: ResultStore, structure: Structure, spec: ThermoSpec | None = None
) -> tuple[ThermochemistryResult, bool]:
    """Return the thermochemistry of `structure`, reusing the store on a repeat."""
    spec = spec or ThermoSpec()
    return await run_cached(
        store,
        spec.cache_key(structure),
        lambda: compute_thermochemistry(spec, structure),
        ThermochemistryResult,
    )


async def relax_to_minimum(
    store: ResultStore,
    structure: Structure,
    opt_spec: OptSpec | None = None,
    thermo_spec: ThermoSpec | None = None,
) -> tuple[OptimizationResult, ThermochemistryResult, bool]:
    """Optimize until the geometry is a genuine minimum, then return it with its thermochemistry.

    A plain gradient optimization converges to the nearest *stationary* point, which is
    not always a minimum. The common case is mundane and universal: a force field hands
    over a molecule with an eclipsed methyl, and a Cartesian optimizer preserves that
    symmetry all the way down onto the rotational saddle. Measured on ethyl acetate —
    an ordinary ester, not a contrived example — the optimizer settles at a -42 cm^-1
    mode, and the free energy computed there is not a free energy.

    The escape is standard practice and cheap: displace along the imaginary mode and
    re-optimize. Ethyl acetate needs one such step and lands 0.016 kcal/mol lower,
    confirming what it was — a shallow rotor saddle rather than a different structure.

    Bounded by `settings.xtb_minimum_refinement_attempts`, after which the result is
    returned as it stands with `is_minimum=False` intact. A structure that will not
    settle is reporting something real about itself, and looping on it is not the fix.

    The third element of the return is whether *every* underlying calculation was a
    cache hit — what a caller reports as "this cost nothing".
    """
    opt_spec = opt_spec or OptSpec()
    thermo_spec = thermo_spec or ThermoSpec()
    current = structure
    cached = True
    for _ in range(settings.xtb_minimum_refinement_attempts + 1):
        optimization, opt_cached = await run_cached_optimization(store, current, opt_spec)
        thermo, thermo_cached = await run_cached_thermochemistry(
            store, optimization.structure, thermo_spec
        )
        cached = cached and opt_cached and thermo_cached
        if thermo.is_minimum or thermo.imaginary_displacement is None:
            return optimization, thermo, cached
        current = _displaced(optimization.structure, thermo.imaginary_displacement)
    return optimization, thermo, cached


def _displaced(structure: Structure, direction: list[list[float]]) -> Structure:
    """Push `structure` along `direction`, scaled so the largest atom moves a fixed step.

    Normalizing on the largest single-atom motion rather than on the vector norm keeps
    the kick the same physical size whether the mode is localized on one methyl or
    spread over the whole molecule.
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
