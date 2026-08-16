"""The shapes a calculation is stored, reconstructed and carried in — and nothing else.

The physics left this repository in `D-2026-08-16-the-physics-leaves-the-cache-stays`: the engines
live in `Chemclaw3-mcp`'s `servers/calc`, exposed as individually-keyed primitives. What stayed is
the D-011 cache, the calibration ledger and the orchestration — and a cache cannot keep what it
cannot reconstruct. **These models are the reconstruction**, and they are also the Temporal wire
types the five durable jobs return, whose field-for-field shape is pinned by workflow histories
already in flight.

**Why one module rather than twenty stripped ones.** The files these came from were named for the
*programs that ran them* — `xtb_engine`, `xtb_cli`, `crest_cli`, `anc`, `xtb_opt` — and none of
those programs runs here any more. Keeping twenty files whose names describe a physics stack this
process does not have would leave the tree's own map pointing at a system that no longer exists,
which is the exact failure `tests/test_docstring_paths.py` was built for. What is left is one
responsibility, stated once: *the shape of a calculation's input, its answer, and the geometry
both are about.* So it is one module, and its sections follow the ladder a calculation climbs —
structure, single point, optimization, second derivatives, and the composites built over them.

**Nothing here derives a `calc_version`, and nothing here computes.** A model whose construction
needed tblite, crest or an embedding is not a model, it is an engine, and the whole point of the
split is that this process has neither. The one exception that is not one: `Structure.structure_id`
is a hash of coordinates the caller already holds, which is why an identity derived here and one
derived on the server agree byte for byte (measured, ADR table).

**Server payloads carry more than these models declare**, and that is deliberate. Every result the
server returns is stamped with its own `calc_version` and `calc_key`; pydantic ignores both on
validation, so the agent-facing surface is unchanged by the split while the stored row keeps the
provenance. The one place the extra field is *read* is `_log_prediction`, which needs the version
the calculation actually ran under and now takes it off the payload instead of deriving it.
"""

from typing import Literal

import numpy as np
from pydantic import BaseModel, Field, model_validator
from rdkit import Chem

from chemclaw.core.ids import stable_hash
from chemclaw.science.calc.uncertainty import Estimate

# Which attack a Fukui ranking is for. f-minus (electron loss) ranks the sites an electron-deficient
# *electrophile* attacks; f-plus (gain) the sites a *nucleophile* attacks; f-zero their average, for
# radicals.
FukuiMode = Literal["electrophilic", "nucleophilic", "radical"]
# Which index each mode ranks by — the whole of what `mode` decides, which is why it is not part of
# a Fukui calculation's cache key on either side of the wire.
_MODE_FIELD: dict[FukuiMode, str] = {
    "electrophilic": "f_minus",
    "nucleophilic": "f_plus",
    "radical": "f_zero",
}

# The CREST searches this system exposes, and how hard to search. CREST's own names, so the two
# repositories' vocabularies stay legible against each other and against its documentation.
EnsembleSearch = Literal["conformers", "tautomers", "protomers", "deprotomers"]
CrestEffort = Literal["quick", "normal", "extensive"]

# How far the ladder is climbed per species of a reaction. `quick` optimizes and differences
# electronic energies; `standard` adds a Hessian and gives enthalpies and free energies; `thorough`
# first searches conformational space, works from the lowest member, and adds the conformational
# entropy that a single-conformer free energy is missing.
ReactionLevel = Literal["quick", "standard", "thorough"]


