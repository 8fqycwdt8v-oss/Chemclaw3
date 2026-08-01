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

The error does **not** cancel within a balanced reaction unless both sides carry the same
symmetry, and for the chemistry that matters they do not: every hydrogenation has H2
(sigma 2) on one side only, and anything aromatic carries benzene's sigma 12. An earlier
version of this paragraph claimed the cancellation as a defence for defaulting sigma, and
`calc.reaction` relied on it — computing every species at 1 with no way for a caller to say
otherwise, which cost 0.41 kcal/mol on a hydrogenation and 1.47 on a benzene. That path now
takes sigma per species and withholds delta-G when it is unstated.
"""

import asyncio
import math
from typing import Literal

import numpy as np
from pydantic import BaseModel, Field
from rdkit import Chem
from scipy.linalg import null_space

from chemclaw.core.config import settings
from chemclaw.science.calc.artifacts import ArtifactStore
from chemclaw.science.calc.postgres_artifacts import default_artifact_store
from chemclaw.science.calc.store import ResultPayload, ResultStore, cached_compute
from chemclaw.science.calc.structure import Structure
from chemclaw.science.calc.xtb_hessian import (
    Hessian,
    HessianSpec,
    compute_hessian,
    run_cached_hessian,
)
from chemclaw.science.calc.xtb_opt import OptimizationResult, OptSpec, run_cached_optimization
from chemclaw.science.calc.xtb_spec import XtbSpec

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
    """Settings of one thermochemistry calculation, Hessian settings included.

    Like `OptSpec`, every field enters the cache key automatically. `symmetry_number`
    is here rather than applied afterwards for that reason: it changes the entropy, so
    a result computed at sigma=1 must not be served for a request at sigma=2.

    **The state variables are keyed here and nowhere else.** The Hessian underneath is a
    separate cached calculation (`chemclaw.science.calc.xtb_hessian`) keyed only on what can move
    the matrix, so
    this spec's `temperature_k` correctly forces a recomputation of the *thermochemistry* while
    leaving the expensive second derivatives a cache hit (STO-2). `hessian_spec` is the projection
    from one to the other, and is the only place the two specs are related.
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

    def hessian_spec(self) -> HessianSpec:
        """The second-derivative calculation this thermochemistry is computed over.

        Carries only the fields a Hessian actually depends on. Two `ThermoSpec`s differing solely
        in temperature, pressure, symmetry number or RRHO cutoff project onto the *same*
        `HessianSpec`, which is precisely what makes the second question cheap.
        """
        return HessianSpec(
            method=self.method,
            engine=self.engine,
            solvent=self.solvent,
            displacement_angstrom=self.displacement_angstrom,
        )


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

    Absolute values are in Hartree (what `chemclaw.science.calc.reaction` differences); the
    corrections
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
        partition = factor * moments[2] / symmetry
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

    The self-contained form: it computes its own Hessian and consults no cache, which is what a
    test or a one-shot script wants. `run_cached_thermochemistry` is the path that reuses a stored
    Hessian, and is what every agent-facing caller should go through.

    Raises `ValueError` above `settings.xtb_hessian_max_atoms` (raised by `compute_hessian`, where
    the cost is actually paid).
    """
    hessian, _ = compute_hessian(spec.hessian_spec(), structure)
    return thermochemistry_from_hessian(spec, structure, hessian)


def thermochemistry_from_hessian(
    spec: ThermoSpec, structure: Structure, hessian: Hessian
) -> ThermochemistryResult:
    """RRHO thermochemistry over an already-computed Hessian — the arithmetic, and only that.

    Separated from the second derivatives so the two can be cached independently (STO-2): this
    part is milliseconds and genuinely depends on the temperature, while the Hessian is minutes and
    does not. Every caller of both goes through here, so the quasi-RRHO treatment and the symmetry
    handling have exactly one implementation regardless of which backend produced the matrix.

    `structure` should be a converged minimum (`chemclaw.science.calc.xtb_opt`); if it is not, the
    result says so
    through `is_minimum` rather than refusing, because "this geometry is a saddle point" is a
    useful answer and often the question.
    """
    masses = _atomic_masses(structure.elements)
    _, positions = structure.arrays()
    wavenumbers, vectors = _normal_modes(hessian.matrix, masses, positions)
    electronic = hessian.electronic_energy_hartree
    if hessian.ir_intensities is not None:
        intensities = _align_intensities(hessian.ir_intensities, wavenumbers.size, structure)
    elif hessian.dipole_derivatives is not None:
        intensities = _ir_intensities(hessian.dipole_derivatives, vectors, masses)
    else:
        # Unreachable through `compute_hessian`, which always populates one of the two. Stated
        # rather than assumed, because a Hessian with neither would silently produce a spectrum of
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
    store: ResultStore,
    structure: Structure,
    spec: ThermoSpec | None = None,
    artifacts: ArtifactStore | None = None,
) -> tuple[ThermochemistryResult, bool]:
    """Return the thermochemistry of `structure`, reusing the store on a repeat.

    **Two caches, one call.** The thermochemistry is cached under the full spec, because the answer
    genuinely depends on the temperature; the Hessian underneath is cached under
    `spec.hessian_spec()`, which carries none of the state variables. So the same geometry at a
    second temperature misses here (correctly — it is a different answer) and hits there (the
    point of STO-2), turning what used to be minutes of second derivatives into milliseconds of
    partition functions.

    `artifacts` defaults to the production artifact store; pass an explicit one (or an
    `InMemoryArtifactStore`) to redirect it. Note that the Hessian is only reusable if the artifact
    store keeps it: with artifacts disabled this behaves exactly as it did before the split.

    Returns `(result, was_cached)`, where `was_cached` refers to the thermochemistry — a run that
    reused a stored Hessian but recomputed the RRHO arithmetic reports `False`, because the value
    handed back was in fact computed now.
    """
    spec = spec or ThermoSpec()
    artifacts = artifacts if artifacts is not None else default_artifact_store()
    # Off the event loop: deriving the key calls `calc_version()`, whose first call in a
    # process shells out to `xtb --version` / `crest --version` (`calc.xtb_cli`), and the
    # hash walks every atom. Both are synchronous, and this runs inside the connector's
    # one-loop MCP server and inside Temporal activities that are coroutines.
    key = await asyncio.to_thread(spec.cache_key, structure)

    async def _compute() -> ResultPayload:
        hessian, _ = await run_cached_hessian(store, artifacts, structure, spec.hessian_spec())
        result = await asyncio.to_thread(thermochemistry_from_hessian, spec, structure, hessian)
        return result.model_dump()

    payload, was_cached = await cached_compute(store, key, _compute)
    return ThermochemistryResult.model_validate(payload), was_cached


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
