"""The shape of a prescriptive experiment design: one envelope for a single run and for a plate.

**A single experiment is a design with one arm and no factors**; an HTE screen is the same
object with factors, levels, N arms and a layout, so everything downstream is written once.

Class docstrings here are short on purpose: pydantic ships them as JSON-schema descriptions in
every tool schema, so rationale lives in `#` comments.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from chemclaw.core.ids import stable_hash
from chemclaw.science.labels.vocabulary import SpeciesRole

#: Every `SpeciesRole` field is declared through this: an explicit description replaces the enum's
#: long class docstring, which pydantic would otherwise inline into every tool schema per use.
_ROLE_FIELD = Field(
    default=SpeciesRole.UNKNOWN,
    description=(
        "starting-material, product, reagent, solvent, catalyst, ligand, base, additive, unknown"
    ),
)

# The identifier stem every design carries. `design-<hash>` reads the way `campaign-<hash>` and
# `reaction-<id>` do, so a citation in prose is recognisable without a lookup.
DESIGN_ID_PREFIX = "design"

#: `screen` is a fixed array run as a batch; `campaign` expects to be re-asked after results
#: (the BO loop) and may ship a first round that does not cover its factor space.
DesignMode = Literal["single", "screen", "campaign"]

#: Where a field in the structured request came from. `stated` obliges a verbatim quote; see
#: `RequestField`.
FieldBasis = Literal["stated", "inferred", "absent"]

#: How severely a failed check bears on the design. A `blocker` is refused at draft time; a
#: `warning` is stored and shown; a `note` is context.
CheckSeverity = Literal["blocker", "warning", "note"]

#: Which checks mean anything about a design. A `request` holds only the structured ask, so a
#: question about its charge table is not "failing" — it has not been asked yet.
CheckStage = Literal["request", "protocol"]

#: What a citation is. Kept apart because a reader's next move differs for each: open the run,
#: re-run the tool, open the note.
EvidenceKind = Literal["precedent", "tool", "note", "record", "observation"]

#: Who wrote a revision. The whole point of the revision table is that these are distinguishable.
AuthorKind = Literal["agent", "human"]

#: Lifecycle of a design. `requested` holds only a structured ask; `draft` holds a protocol nobody
#: has signed off; `approved` is a human's sign-off; `executed` means runs exist; `abandoned` is a
#: design deliberately not run, kept because a rejected design is evidence too.
DesignStatus = Literal["requested", "draft", "approved", "executed", "abandoned"]


class ProtocolStepKind(StrEnum):
    """What one step of a procedure does."""

    # Deliberately not `ingest.eln.ord.StepKind`: that is the record vocabulary a source may write,
    # and this prescriptive one adds sample, analyse and hold, which the ingest path cannot
    # interpret. The shared values are spelled identically so a design transcribed as a run maps
    # across.
    CHARGE = "charge"
    ADDITION = "addition"
    TEMPERATURE = "temperature"
    STIR = "stir"
    HOLD = "hold"
    SAMPLING = "sampling"
    ANALYSIS = "analysis"
    WORKUP = "workup"
    PURIFICATION = "purification"
    CUSTOM = "custom"


class RequestField(BaseModel):
    """One slot of the ask: its value, where it came from, and the words that said so."""

    # `stated` requires the chemist's verbatim words in `quote`; `inferred` is chemical judgment;
    # `absent` means the text did not say. Caller guidance lives in
    # `structure_experiment_request`, since a docstring here would ship once per use in the schema.

    value: str = ""
    basis: FieldBasis = "absent"
    # The verbatim span, checked against the supplied text by
    # `agent.protocol_design_tools.require_quotes_are_verbatim`.
    quote: str = ""

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    @model_validator(mode="after")
    def _stated_is_quoted(self) -> RequestField:
        """A stated value carries the words that state it; an absent one carries no value."""
        if self.basis == "stated" and not self.quote.strip():
            raise ValueError(
                "basis='stated' needs the verbatim quote that states it; use 'inferred' when the "
                "value is your own judgment rather than the chemist's words"
            )
        if self.basis == "absent" and self.value.strip():
            raise ValueError("basis='absent' means there is no value; leave `value` empty")
        return self


class RequestedComponent(BaseModel):
    """One species the chemist named, as written and as resolved."""

    name_as_written: str = Field(min_length=1)
    # Empty when the name could not be resolved. That is a *finding*, not a reason to guess a
    # structure — `checks.components_resolve` reports it and the chemist supplies the structure.
    smiles: str = ""
    role: SpeciesRole = _ROLE_FIELD
    # `resolve_compound`'s answer, or a note saying why there is none.
    resolution: str = ""

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)


class ExperimentRequest(BaseModel):
    """The chemist's ask, structured."""

    # Fill from the chemist's own words: anything unsaid is a marked inference or `absent`, never a
    # silent default (guidance in `structure_experiment_request`).

    # One line a human recognises the design by. Not derived from the objective, because a chemist
    # names a piece of work after the step it belongs to ("SM-3 Suzuki, deactivated aryl chloride").
    title: str = Field(min_length=1, max_length=200)
    goal: str = Field(min_length=1)
    mode: DesignMode = "single"
    # `A.B>>C` when the ask is a transformation. Empty for an ask that is not one (a stability
    # study, a solubility screen), which is why this is not required.
    reaction_smiles: str = ""
    components: list[RequestedComponent] = Field(default_factory=list, max_length=200)
    # Named, directional. Reuses the wording `science.bo.problem.Objective` uses so a design that
    # becomes a BO campaign needs no translation.
    objectives: list[str] = Field(default_factory=list, max_length=50)
    # The four limits that decide whether a design is runnable at all, each with its basis.
    scale: RequestField = Field(default_factory=RequestField)
    plate_format: RequestField = Field(default_factory=RequestField)
    max_runs: RequestField = Field(default_factory=RequestField)
    deadline: RequestField = Field(default_factory=RequestField)
    # Hard exclusions. A reagent here that appears anywhere in the design is a `blocker`.
    forbidden: list[str] = Field(default_factory=list, max_length=200)
    # What the chemist says has already been tried. Free text on purpose: it is a pointer for the
    # precedent search, not a record, and structuring it would be inventing runs.
    prior_work: str = ""
    project: str = ""
    # Everything else the text said that no slot above holds, so nothing is lost in structuring.
    notes: str = ""

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)