class Structure(BaseModel):
    """One 3D molecular structure, addressed by the hash of its chemical content.

    `elements` and `positions` are parallel: atom `i` has atomic number `elements[i]` at
    `positions[i]` (Angstrom). Positions are normalized on construction (rounded to
    `settings.xtb_geometry_decimals`) so that float noise from a re-run cannot fork the cache while
    the stored coordinates still *are* the ones that were hashed.

    **This model is a cross-repository contract now.** It is what `relax_structure`,
    `compute_hessian`, `scan_point` and the two CREST searches take and return over the wire, and
    `structure_id` is half of every key those calculations are cached under. The rounding and the
    hash payload below therefore have to agree with the server's to the byte — they do, measured on
    `CCO` (`st_739a222f45be0c3a` on both sides, ADR table), and a divergence would not raise
    anywhere: every lookup would simply miss, forever.
    """

    elements: list[int] = Field(min_length=1)
    positions: list[list[float]] = Field(min_length=1)
    charge: int = 0
    # Spin multiplicity 2S+1: 1 = closed-shell singlet, 2 = doublet, 3 = triplet.
    multiplicity: int = Field(default=1, ge=1)
    # The canonical SMILES this structure represents, when it came from (or maps to) one. Carried
    # for reporting and for the atom-index mapping in `symbols`.
    smiles: str | None = None
    # `CalculationKey.as_str()` of the calculation that produced this geometry, for structures that
    # are a calculation's *output* rather than an embedding.
    origin: str | None = None

    @model_validator(mode="after")
    def _normalize_and_validate(self) -> "Structure":
        """Round coordinates, then reject a structure that is not physically consistent.

        Three ways a structure can be wrong are caught here rather than by a converged SCF that
        means nothing (gate G4): mismatched array lengths, a coordinate row that is not 3D, and an
        electron count that cannot produce the declared multiplicity. Kept on this side of the wire
        as well as the server's, because a `Structure` is built here — by the thermochemistry
        refinement loop's displacement — and not only received.
        """
        # Imported here rather than at module scope: `core.config` reads the environment, and this
        # module is imported by `connectors/calc/results.py` on a path that must stay leaf-light.
        from chemclaw.core.config import settings

        if len(self.positions) != len(self.elements):
            raise ValueError(f"{len(self.positions)} positions for {len(self.elements)} elements")
        if any(len(row) != 3 for row in self.positions):
            raise ValueError("every position must have exactly three coordinates")
        decimals = settings.xtb_geometry_decimals
        # `+ 0.0` normalizes the negative zero that rounding can produce, so two geometrically
        # identical structures cannot differ in their hash by a sign bit.
        self.positions = [[round(value, decimals) + 0.0 for value in row] for row in self.positions]
        unpaired = self.multiplicity - 1
        electrons = sum(self.elements) - self.charge
        if electrons < unpaired or (electrons - unpaired) % 2:
            # The default (closed-shell) case gets the specific message, because it is the one a
            # caller hits by accident — from a radical SMILES or a wrong charge — and the fix is to
            # declare the multiplicity, not to fix the atoms.
            if self.multiplicity == 1:
                raise ValueError(
                    f"open-shell species ({electrons} electrons at charge {self.charge}) "
                    "cannot be a closed-shell singlet: declare its multiplicity explicitly"
                )
            raise ValueError(
                f"{electrons} electrons at charge {self.charge} cannot form multiplicity "
                f"{self.multiplicity} ({unpaired} unpaired)"
            )
        return self

    @property
    def structure_id(self) -> str:
        """Content address: `st_` + a stable hash of the chemistry, not the provenance.

        Deliberately excludes `smiles` and `origin`: two identical geometries are the same structure
        whether one was embedded from a SMILES and the other optimized, and that is exactly the
        identity that lets a downstream task hit the cache regardless of which route produced its
        input.
        """
        payload = {
            "elements": self.elements,
            "positions": self.positions,
            "charge": self.charge,
            "multiplicity": self.multiplicity,
        }
        return f"st_{stable_hash(payload)}"

    @property
    def symbols(self) -> list[str]:
        """Element symbols, one per atom, for human-readable per-atom results."""
        table = Chem.GetPeriodicTable()
        return [table.GetElementSymbol(number) for number in self.elements]

    def arrays(self) -> tuple[np.ndarray, np.ndarray]:
        """Return (atomic numbers, positions in Angstrom) — what the RRHO arithmetic reads."""
        return np.array(self.elements), np.array(self.positions)


