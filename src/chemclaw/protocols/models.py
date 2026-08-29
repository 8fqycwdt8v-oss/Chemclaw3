"""The shape of a prescriptive experiment design — one envelope for a single run and for a plate.

The one thing to internalize: **a single experiment is a design with one arm and no factors.** An
HTE screen is the same object with factors, levels, N arms and a layout. Everything downstream —
the checks, the store, the renderer, the CSV export, the UI — is written once because of that.

Docstrings here are short on purpose. Pydantic turns a class docstring into the JSON-schema
`description` and `convert_to_openai_tool` ships it on every turn, so design rationale lives in `#`
comments (which do not ship) and only caller-facing guidance lives in the docstring. This is the
rule `docs/planning/BACKLOG.md` records after measuring `science/bo/problem.py` at 38% rationale.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from chemclaw.core.ids import stable_hash
from chemclaw.science.labels.vocabulary import SpeciesRole

#: Every `SpeciesRole` field in this module is declared through this, and the `description` is the
#: reason. Pydantic publishes a referenced enum's **class docstring** as the field's JSON-schema
#: description, and `convert_to_openai_tool` inlines rather than `$ref`s — so `SpeciesRole`'s
#: docstring (180 tokens arguing why the derived vocabulary is not `Role`, which is exactly the
#: right thing for a reader of `science/labels`) shipped once for every field that names it, three
#: times in one tool schema. An explicit description wins over the inherited one and says the only
#: thing a caller needs: the values.
_ROLE_FIELD = Field(
    default=SpeciesRole.UNKNOWN,
    description=(
        "starting-material, product, reagent, solvent, catalyst, ligand, base, additive, unknown"
    ),
)

# The identifier stem every design carries. `design-<hash>` reads the way `campaign-<hash>` and
# `reaction-<id>` do, so a citation in prose is recognisable without a lookup.
DESIGN_ID_PREFIX = "design"

#: Modes a design can be in. `screen` is a fixed up-front array a human runs as a batch; `campaign`
#: is a screen that expects to be re-asked after results arrive (the BO loop). They are one enum
#: rather than a boolean because the *checks* differ: a campaign is allowed to ship a first round
#: that does not cover its factor space, and a screen is not.
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

    # Deliberately *not* `ingest.eln.ord.StepKind`, for the reason `science.labels.vocabulary`
    # gives for not widening `Role`: that enum is the **record** vocabulary — the values a source
    # is allowed to state and a warehouse binding's `value_map` may write — and its members decide
    # how a recorded procedure is segmented. This is the **prescriptive** vocabulary, and it needs
    # three verbs a record has no reason to carry: an instruction to take a sample, an instruction
    # to run an analysis, and an instruction to hold. Widening the record enum to carry them would
    # let a tenant YAML file write a value the ingest path cannot interpret.
    #
    # The first six values are spelled identically to `StepKind`'s so a design that is later
    # transcribed as a run maps across without a translation table.
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

    # `basis="stated"` obliges the chemist's verbatim words in `quote`, checked against their
    # text; `inferred` is a value supplied from chemical judgment rather than from what they
    # wrote, and is expected rather than an admission; `absent` means the text did not say. The
    # whole honesty claim of the structured ask is this model, and the prose describing it was
    # the largest single item in the request schema: pydantic publishes a class docstring as the
    # JSON-schema description and `convert_to_openai_tool` inlines it once per *use*, so this one
    # shipped four times in one tool. The guidance now lives where a caller reads it, in
    # `structure_experiment_request`.

    value: str = ""
    basis: FieldBasis = "absent"
    # The verbatim span. Checked against the supplied text rather than trusted — see
    # `agent.protocol_design_tools.require_quotes_are_verbatim`. A paraphrase is the failure
    # this field exists to catch.
    quote: str = ""

    model_config = ConfigDict(frozen=True, extra="forbid")

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

    model_config = ConfigDict(frozen=True, extra="forbid")


class ExperimentRequest(BaseModel):
    """The chemist's ask, structured."""

    # Fill it from the chemist's own words: anything the text does not say is either a marked
    # inference or `absent`, never a silent default. The guidance is in
    # `structure_experiment_request`'s docstring rather than here, because pydantic would ship
    # this one again inside every schema that nests the model.

    # One line a human recognises the design by. Not derived from the objective, because a chemist
    # names a piece of work after the step it belongs to ("SM-3 Suzuki, deactivated aryl chloride").
    title: str = Field(min_length=1, max_length=200)
    goal: str = Field(min_length=1)
    mode: DesignMode = "single"
    # `A.B>>C` when the ask is a transformation. Empty for an ask that is not one (a stability
    # study, a solubility screen), which is why this is not required.
    reaction_smiles: str = ""
    components: list[RequestedComponent] = Field(default_factory=list)
    # Named, directional. Reuses the wording `science.bo.problem.Objective` uses so a design that
    # becomes a BO campaign needs no translation.
    objectives: list[str] = Field(default_factory=list)
    # The four limits that decide whether a design is runnable at all, each with its basis.
    scale: RequestField = Field(default_factory=RequestField)
    plate_format: RequestField = Field(default_factory=RequestField)
    max_runs: RequestField = Field(default_factory=RequestField)
    deadline: RequestField = Field(default_factory=RequestField)
    # Hard exclusions. A reagent here that appears anywhere in the design is a `blocker`.
    forbidden: list[str] = Field(default_factory=list)
    # What the chemist says has already been tried. Free text on purpose: it is a pointer for the
    # precedent search, not a record, and structuring it would be inventing runs.
    prior_work: str = ""
    project: str = ""
    # Everything else the text said that no slot above holds, so nothing is lost in structuring.
    notes: str = ""

    model_config = ConfigDict(frozen=True, extra="forbid")


