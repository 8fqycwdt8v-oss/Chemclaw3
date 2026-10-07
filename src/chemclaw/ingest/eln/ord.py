"""The stable, ELN-agnostic reaction target schema.

An ORD-inspired pydantic subset — only the fields Chemclaw consumes (structure, roles, amounts,
headline conditions and yield, provenance) — that every layer above ELN integration knows. Adapters
map their format into this; nothing here knows any ELN's quirks.

Development recipes are step-by-step, so beside the flat headline fields (the summary existing
consumers read) the schema carries an ordered `steps` list and the verbatim `procedure_text`.
`steps` is purely additive and never feeds the reaction SMILES or fingerprints.
"""

from datetime import date
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from chemclaw.core.chem import standard_smiles
from chemclaw.core.errors import ChemclawError


class Role(StrEnum):
    """A component's role in the reaction (a subset of ORD's reaction roles)."""

    REACTANT = "reactant"
    REAGENT = "reagent"
    SOLVENT = "solvent"
    CATALYST = "catalyst"
    PRODUCT = "product"


# The roles the reaction-SMILES convention calls agents: present but not consumed into the product.
# One definition so `reaction_smiles` (shows them) and `transformation_smiles` (omits them) agree.
_AGENT_ROLES = frozenset({Role.SOLVENT, Role.CATALYST})


class RoleSpecies(BaseModel):
    """The canonical structures of each role two runs are compared on — a projection, not a charge.

    **Which roles, defined once.** `product` is not here: a changed product is a different
    transformation, which is the grouping layer's business rather than a condition the chemist
    turned. `memory.progression` diffs exactly these fields and `reaction_records.species` stores
    exactly these fields, so the campaign note mined from `OrdReaction`s and the turn-time
    comparison read from stored rows cannot disagree about which roles count.

    **A projection rather than the component list**: structures only, no amounts and no order,
    because amounts are optional on `Component` and diffing them reports a change whenever one run
    happened to record a mass and its neighbour did not. So a row stays a serving copy of what the
    source said, never a second transcription of it. An empty list is the record saying the run
    used nothing in that role, which is a real answer (`memory.progression.species_change`); a
    record with no projection at all is `None` on the record, never an instance of this with four
    empty lists.
    """

    model_config = ConfigDict(frozen=True)

    reactant: list[str] = Field(default_factory=list)
    reagent: list[str] = Field(default_factory=list)
    solvent: list[str] = Field(default_factory=list)
    catalyst: list[str] = Field(default_factory=list)

    def of(self, role: Role) -> frozenset[str]:
        """One role's structures as a set, which is the form every comparison wants."""
        return frozenset(getattr(self, role.value))


class _Charged(BaseModel):
    """What a record says about one species apart from its identity: role, amounts, attributes.

    Shared by `Component` (a species with a structure) and `UnstructuredComponent` (a species the
    source names without one), because the two differ in exactly one thing — whether a structure
    was given — and everything else a source records about a charge is the same fact either way.
    """

    role: Role
    # Amounts are optional (an ELN may omit them), and kept in milligrams when known. `mass_mg`
    # feeds the charge sheet and scale in `ingest/eln/record.py`; the mass-balance check compares
    # element sets and reads neither field.
    amount_mmol: float | None = Field(default=None, ge=0.0)
    mass_mg: float | None = Field(default=None, ge=0.0)
    # Millilitres, for a species charged by volume (neat liquids, solvents). A separate field rather
    # than a conversion, since converting needs a density the record does not carry
    # (D-2026-08-26-a-transcription-may-not-infer-a-setpoint).
    volume_ml: float | None = Field(default=None, ge=0.0)
    # Whatever else the source recorded about this species (lot, supplier, equivalents, assay); see
    # `OrdReaction.attributes`.
    attributes: dict[str, str] = Field(default_factory=dict)


class Component(_Charged):
    """One chemical species in a reaction: its structure, role, and optional amount."""

    smiles: str = Field(min_length=1)