class XtbInput(BaseModel):
    """A single-point xTB request: a molecule and its charge.

    `charge` is redundant with the SMILES — the server validates it against the formal charge the
    structure already carries, so it cannot disagree. It is kept anyway, deliberately: the LLM tool
    signature stays loud, and a model that passes a charge contradicting the structure gets an
    error instead of having its argument silently ignored.
    """

    smiles: str = Field(min_length=1)
    charge: int = 0


class XtbResult(BaseModel):
    """The parsed result of a GFN2-xTB single point."""

    smiles: str
    method: str
    charge: int
    total_energy_hartree: float


class PkaInput(BaseModel):
    """A pKa request: the neutral acid as SMILES."""

    smiles: str = Field(min_length=1)


class PkaResult(BaseModel):
    """A predicted pKa with its uncertainty, and which calibration produced it.

    `deprotonation_energy_kcal` is always the solvated GFN2-xTB energy of the **deprotonated**
    species minus the protonated one — for an acid that is anion minus neutral, for a base neutral
    minus cation. `site` says which, because the number a chemist needs is different: an acid's own
    pKa, or a base's *conjugate acid* pKa.

    `smiles` is the **canonical** form the computation actually ran on, not the caller's spelling —
    which is also what the key's `input_hash` was derived from, and what `science/calc/logd.py`
    re-parses for its Crippen term.
    """

    smiles: str
    method: str
    pka: float
    deprotonation_energy_kcal: float
    uncertainty: float
    # "acid" = an O-H/S-H proton came off; "base" = the pKa of the protonated form (pKaH), which is
    # what is tabulated for amines and what an extraction pH is set against. Each has its own
    # calibration, fitted separately.
    site: Literal["acid", "base"] = "acid"


class SolubilityInput(BaseModel):
    """A solubility request: just the molecule."""

    smiles: str = Field(min_length=1)


class SolubilityResult(BaseModel):
    """Predicted aqueous solubility as log S (mol/L), with an uncertainty.

    `uncertainty_log` is one standard deviation in log-S units — report it so a consumer never
    treats the point estimate as exact.

    `estimate` carries the same number in the uniform F8-T1 shape, adding the two things this model
    could not previously say: **where the uncertainty came from** and **whether this molecule is
    something ESOL can speak about at all**. Kept beside the domain fields rather than replacing
    them, so a chemist still reads `model` and a skill reads one shape across every calculator.
    """

    smiles: str
    model: str
    log_s_mol_per_l: float
    uncertainty_log: float
    estimate: Estimate | None = None


class DescriptorInput(BaseModel):
    """A descriptor-panel request: just the molecule."""

    smiles: str


class DescriptorProfile(BaseModel):
    """The developability descriptor panel for one molecule, plus rule-of-thumb flags.

    `lipinski_violations` counts the four Rule-of-Five criteria (MW>500, LogP>5, HBD>5, HBA>10) the
    molecule breaks; `veber_pass` is Veber's oral-bioavailability heuristic (rotatable bonds <=10
    and TPSA<=140 A^2). Both are widely used triage heuristics, not developability verdicts —
    report them as flags a chemist weighs, never as a pass/fail gate on their own.
    """

    smiles: str
    molecular_weight: float
    clogp: float
    tpsa: float
    h_bond_donors: int
    h_bond_acceptors: int
    rotatable_bonds: int
    aromatic_rings: int
    fraction_csp3: float
    qed: float
    lipinski_violations: int
    veber_pass: bool


class LogdInput(BaseModel):
    """A logD request: the molecule and the pH (defaults to `settings.logd_default_ph`)."""

    smiles: str = Field(min_length=1)
    ph: float | None = None