class FactorLevel(BaseModel):
    """One setting a factor can take."""

    label: str = Field(min_length=1)
    # For a categorical factor: the structure behind the label, when it is a species. Carrying it
    # is what lets `checks` screen a level for hazard and lets a downstream BO campaign featurize
    # the category rather than one-hot it (`science.bo.problem.CategoricalParameter.structures`).
    smiles: str = ""
    # For a continuous factor.
    value: float | None = None
    unit: str = ""
    # Why this level and not another. The single most useful sentence on a screening plate.
    rationale: str = ""

    model_config = ConfigDict(frozen=True, extra="forbid")


class Factor(BaseModel):
    """One thing the screen varies."""

    name: str = Field(min_length=1, pattern=r"^[a-z][a-z0-9_]*$")
    kind: Literal["categorical", "continuous"]
    role: SpeciesRole = _ROLE_FIELD
    levels: list[FactorLevel] = Field(min_length=2)
    unit: str = ""

    model_config = ConfigDict(frozen=True, extra="forbid")

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

    # Deliberately not `kg.note.ProcessConditions`, which is *recorded* — it mixes setpoints with
    # yield and impurity, and its docstring says it holds "what a run recorded". A plan's
    # temperature is an instruction; a plan's yield is a prediction. Two models, so a reader can
    # never take one for the other.

    # Deliberately not `kg.note.ProcessConditions`, which is *recorded* — it mixes setpoints with
    # yield and impurity, and its docstring says it holds "what a run recorded". A plan's
    # temperature is an instruction; a plan's yield is a prediction. Two models, so a reader can
    # never take one for the other.
    temperature_c: float | None = None
    time_h: float | None = Field(default=None, gt=0.0)
    pressure_bar: float | None = Field(default=None, gt=0.0)
    atmosphere: str = ""
    concentration_molar: float | None = Field(default=None, gt=0.0)
    solvent: str = ""
    ph: float | None = None

    model_config = ConfigDict(frozen=True, extra="forbid")


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

    model_config = ConfigDict(frozen=True, extra="forbid")


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

    model_config = ConfigDict(frozen=True, extra="forbid")


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

    model_config = ConfigDict(frozen=True, extra="forbid")


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

    model_config = ConfigDict(frozen=True, extra="forbid")