class UnstructuredComponent(_Charged):
    """A species the source **names** and gives no structure for — carried as named, never drawn.

    What is left when `ord_adapter`'s exact routes to a structure (SMILES, InChI, known reagent
    name) all fail but the source still named the species. `name` is the source's text verbatim, and
    no code path may turn it into a structure: a guess would propagate into fingerprints and
    citations. A reaction carrying one is `RecordTier.CITATION_ONLY`
    (D-2026-09-27-a-reaction-without-a-structure-is-citable-not-searchable).
    """

    name: str = Field(min_length=1)


class RecordTier(StrEnum):
    """Which evidence tier a transcribed reaction belongs to.

    `STRUCTURED` — every species has a structure: fingerprinted, labelled and reachable by structure
    and similarity search.

    `CITATION_ONLY` — at least one species is an `UnstructuredComponent`. Stored and citable for
    what it states, excluded from every structure and similarity search, since any fingerprint would
    describe a reaction nobody ran.
    """

    STRUCTURED = "structured"
    CITATION_ONLY = "citation-only"


class StructureNotGiven(ChemclawError):
    """A structure was asked of a reaction whose source gave none for one of its species.

    Raised by `reaction_smiles`/`transformation_smiles` on a citation-only record. A
    `ChemclawError`, so a path reaching it by mistake rejects one entry loudly rather than
    fingerprinting a different reaction.
    """


class StepKind(StrEnum):
    """The kind of action a procedure step performs (a coarse subset of ORD's actions).

    Deliberately small; the verbatim instruction is always kept on the step, so a coarse label loses
    nothing.
    """

    ADDITION = "addition"  # charge/add/dissolve a species into the vessel
    TEMPERATURE = "temperature"  # cool/heat/reflux/hold at a setpoint
    STIR = "stir"  # stir/age/hold for a duration
    WORKUP = "workup"  # quench/wash/extract/filter/dry/concentrate
    PURIFICATION = "purification"  # crystallize/chromatograph/distill/triturate
    CUSTOM = "custom"  # anything the classifier could not place


class ReactionStep(BaseModel):
    """One ordered action in a step-by-step procedure (an ORD input/condition/workup, flattened).

    `text` is the verbatim instruction — always preserved, so no detail is lost even when the
    coarse `kind` label or the parsed `temperature_c`/`duration_h` are absent. `components`
    are the species this step introduces (structured adapters can link them; free-text
    segmentation leaves them empty rather than guess a SMILES from prose).
    """

    index: int = Field(ge=1)
    kind: StepKind
    text: str = Field(min_length=1)
    components: list[Component] = Field(default_factory=list)
    temperature_c: float | None = None
    duration_h: float | None = Field(default=None, ge=0.0)


# Where a record's `performed_at` came from. "stated" is the source's own experiment date; "entry"
# is the entry's creation timestamp, filled in by the seam when the source has no date column.
DateSource = Literal["stated", "entry"]


class OutcomeClass(StrEnum):
    """How an experiment turned out.

    Failures do not recur across projects, so without an explicit marker the most valuable negative
    knowledge is lost. `INCONCLUSIVE` is distinct from `FAILURE`: an aborted, mis-charged or
    unassayed run carries no evidence about the chemistry.
    """

    SUCCESS = "success"
    FAILURE = "failure"
    INCONCLUSIVE = "inconclusive"