class LogdResult(BaseModel):
    """Predicted logD at a given pH, alongside the logP/pKa it was derived from.

    `uncertainty` propagates only the pKa calibration's residual (the dominant error term); Crippen
    LogP itself carries no reported uncertainty in RDKit.
    """

    smiles: str
    ph: float
    clogp: float
    pka: float
    log_d: float
    uncertainty: float


class AtomCharge(BaseModel):
    """One atom's Mulliken partial charge, with the index a chemist can locate."""

    index: int
    element: str
    charge: float


class BondOrder(BaseModel):
    """A Wiberg bond order between two atoms, above the reporting threshold."""

    atom_i: int
    atom_j: int
    order: float


class ElectronicProperties(BaseModel):
    """The electronic structure of one geometry, as read from a single GFN2-xTB SCF.

    `homo_ev`/`lumo_ev`/`gap_ev` are frontier orbital energies, not ionization potentials —
    semiempirical orbital energies are useful for *comparing* related molecules and poor as
    absolute quantities. `lumo_ev` and `gap_ev` are None for the rare system with no virtual
    orbital.
    """

    smiles: str | None
    structure_id: str
    method: str
    solvent: str | None
    total_energy_hartree: float
    homo_ev: float
    lumo_ev: float | None
    gap_ev: float | None
    dipole_debye: float
    atom_charges: list[AtomCharge]
    bond_orders: list[BondOrder]


class FukuiSite(BaseModel):
    """Condensed Fukui indices for one atom.

    By construction `f_zero` is the mean of `f_minus` and `f_plus`. A larger value means the site
    is more susceptible to the corresponding attack.
    """

    index: int
    element: str
    f_minus: float = Field(description="electrophilic attack (site donates electrons)")
    f_plus: float = Field(description="nucleophilic attack (site accepts electrons)")
    f_zero: float = Field(description="radical attack (the mean of the other two)")


class SiteReactivityResult(BaseModel):
    """Atoms ranked by susceptibility to the requested attack.

    `sites` is ordered most-susceptible first by the index named in `ranked_by`, and truncated to
    the most susceptible `len(sites)` of `total_atoms`. The ranking is valid *within* this molecule
    only: Fukui indices are normalized per molecule, so comparing them between molecules is
    meaningless, and they describe electronic susceptibility alone — sterics and the specific
    reagent are not in the model.
    """

    smiles: str | None
    structure_id: str
    method: str
    solvent: str | None
    mode: FukuiMode
    ranked_by: str
    total_atoms: int
    sites: list[FukuiSite]

    def ranked_for(self, mode: FukuiMode) -> "SiteReactivityResult":
        """Re-rank this result for `mode` without recomputing anything.

        **This is not a convenience, it is a correctness fix, and the split is what made it one.**
        The three single points behind a Fukui ranking do not depend on the mode — it only chooses
        the sort — so the server keys them without it, verified over the wire: `electrophilic`,
        `nucleophilic` and `radical` on phenol all derive
        `xtb.fukui@…:3aaf5b0543327fb5:b41312b0cdc59ab7`, one key. The server re-ranks on the way
        out, so a *remote* call is always right. A **cache hit here never reaches the server**, so
        without this the second mode asked for would be served the first mode's ordering carrying
        the first mode's `mode` and `ranked_by` labels — a confidently wrong regiochemistry answer,
        with nothing raising anywhere.

        Every site carries all three indices, so this costs a sort and no calculation, which is also
        why asking a second mode was always advertised as free.
        """
        if self.mode == mode:
            return self
        ranked_by = _MODE_FIELD[mode]
        return self.model_copy(
            update={
                "mode": mode,
                "ranked_by": ranked_by,
                "sites": sorted(
                    self.sites, key=lambda site: getattr(site, ranked_by), reverse=True
                ),
            }
        )


