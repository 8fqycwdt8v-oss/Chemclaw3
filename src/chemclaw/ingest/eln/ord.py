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

from chemclaw.core.chem import standard_smiles


class Role(StrEnum):
    """A component's role in the reaction (a subset of ORD's reaction roles)."""

    REACTANT = "reactant"
    REAGENT = "reagent"
    SOLVENT = "solvent"
    CATALYST = "catalyst"
    PRODUCT = "product"


# The roles the reaction-SMILES convention calls agents: present in the flask, not consumed into
# the product skeleton. One definition, because `reaction_smiles` and `transformation_smiles` must
# agree on which species the middle slot names — one showing them and the other omitting them is
# the whole distinction between the two methods.
_AGENT_ROLES = frozenset({Role.SOLVENT, Role.CATALYST})


class Component(BaseModel):
    """One chemical species in a reaction: its structure, role, and optional amount."""

    smiles: str = Field(min_length=1)
    role: Role
    # Amounts are optional (an ELN may omit them), and kept in milligrams when known.
    #
    # This used to say "mass drives the mass-balance and green-chemistry checks". It does not
    # drive the mass-balance check: `ingest/eln/validate.py` reads neither field — it compares
    # element *sets*, which is the strongest sound check available without stoichiometric
    # coefficients. Nor could it today, even if the check were written: measured across every
    # shipped fixture, **no outcome carries a mass at all** (inputs do), so a
    # products-cannot-outweigh-inputs check would be a no-op on the whole corpus. `mass_mg` is
    # read by `ingest/eln/note.py` for the charge sheet and the note's scale, which is real.
    amount_mmol: float | None = Field(default=None, ge=0.0)
    mass_mg: float | None = Field(default=None, ge=0.0)
    # Whatever else the source recorded about this species — a lot number, a supplier, an
    # equivalents figure, an assay. See `OrdReaction.attributes` for why this is a bag of strings
    # and not a set of fields.
    attributes: dict[str, str] = Field(default_factory=dict)


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
    # What this run was set up to test, in the chemist's own words (D-162). Process development is
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
    # Everything the source recorded that this schema has no field for, as the source labelled it.
    #
    # It exists because of what a declaratively-bound source is: `ingest.eln.warehouse` maps a
    # site's own tables onto this model from a YAML binding, and no schema written today can name
    # the columns a corporate ELN will carry — a lot number, an equivalents figure, an assay, a
    # vessel id, whichever of a dozen child tables the site keeps. Without somewhere for them to
    # land, each newly-interesting column costs an edit to this model, to `eln.note` and to their
    # tests; with it, that column is a line of YAML. That is the whole trade, and it is the reason
    # this field is here rather than a set of typed ones.
    #
    # **Strings, not values.** These are unmodelled by definition, so there is no type to validate
    # against and no unit to normalise to. Stringifying keeps the note body deterministic (it is
    # rendered, and amendment detection compares bodies byte-for-byte) and keeps this from becoming
    # a second, untyped schema competing with the fields above. A datum that earns a real question
    # earns a real field, in its own change.
    #
    # **Never chemistry.** `reaction_smiles`, `transformation_smiles` and both fingerprint paths
    # ignore this entirely — a structure reaching the corpus through an unvalidated bag of strings
    # is exactly the failure the typed fields exist to prevent.
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
        """The **record** form: `reactants>agents>products`, exactly as the chemist wrote it.

        Three-part because that is what the reaction-SMILES convention has always meant by an
        agent — a species present in the reaction but not consumed into the product skeleton — so
        a note body, a campaign step list and a playbook's representative reaction all show the
        solvent and the catalyst in the slot that says what they are. Raw component SMILES for the
        same reason (D-2026-07-31): what is displayed should be what was recorded, and the
        standardized spellings exist to be *keys*, not to be read.

        **This is not the string that is fingerprinted, and believing that it was is what made a
        previous fix a no-op.** `DrfpEncoder.internal_encode` begins by folding the agent slot back
        onto the reactants (`sides[0] += "." + sides[1]`), so `A.B>solvent>C` and `A.B.solvent>>C`
        produce byte-identical bits — moving the solvent here changed the notation and nothing
        else. What the fingerprints index is `transformation_smiles`; see it for what the agent
        slot now actually does.
        """
        agents = ".".join(c.smiles for c in self.inputs if c.role in _AGENT_ROLES)
        left = ".".join(c.smiles for c in self.inputs if c.role not in _AGENT_ROLES)
        right = ".".join(c.smiles for c in self.outcomes)
        return f"{left}>{agents}>{right}"

    def transformation_smiles(self) -> str:
        """The **fingerprint** form: `reactants>>products`, agent-slot species left out entirely.

        Solvent and catalyst are dropped rather than moved, because dropping them is the only
        thing DRFP can see. DRFP shingles each side and keeps the symmetric difference, so a
        species that appears only on the left — which every solvent and every catalyst does —
        survives that difference whole and contributes a large, nearly constant block of set bits.
        The solvent is often the largest fragment present and is present in every run, so
        similarity was dominated by the variable process development is usually *optimizing*: two
        runs of one coupling in THF and in 2-MeTHF scored 0.82 against each other, less than two
        unrelated reactions sharing a solvent. Excluded, the same pair scores 1.0 — they are the
        same transformation, which is what campaign grouping (`memory.optimization`) and
        `similar_reactions` are asking about. Conditions are recorded beside the note, not inside
        the structure.

        Reagents stay on the left: a base or an oxidant participates stoichiometrically and is part
        of what the transformation *is*.

        `standard_smiles` per species, because "the same compound" is what a fingerprint row should
        be keyed on and `STANDARDIZATION_VERSION` is already folded into `reaction_definition()` —
        a claim the reaction rows did not honour while this built the string from raw `smiles`. The
        lenient helper, not the strict one: an ELN drop with one odd label must not abort ingestion
        (a genuinely unparseable reaction is caught downstream by `drfp_bitstring`).
        """
        reactants = (c for c in self.inputs if c.role not in _AGENT_ROLES)
        left = ".".join(standard_smiles(c.smiles) for c in reactants)
        right = ".".join(standard_smiles(c.smiles) for c in self.outcomes)
        return f"{left}>>{right}"

    def compounds(self) -> list[Component]:
        """Every distinct component (inputs + outcomes), for per-compound indexing."""
        return [*self.inputs, *self.outcomes]
