"""What an xTB job may be asked to do — the request half of this bundle's durable contract.

**A leaf module, and that is the whole point.** These models are named by `connector.yaml`'s
`params_model`, and `connectors/jobs.py` resolves that name by *importing* it — on every
`build_langgraph_agent`, in the chat service's own process, and again in `make connector-validate`.
So whatever this module imports, the chat service imports too.

They used to sit beside the *result* types, which named four science modules and, through them,
`tblite` — the compiled quantum-chemistry library. Measured on `main` at the time, building the
enabled job tools pulled **`tblite` and fifteen science modules** into the agent's process: the
entire quantum-chemistry closure this bundle exists to keep out of it (D-114), let back in through
the one manifest field that resolves an import. Nothing failed; the chat pod simply carried a
compiled QM library it never called.

So the split here is not stylistic. Requests live in this module and import pydantic and config
only; results live in `connectors/calc/results.py`. That weight is gone —
`D-2026-08-16-the-physics-leaves-the-cache-stays` took the engines out of this repository entirely
— and the split stays anyway, because these shapes are pinned by workflow histories in flight and
because a boundary that only holds while nothing heavy exists is not a boundary.
`cli/validate_connectors.py` enforces it rather than trusting it, and
`tests/test_connector_isolation.py` asserts it in a fresh interpreter (D-118).
"""

from typing import Annotated, Literal

from pydantic import BaseModel, Field, model_validator

# What a caller has to be told about `symmetry_numbers` to supply it correctly, written once
# because the two reaction-shaped specs advertise the identical contract. It is the *only* field
# here carrying a description, and deliberately so: every other one is self-evident from its name
# and type, while this one is a physical quantity whose omission silently costs the free energy.
# A plain `dict[str, int]` keeps this module leaf — no `chemclaw.science.*` import is needed to
# state it (D-118).
# What a caller has to be told to use a geometry handle, written once because three specs take one
# (D-2026-08-21-a-geometry-is-an-address-not-a-payload). Every field here that carries a description
# does so because a model cannot infer it from the name and the type, and this is the clearest case:
# the argument is a string, and *which* strings are valid is the whole of what has to be said.
_STRUCTURE_ID_DESCRIPTION = (
    "A specific 3D geometry to start from, as `structure_id` — the `st_...` address reported by "
    "optimize_geometry, sample_conformers, scan_coordinate and compute_thermochemistry. Use it to "
    "carry a chosen conformer from one calculation into the next: without it the calculation is "
    "run on a fresh force-field embedding, which discards whichever conformer an earlier search "
    "settled on. Leave it unset to start from the SMILES."
)

_SYMMETRY_NUMBERS_DESCRIPTION = (
    "Rotational symmetry number per species, keyed by the exact SMILES string given in "
    "reactants/products: 1 for a molecule with no rotational symmetry, 2 for H2/N2/O2/CO2/water, "
    "3 for ammonia, 6 for ethane, 12 for benzene. Above level='quick', a species left out of "
    "this map has its entropy computed at sigma=1 and the job reports no free energy at all "
    "rather than one too high by R*ln(sigma); the electronic energy and enthalpy do not depend "
    "on it and are reported either way. Stating 1 is a real statement and does yield a free "
    "energy — 'no rotational symmetry' and 'not considered' are different claims."
)


class BondCleavageSpec(BaseModel):
    """One bond to break, as `chem`'s `enumerate_bond_cleavages` reports it.

    A model rather than a tuple because it crosses the Temporal wire and a positional payload is
    one field-order change away from computing a different bond than the caller named.
    """

    atoms: list[int] = Field(min_length=2, max_length=2)
    bond: str = Field(min_length=1)
    fragments: list[str] = Field(min_length=2, max_length=2)