class OptimizationResult(BaseModel):
    """A converged GFN2-xTB minimum, with what it took to get there.

    `structure` is the optimized geometry and is the value downstream tasks consume; it carries
    `origin`, the key of the calculation that produced it, so a thermochemistry or reaction result
    computed from it has its lineage recorded rather than implied.

    A *non*-converged optimization is never returned: the server raises. A geometry that is not a
    stationary point produces frequencies, thermochemistry and reaction energies that all look
    ordinary and mean nothing, so the honest contract is that holding an `OptimizationResult`
    guarantees convergence (gate G4).

    `max_gradient` is `None` for **GFN-FF only** — a force field has no tblite equivalent, so its
    gradient cannot be re-evaluated, and convergence is xtb's own ANCopt convergence on the GFN-FF
    surface.
    """

    smiles: str | None
    input_structure_id: str
    structure: Structure
    method: str
    # Which backend produced this geometry. Recorded because the two do not agree to the last
    # decimal, so a reader comparing two results needs to know they are comparable.
    engine: str
    solvent: str | None
    initial_energy_hartree: float
    energy_hartree: float
    # How much the relaxation was worth, in the unit a chemist reads. A large value on a supposedly
    # relaxed input means the starting geometry was misleading.
    relaxation_kcal: float
    steps: int
    # Largest absolute gradient component (Hartree/Angstrom) at the final geometry, over the free
    # atoms. `None` only for GFN-FF (see the class docstring).
    max_gradient: float | None
    # Root-mean-square coordinate displacement, in Angstrom. Not Kabsch-aligned: the forces of a
    # molecule sum to zero, so an optimization introduces no net translation and this is a movement
    # measure, not a superposition.
    displacement_rms_angstrom: float
    frozen_atoms: list[int]


class OptimizationSummary(BaseModel):
    """An optimization without its coordinates — what an agent can actually use.

    A model cannot read 3N Cartesians, and pasting them into a conversation is the
    unbounded-context failure the retrieval layer was already audited for. The geometry keeps
    flowing between calculations; `structure_id` is what makes it referable from a transcript.
    """

    smiles: str | None
    structure_id: str
    method: str
    engine: str
    solvent: str | None
    energy_hartree: float
    relaxation_kcal: float
    steps: int
    max_gradient: float | None
    displacement_rms_angstrom: float

    @classmethod
    def of(cls, result: OptimizationResult) -> "OptimizationSummary":
        """Drop the geometry from a full result, keeping its address."""
        return cls(
            smiles=result.smiles,
            structure_id=result.structure.structure_id,
            method=result.method,
            engine=result.engine,
            solvent=result.solvent,
            energy_hartree=result.energy_hartree,
            relaxation_kcal=result.relaxation_kcal,
            steps=result.steps,
            max_gradient=result.max_gradient,
            displacement_rms_angstrom=result.displacement_rms_angstrom,
        )


class HessianPayload(BaseModel):
    """Second derivatives at one geometry, with the arrays base64-encoded as `.npy`.

    `hessian_npy` is (3N, 3N) in Hartree/Angstrom^2. Exactly one of `dipole_derivatives_npy`
    (3N, 3) in Debye/Angstrom and `ir_intensities` (one per Cartesian mode, km/mol) is populated,
    and which one says which backend ran: the in-process path collects dipole derivatives while it
    displaces, the `xtb` binary computes intensities itself.

    **Both are what a caller needs to derive an IR spectrum, and neither is a spectrum.** The
    normal-mode projection and the RRHO arithmetic over them are pure partition functions — no
    quantum chemistry, no binary, milliseconds — so they stayed here (`science/calc/thermo.py`)
    while the second derivatives that cost minutes went to the server. That split is the whole
    reason `compute_thermochemistry` is composed rather than shipped: the expensive half is cached
    under a key the server derives, and the cheap half is recomputed at whatever temperature is
    asked for.

    The arrays cross as base64 rather than as JSON number lists because they are megabytes at drug
    size: a 33-atom Hessian is 99x99 doubles, and `.npy` is self-describing so the shape cannot be
    lost in transit.
    """

    structure_id: str
    method: str
    solvent: str | None
    atom_count: int
    electronic_energy_hartree: float
    hessian_npy: str
    dipole_derivatives_npy: str | None = None
    ir_intensities: list[float] | None = None