class FactorLevel(BaseModel):
    """One setting a factor can take."""

    label: str = Field(min_length=1)
    # For a categorical factor: the structure behind the label, so `checks` can screen it and a BO
    # campaign can featurize it.
    smiles: str = ""
    # For a continuous factor.
    value: float | None = None
    unit: str = ""
    # Why this level and not another. The single most useful sentence on a screening plate.
    rationale: str = ""

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)


class Factor(BaseModel):
    """One thing the screen varies."""

    name: str = Field(min_length=1, pattern=r"^[a-z][a-z0-9_]*$")
    kind: Literal["categorical", "continuous"]
    role: SpeciesRole = _ROLE_FIELD
    # Bounded like every sibling collection: the design is browser-supplied.
    levels: list[FactorLevel] = Field(min_length=2, max_length=96)
    unit: str = ""

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    @model_validator(mode="after")
    def _levels_match_kind(self) -> Factor:
        """A continuous factor's levels are numbers; a categorical factor's labels are distinct."""
        labels = [level.label for level in self.levels]
        if len(set(labels)) != len(labels):
            raise ValueError(f"factor {self.name!r} repeats a level label")
        if self.kind == "continuous":
            missing = [level.label for level in self.levels if level.value is None]
            if missing:
                raise ValueError(
                    f"factor {self.name!r} is continuous, so every level needs a numeric `value`; "
                    f"missing on: {', '.join(missing)}"
                )
        return self


class Setpoints(BaseModel):
    """The physical conditions one arm is run at."""

    # Deliberately not `kg.note.ProcessConditions`, which is recorded and mixes setpoints with
    # outcomes: a plan's temperature is an instruction, a plan's yield a prediction.
    temperature_c: float | None = None
    time_h: float | None = Field(default=None, gt=0.0)
    pressure_bar: float | None = Field(default=None, gt=0.0)
    atmosphere: str = ""
    concentration_molar: float | None = Field(default=None, gt=0.0)
    solvent: str = ""
    ph: float | None = None

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)