class TorsionSpec(BaseModel):
    """The bond to rotate, as `chem`'s `enumerate_torsions` reported it.

    A model rather than four bare indices, for the reason `BondCleavageSpec` states and one more.
    The stated one: a positional payload is one field-order change away from computing a different
    bond than the caller named. The additional one is measured — an atom index is not a name at
    all. `(4, 5)` is the amide C-N of `c1ccc(NC(C)=O)cc1` and an aromatic *ring* bond of
    `CC(=O)Nc1ccccc1`, the same compound rewritten, really bonded, in range. A scan driven from a
    stale index therefore runs and reports a plausible barrier for a question nobody asked.

    `torsion_id` is what closes that: a handle derived from the molecule rather than from the order
    its atoms happen to appear in. The job recomputes it from the structure it is about to
    calculate and refuses a mismatch, so the handle is a checksum on the indices rather than
    decoration. Carry the whole entry from the enumeration; do not assemble one by hand.

    `science/calc/models.py::Torsion` is the same shape inside the calculation. Two files by rule:
    this module is a leaf the chat service imports on every agent build and may not import
    `science` (D-118).
    """

    torsion_id: str = Field(
        min_length=1,
        description=(
            "The `tor_...` handle from enumerate_torsions on this molecule. Checked against the "
            "structure, so one carried from another compound or another RDKit build is refused "
            "rather than silently scanned."
        ),
    )
    atoms: list[int] = Field(
        min_length=4,
        max_length=4,
        description="The four atom indices of the dihedral, exactly as the enumeration gave them.",
    )
    bond: list[int] = Field(min_length=2, max_length=2)
    label: str = Field(min_length=1)
    symmetry_order: int = Field(default=1, ge=1)
    period_degrees: float = Field(default=360.0, gt=0.0, le=360.0)


class ReactionJobSpec(BaseModel):
    """A durable reaction-energy request (xTB plan X4)."""

    kind: Literal["reaction"] = "reaction"
    reactants: list[str] = Field(min_length=1)
    products: list[str] = Field(min_length=1)
    solvent: str | None = None
    temperature_k: float | None = None
    level: Literal["quick", "standard", "thorough"] = "standard"
    symmetry_numbers: dict[str, int] | None = Field(
        default=None, description=_SYMMETRY_NUMBERS_DESCRIPTION
    )


class SolventScreenJobSpec(BaseModel):
    """A durable solvent-comparison request over one reaction (xTB plan X4)."""

    kind: Literal["solvents"] = "solvents"
    reactants: list[str] = Field(min_length=1)
    products: list[str] = Field(min_length=1)
    solvents: list[str] = Field(min_length=1)
    temperature_k: float | None = None
    level: Literal["quick", "standard", "thorough"] = "standard"
    # The same species appear in every solvent, so one map covers the whole screen.
    symmetry_numbers: dict[str, int] | None = Field(
        default=None, description=_SYMMETRY_NUMBERS_DESCRIPTION
    )


class ScanJobSpec(BaseModel):
    """A durable relaxed-scan request along one internal coordinate (xTB plan X3).

    `smiles` stays required even when `structure_id` is given, and that is not redundancy: the atom
    indices this scan drives are indices *into* a molecule, the result is reported and cited under a
    molecule, and a geometry that is not of that molecule is a request nobody can read. It is
    checked against the resolved structure rather than assumed.
    """

    kind: Literal["scan"] = "scan"
    smiles: str = Field(min_length=1)
    atoms: list[int] = Field(min_length=2, max_length=4)
    values: list[float] = Field(min_length=2)
    solvent: str | None = None
    structure_id: str | None = Field(default=None, description=_STRUCTURE_ID_DESCRIPTION)