class EvidenceRef(BaseModel):
    """One citation behind a decision in this design."""

    kind: EvidenceKind
    # A note id, a `reaction-<id>`, a `source:doc_id`, or a tool result reference. Empty only for a
    # `tool` citation whose result was not stored.
    ref: str = ""
    tool: str = ""
    summary: str = Field(min_length=1)
    # Dotted paths into the design this citation is offered for — `base.setpoints.temperature_c`,
    # `factors.ligand.levels`. What lets a reader put the reason next to the number instead of at
    # the bottom of the page.
    supports: list[str] = Field(default_factory=list)

    model_config = ConfigDict(frozen=True, extra="forbid")


class ProtocolBody(BaseModel):
    """What every arm of the design shares."""

    setpoints: Setpoints = Field(default_factory=Setpoints)
    charge: list[ChargeLine] = Field(default_factory=list)
    steps: list[ProtocolStep] = Field(default_factory=list)
    analytics: list[Analytic] = Field(default_factory=list)
    # In-process controls: what to check *during* the run and what to do about it.
    in_process_controls: list[str] = Field(default_factory=list)
    # What `screen_hazards` / `screen_genotoxic_alerts` / `ich_impurity_limit` said, in the
    # chemist's words. This system flags, it never certifies — see `safety`'s own tools.
    hazards: list[str] = Field(default_factory=list)
    waste: str = ""
    expected: ExpectedOutcome = Field(default_factory=ExpectedOutcome)

    model_config = ConfigDict(frozen=True, extra="forbid")