class ChargeLine(BaseModel):
    """One row of the charge table: what to weigh out."""

    component: str = Field(min_length=1)
    smiles: str = ""
    role: SpeciesRole = _ROLE_FIELD
    equivalents: float | None = Field(default=None, ge=0.0)
    amount_mmol: float | None = Field(default=None, ge=0.0)
    mass_mg: float | None = Field(default=None, ge=0.0)
    volume_ml: float | None = Field(default=None, ge=0.0)
    # Exactly one line in a protocol carries this. It is what every equivalent is relative to, and
    # `checks.charge_is_consistent` refuses a table with none or with two.
    limiting: bool = False
    note: str = ""

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)


class ProtocolStep(BaseModel):
    """One instruction, in order."""

    index: int = Field(ge=1)
    kind: ProtocolStepKind
    text: str = Field(min_length=1)
    # Which charge lines this step consumes, by `component`. Empty is legitimate — a stir step
    # charges nothing — and is not the same as "we did not say".
    components: list[str] = Field(default_factory=list)
    temperature_c: float | None = None
    duration_h: float | None = Field(default=None, ge=0.0)

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)


class Analytic(BaseModel):
    """One measurement the run has to produce."""

    name: str = Field(min_length=1)
    # When to take it. Free text (`t=0, 1 h, on completion`) because the useful answers are not an
    # enum and forcing one would push the real instruction into a `notes` field.
    timing: str = ""
    method: str = ""
    # Which objective this measurement answers. A screen whose objective nothing measures is the
    # commonest way a plate comes back unanswerable, and `checks.objectives_are_measured` says so.
    measures: list[str] = Field(default_factory=list)

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)


class ExpectedOutcome(BaseModel):
    """What you expect, and on what grounds."""

    # A prediction, never a promise. `basis` is rendered beside the number everywhere so a figure
    # cannot travel without the reason it was believed.

    # A prediction, never a promise. It is rendered beside `basis` everywhere so a number cannot
    # travel without the reason it was believed.
    yield_percent: float | None = Field(default=None, ge=0.0, le=100.0)
    selectivity: str = ""
    # `precedent` (a run like this gave it), `predicted` (a tool said so), `assumed` (neither).
    basis: Literal["precedent", "predicted", "assumed"] = "assumed"
    detail: str = ""

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)


class EvidenceRef(BaseModel):
    """One citation behind a decision in this design."""

    kind: EvidenceKind
    # A note id, a `reaction-<id>`, a `source:doc_id`, or a tool result reference. Empty only for a
    # `tool` citation whose result was not stored.
    ref: str = ""
    tool: str = ""
    summary: str = Field(min_length=1)
    # Dotted paths into the design this citation supports, so a reader sees the reason beside the
    # number.
    supports: list[str] = Field(default_factory=list)

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)


class UncitedPrecedent(BaseModel):
    """One run the record holds that resembles this design and that the design does not cite.

    Beside `RecordedFailure` and for the same layering reason: the fingerprint index answers in
    `science.fingerprints`'s vocabulary, `agent/` reduces what it found, and the check stays pure
    over its arguments.

    **`searched` is the honest half and it is not optional.** An empty list of hits means three
    different things — nobody looked, the index is empty or mid-rebuild, or the record genuinely
    holds nothing like this — and `FingerprintSearch` exists in the first place because a chemist
    told "no precedent" over an unindexed corpus is worse than one told nothing. A check cannot
    re-derive that distinction from a list, so the caller carries it across.
    """

    #: The reaction record's id, so a chemist can open what is being pointed at.
    id: str = Field(min_length=1)
    #: How close it is, on the same Tanimoto scale `similar_reactions` reports.
    similarity: float = Field(ge=0.0, le=1.0)
    #: What the record calls it.
    label: str = ""

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)