class RotationJobSpec(BaseModel):
    """A durable rotational profile about one named bond.

    Distinct from `ScanJobSpec`, which drives any internal coordinate and reports points. This one
    is about a torsion specifically, and everything it adds follows from that: the bond is named
    rather than indexed, the scan covers one *period* rather than always 360 degrees, the wells are
    released from their constraint into real rotamers, and the barriers between them are directional
    and carry a half-life.
    """

    kind: Literal["rotation"] = "rotation"
    smiles: str = Field(min_length=1)
    torsion: TorsionSpec
    solvent: str | None = None
    temperature_k: float | None = None
    step_degrees: float | None = Field(
        default=None,
        gt=0.0,
        le=120.0,
        description=(
            "The coarse step in degrees; 30 by default. Every point is a constrained optimization, "
            "so halving this doubles the cost — and the maxima are refined finely regardless, so a "
            "smaller step buys resolution of the *wells* rather than of the barrier."
        ),
    )
    level: Literal["quick", "standard", "thorough"] = "quick"
    structure_id: str | None = Field(default=None, description=_STRUCTURE_ID_DESCRIPTION)


class EnsembleJobSpec(BaseModel):
    """A durable conformer/tautomer/protomer search request (xTB plan X6)."""

    kind: Literal["ensemble"] = "ensemble"
    smiles: str = Field(min_length=1)
    search: Literal["conformers", "tautomers", "protomers", "deprotomers"] = "conformers"
    solvent: str | None = None
    effort: Literal["quick", "normal", "extensive"] = "quick"
    structure_id: str | None = Field(default=None, description=_STRUCTURE_ID_DESCRIPTION)


class ComplexJobSpec(BaseModel):
    """A durable non-covalent complex search over two molecules (xTB plan X11)."""

    kind: Literal["complex"] = "complex"
    smiles_a: str = Field(min_length=1)
    smiles_b: str = Field(min_length=1)
    solvent: str | None = None
    effort: Literal["quick", "normal", "extensive"] = "quick"
    # Both or neither: a search that starts from one chosen conformer and one fresh embedding is
    # not a comparison anybody asked for, and silently pairing them is how a number that means
    # nothing gets reported as a binding energy.
    structure_id_a: str | None = Field(default=None, description=_STRUCTURE_ID_DESCRIPTION)
    structure_id_b: str | None = Field(default=None, description=_STRUCTURE_ID_DESCRIPTION)

    @model_validator(mode="after")
    def _both_geometries_or_neither(self) -> "ComplexJobSpec":
        """Refuse a half-specified pair rather than quietly embedding the other monomer."""
        if (self.structure_id_a is None) != (self.structure_id_b is None):
            raise ValueError(
                "give structure_id_a and structure_id_b together or not at all: one chosen "
                "geometry against one fresh embedding is not a comparison of the two conformers"
            )
        return self


class RefinedEnsembleJobSpec(BaseModel):
    """A durable free-energy-weighted conformer ensemble.

    The Literals below are re-declared rather than imported from `science/calc/models.py`, exactly
    as every other member of this union does it, and for the module-level reason: this file is a
    leaf the chat service imports on every `build_langgraph_agent`.
    """

    kind: Literal["refined_ensemble"] = "refined_ensemble"
    smiles: str = Field(min_length=1)
    solvent: str | None = None
    temperature_k: float | None = None
    top_n: int | None = Field(
        default=None,
        ge=1,
        description=(
            "How many of the lowest-energy conformers get their own optimization and Hessian. "
            "Each one is minutes of CPU, so this is the cost knob; the result reports what share "
            "of the ensemble population the refined members actually cover."
        ),
    )
    structure_id: str | None = Field(default=None, description=_STRUCTURE_ID_DESCRIPTION)


class EnsemblePropertyJobSpec(BaseModel):
    """A durable Boltzmann-averaged property over a conformer ensemble."""

    kind: Literal["ensemble_property"] = "ensemble_property"
    smiles: str = Field(min_length=1)
    prop: Literal["dipole_debye", "homo_ev", "lumo_ev", "gap_ev", "charges", "fukui"] = (
        "dipole_debye"
    )
    solvent: str | None = None
    temperature_k: float | None = None
    max_members: int | None = Field(default=None, ge=1)