class Impurity(BaseModel):
    """One identified or observed impurity in a reaction outcome (gap KNW-2).

    For late-stage *process* development, impurity control is usually the point — the agent is
    instructed to answer about "yield, purity, impurities", but the canonical record carried only
    `yield_percent`, so every purity question could only ever be answered "the data is silent".

    Every descriptor is optional because ELNs report impurities inconsistently: sometimes a
    structure, often only a chromatographic name/RRT, usually an area%. Requiring any one of them
    would silently drop the rest at ingest, which is the failure this field exists to prevent.

    **`rrt` was named in this docstring for as long as it existed and had nowhere to go.** This
    paragraph said RRT is how an impurity is "often" identified, and
    `ingest/eln/warehouse/binding.py` said a site's analytics table carries "a chromatographic name
    or RRT far more often than a structure" — while the model held name, SMILES and area% and
    nothing else. So the one identifier a process chemist uses to say *which peak* fell to
    `attributes`, a `dict[str, str]` whose own docstring says it holds "strings, not values"
    (`D-2026-09-15-a-relation-with-no-legal-target-is-a-question-nobody-can-answer`). Two
    unresolved impurities at 0.11% and 0.19% are distinguishable by RRT and by nothing else here.
    """

    name: str | None = None
    smiles: str | None = None
    # Chromatographic area percent (HPLC/GC) — the number a process chemist actually tracks.
    area_percent: float | None = Field(default=None, ge=0.0, le=100.0)
    # Relative retention time: this peak's retention over the main peak's, on the method that ran.
    # Unitless and method-relative (the `analytical-method` note and `measured-by` edge carry the
    # method). Unbounded above; must be positive.
    rrt: float | None = Field(default=None, gt=0.0)

    @model_validator(mode="after")
    def _identifiable(self) -> "Impurity":
        """An impurity with neither a name nor a structure is not a record of anything.

        An RRT alone does not identify one: "the RRT 0.94 peak" is how a chemist names an unknown,
        so such a row belongs under that name (`unresolved_peak_name`) rather than as an unjoinable
        row.
        """
        if not self.name and not self.smiles:
            raise ValueError("an impurity needs at least a name or a SMILES")
        return self


def unresolved_peak_name(rrt: float) -> str:
    """The name an impurity known only by its retention time is recorded under.

    The remedy `Impurity._identifiable` prescribes, so adapters name an RRT-only row rather than
    drop it. One function because the form is the contract every source must share; `:g` renders
    `0.94` and `1` without a trailing `.0`.
    """
    return f"RRT {rrt:g} peak"