class ProtocolArm(BaseModel):
    """One runnable set of conditions."""

    # Stable within a design and used as the well's label, the CSV row key and the id a result is
    # reported against. Not an index, because arms get reordered by a randomised run order and a
    # positional key would then name a different experiment.
    arm_id: str = Field(min_length=1, pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
    # `{factor_name: level_label}`. Empty for a single experiment.
    levels: dict[str, str] = Field(default_factory=dict)
    # Only what differs from `ProtocolBody`. A screen whose arms each restated the whole body would
    # be N protocols rather than one design, and a reader could not see what is being varied.
    setpoints: Setpoints | None = None
    # There is deliberately no per-arm charge override. An arm that varies an *amount* declares
    # that amount as a continuous factor, which is the same statement in the vocabulary the design
    # already has — and a second way to say it would have inlined the whole `ChargeLine` model into
    # every tool schema for a field nothing needed. A control that genuinely differs says so in
    # `note`; if that stops being enough, adding it back is a decision with a caller behind it.
    # A control is excluded from the factor-coverage check and rendered apart on the plate.
    control: Literal["", "positive", "negative", "blank"] = ""
    replicate_of: str = ""
    note: str = ""

    model_config = ConfigDict(frozen=True, extra="forbid")


class Well(BaseModel):
    """One position on the plate."""

    label: str = Field(min_length=1)
    row: int = Field(ge=0)
    column: int = Field(ge=0)
    arm_id: str = Field(min_length=1)
    # 1-based position in the order the arms are to be run, which is not the well order when the
    # design is randomised against session drift.
    run_order: int = Field(ge=1)

    model_config = ConfigDict(frozen=True, extra="forbid")


class PlateLayout(BaseModel):
    """Where each arm sits and in what order it is run."""

    plate_format: int = Field(gt=0)
    rows: int = Field(gt=0)
    columns: int = Field(gt=0)
    wells: list[Well] = Field(default_factory=list)
    randomized: bool = False
    seed: int | None = None

    model_config = ConfigDict(frozen=True, extra="forbid")


class ProtocolCheck(BaseModel):
    """One deterministic verdict about the design."""

    # Computed by `checks`, never supplied by a caller — which is why this model is not part of any
    # tool's argument schema. A design that graded itself would be a second answer about its own
    # first answer.
    check_id: str = Field(min_length=1)
    severity: CheckSeverity
    passed: bool
    detail: str = ""

    model_config = ConfigDict(frozen=True, extra="forbid")


class ExperimentDesign(BaseModel):
    """A complete experiment design: one arm for a single run, N arms and factors for a plate."""

    request: ExperimentRequest
    base: ProtocolBody = Field(default_factory=ProtocolBody)
    factors: list[Factor] = Field(default_factory=list)
    arms: list[ProtocolArm] = Field(default_factory=list)
    layout: PlateLayout | None = None
    evidence: list[EvidenceRef] = Field(default_factory=list)

    model_config = ConfigDict(frozen=True, extra="forbid")

    @model_validator(mode="after")
    def _names_resolve_and_steps_are_ordered(self) -> ExperimentDesign:
        """Ids are unique, `replicate_of` names a real arm, and steps are numbered 1..n."""
        # The last two are here rather than in `checks` because they are not judgments about a
        # design — they are the difference between a document that means something and one that
        # does not, and both were measured *defeating* a check. A `replicate_of` naming an arm that
        # does not exist exempted its arm from `arms_are_distinct`, and two factors sharing a name
        # collapsed in `factor_levels_declared`'s `declared` dict, so the first factor's levels
        # left the *blocker* and the diff at once.
        ids = [arm.arm_id for arm in self.arms]
        if len(set(ids)) != len(ids):
            raise ValueError("arm_id repeats; each arm needs its own id")
        names = [factor.name for factor in self.factors]
        if len(set(names)) != len(names):
            raise ValueError("a factor name repeats; each factor needs its own name")
        dangling = sorted({arm.replicate_of for arm in self.arms if arm.replicate_of} - set(ids))
        if dangling:
            raise ValueError(f"replicate_of names no arm in this design: {', '.join(dangling)}")
        expected = list(range(1, len(self.base.steps) + 1))
        if [step.index for step in self.base.steps] != expected:
            raise ValueError("steps must be numbered 1..n in order")
        return self

    @property
    def has_protocol(self) -> bool:
        """Whether this design says what to do, rather than only what is being asked for.

        The one definition, read by `checks.is_a_protocol`, by the intake (which must not replace a
        drafted design with an empty ask) and by the edit route (which must not grade a correction
        to the *ask* at the protocol stage). Three callers deciding it separately is how the second
        and third got it wrong.
        """
        return bool(self.arms or self.base.steps or self.base.charge)

    def arm(self, arm_id: str) -> ProtocolArm | None:
        """The arm with this id, or `None`."""
        return next((a for a in self.arms if a.arm_id == arm_id), None)

    def setpoints_for(self, arm: ProtocolArm) -> Setpoints:
        """The arm's own setpoints, falling back to the shared body's."""
        return arm.setpoints or self.base.setpoints


class DesignRevision(BaseModel):
    """One immutable version of a design, and who wrote it."""

    design_id: str = Field(min_length=1)
    revision: int = Field(ge=1)
    # `request` holds only a structured ask (the intake, before any protocol exists); `protocol`
    # holds a whole design. Two kinds in one table because they are the same document growing, and
    # a reader wants the history in one list.
    kind: Literal["request", "protocol"]
    author_kind: AuthorKind
    author: str = ""
    # 0 on the first revision. Every later one names the revision it was derived from, which is
    # what makes a concurrent edit a 409 rather than a silent overwrite.
    parent_revision: int = Field(default=0, ge=0)
    change_note: str = ""
    design: ExperimentDesign
    checks: list[ProtocolCheck] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    model_config = ConfigDict(frozen=True, extra="forbid")

    @property
    def blockers(self) -> list[ProtocolCheck]:
        """The checks that failed at `blocker` severity."""
        return [c for c in self.checks if c.severity == "blocker" and not c.passed]


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

    model_config = ConfigDict(frozen=True, extra="forbid")


def design_id_for(request: ExperimentRequest, *, salt: str = "") -> str:
    """The id a new design is filed under.

    Derived from the ask rather than random, so the same request restructured in the same session
    reaches the same design instead of forking one. `salt` is how a chemist deliberately opens a
    second design for the same ask — a `campaign_id_for`-shaped decision, taken by the caller.
    """
    identity = {
        "title": request.title.strip().lower(),
        "goal": request.goal.strip().lower(),
        "reaction": request.reaction_smiles.strip(),
        "mode": request.mode,
        "salt": salt,
    }
    return f"{DESIGN_ID_PREFIX}-{stable_hash(identity, chars=12)}"


#: The design shape a tool accepts as an argument. Named so the schema description does not repeat
#: the whole model docstring on every turn.
DesignInput = Annotated[ExperimentDesign, Field(description="The complete experiment design.")]