class VibrationalMode(BaseModel):
    """One normal mode: its wavenumber and how strongly it absorbs in the IR.

    A **negative** `wavenumber_cm` is an imaginary frequency — the geometry is not a minimum along
    that mode. Reported as a negative number because that is the convention every quantum chemistry
    program prints and every chemist reads.
    """

    wavenumber_cm: float
    ir_intensity_km_per_mol: float


class ThermochemistryResult(BaseModel):
    """RRHO thermochemistry at the semiempirical level, with its caveats in the data.

    Absolute values are in Hartree (what a reaction differences); the corrections are in kcal/mol
    (what a person reads). `is_minimum=False` with a populated `imaginary_frequencies_cm` is the
    point of the model: the result states that its own free energy is not a free energy, rather
    than relying on the caller to notice.

    `conformer_treatment="single"` is the second built-in caveat. Everything here describes one
    conformer, and a single-conformer free energy is the most common silent error in semiempirical
    work.
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
    # Every normal mode, ordered by wavenumber. A caller with a context budget may truncate this to
    # the bands that matter (`strongest_bands`); `mode_count` is then the honest statement of how
    # many there were — the same truncation contract `SiteReactivityResult` uses for atoms.
    modes: list[VibrationalMode]
    mode_count: int
    # The five lowest modes, always from the *full* set. RRHO is weakest here, so this is where
    # doubt about the free energy belongs, and it must survive truncation.
    lowest_wavenumbers_cm: list[float]

    electronic_energy_hartree: float
    zero_point_energy_kcal: float
    # Enthalpy minus the electronic energy: ZPE plus the thermal population of every degree of
    # freedom plus RT. This is the number that transfers between molecules.
    thermal_enthalpy_correction_kcal: float
    entropy_cal_per_mol_k: float
    gibbs_correction_kcal: float
    enthalpy_hartree: float
    gibbs_free_energy_hartree: float

    uncertainty_kcal: float
    conformer_treatment: Literal["single"] = "single"
    # Cartesian direction of the most negative mode (Angstrom per unit displacement), present only
    # when the geometry is not a minimum. It is what the structure wants to do, so it is also what
    # the refinement loop displaces along to escape.
    imaginary_displacement: list[list[float]] | None = None

    def strongest_bands(self, limit: int) -> list[VibrationalMode]:
        """The `limit` most intense bands, plus every imaginary mode, by wavenumber.

        What a computed IR spectrum is compared against: a measured spectrum shows the bands that
        absorb, and the weak modes between them carry no information for that comparison. Imaginary
        modes are never dropped — they are the reason to distrust the whole result, and they have
        no intensity to rank by.
        """
        real = [index for index, mode in enumerate(self.modes) if mode.wavenumber_cm > 0]
        real.sort(key=lambda index: self.modes[index].ir_intensity_km_per_mol, reverse=True)
        kept = set(real[:limit])
        return [
            mode
            for index, mode in enumerate(self.modes)
            if mode.wavenumber_cm <= 0 or index in kept
        ]


class ScanPoint(BaseModel):
    """One relaxed point of the profile."""

    value: float
    energy_hartree: float
    # Energy relative to the lowest point of this scan, in kcal/mol — the only form in which a scan
    # energy means anything.
    relative_kcal: float


class ScanResult(BaseModel):
    """A relaxed energy profile along one internal coordinate.

    `maximum_relative_kcal` is the highest point of the *profile*, not an optimized transition
    state. For a torsion it is a sound barrier estimate; for a bond being broken it is an
    upper-bound sketch. `minimum_structure` is the lowest point's geometry, so a scan that finds a
    better conformer hands it back usable.
    """

    smiles: str | None
    input_structure_id: str
    method: str
    solvent: str | None
    coordinate: str
    atoms: list[int]
    unit: str
    points: list[ScanPoint]
    minimum_value: float
    maximum_relative_kcal: float
    minimum_structure: Structure


class EnsembleMember(BaseModel):
    """One structure of a CREST ensemble, with the energy it was ranked by.

    `degeneracy` is how many **rotamers** collapse onto this conformer — n-butane's gauche is two
    mirror-image rotamers, and its methyl rotations multiply further. It is not bookkeeping: a
    population that ignores it is simply wrong, and by a lot. Measured on n-butane,
    degeneracy-weighted populations give the anti 59.2% against CREST's own reported 59.14%;
    ignoring degeneracy gives 73%.
    """

    energy_hartree: float
    degeneracy: int = 1
    structure: Structure


class EnsemblePayload(BaseModel):
    """What a CREST search returns and what the cache stores for one — members, not populations.

    The search is the minutes-to-hours half and is cached under the server's own key; the
    Boltzmann weighting, the conformational entropy and the truncation to what a reader can hold
    are arithmetic over `members` and are recomputed here every time. That is why `temperature_k`
    is *not* on this model: it does not move the search, so a second question at another
    temperature must not miss the cache (the same STO-2 argument that keeps `ThermoSpec`'s state
    variables out of a `HessianSpec`).
    """

    structure_id: str
    method: str
    solvent: str | None
    search: str
    effort: CrestEffort
    members: list[EnsembleMember]
    total_found: int


class Conformer(BaseModel):
    """One member of an ensemble, with what it contributes.

    `degeneracy` is how many rotamers this conformer stands for, and it multiplies the population —
    a detail that changes n-butane's anti fraction from 73% to the correct 59%, so it is
    load-bearing rather than descriptive.
    """

    relative_kcal: float
    population: float
    degeneracy: int
    structure: Structure


class ConformerEnsemble(BaseModel):
    """A sampled ensemble with its Boltzmann populations and conformational entropy.

    `conformational_entropy_cal_per_mol_k` is the term a single-conformer free energy is missing:
    -R * sum(p ln p) over the populations. It is always positive, so ignoring it systematically
    *over*-estimates the free energy of a flexible species — and does so unequally when a reaction
    changes flexibility, which is exactly when it matters.

    A metadynamics search is stochastic, so this is a *sample* of conformational space rather than
    an enumeration of it. The cache is what makes it **stable**: the first search's members are the
    ones every later question about that molecule is weighted from, so a report and the number
    behind it cannot drift apart even though the underlying search would.
    """

    smiles: str | None
    method: str
    search: EnsembleSearch
    effort: CrestEffort
    solvent: str | None
    temperature_k: float
    conformers: list[Conformer]
    total_found: int
    conformational_entropy_cal_per_mol_k: float
    # Carried on the result so a reader treats the populations as sampled rather than exact.
    sampled: Literal[True] = True
    # Free energy of the ensemble relative to its lowest member, in kcal/mol: the -T*S_conf
    # correction to add to a single-conformer free energy.
    ensemble_correction_kcal: float
    treatment: Literal["lowest-plus-conformational-entropy"] = "lowest-plus-conformational-entropy"

    @property
    def lowest(self) -> Structure:
        """The lowest-energy member — what a downstream single-structure task should use."""
        return self.conformers[0].structure


class InteractionResult(BaseModel):
    """How two molecules associate, and how strongly.

    `interaction_energy_kcal` is negative for a bound complex. It is an **electronic** interaction
    energy: the association entropy that decides whether the complex exists at a given temperature
    is not in it.
    """

    smiles_a: str
    smiles_b: str
    method: str
    solvent: str | None
    interaction_energy_kcal: float
    complex_energy_hartree: float
    monomer_energies_hartree: list[float]
    # How many distinct binding modes the search found. One is a weak result, not a confident one:
    # it usually means the search was too quick rather than that the pair has a single way to bind.
    binding_modes: int
    structure: Structure
    # A metadynamics search samples binding modes rather than enumerating them.
    sampled: Literal[True] = True


class SpeciesEnergy(BaseModel):
    """One species of the equation, and what was computed for it.

    `enthalpy_hartree` and `gibbs_free_energy_hartree` are None at `quick` level. Both are absolute
    values, in Hartree, because their only use is being differenced.
    """

    smiles: str
    role: Literal["reactant", "product"]
    multiplicity: int
    # The rotational symmetry number this species' entropy was computed with, and the field that
    # says whether it was *known*. None means the caller stated none, so sigma=1 was used and the
    # entropy is too high by R ln(sigma_true) — the reason the reaction then withholds ΔG. It is
    # also None at `quick`, where no entropy exists at all.
    symmetry_number: int | None
    electronic_energy_hartree: float
    enthalpy_hartree: float | None
    gibbs_free_energy_hartree: float | None
    is_minimum: bool | None
    # The -T*S_conf term the ensemble contributed, present only at `thorough`. Positive flexibility
    # lowers a free energy, so this is negative when it is present at all.
    conformational_entropy_kcal: float | None = None
    was_cached: bool
    # The method the *server* reported for this species' optimisation, so a reaction can state the
    # level of theory it was actually run at. Additive and defaulted because this crosses the
    # Temporal wire and histories are in flight; empty means a run from before it was recorded, and
    # `reaction_energy` falls back to the configured name only then.
    method: str = ""


class ReactionEnergyResult(BaseModel):
    """The energetics of one balanced reaction, with its per-species breakdown.

    Deltas are products minus reactants in kcal/mol: negative is downhill. Report the uncertainty
    with the number — a semiempirical reaction free energy is a screening quantity, good for
    comparing related reactions and poor as an absolute.
    """

    reactants: list[str]
    products: list[str]
    method: str
    solvent: str | None
    temperature_k: float
    level: ReactionLevel
    delta_e_kcal: float
    delta_h_kcal: float | None
    # None at `quick` (no Hessian, so no entropy) and None when any species' rotational symmetry
    # number was left unstated, which `species[i].symmetry_number` pinpoints and `warnings`
    # explains. One field for the two because they are one fact: a free energy this run is not
    # entitled to report.
    delta_g_kcal: float | None
    species: list[SpeciesEnergy]
    cache_hits: int
    uncertainty_kcal: float
    # Thermal-hazard screening flag. Advisory exactly as the structural hazard screen is (D-080): a
    # strongly negative electronic energy is a reason to look at the thermal data, never a heat of
    # reaction and never a clearance. It reads ΔE rather than ΔG deliberately — a runaway is driven
    # by the heat released, which is the enthalpic quantity.
    is_strongly_exothermic: bool
    exotherm_threshold_kcal: float
    # Which conformational treatment produced the deltas.
    conformer_treatment: Literal["single", "lowest-plus-conformational-entropy"]
    warnings: list[str] = Field(default_factory=list)


class SolventEffect(BaseModel):
    """One solvent's effect on the same reaction. `solvent=None` is the gas phase."""

    solvent: str | None
    delta_e_kcal: float
    delta_h_kcal: float | None
    delta_g_kcal: float | None


class SolventComparisonResult(BaseModel):
    """The same reaction across several solvents, most favourable first.

    `spread_kcal` is the range of the ranking quantity across the solvents tried. When it is not
    larger than `uncertainty_kcal`, the calculation has **not** distinguished them, and `warnings`
    says so — an implicit continuum model resolving 0.4 kcal/mol between two solvents is reading
    its own noise.
    """

    reactants: list[str]
    products: list[str]
    method: str
    temperature_k: float
    level: ReactionLevel
    effects: list[SolventEffect]
    best_solvent: str | None
    spread_kcal: float
    uncertainty_kcal: float
    warnings: list[str] = Field(default_factory=list)
