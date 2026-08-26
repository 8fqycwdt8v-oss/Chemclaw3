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
from pydantic import BaseModel, Field, computed_field, model_validator
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

# Decimal places coordinates are rounded to before a `Structure` is hashed. 4 decimals = 0.1 pm,
# far below any chemical significance, so run-to-run float noise cannot fork the cache.
#
# **A constant rather than a setting, deliberately.** It was `settings.xtb_geometry_decimals`, an
# ordinary ENV-overridable field advertised in `.env.example` — and these are the bytes that cross
# the wire, so it is what the *server* derives `input_hash` from. An operator who changed it was
# not re-addressing a local cache, which is what its comment claimed: they were making every
# relaxation, Hessian, scan point and CREST search in that deployment miss forever, silently, and
# diverge from every other deployment against the same server. Nothing raises on a key that does
# not match; the calculation simply runs again, every time.
#
# Changing this value is therefore a cross-repository change that has to land on both sides at
# once, which is exactly the kind of decision a deployment must not be able to take alone.
_GEOMETRY_DECIMALS = 4


class Structure(BaseModel):
    """One 3D molecular structure, addressed by the hash of its chemical content.

    `elements` and `positions` are parallel: atom `i` has atomic number `elements[i]` at
    `positions[i]` (Angstrom). Positions are normalized on construction (rounded to
    `_GEOMETRY_DECIMALS`) so that float noise from a re-run cannot fork the cache while the stored
    coordinates still *are* the ones that were hashed.

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
        if len(self.positions) != len(self.elements):
            raise ValueError(f"{len(self.positions)} positions for {len(self.elements)} elements")
        if any(len(row) != 3 for row in self.positions):
            raise ValueError("every position must have exactly three coordinates")
        # `+ 0.0` normalizes the negative zero that rounding can produce, so two geometrically
        # identical structures cannot differ in their hash by a sign bit.
        self.positions = [
            [round(value, _GEOMETRY_DECIMALS) + 0.0 for value in row] for row in self.positions
        ]
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

    def as_xyz(self, comment: str = "") -> str:
        """This geometry as an XYZ block — the one interchange format every QM program reads.

        For crossing a boundary that is not this system's: the HPC pipeline consumes a starting
        geometry as a file, not as a pydantic model (D-2026-08-21). Everything inside this
        repository passes the `Structure` itself, so this has exactly one caller and is deliberately
        not a general serialization — `model_dump` is that.

        Coordinates are written at the precision they are hashed at, so the block a pipeline
        receives is the geometry `structure_id` names rather than a rounded neighbour of it.

        Args:
            comment: The second line, which XYZ reserves for free text; conventionally the
                molecule's identity.

        Returns:
            The atom count, the comment, then one `<symbol> <x> <y> <z>` line per atom,
            newline-terminated.
        """
        lines = [str(len(self.elements)), comment]
        lines.extend(
            f"{symbol} {x:.{_GEOMETRY_DECIMALS}f} {y:.{_GEOMETRY_DECIMALS}f} "
            f"{z:.{_GEOMETRY_DECIMALS}f}"
            for symbol, (x, y, z) in zip(self.symbols, self.positions, strict=True)
        )
        return "\n".join(lines) + "\n"


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


class Interconversion(BaseModel):
    """What a barrier means as a rate, with the uncertainty that decides whether it means anything.

    **The band is not decoration.** Eyring is exponential in the barrier, so the method's own
    ±3 kcal/mol (`xtb_reaction_uncertainty_kcal`) is roughly five orders of magnitude in half-life
    at room temperature. `skills/atropisomer-assessment` puts the consequence plainly: a computed
    26 kcal/mol spans "hours" to "years" and therefore spans two ICH classes. A single number here
    would read exactly like a measurement, which is the one thing it is not — so the fast and slow
    ends travel with the mean and every caller that reports one reports all three.
    """

    barrier_kcal: float
    temperature_k: float
    rate_per_second: float
    half_life_seconds: float
    # The half-life at the barrier plus and minus the method's uncertainty. Named for what they
    # are to a chemist — the shortest and longest lifetime consistent with this calculation —
    # rather than for the sign of the shift that produced them.
    half_life_seconds_fastest: float
    half_life_seconds_slowest: float
    uncertainty_kcal: float


class Torsion(BaseModel):
    """The bond a rotational profile is about, as `chem`'s `enumerate_torsions` described it.

    A model rather than five loose arguments because these five values travel together through the
    composite and into the result, and because `BondCleavageSpec` next door records what a
    positional payload costs: a tuple is one field-order change away from computing a different
    bond than the caller named. It is *checked* rather than trusted — `_verified_torsion` recomputes
    the handle from the molecule being calculated and refuses a mismatch.

    `connectors/calc/specs.py::TorsionSpec` is the same shape on the wire, deliberately declared
    separately: that module is a leaf the chat service imports on every agent build and may not
    import `science` (D-118), so the wire shape and the domain shape are two files by rule. The
    activity maps one onto the other field by field.
    """

    torsion_id: str
    atoms: list[int] = Field(min_length=0, max_length=4)
    bond: list[int] = Field(min_length=2, max_length=2)
    label: str
    symmetry_order: int = Field(default=1, ge=1)
    period_degrees: float = Field(default=360.0, gt=0.0, le=360.0)


class Rotamer(BaseModel):
    """One populated minimum of a torsion profile — a released geometry, not a scan point.

    The distinction is the whole reason this model exists beside `ScanPoint`. Every point of a
    relaxed scan is *constrained*: the dihedral is frozen and everything else relaxes around it, so
    the lowest point of a profile is not a minimum of the molecule, it is the best of a set of
    partially-optimized geometries. A rotamer is what the well becomes once the constraint is
    released and the geometry is optimized freely — which is the structure any later calculation
    should start from, and the reason `structure_id` here is worth carrying.
    """

    dihedral_degrees: float
    structure_id: str
    relative_kcal: float
    population: float
    # How many times this rotamer occurs in a full turn. The profile is scanned over one period, so
    # a well found there stands for `symmetry_order` copies of itself — and a population that
    # ignores that is wrong in the same way, and by the same arithmetic, as an ensemble population
    # that ignores conformer degeneracy (measured on n-butane: 73% against the correct 59.2%).
    degeneracy: int = Field(default=1, ge=1)
    # Free energy relative to the lowest rotamer, above `level="quick"`. `None` says the ranking is
    # electronic, which is a different claim and one a reader must not have to infer.
    relative_g_kcal: float | None = None


class RotationBarrier(BaseModel):
    """The cost of getting from one rotamer to the next, in the direction it is quoted.

    **Directional, because a barrier is.** `forward_kcal` is measured from `from_rotamer` and
    `reverse_kcal` from `to_rotamer`, and the two are equal only when the wells are degenerate. The
    one that decides configurational stability is the barrier out of the *populated* well, so
    reporting a single "the barrier" for a pair of unequal wells is reporting the wrong number half
    the time.
    """

    # **`from_rotamer == to_rotamer` is a real and important case, not a bug.** A torsion with one
    # populated form per period rotates into its own symmetry image over the pass between them —
    # which is exactly what an amide or a hindered biaryl with a single minimum does, and is the
    # barrier variable-temperature NMR measures. Reported with equal forward and reverse energies,
    # because by symmetry it is the same well on both sides.
    from_rotamer: int
    to_rotamer: int
    at_degrees: float
    forward_kcal: float
    reverse_kcal: float
    # `E` when the barrier is electronic, `G` when a Hessian was taken at the pass and its one
    # imaginary mode dropped — the standard transition-state treatment, applied to a geometry that
    # is a constrained maximum rather than an optimized saddle. Named on the barrier rather than on
    # the profile because a run can produce both: a pass with two imaginary modes falls back to `E`
    # and says so in the warnings.
    basis: Literal["E", "G"] = "E"
    # Rate and lifetime at the profile's temperature, with the band the method's uncertainty
    # implies. Absent only when the arithmetic could not be done.
    interconversion: Interconversion | None = None


class RotationProfile(BaseModel):
    """A torsion driven through one full period: the profile, its rotamers, and their barriers.

    What `ScanResult` is not. A scan reports points and the highest one relative to the lowest; this
    reports the *wells as released minima*, the barrier between each adjacent pair in both
    directions, the populations at a temperature, and the half-life each barrier implies. Those are
    the four things a chemist asking "which rotamer is it in and can I separate them" needs, and
    every one of them was previously left to be worked out by hand from a profile.
    """

    smiles: str | None
    input_structure_id: str
    method: str
    solvent: str | None
    temperature_k: float
    level: ReactionLevel
    # The bond this profile is about, in the form a reader can check it by: the handle, the atoms,
    # and the label a chemist recognises. Carried on the result rather than only on the request,
    # because the answer has to say which bond it is about.
    torsion_id: str
    atoms: list[int]
    label: str
    symmetry_order: int
    period_degrees: float
    points: list[ScanPoint]
    rotamers: list[Rotamer]
    barriers: list[RotationBarrier]
    highest_barrier_kcal: float
    uncertainty_kcal: float
    # What the profile itself says about how far to trust it: a step that may have driven over a
    # maximum, a point that relaxed into another basin, a well that would not settle. The three
    # pathologies `skills/conformational-analysis` asks a human to spot by eye — checked here,
    # because a check nobody runs is a check that does not exist.
    warnings: list[str] = Field(default_factory=list)


class EnsembleMember(BaseModel):
    """One structure of a CREST ensemble, with the energy it was ranked by.

    `degeneracy` is how many **rotamers** collapse onto this conformer — n-butane's gauche is two
    mirror-image rotamers, and its methyl rotations multiply further. It is not bookkeeping: a
    population that ignores it is simply wrong, and by a lot. Measured on n-butane,
    degeneracy-weighted populations give the anti 59.2% against CREST's own reported 59.14%;
    ignoring degeneracy gives 73%.
    """

    energy_hartree: float
    # `ge=1`, because `boltzmann_populations` divides by the sum of the weights and a
    # payload whose degeneracies were all zero would raise ZeroDivisionError from inside
    # the arithmetic rather than at the boundary. `ThermoSettings.symmetry_number` next
    # door already carries the same constraint for the same reason.
    degeneracy: int = Field(default=1, ge=1)
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
    # Which approximation produced the populations above. **Additive and defaulted**, because this
    # model crosses the Temporal wire and histories are in flight: a result decoded from an older
    # one carries the value it always had. `free-energy-weighted-top-n` is what a refined ensemble
    # reports — a different treatment rather than a better one, and a reader must not have to infer
    # which ran (D-101).
    treatment: Literal["lowest-plus-conformational-entropy", "free-energy-weighted-top-n"] = (
        "lowest-plus-conformational-entropy"
    )

    @property
    def lowest(self) -> Structure:
        """The lowest-energy member — what a downstream single-structure task should use."""
        return self.conformers[0].structure

    @computed_field  # type: ignore[prop-decorator]
    @property
    def lowest_structure_id(self) -> str:
        """The address of the lowest-energy member, hoisted so nobody has to index into a list.

        **A `computed_field`, so it survives `model_dump` and reaches the model and the template
        resolver** (D-2026-08-21). The lowest conformer is what every downstream single-structure
        question wants — relax it, take its Hessian, run DFT on it — and reaching it through
        `conformers[0].structure.structure_id` would need list indexing in the one place this
        system deliberately refuses to grow an expression language (`templates/resolve.py`).

        Derived rather than stored: `conformers` is built lowest-first by `ensemble_from_members`,
        and a second copy of that fact is a second thing that can disagree with it.
        """
        return self.conformers[0].structure.structure_id


class WeightedValue(BaseModel):
    """A Boltzmann-averaged property, with the spread of what was averaged.

    **The spread travels with the mean, and that is the whole design of this model.** A property
    whose values scatter across the ensemble by more than the difference it is being used to argue
    is not a number to report as a single figure: a mean dipole of 2.1 D over conformers ranging
    0.4 to 4.8 D says the molecule does not have *a* dipole at this temperature. Reporting the mean
    alone turns that into a false precision reading exactly like a measurement, so a caller has to
    look away from the spread deliberately rather than by omission.
    """

    mean: float
    minimum: float
    maximum: float
    spread: float = Field(description="maximum minus minimum, in the property's own unit")


class WeightedAtom(BaseModel):
    """One atom's Boltzmann-averaged per-atom property across an ensemble."""

    index: int
    element: str
    value: WeightedValue