class ProtocolBody(BaseModel):
    """What every arm of the design shares."""

    setpoints: Setpoints = Field(default_factory=Setpoints)
    charge: list[ChargeLine] = Field(default_factory=list, max_length=500)
    steps: list[ProtocolStep] = Field(default_factory=list, max_length=500)
    analytics: list[Analytic] = Field(default_factory=list, max_length=100)
    # In-process controls: what to check *during* the run and what to do about it.
    in_process_controls: list[str] = Field(default_factory=list, max_length=100)
    # What `screen_hazards` / `screen_genotoxic_alerts` / `ich_impurity_limit` said, in the
    # chemist's words. This system flags, it never certifies — see `safety`'s own tools.
    hazards: list[str] = Field(default_factory=list, max_length=100)
    waste: str = ""
    expected: ExpectedOutcome = Field(default_factory=ExpectedOutcome)

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)


class ProtocolArm(BaseModel):
    """One runnable set of conditions."""

    # Stable within a design: the well label, CSV row key and result id. Not an index, because a
    # randomised run order reorders arms.
    arm_id: str = Field(min_length=1, pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
    # `{factor_name: level_label}`. Empty for a single experiment. Bounded for the reason
    # `Factor.levels` is: this is the per-arm half of the same unbounded document.
    levels: dict[str, str] = Field(default_factory=dict, max_length=64)
    # Only what differs from `ProtocolBody`. A screen whose arms each restated the whole body would
    # be N protocols rather than one design, and a reader could not see what is being varied.
    setpoints: Setpoints | None = None
    # No per-arm charge override: an arm varying an amount declares it as a continuous factor. A
    # control that genuinely differs says so in `note`. Controls are excluded from the coverage
    # check and rendered apart on the plate.
    control: Literal["", "positive", "negative", "blank"] = ""
    replicate_of: str = ""
    note: str = ""

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)


class Well(BaseModel):
    """One position on the plate."""

    label: str = Field(min_length=1)
    row: int = Field(ge=0)
    column: int = Field(ge=0)
    arm_id: str = Field(min_length=1)
    # 1-based position in the order the arms are to be run, which is not the well order when the
    # design is randomised against session drift.
    run_order: int = Field(ge=1)

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)


class PlateLayout(BaseModel):
    """Where each arm sits and in what order it is run."""

    plate_format: int = Field(gt=0)
    rows: int = Field(gt=0)
    columns: int = Field(gt=0)
    wells: list[Well] = Field(default_factory=list, max_length=1536)
    randomized: bool = False
    seed: int | None = None

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    @model_validator(mode="after")
    def _randomized_is_reproducible(self) -> PlateLayout:
        """A shuffled run order carries the seed that produced it.

        On the model rather than only in `place()`, because a layout posted by a browser bypasses
        `place()` and reproducibility is the point of recording a randomisation.
        """
        if self.randomized and self.seed is None:
            raise ValueError("a randomized layout needs a seed so the run order can be reproduced")
        return self


class RecordedFailure(BaseModel):
    """One `failure-mode` note from the corpus, reduced to what a check decides with.

    **Here rather than in `memory/failure.py`, because `protocols` may import only `core` and
    `science`** (`tests/test_layering.py`), and that restriction is right: a deterministic check
    must not depend on a corpus being loadable. So the knowledge graph answers in its own
    vocabulary, `agent/` reduces what it found into this, and the check stays pure over its
    arguments — the same division `forbidden_absent` already has with `request.forbidden`.

    Two fields and no more: what a refusal has to name is the note to go and read and the sentence
    saying what happened. The refuted id and the molecule are how the *lookup* found it, not what a
    chemist needs told.
    """

    #: The failure note's id, so a chemist can open what the check is pointing at.
    id: str = Field(min_length=1)
    #: The observation itself — the value of a negative result is entirely in this text.
    summary: str = ""

    model_config = ConfigDict(frozen=True, extra="forbid")


class ProtocolCheck(BaseModel):
    """One deterministic verdict about the design."""

    # Computed by `checks`, never supplied by a caller, so this model is in no tool's argument
    # schema.
    check_id: str = Field(min_length=1)
    severity: CheckSeverity
    passed: bool
    detail: str = ""

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)


