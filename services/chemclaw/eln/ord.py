"""The stable, ELN-agnostic reaction target schema (plan step 4.1).

An **ORD-inspired** pydantic subset — the canonical shape every layer above the ELN
integration knows (graph notes, fingerprint search, metrics). It is deliberately a subset
of the full Open Reaction Database proto: only the fields Chemclaw actually consumes
(structure, roles, amounts, the headline conditions and yield, provenance), so there is no
speculative schema. An ELN adapter maps its own format *into* this; nothing here knows any
ELN's quirks (G6).

Late-development recipes are **step-by-step** — charge, cool, add dropwise over time, age,
quench, extract, crystallize — not a single set of conditions. Mirroring ORD's ordered
`inputs` (with `addition_time`/`addition_order`) + `conditions` + `workups[]`, the schema
carries an ordered `steps` list and the raw `procedure_text`, so a detailed procedure is
represented and preserved rather than flattened to one headline temperature/time. The
flat headline fields remain the summary every existing consumer reads; `steps` is a
purely additive procedural overlay (it never feeds the reaction SMILES / fingerprints).
"""

from enum import StrEnum

from pydantic import BaseModel, Field, model_validator


class Role(StrEnum):
    """A component's role in the reaction (a subset of ORD's reaction roles)."""

    REACTANT = "reactant"
    REAGENT = "reagent"
    SOLVENT = "solvent"
    CATALYST = "catalyst"
    PRODUCT = "product"


class Component(BaseModel):
    """One chemical species in a reaction: its structure, role, and optional amount."""

    smiles: str = Field(min_length=1)
    role: Role
    # Amounts are optional (an ELN may omit them); mass drives the mass-balance and
    # green-chemistry checks, so it is kept in milligrams when known.
    amount_mmol: float | None = Field(default=None, ge=0.0)
    mass_mg: float | None = Field(default=None, ge=0.0)


class StepKind(StrEnum):
    """The kind of action a procedure step performs (a coarse subset of ORD's actions).

    Deliberately small: it labels a preserved instruction so the graph and metrics can
    reason about *what happens when* (an addition vs. a workup vs. a purification) without
    reproducing ORD's full `ReactionWorkup`/`ReactionConditions` type space. The verbatim
    instruction is always kept on the step, so a coarse label never loses information.
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


class OrdReaction(BaseModel):
    """A canonical reaction record: inputs, outcomes, headline conditions, provenance.

    `reaction_id` is the ELN's stable entry id (carried for idempotency and provenance).
    Inputs carry every non-product species (reactant/reagent/solvent/catalyst); outcomes
    are the products. Conditions are the few an ELN reliably records; richer setup is out
    of this subset until a consumer needs it.
    """

    reaction_id: str = Field(min_length=1)
    inputs: list[Component] = Field(min_length=1)
    outcomes: list[Component] = Field(min_length=1)
    temperature_c: float | None = None
    time_h: float | None = Field(default=None, ge=0.0)
    yield_percent: float | None = Field(default=None, ge=0.0, le=100.0)
    provenance: str = Field(min_length=1)
    # The project/campaign this experiment belongs to — the grouping key for the semantic
    # memory layer (a playbook distils patterns that recur across >=2 projects, plan 5.4).
    project: str | None = None
    # The detailed procedure, when the source records one. `steps` is the ordered recipe
    # (empty for sources that give only headline conditions); `procedure_text` is the raw
    # prose, kept verbatim so nothing a chemist wrote is dropped on ingest.
    steps: list[ReactionStep] = Field(default_factory=list)
    procedure_text: str | None = None

    @model_validator(mode="after")
    def _roles_are_consistent(self) -> "OrdReaction":
        """Inputs must not be products, and outcomes must all be products (G4)."""
        if any(c.role == Role.PRODUCT for c in self.inputs):
            raise ValueError("an input component has role 'product'")
        if any(c.role != Role.PRODUCT for c in self.outcomes):
            raise ValueError("an outcome component is not a product")
        return self

    @model_validator(mode="after")
    def _steps_are_ordered(self) -> "OrdReaction":
        """Step indices must be the contiguous sequence 1..n (a well-formed ordering, G4)."""
        if [s.index for s in self.steps] != list(range(1, len(self.steps) + 1)):
            raise ValueError("step indices must be contiguous starting at 1")
        return self

    def step_components(self) -> list[Component]:
        """Every species introduced by a step (e.g. a mid-procedure reagent or a quench).

        Distinct from `inputs`: a workup reagent (brine, drying agent) or a reagent added
        only partway through belongs to the procedure, not the reaction SMILES. The mass-
        balance check folds these into the available-element set so they never cause a
        false rejection, but they stay out of the fingerprinted reaction.
        """
        return [c for step in self.steps for c in step.components]

    def reaction_smiles(self) -> str:
        """Build the reaction SMILES (`inputs>>products`) for DRFP fingerprinting.

        All inputs (reactants, reagents, solvent, catalyst) go on the left, products on
        the right — the whole-reaction form DRFP expects.
        """
        left = ".".join(c.smiles for c in self.inputs)
        right = ".".join(c.smiles for c in self.outcomes)
        return f"{left}>>{right}"

    def compounds(self) -> list[Component]:
        """Every distinct component (inputs + outcomes), for per-compound indexing."""
        return [*self.inputs, *self.outcomes]