class EnsembleProperty(BaseModel):
    """A property averaged over a conformer ensemble rather than read off one geometry.

    The standing caveat on every other number in this system is that it describes **one** conformer.
    This is the shape that lifts it: each member's property computed at that member's own geometry,
    then weighted by the population the ensemble gives it.

    **These populations are electronic-energy weighted, always.** A `refined` flag once sat here to
    say which weighting ran, and nothing ever wrote it — a promise with no implementation, which is
    worse than the absence it papered over, because a reader takes `refined=False` for a recorded
    choice rather than a default nobody set. Free-energy weighting costs one Hessian per member and
    is what `RefinedEnsemble` is for; when a property average over *those* populations is wanted,
    that is a new field on this model and a writer for it, not a boolean.
    """

    smiles: str | None
    property_name: str
    method: str
    solvent: str | None
    temperature_k: float
    members_averaged: int
    total_found: int
    sampled: Literal[True] = True
    value: WeightedValue | None = None
    per_atom: list[WeightedAtom] = Field(default_factory=list)
    # The population fraction the averaged members account for, from the ensemble's own weighting.
    # Below 1.0 the average is over a *truncation* of the ensemble, and saying so is what stops
    # "the Boltzmann-averaged dipole" meaning "the dipole of the five conformers we could afford".
    population_covered: float = 1.0
    # **`population_covered` records the truncation; this is what *says* it.** The field above
    # shipped without one, so the cheap composite disclosed its partial coverage only to a reader
    # who thought to divide, while `RefinedEnsemble` — the expensive one — warned. Same rule, both
    # of them now.
    warnings: list[str] = Field(default_factory=list)