class ExperimentDesign(BaseModel):
    """A complete experiment design: one arm for a single run, N arms and factors for a plate."""

    request: ExperimentRequest
    base: ProtocolBody = Field(default_factory=ProtocolBody)
    # Every collection is bounded because a whole design arrives from a browser
    # (`POST /protocols/{id}/revisions`). The ceilings bound path counts, which set the diff cost;
    # free text is bounded only by the body cap. `max_length=1536` is exactly the largest plate this
    # system knows. Numbers are what a chemist could plausibly mean, not what the machine survives.
    factors: list[Factor] = Field(default_factory=list, max_length=50)
    arms: list[ProtocolArm] = Field(default_factory=list, max_length=1536)
    layout: PlateLayout | None = None
    evidence: list[EvidenceRef] = Field(default_factory=list, max_length=500)

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    @model_validator(mode="after")
    def _names_resolve_and_steps_are_ordered(self) -> ExperimentDesign:
        """Ids are unique, `replicate_of` names a real arm, and steps are numbered 1..n."""
        # These structural rules live here, not in `checks`: a dangling `replicate_of` or duplicate
        # factor names would let an arm or factor escape the checks entirely.
        ids = [arm.arm_id for arm in self.arms]
        if len(set(ids)) != len(ids):
            raise ValueError("arm_id repeats; each arm needs its own id")
        names = [factor.name for factor in self.factors]
        if len(set(names)) != len(names):
            raise ValueError("a factor name repeats; each factor needs its own name")
        dangling = sorted({arm.replicate_of for arm in self.arms if arm.replicate_of} - set(ids))
        if dangling:
            raise ValueError(f"replicate_of names no arm in this design: {', '.join(dangling)}")
        # A replicate chain must terminate: following each chain to its end catches cycles of any
        # length, which would otherwise exempt every arm in the ring from the distinctness and
        # coverage checks.
        parent = {arm.arm_id: arm.replicate_of for arm in self.arms}
        looped: list[str] = []
        for start in parent:
            seen, node = {start}, parent[start]
            while node:
                if node in seen:
                    looped.append(start)
                    break
                seen.add(node)
                node = parent.get(node, "")
        if looped:
            raise ValueError(
                f"replicate_of forms a cycle through these arms: {', '.join(sorted(looped))}. "
                "A replicate names the arm it repeats, and that chain has to end at an arm that "
                "is not itself a replicate"
            )
        # A replicate must run the same conditions: averaging two different conditions would report
        # assay noise as the answer.
        by_id = {arm.arm_id: arm for arm in self.arms}
        # Compare the *effective* conditions (`setpoints_for`), the definition
        # `checks.arms_are_distinct` uses, so the remedy it prescribes is never refused here.
        differing = [
            arm.arm_id
            for arm in self.arms
            if arm.replicate_of
            and (arm.levels, self.setpoints_for(arm))
            != (by_id[arm.replicate_of].levels, self.setpoints_for(by_id[arm.replicate_of]))
        ]
        if differing:
            raise ValueError(
                "these arms are marked as replicates but run different conditions from the arm "
                f"they name: {', '.join(differing)}. Clear `replicate_of` on an arm that varies "
                "something — a replicate is the same conditions run again"
            )
        expected = list(range(1, len(self.base.steps) + 1))
        if [step.index for step in self.base.steps] != expected:
            raise ValueError("steps must be numbered 1..n in order")
        return self

    @property
    def has_protocol(self) -> bool:
        """Whether this design says what to do, rather than only what is being asked for.

        The one definition, read by `checks.is_a_protocol`, the intake, the edit route and
        `render.summarise`.
        """
        return bool(self.arms or self.base.steps or self.base.charge)

    @property
    def is_single_experiment(self) -> bool:
        """Whether this is one experiment rather than a screen: one distinct arm and nothing varied.

        The one definition for every check. Not `request.mode` (the ask is tied to nothing the
        design is), and not the raw arm count: a one-arm design with factors is a screen's first
        round, and an experiment in triplicate (arms carrying `replicate_of`) is still one
        experiment.
        """
        return len(self.distinct_arms) <= 1 and not self.factors

    @property
    def distinct_arms(self) -> list[ProtocolArm]:
        """The arms that are their own conditions: every arm that is not a repeat of another.

        The validator guarantees a replicate matches its target, so this is the set of conditions
        tried.
        """
        return [arm for arm in self.arms if not arm.replicate_of]

    @property
    def is_plate(self) -> bool:
        """Whether the plate checks apply: **either** the shape or the ask says this is one.

        The union is deliberate: a many-arm design with a stale `single` ask and a one-arm design
        whose chemist said `screen` both need plate checks. Exempt only when nothing claims to be a
        plate.
        """
        return not self.is_single_experiment or self.request.mode != "single"

    def arm(self, arm_id: str) -> ProtocolArm | None:
        """The arm with this id, or `None`."""
        return next((a for a in self.arms if a.arm_id == arm_id), None)

    def setpoints_for(self, arm: ProtocolArm) -> Setpoints:
        """The arm's own setpoints over the shared body's, **field by field**.

        A field is stated when it is not the default (`None` for numbers, `""` for strings); an arm
        states what it changes and inherits the rest.
        """
        if arm.setpoints is None:
            return self.base.setpoints
        stated = {
            name: value
            for name, value in arm.setpoints.__dict__.items()
            if value is not None and value != ""
        }
        return self.base.setpoints.model_copy(update=stated)