class OrdReaction(BaseModel):
    """A canonical reaction record: inputs, outcomes, headline conditions, provenance.

    `reaction_id` is the ELN's stable entry id (carried for idempotency and provenance).
    Inputs carry every non-product species (reactant/reagent/solvent/catalyst); outcomes
    are the products. Conditions are the few an ELN reliably records; richer setup is out
    of this subset until a consumer needs it.

    `inputs` and `outcomes` hold only species with a structure; a species the source named without
    one is in `unstructured`, whatever its role. The split is what keeps every structural reader
    honest without each of them having to know about the tier: a loop over `inputs` still meets
    only structures, and the one place a structure is *assembled* from the lists —
    `reaction_smiles`/`transformation_smiles` — refuses a citation-only record outright instead of
    returning the reaction with a species silently missing.
    """

    reaction_id: str = Field(min_length=1)
    inputs: list[Component] = Field(default_factory=list)
    outcomes: list[Component] = Field(default_factory=list)
    # Species the source named and gave no structure for, in any role (`UnstructuredComponent`).
    # Non-empty makes this record `RecordTier.CITATION_ONLY`.
    unstructured: list[UnstructuredComponent] = Field(default_factory=list)
    temperature_c: float | None = None
    time_h: float | None = Field(default=None, ge=0.0)
    yield_percent: float | None = Field(default=None, ge=0.0, le=100.0)
    provenance: str = Field(min_length=1)
    # When the experiment was actually run: the time axis for recency ranking, bi-temporal note
    # fields and `memory.chains` ordering. Optional because a source may not record it.
    performed_at: date | None = None
    # Where `performed_at` came from: `"stated"` by the source, or `"entry"` when
    # `adapter.DatedIngest` filled it from the entry's creation time, which may be days after the
    # bench work. Without this the weaker date would make `Progression.is_timeline()` claim runs are
    # in performed order.
    date_source: DateSource = "stated"
    # Outcome quality beyond yield: `purity_percent` is the headline assay figure and `impurities`
    # the profile behind it. Both optional, and both excluded from the reaction SMILES and
    # fingerprints as outcomes, not structure.
    purity_percent: float | None = Field(default=None, ge=0.0, le=100.0)
    impurities: list[Impurity] = Field(default_factory=list)
    # How the experiment turned out, and (for a failure) why in the chemist's own words.
    # `None` is "the source did not say", which is neither success nor `INCONCLUSIVE`
    # (D-2026-08-26-silence-is-not-a-successful-run): defaulting to success would make an
    # unstructured source report a 100% success rate.
    outcome_class: OutcomeClass | None = None
    failure_reason: str | None = None

    @model_validator(mode="after")
    def _failure_is_explained(self) -> "OrdReaction":
        """A recorded failure needs its reason, or it teaches nothing worth keeping.

        An unexplained failure looks like evidence while being unactionable.
        """
        if self.outcome_class is OutcomeClass.FAILURE and not (self.failure_reason or "").strip():
            raise ValueError("a reaction recorded as a failure must carry a failure_reason")
        return self

    # The project/campaign this experiment belongs to — the grouping key for the semantic
    # memory layer (a playbook distils patterns that recur across >=2 projects, plan 5.4).
    project: str | None = None
    # What this run was set up to test, in the chemist's own words (D-162). Optional and never
    # inferred: empty means "not recorded", not "no hypothesis".
    hypothesis: str | None = None
    # The detailed procedure, when recorded: `steps` is the ordered recipe (empty for headline-only
    # sources) and `procedure_text` the verbatim prose.
    steps: list[ReactionStep] = Field(default_factory=list)
    procedure_text: str | None = None
    # Everything the source recorded that this schema has no field for, as the source labelled it.
    # A declaratively bound source (`ingest.eln.warehouse`) maps a site's own tables, whose extra
    # columns no schema can name in advance; this keeps each such column a line of YAML rather than
    # a model change. Strings, not typed values, so the rendered note stays deterministic; a datum
    # that earns a real question earns a real field. Never chemistry: structures and fingerprints
    # ignore this entirely.
    attributes: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _roles_are_consistent(self) -> "OrdReaction":
        """Inputs must not be products, and outcomes must all be products (G4)."""
        if any(c.role == Role.PRODUCT for c in self.inputs):
            raise ValueError("an input component has role 'product'")
        if any(c.role != Role.PRODUCT for c in self.outcomes):
            raise ValueError("an outcome component is not a product")
        return self

    @model_validator(mode="after")
    def _has_inputs_and_a_product(self) -> "OrdReaction":
        """A reaction needs at least one input and one product, in either tier.

        Counted across structured and `unstructured` species, since a citation-only record may name
        its product without a structure.
        """
        if not self.inputs and all(c.role is Role.PRODUCT for c in self.unstructured):
            raise ValueError("a reaction needs at least one input component")
        if not self.outcomes and not any(c.role is Role.PRODUCT for c in self.unstructured):
            raise ValueError("a reaction needs at least one product")
        return self

    @property
    def tier(self) -> RecordTier:
        """`CITATION_ONLY` when any species was named without a structure, else `STRUCTURED`."""
        return RecordTier.CITATION_ONLY if self.unstructured else RecordTier.STRUCTURED

    def product_count(self) -> int:
        """How many products the source recorded, with or without a structure.

        `len(outcomes)` misses unstructured products; "exactly one product" is asked by
        `record._principal_product` and the headline yield.
        """
        return len(self.outcomes) + sum(1 for c in self.unstructured if c.role is Role.PRODUCT)

    def _require_structure(self) -> None:
        """Refuse to assemble a structure for a citation-only record (`StructureNotGiven`)."""
        if self.unstructured:
            names = ", ".join(repr(c.name) for c in self.unstructured)
            raise StructureNotGiven(
                f"reaction {self.reaction_id!r} is citation-only: the source gives no structure "
                f"for {names}, so no reaction SMILES exists for it and none may be assembled from "
                "the species that do have one"
            )

    @model_validator(mode="after")
    def _steps_are_ordered(self) -> "OrdReaction":
        """Step indices must be the contiguous sequence 1..n (a well-formed ordering, G4)."""
        if [s.index for s in self.steps] != list(range(1, len(self.steps) + 1)):
            raise ValueError("step indices must be contiguous starting at 1")
        return self

    def major_impurity(self) -> "Impurity | None":
        """The impurity a chemist would call the major one, or `None` when the record cannot say.

        Ranked by recorded `area_percent`. With no area% the list is unranked and naming one would
        be an artefact of export order, except that a single recorded impurity is trivially the
        major one. On the record because both the comparative table and the reaction note's
        frontmatter ask it.
        """
        ranked = [imp for imp in self.impurities if imp.area_percent is not None]
        if ranked:
            return max(ranked, key=lambda imp: imp.area_percent or 0.0)
        return self.impurities[0] if len(self.impurities) == 1 else None

    def step_components(self) -> list[Component]:
        """Every species introduced by a step (e.g. a mid-procedure reagent or a quench).

        Belongs to the procedure, not the reaction SMILES. The mass-balance check counts these as
        element sources, but they stay out of the fingerprinted reaction.
        """
        return [c for step in self.steps for c in step.components]

    def species(self, role: Role) -> frozenset[str]:
        """The canonical structures playing `role` in this run, a mid-procedure step's included.

        Step reagents are included because swapping one is a typical optimization change; canonical
        so two spellings of one molecule never look like a change.
        """
        return frozenset(
            standard_smiles(c.smiles)
            for c in [*self.inputs, *self.step_components()]
            if c.role == role
        )

    def role_species(self) -> "RoleSpecies":
        """Every compared role's species set at once — the projection a stored record carries."""
        return RoleSpecies(
            **{name: sorted(self.species(Role(name))) for name in RoleSpecies.model_fields}
        )

    def reaction_smiles(self) -> str:
        """The **record** form: `reactants>agents>products`, exactly as the chemist wrote it.

        Agents (solvent, catalyst) in the middle slot and raw component SMILES, because this is what
        is displayed. Not the fingerprinted string: DRFP folds the agent slot back onto the
        reactants, so fingerprints use `transformation_smiles`.

        Raises `StructureNotGiven` on a citation-only record rather than leaving the unnamed species
        out.
        """
        self._require_structure()
        agents = ".".join(c.smiles for c in self.inputs if c.role in _AGENT_ROLES)
        left = ".".join(c.smiles for c in self.inputs if c.role not in _AGENT_ROLES)
        right = ".".join(c.smiles for c in self.outcomes)
        return f"{left}>{agents}>{right}"

    def transformation_smiles(self) -> str:
        """The **fingerprint** form: `reactants>>products`, agent-slot species left out entirely.

        DRFP keeps the symmetric difference of the two sides, so a solvent or catalyst on the left
        survives whole and dominates similarity with the variable being optimized; dropped, two runs
        of one coupling in different solvents score as the same transformation. Reagents stay: they
        participate stoichiometrically.

        Built from `standard_smiles` per species (the lenient helper, so one odd label does not
        abort ingestion), matching `STANDARDIZATION_VERSION` in `reaction_definition()`.

        Raises `StructureNotGiven` on a citation-only record, since a fingerprint of the structured
        subset would index a reaction nobody ran.
        """
        self._require_structure()
        reactants = (c for c in self.inputs if c.role not in _AGENT_ROLES)
        left = ".".join(standard_smiles(c.smiles) for c in reactants)
        right = ".".join(standard_smiles(c.smiles) for c in self.outcomes)
        return f"{left}>>{right}"

    def compounds(self) -> list[Component]:
        """Every structured component (inputs + outcomes), for per-compound indexing.

        `unstructured` is excluded: this list is read as structures.
        """
        return [*self.inputs, *self.outcomes]
