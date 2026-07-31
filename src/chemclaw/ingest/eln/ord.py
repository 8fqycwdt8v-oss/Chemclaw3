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

from datetime import date
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


class OutcomeClass(StrEnum):
    """How an experiment turned out (gap KNW-3).

    Nothing previously marked an experiment as failed, and the distillation is structurally biased
    against failures: `find_playbook_candidates` distils what *recurs* across projects, and failures
    do not recur — they get abandoned after one attempt. So "don't try X, we did, it decomposed on
    scale" — the most valuable and most systematically lost knowledge in process development — had
    nowhere to live.

    `INCONCLUSIVE` is deliberately distinct from `FAILURE`: a run that was aborted, mis-charged, or
    never assayed carries no evidence about the chemistry, and collapsing it into "failure" would
    teach the corpus something untrue.
    """

    SUCCESS = "success"
    FAILURE = "failure"
    INCONCLUSIVE = "inconclusive"


class Impurity(BaseModel):
    """One identified or observed impurity in a reaction outcome (gap KNW-2).

    For late-stage *process* development, impurity control is usually the point — the agent is
    instructed to answer about "yield, purity, impurities", but the canonical record carried only
    `yield_percent`, so every purity question could only ever be answered "the data is silent".

    All three descriptors are optional because ELNs report impurities inconsistently: sometimes a
    structure, often only a chromatographic name/RRT, usually an area%. Requiring any one of them
    would silently drop the rest at ingest, which is the failure this field exists to prevent.
    """

    name: str | None = None
    smiles: str | None = None
    # Chromatographic area percent (HPLC/GC) — the number a process chemist actually tracks.
    area_percent: float | None = Field(default=None, ge=0.0, le=100.0)

    @model_validator(mode="after")
    def _identifiable(self) -> "Impurity":
        """An impurity with neither a name nor a structure is not a record of anything."""
        if not self.name and not self.smiles:
            raise ValueError("an impurity needs at least a name or a SMILES")
        return self


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
    # When the experiment was actually run (gap KNW-1). Without it the largest note class in the
    # system has no time axis at all: reaction evidence cannot be recency-ranked, F10-G2's
    # bi-temporal note fields have nothing to be populated from, and `memory.chains` has no
    # fallback ordering when the product->reactant graph is cyclic. Optional because a source may
    # genuinely not record it, never because we do not care.
    performed_at: date | None = None
    # Outcome quality beyond yield (gap KNW-2). `purity_percent` is the headline assay/area figure
    # for the product; `impurities` is the profile behind it. Both optional — an early-route entry
    # may report yield only — and both deliberately excluded from the reaction SMILES and every
    # fingerprint: they are *outcomes*, not structure.
    purity_percent: float | None = Field(default=None, ge=0.0, le=100.0)
    impurities: list[Impurity] = Field(default_factory=list)
    # How the experiment turned out, and (for a failure) why in the chemist's own words. Defaults
    # to SUCCESS so every existing record and every source that does not report it keeps today's
    # meaning — the field adds the ability to say "this failed", it does not reinterpret silence.
    outcome_class: OutcomeClass = OutcomeClass.SUCCESS
    failure_reason: str | None = None

    @model_validator(mode="after")
    def _failure_is_explained(self) -> "OrdReaction":
        """A recorded failure needs its reason, or it teaches nothing worth keeping.

        The entire value of a negative result is *why* it failed; an unexplained one would enter
        the corpus as an unactionable "someone tried this once", which is worse than absent because
        it looks like evidence.
        """
        if self.outcome_class is OutcomeClass.FAILURE and not (self.failure_reason or "").strip():
            raise ValueError("a reaction recorded as a failure must carry a failure_reason")
        return self

    # The project/campaign this experiment belongs to — the grouping key for the semantic
    # memory layer (a playbook distils patterns that recur across >=2 projects, plan 5.4).
    project: str | None = None
    # What this run was set up to test, in the chemist's own words (D-157). Process development is
    # a sequence of hypotheses, not a screen: "does dropping to 60 °C keep the yield and kill the
    # des-bromo impurity" is the *reason* a run exists, and without it the record shows only that
    # a condition moved, leaving the agent to invent a motive or ignore the question. Optional and
    # never inferred — a source that does not capture intent leaves this empty, which reads as
    # "not recorded", not as "no hypothesis".
    hypothesis: str | None = None
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