#: What a stored revision is: `request` holds only a structured ask, `protocol` a whole design.
#: One table because they are the same document growing. Derived by `store.revision_kind`, never
#: declared by a caller.
RevisionKind = Literal["request", "protocol"]


class DesignRevision(BaseModel):
    """One immutable version of a design, and who wrote it."""

    design_id: str = Field(min_length=1)
    revision: int = Field(ge=1)
    kind: RevisionKind
    author_kind: AuthorKind
    author: str = ""
    # 0 on the first revision. Every later one names the revision it was derived from, which is
    # what makes a concurrent edit a 409 rather than a silent overwrite.
    parent_revision: int = Field(default=0, ge=0)
    change_note: str = ""
    design: ExperimentDesign
    checks: list[ProtocolCheck] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    @property
    def blockers(self) -> list[ProtocolCheck]:
        """The checks that failed at `blocker` severity."""
        return [c for c in self.checks if c.severity == "blocker" and not c.passed]


class StatusEvent(BaseModel):
    """One recorded lifecycle move: which revision somebody signed off on, and why.

    The header's `status` describes the *head*, and `store.advanced` moves it back to `draft` when
    a new revision lands on an approved or executed design — correctly, because an approval is a
    statement about a document and the document changed. That leaves exactly one question with no
    answer on the header row, and this is it: which revision a person actually approved, executed
    or abandoned. Only a deliberate move is recorded; an automatic demotion has no actor and no
    reason, and the revision that carries it is already in the history.
    """

    status: DesignStatus
    #: The head revision at the instant of the move — the document that was signed off on.
    revision: int = Field(ge=0)
    actor: str = ""
    reason: str = ""
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)


class DesignSummary(BaseModel):
    """One row of a listing: enough to choose which design to open."""

    design_id: str
    title: str
    mode: DesignMode
    status: DesignStatus
    project: str = ""
    opened_by: str = ""
    head_revision: int = 0
    arms: int = 0
    blockers: int = 0
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)


def design_id_for(request: ExperimentRequest, *, owner: str, salt: str = "") -> str:
    """The id a new design is filed under.

    Derived from the ask and the owner, so the same request restructured in the same session reaches
    the same design, while two chemists phrasing the same ask get separate designs. Together with
    the write path's ownership gate, nobody can overwrite another's design. `salt` deliberately
    opens a second design for the same ask. `owner` is required keyword-only so a caller cannot
    omit it.
    """
    identity = {
        "title": request.title.strip().lower(),
        "goal": request.goal.strip().lower(),
        "reaction": request.reaction_smiles.strip(),
        "mode": request.mode,
        "owner": owner,
        "salt": salt,
    }
    return f"{DESIGN_ID_PREFIX}-{stable_hash(identity, chars=12)}"


#: The design shape a tool accepts as an argument. Named so the schema description does not repeat
#: the whole model docstring on every turn.
DesignInput = Annotated[ExperimentDesign, Field(description="The complete experiment design.")]