class RefinedConformer(BaseModel):
    """One ensemble member after its own optimization and Hessian."""

    structure: Structure
    relative_kcal: float
    population: float
    # `ge=1`, because `boltzmann_populations` divides by the sum of the weights and a
    # payload whose degeneracies were all zero would raise ZeroDivisionError from inside
    # the arithmetic rather than at the boundary. `ThermoSettings.symmetry_number` next
    # door already carries the same constraint for the same reason.
    degeneracy: int = Field(default=1, ge=1)
    gibbs_free_energy_hartree: float
    electronic_energy_hartree: float
    is_minimum: bool


class RefinedEnsemble(BaseModel):
    """A conformer ensemble re-weighted by free energy instead of by electronic energy.

    **A different approximation, not a better one**, and two fields say so rather than leaving a
    reader to infer it. `refined_population_covered` is the E-weighted population fraction the
    refined members account for: refining the top five of forty-seven and reporting the result as
    "the ensemble" is the same error `ensemble_from_members` already refuses for `max_members`, and
    it is worse here because the number looks more careful. `treatment` on the underlying
    `ConformerEnsemble` names the weighting.

    D-101 recorded that the system does not free-energy-weight an ensemble because it is one Hessian
    per member. That is still true; this is the shape for when a caller decides to pay it, bounded
    to the top `ensemble_refine_top_n`.
    """

    smiles: str | None
    method: str
    solvent: str | None
    temperature_k: float
    conformers: list[RefinedConformer]
    total_found: int
    refined_count: int
    refined_population_covered: float
    # **Named for the subset, because that is what they describe.** `ConformerEnsemble` carries
    # fields with the first of these names and they mean something else: `ensemble_from_members`
    # computes them over *all* members and deliberately refuses to truncate them, arguing that
    # doing so "would turn 'here are the 10 that matter out of 47' into a quietly wrong claim that
    # there were 10". Here the populations are renormalised over the refined top N, so the entropy
    # is over N states and the correction is systematically too small. Shipping that under the
    # ensemble-wide name put two meanings one model apart; `refined_` says which one this is, and
    # `refined_population_covered` beside it says how much of the ensemble that N accounts for.
    refined_conformational_entropy_cal_per_mol_k: float
    refined_ensemble_correction_kcal: float
    sampled: Literal[True] = True
    treatment: Literal["free-energy-weighted-top-n"] = "free-energy-weighted-top-n"
    warnings: list[str] = Field(default_factory=list)

    @property
    def lowest(self) -> Structure:
        """The lowest free-energy member — the geometry a downstream single-structure task wants."""
        return self.conformers[0].structure

    @computed_field  # type: ignore[prop-decorator]
    @property
    def lowest_structure_id(self) -> str:
        """The address of the lowest free-energy member, hoisted for the template resolver."""
        return self.conformers[0].structure.structure_id