class SpeciesRankingJobSpec(BaseModel):
    """A durable free-energy ranking over a set of distinct species.

    `species` is a list of SMILES the caller enumerated — `chem`'s `enumerate_tautomers`,
    `enumerate_protonation_states` and `enumerate_stereoisomers` each produce one. It is not
    enumerated here: this bundle computes, and deciding *which* forms exist is a cheminformatics
    question answered before any calculation is worth starting.
    """

    kind: Literal["species_ranking"] = "species_ranking"
    species: list[str] = Field(
        min_length=1,
        description=(
            "The SMILES to rank against each other. A form that is not in this list is not ranked, "
            "so the distribution describes exactly the set given — enumerate first."
        ),
    )
    labels: list[str] | None = Field(
        default=None,
        description=(
            "An optional name per species, in the same order, for the result to report instead of "
            "a bare SMILES. Must be the same length as `species` if given."
        ),
    )
    ranking: Literal["tautomers", "microstates", "stereoisomers", "custom"] = "custom"
    solvent: str | None = None
    temperature_k: float | None = None
    level: Literal["quick", "standard", "thorough"] = "standard"
    # Its own description, shorter and — the part that matters — *true here*. The reaction specs'
    # text says the job "reports no free energy at all" for a species left out, which is their
    # behaviour and not this one's: a ranking has no useful E-only substitute above `quick`, so it
    # ranks anyway and warns. Reusing that string would have been the more wrong of the two.
    symmetry_numbers: dict[str, int] | None = Field(
        default=None,
        description=(
            "Rotational symmetry number per species, keyed by its exact SMILES: 1 = none, "
            "2 = a C2 axis, 6 = ethane, 12 = benzene. One left out is ranked at sigma=1 and "
            "warned about; the error is R*ln(sigma), 0.41 kcal/mol per factor of two."
        ),
    )

    @model_validator(mode="after")
    def _labels_match_species(self) -> "SpeciesRankingJobSpec":
        """Refuse a mismatched label list rather than silently pairing the wrong names to forms."""
        if self.labels is not None and len(self.labels) != len(self.species):
            raise ValueError(
                f"{len(self.labels)} labels for {len(self.species)} species: give one label per "
                "species in the same order, or none at all"
            )
        return self


class BondSurveyJobSpec(BaseModel):
    """A durable bond-dissociation survey over every breakable bond of one molecule.

    Like `SpeciesRankingJobSpec`, the enumeration arrives rather than happening here:
    `chem`'s `enumerate_bond_cleavages` produces the fragment pairs, written with explicit radical
    electrons so the open shell needs no declared spin state.
    """

    kind: Literal["bond_survey"] = "bond_survey"
    smiles: str = Field(min_length=1)
    cleavages: list[BondCleavageSpec] = Field(
        min_length=1,
        description=(
            "The bonds to break, as `enumerate_bond_cleavages` reports them. Every entry costs one "
            "reaction energy, so a whole-molecule survey of a drug-sized structure is the "
            "expensive case this job exists for."
        ),
    )
    solvent: str | None = None
    temperature_k: float | None = None
    level: Literal["quick", "standard", "thorough"] = "quick"


# What an xTB job may be asked to do, discriminated on `kind`. A closed, typed union
# rather than a free-form request is the same boundary rule the proposal sets for the
# expert escape hatch: a model-authored payload can select among calculations we
# defined, and can never describe one we did not.
XtbJobSpec = Annotated[
    ReactionJobSpec
    | SolventScreenJobSpec
    | ScanJobSpec
    | RotationJobSpec
    | EnsembleJobSpec
    | ComplexJobSpec
    | RefinedEnsembleJobSpec
    | EnsemblePropertyJobSpec
    | SpeciesRankingJobSpec
    | BondSurveyJobSpec,
    Field(discriminator="kind"),
]
