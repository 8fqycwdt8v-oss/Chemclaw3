"""What an xTB job may be asked to do: the request half of this bundle's durable contract.

A leaf module: `connector.yaml`'s `params_model` names these models and `connectors/jobs.py`
imports them in the chat service's process, so whatever this module imports, the chat service
imports too (D-118). It imports pydantic and config only; results live in
`connectors/calc/results.py`. `cli/validate_connectors.py` and
`tests/test_connector_isolation.py` enforce it.
"""

from typing import Annotated, Literal

from pydantic import BaseModel, Field, model_validator

from chemclaw.core.model_prose import ModelProse

# Field descriptions shared by several specs. A field carries a description only when a model cannot
# infer it from name and type: which strings are valid geometry handles, and what a symmetry number
# is and what omitting it costs.
_STRUCTURE_ID_DESCRIPTION = ModelProse(
    "A specific 3D geometry to start from, as `structure_id` — the `st_...` address reported by "
    "optimize_geometry, sample_conformers, scan_coordinate and compute_thermochemistry. Use it to "
    "carry a chosen conformer from one calculation into the next: without it the calculation is "
    "run on a fresh force-field embedding, which discards whichever conformer an earlier search "
    "settled on. Leave it unset to start from the SMILES."
)

_SYMMETRY_NUMBERS_DESCRIPTION = ModelProse(
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


# A model rather than four bare indices, because an atom index is not a name: the same indices pick
# a different bond once the SMILES is rewritten. `torsion_id` is derived from the molecule; the job
# recomputes it and refuses a mismatch, so it checksums the indices. Mirrors
# `science/calc/models.py::Torsion` without importing it (this module is a leaf).
#
# Rationale lives in comments, not docstrings, because pydantic publishes a docstring as the schema
# `description`, which costs tokens on every turn (`tests/test_context_floor.py`).
class TorsionSpec(BaseModel):
    """The bond to rotate, exactly as `chem`'s `enumerate_torsions` reported it."""

    # No per-field descriptions: this model is copied from an `enumerate_torsions` entry, not
    # assembled.
    torsion_id: str = Field(min_length=1)
    # 0 or 4: `enumerate_torsions` reports an empty list for a rotor whose rotating end carries only
    # hydrogens, and the composite builds that dihedral itself (`compose._rotor_dihedral`).
    atoms: list[int] = Field(min_length=0, max_length=4)
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


# Unlike `ScanJobSpec`, specific to a torsion: the bond is named, the scan covers one period, wells
# are released into real rotamers, and barriers are directional with a half-life.
class RotationJobSpec(BaseModel):
    """A durable rotational profile about one named bond."""

    kind: Literal["rotation"] = "rotation"
    smiles: str = Field(min_length=1)
    torsion: TorsionSpec
    solvent: str | None = None
    temperature_k: float | None = None
    # The cost knob: each point is a constrained optimization. Maxima are refined regardless, so a
    # smaller step resolves the wells rather than the barrier.
    step_degrees: float | None = Field(default=None, gt=0.0, le=120.0)
    level: Literal["quick", "standard", "thorough"] = "quick"
    # A short form of `_STRUCTURE_ID_DESCRIPTION`: this schema is near the per-tool token ceiling
    # `tests/test_context_floor.py` enforces, and the tool description already says where handles
    # come
    # from.
    structure_id: str | None = Field(
        default=None,
        description="A conformer to profile in, as `st_...`; a barrier depends on which one.",
    )


class EnsembleJobSpec(BaseModel):
    """A durable conformer/tautomer/protomer search request (xTB plan X6)."""

    kind: Literal["ensemble"] = "ensemble"
    smiles: str = Field(min_length=1)
    search: Literal["conformers", "tautomers", "protomers", "deprotomers"] = "conformers"
    solvent: str | None = None
    effort: Literal["quick", "normal", "extensive"] = "quick"
    structure_id: str | None = Field(default=None, description=_STRUCTURE_ID_DESCRIPTION)


class MicrostatePkaJobSpec(BaseModel):
    """A durable pKa from two CREST searches (`microstate_pka`).

    Its own job rather than a level on the existing `predict_pka` tool, because the cost class is
    different by three orders of magnitude: that tool is a cached sub-second lookup and this is two
    metadynamics searches, minutes to hours. A knob that turns a fast tool into an expensive one is
    the shape that gets set by accident.
    """

    kind: Literal["microstate_pka"] = "microstate_pka"
    smiles: str = Field(min_length=1)
    branch: Literal["auto", "acid", "base"] = Field(
        default="auto",
        description=(
            "`auto` asks the acid question of an O-H/S-H molecule and the base question of a "
            "nitrogen one. Name it for a molecule that is both: an aminophenol has an acid pKa "
            "and a conjugate-acid pKaH, and they are different numbers."
        ),
    )
    solvent: str | None = None
    temperature_k: float | None = None
    effort: Literal["quick", "normal", "extensive"] = "quick"

    # No `structure_id`: this job starts with a conformer search that re-samples whatever it is
    # handed,
    # so a starting geometry would control nothing.


class ComplexJobSpec(BaseModel):
    """A durable non-covalent complex search over two molecules (xTB plan X11)."""

    kind: Literal["complex"] = "complex"
    smiles_a: str = Field(min_length=1)
    smiles_b: str = Field(min_length=1)
    solvent: str | None = None
    effort: Literal["quick", "normal", "extensive"] = "quick"
    # Both or neither: pairing a chosen conformer with a fresh embedding is not a meaningful
    # comparison.
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
    # Its own description, because the behaviour differs from the reaction specs': a ranking with a
    # species left out still ranks above `quick`, and warns.
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


class SpeciesSolventScreenJobSpec(BaseModel):
    """A durable ranking of one species set in each of several media.

    `SpeciesRankingJobSpec` with `solvent` replaced by `solvents`, and its rule about the set being
    the answer's universe applies unchanged — this fans a ranking out over media, it enumerates no
    more than that one does.
    """

    kind: Literal["species_solvents"] = "species_solvents"
    species: list[str] = Field(
        min_length=1,
        description="The SMILES to rank against each other, in every medium. Enumerate first.",
    )
    labels: list[str] | None = Field(
        default=None,
        description="An optional name per species, in the same order and the same length.",
    )
    ranking: Literal["tautomers", "microstates", "stereoisomers", "custom"] = "custom"
    solvents: list[str] = Field(
        min_length=1,
        description="ALPB solvent names. The gas phase is always added and need not be listed.",
    )
    temperature_k: float | None = None
    level: Literal["quick", "standard", "thorough"] = "standard"
    # One map covers the whole screen: a symmetry number is a property of the molecule, not the
    # medium.
    symmetry_numbers: dict[str, int] | None = Field(
        default=None,
        description=(
            "Rotational symmetry number per species, keyed by its exact SMILES: 1 = none, "
            "2 = a C2 axis, 6 = ethane, 12 = benzene. One left out is ranked at sigma=1 and "
            "warned about; the error is R*ln(sigma), 0.41 kcal/mol per factor of two."
        ),
    )

    @model_validator(mode="after")
    def _labels_match_species(self) -> "SpeciesSolventScreenJobSpec":
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


# Discriminated on `kind`: a model-authored payload can select among calculations we defined and can
# never describe one we did not.
XtbJobSpec = Annotated[
    ReactionJobSpec
    | SolventScreenJobSpec
    | ScanJobSpec
    | RotationJobSpec
    | EnsembleJobSpec
    | MicrostatePkaJobSpec
    | ComplexJobSpec
    | RefinedEnsembleJobSpec
    | EnsemblePropertyJobSpec
    | SpeciesRankingJobSpec
    | SpeciesSolventScreenJobSpec
    | BondSurveyJobSpec,
    Field(discriminator="kind"),
]