class RankedSpecies(BaseModel):
    """One species of a distribution, with the free energy it was ranked by."""

    smiles: str
    label: str = ""
    relative_kcal: float
    population: float
    gibbs_free_energy_hartree: float | None = None
    electronic_energy_hartree: float
    structure_id: str = ""
    conformers_found: int = 0


class SpeciesDistribution(BaseModel):
    """How a molecule distributes over a set of *distinct species* at equilibrium.

    One shape for three questions that are the same arithmetic over different species sets: which
    tautomer dominates, which protonation microstate is present at a pH, and which diastereomer is
    favoured. `kind` says which was asked, because the number means something different in each and
    a reader must not have to guess from the SMILES.

    The caveat that has to travel with it: a species not in the set was not ranked, and a
    distribution over an incomplete enumeration is confident about the wrong universe. `enumerated`
    records how many the enumeration produced against how many survived to be computed.
    """

    kind: Literal["tautomers", "microstates", "stereoisomers", "custom"]
    method: str
    solvent: str | None
    temperature_k: float
    level: ReactionLevel
    species: list[RankedSpecies]
    enumerated: int
    uncertainty_kcal: float
    sampled: bool = False
    warnings: list[str] = Field(default_factory=list)

    @property
    def dominant(self) -> RankedSpecies:
        """The most populated species — the form every other number should be about."""
        return max(self.species, key=lambda candidate: candidate.population)


class DissociatedBond(BaseModel):
    """One bond's dissociation energy, and the fragments it was computed from."""

    atoms: list[int]
    bond: str
    fragments: list[str]
    dissociation_energy_kcal: float
    is_weakest: bool = False


class BondDissociationSurvey(BaseModel):
    """Every breakable bond of a molecule, ranked by how much it costs to break.

    Semiempirical and therefore a *ranking* — GFN2 bond dissociation energies carry several
    kcal/mol of error, so the ordering is the answer and the magnitudes are not. What the ordering
    supports: which C-H a radical abstracts, which bond an autoxidation attacks first, which
    linkage a forced-degradation study should look for.
    """

    smiles: str
    method: str
    solvent: str | None
    temperature_k: float
    mode: Literal["homolytic", "heterolytic"]
    bonds: list[DissociatedBond]
    considered: int
    uncertainty_kcal: float
    warnings: list[str] = Field(default_factory=list)


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
    # The geometry this energy describes, so a caller can carry it into the next calculation
    # (D-2026-08-21). Additive and defaulted, because histories written before it exist.
    structure_id: str = ""
    # How many conformers the search found, at `thorough`; 0 where no search ran. `species_ranking`
    # reported a hardcoded 0 beside `sampled=True`, which reads as "sampled and found nothing".
    conformers_found: int = 0
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
