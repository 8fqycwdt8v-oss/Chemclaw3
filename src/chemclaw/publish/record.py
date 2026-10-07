"""The canonical shape a computed result takes on its way out of this system.

`calculation_results` is a deliberately opaque cache keyed for exact lookup; this module is the
same science projected into something a chemist can put a `WHERE` clause on.

A calculation is an event with a subject, and results are typed facts about that subject:

- a **spine** (`ResultRecord`) carrying identity, subject, conditions and provenance;
- a **governed fact layer**: every fact names a registered `property`;
- the **verbatim payload**, so every fact can be rebuilt by re-projecting.

One subject shape (an identity plus 1..N members with roles) covers a molecule, a reaction, a
complex and an ensemble (whose member is the seed geometry). A continuum solvent is a condition,
not a member; an explicit solvent molecule is a member.

Subject identity excludes solvent, temperature and method, so "this reaction across every
solvent" is a `GROUP BY subject_id`. A calculation's identity excludes who asked: the actor lives
on `Publication`, N per record, which also makes re-delivery idempotent.
"""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from chemclaw.core.ids import stable_hash
from chemclaw.publish.solvents import canonical_solvent

# The contract version this writer builds records against, stamped on every record so a consumer
# can tell an absent *measurement* from an absent *column*.
#
# Bump when the meaning of an already-published field changes; an added field needs no bump. A
# bump is also what lets corrected documents enqueue, since the outbox ignores a duplicate
# `(sink, calc_ref, schema_version)`.
#
# - **2**: `max_gradient` is converted from Hartree/Angstrom; version-1 rows are 1.89x too large
#   (their `reported_unit` reads `hartree/bohr`).
# - **3**: `subject_id` identifies members by SMILES before `compound_id`, so a subject has a
#   different id on either side; re-projecting the corpus re-points old rows.
CONTRACT_VERSION = 3

# What a member is to the subject. Closed: an unknown role is a projection bug, and accepting one
# would put an unqueryable value in the column every reaction query filters on.
MemberRole = Literal[
    "subject",  # the single molecule or geometry a calculation is about
    "reactant",
    "product",
    "monomer",  # one half of a non-covalent complex
    "complex",  # the associated pair itself
    "solvent",  # an EXPLICIT solvent molecule; a continuum model is a condition, not a member
    "catalyst",
]

# The subject shapes. `system` is the escape hatch for a multi-component subject that is none of
# the others; a projection reaching for it signals a missing kind.
SubjectKind = Literal["molecule", "geometry", "ensemble", "reaction", "complex", "system"]

# Where a fact attaches. `calculation` is a fact about the whole run (a reaction's ΔG); `member` is
# a fact about one participant (a species' absolute Gibbs energy). One table answers both.
FactScope = Literal["calculation", "member"]

# Every model in this document, on the same three terms. `allow_inf_nan=False` because a published
# document is JSON and NaN/Infinity are not; refusing here counts the failure as a permanent
# projection gap rather than a transient publish failure at the database. The verbatim payload is
# unaffected.
_STORABLE = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class SubjectMember(BaseModel):
    """One participant in what a calculation was about.

    `stoichiometry` is carried rather than inferred, but the tools' own convention is to list a
    species once per equivalent (`["O", "O"]` for two waters), so the faithful projection of a
    reaction is N members at stoichiometry 1 rather than one member at 2. The field exists for a
    source that states coefficients instead.
    """

    model_config = _STORABLE

    ordinal: int = Field(ge=0)
    role: MemberRole
    # `core.chem.compound_id`, a hash over the standardized SMILES: the join key the knowledge graph
    # and
    # fingerprint search already use.
    compound_id: str = ""
    smiles: str = ""
    # The geometry, when this member is one. Content-addressed and byte-identical on both sides of
    # the calc wire (D-2026-08-21).
    structure_id: str = ""
    stoichiometry: float = 1.0
    charge: int | None = None
    multiplicity: int | None = None

    @model_validator(mode="after")
    def _identifies_something(self) -> "SubjectMember":
        """Reject a member that names neither a molecule nor a geometry.

        Such a row would join to nothing and make "what do we hold for compound X" silently
        under-return.
        """
        if not (self.compound_id or self.smiles or self.structure_id):
            raise ValueError(
                f"subject member {self.ordinal} names no compound and no structure; "
                "a member must identify something"
            )
        return self


class Subject(BaseModel):
    """What a calculation was about — identity plus its members.

    `subject_id` is derived, never supplied, and is deliberately **order-insensitive and
    stoichiometry-sensitive**: members are sorted by `(role, identity, stoichiometry)` before
    hashing, so `A+B -> C` and `B+A -> C` are one subject while `2A -> B` and `A -> B` are two.
    """

    model_config = _STORABLE

    kind: SubjectKind
    members: list[SubjectMember] = Field(min_length=1)
    # A human-readable form (a reaction SMILES, or the compound's SMILES), so a published row is
    # legible without a join — the same argument migration 030 makes for `measurements.subject`.
    label: str = ""

    @property
    def subject_id(self) -> str:
        """The content address of this subject, excluding solvent, temperature and method.

        Each member is identified by its own SMILES first, then `structure_id`, then `compound_id`
        as a
        last resort: `compound_id` standardizes, so it would collapse the tautomers and microstates
        a
        species distribution enumerates into one subject. Precedence rather than hashing all three,
        so a
        member that later gains a `structure_id` stays the same subject.
        """
        parts = sorted(
            (
                member.role,
                member.smiles or member.structure_id or member.compound_id,
                member.stoichiometry,
            )
            for member in self.members
        )
        return f"sub_{stable_hash({'kind': self.kind, 'members': parts})}"


class Conditions(BaseModel):
    """The state a calculation was run at — everything but the level of theory.

    **A continuum solvent lives here rather than in the subject**, because an implicit model is a
    parameter of the Hamiltonian and not a species present in the flask.

    `solvent` is canonicalized **here**, on the way in, against the shipped alias table. That
    matters more than it looks: `ALPB_SOLVENTS` accepts `thf` **and** `tetrahydrofuran`,
    `hexane`/`n-hexane`/`nhexane`, `dichloromethane` **and** `dichlormethane`, nothing in the
    calculation layer maps between them, and `dialect.rows_for` mints `solvent_id` straight from
    this field — so a name that arrived as given becomes a first-class solvent in the store and
    "every reaction in THF" answers with a confident subset, raising nothing.

    **It is a validator rather than a call each projector makes**, and that is the fix for a
    measured defect: canonicalization was fourteen hand-written calls in `project.py` and the
    fifteenth projector — the microstate pKa, the most expensive calculation in the tier — did not
    make it. One model owning it is what makes the sixteenth safe.

    `solvent=None` means **gas phase**, which is a real state and not a missing value.
    """

    model_config = _STORABLE

    solvent: str | None = None
    solvent_model: str = ""  # 'alpb' | 'cpcm' | ''
    temperature_k: float | None = None
    pressure_pa: float | None = None
    ph: float | None = None
    charge: int | None = None
    multiplicity: int | None = None

    @field_validator("solvent")
    @classmethod
    def _canonical_solvent(cls, value: str | None) -> str | None:
        """Resolve an accepted spelling to the one id every query filters on.

        An unrecognized name is normalized and kept, not refused (it is still a fact about the run).
        An
        empty or whitespace name reads as gas phase.
        """
        return canonical_solvent(value)

    @property
    def condition_id(self) -> str:
        """The content address of this condition set.

        A record with nothing set resolves to one shared id rather than null, so consumers need no
        `LEFT JOIN`.
        """
        return f"cond_{stable_hash(self.model_dump(mode='json'))}"


class TheoryLevel(BaseModel):
    """How a calculation was run: the method, and what implemented it.

    Separate from `Conditions` because it answers a different question — *what approximation* rather
    than *what state* — and because "at GFN2" and "in THF" are independently varied in every screen
    a chemist runs.
    """

    model_config = _STORABLE

    method: str = Field(min_length=1)  # 'GFN2-xTB' | 'B3LYP' | 'ESOL'
    family: str = ""  # semiempirical | dft | ff | ml | empirical
    basis_set: str = ""
    engine: str = ""  # tblite | xtb | crest | rdkit
    treatment: str = ""  # ReactionLevel, or a conformer treatment

    @property
    def level_id(self) -> str:
        """The content address of this level of theory."""
        return f"lvl_{stable_hash(self.model_dump(mode='json'))}"


class PropertyFact(BaseModel):
    """One scalar a calculation established, with everything needed to trust it.

    **Uncertainty sits on the same row as its value, deliberately.** Split across two rows, reading
    a value without its error bar becomes a self-join that silently succeeds when the second row is
    missing — and a semiempirical number quoted without its uncertainty is the exact failure every
    result docstring in `science/calc/models.py` warns about.

    The fields mirror `science/calc/uncertainty.py`'s `Estimate` — *"the uniform part, produced
    beside them, so a consumer has one shape to consult regardless of which calculator answered"* —
    which is the existing abstraction this reuses rather than a new one.

    `in_domain=None` means no applicability domain was declared, which is **not** the same as
    `False` and must never be read as "yes".

    **This model is also the parse model for a document already in the outbox, so a validator here
    is a filter on stored bytes.** The registry-scope check lived on this class for one release and
    that is what it did: `durable/publish_results._drain_one` re-validates every queued
    `result_publications.document`, so every already-enqueued species distribution — each carrying
    `relative_energy`, a per-conformer quantity, as a calculation scalar, which is the very defect
    the check was written for — became unparseable, spent an attempt per pass and dead-lettered,
    with the backfill CLI unable to help because the stored bytes do not change. The check is now
    `project._facts_belong_in_the_scalar_table`, on the write path, where its own argument — *a
    registry mismatch is a projection bug and cannot be caused by data* — is true. A rule about
    what this system may **produce** belongs where production happens; only a rule about what a
    document must **be** to be read at all belongs here.
    """

    model_config = _STORABLE

    property: str = Field(min_length=1)
    scope: FactScope = "calculation"
    # Which member this is about, at member scope. None at calculation scope.
    member_ordinal: int | None = None
    # Exactly one of these three carries the value: `value` (numeric), `value_bool` (`converged`),
    # or
    # `value_text` (`site='acid'`).
    value: float | None = None
    value_bool: bool | None = None
    value_text: str = ""
    # The unit the calculator reported, which `reported_value` is in; `value` is always canonical.
    unit: str = ""
    # What the calculator said, before canonicalization, in `unit`; None where the two are the same
    # number. Carried as a pair with `unit` so the canonical column can be rebuilt if a conversion
    # is
    # ever found wrong.
    reported_value: float | None = None
    uncertainty: float | None = None
    uncertainty_kind: str = ""  # Estimate.method: reported | propagated | none
    in_domain: bool | None = None

    @model_validator(mode="after")
    def _carries_exactly_one_value(self) -> "PropertyFact":
        """Reject a fact with no value, or with more than one kind of value.

        Both are silent in storage: one dropped its number, the other could be read two ways.
        """
        filled = [self.value is not None, self.value_bool is not None, bool(self.value_text)]
        if sum(filled) != 1:
            raise ValueError(
                f"property {self.property!r} must carry exactly one of value, value_bool or "
                f"value_text (got {sum(filled)})"
            )
        return self

    @model_validator(mode="after")
    def _scope_and_ordinal_agree(self) -> "PropertyFact":
        """Reject a member-scope fact with no member, or a calculation-scope fact with one."""
        if self.scope == "member" and self.member_ordinal is None:
            raise ValueError(f"property {self.property!r} is member-scoped but names no member")
        if self.scope == "calculation" and self.member_ordinal is not None:
            raise ValueError(
                f"property {self.property!r} is calculation-scoped but names member "
                f"{self.member_ordinal}"
            )
        return self


class SiteFact(BaseModel):
    """A per-atom or per-atom-pair value: a Mulliken charge, a Fukui index, a bond order.

    Its own shape rather than a `PropertyFact` with a synthetic member, because the cardinality is
    different in kind. A 33-atom molecule contributes one calculation-scope energy and ~68 site
    values, so folding these into the scalar table would build the index that answers "pKa between
    4 and 6" over rows that are overwhelmingly atom charges.

    `atom_j = -1` means a single-site value; a non-negative `atom_j` makes it a pair.
    """

    model_config = _STORABLE

    atom_i: int = Field(ge=0)
    atom_j: int = -1
    element: str = ""
    property: str = Field(min_length=1)
    value: float


class PointFact(BaseModel):
    """One point of an ordered series: a scan step, a vibrational mode, a spectral band.

    Ordered, which is why this is not EAV: "the third mode" must be an integer comparison and not a
    string sort. `x_value` is the abscissa the point sits at — a dihedral in degrees, a bond length
    in Angstrom, a wavenumber — and it is what makes a series plottable without knowing which tool
    produced it.

    A spectrum, a chromatogram and a titration curve are all this shape, which is worth checking
    before any future result type earns a table of its own.
    """

    model_config = _STORABLE

    series: str = Field(min_length=1)  # 'scan' | 'modes' | 'spectrum'
    ordinal: int = Field(ge=0)
    property: str = Field(min_length=1)
    value: float
    x_value: float | None = None
    x_unit: str = ""
    x_label: str = ""
    # A relaxed scan point has a geometry; a vibrational mode does not.
    structure_id: str = ""


class ConformerFact(BaseModel):
    """One member of a conformer ensemble.

    Its own shape rather than a point series, because a conformer is a *geometry with a degeneracy*
    and not a value at an abscissa.

    **`population` is temperature-dependent and the search that found the member is not.**
    `EnsemblePayload` is cached without a temperature — deliberately, so a second temperature is a
    cache hit — and `ConformerEnsemble` is arithmetic over it at a stated T. So the search publishes
    with `population=None`, and the populations publish as their own record at a `Conditions`
    carrying `temperature_k`, edged back to the search. Recomputing at 353 K is then a new record
    rather than an overwrite of the 298 K one.

    **Both energy fields are optional, because the two upstream shapes carry different halves and
    neither carries both.** Measured against the models rather than assumed: `EnsembleMember` (what
    is cached) has `energy_hartree` and no relative energy; `Conformer` (what is returned) has
    `relative_kcal` and `population` and no absolute energy. Requiring the absolute one would make
    every returned ensemble unpublishable. `_at_least_one_energy` is what keeps that flexibility
    from admitting a member with no energy at all.
    """

    model_config = _STORABLE

    ordinal: int = Field(ge=0)  # 0 = lowest, as `ensemble_from_members` orders them
    structure_id: str = Field(min_length=1)
    # The electronic state this geometry was computed at, when the payload states it. `structure_id`
    # hashes over it and cannot be read back, and the `structure` row filters on it. `None` means
    # unstated, not neutral (see `dialect.PRESERVE_ON_BLANK`).
    charge: int | None = None
    multiplicity: int | None = None
    energy_hartree: float | None = None
    relative_kcal: float | None = None
    population: float | None = None
    degeneracy: int = 1

    @model_validator(mode="after")
    def _at_least_one_energy(self) -> "ConformerFact":
        """Reject a member carrying neither an absolute nor a relative energy.

        A conformer with no energy cannot be ranked and would have to be filtered out by every
        query.
        """
        if self.energy_hartree is None and self.relative_kcal is None:
            raise ValueError(
                f"conformer {self.ordinal} carries neither energy_hartree nor relative_kcal"
            )
        return self


class CandidateFact(BaseModel):
    """One ranked output: a predicted product, a suggested condition, a similarity hit.

    One shape for four tools — `rxnpredict`'s products, BO's candidates, `molfp`/`rxnfp`'s
    neighbours — because they are the same question ("what does this suggest, and how strongly")
    asked of different chemistry. `detail` carries the tool's own extra fields verbatim; it is never
    a predicate.
    """

    model_config = _STORABLE

    ordinal: int = Field(ge=0)
    kind: str = Field(min_length=1)  # compound | reaction | condition | point
    compound_id: str = ""
    smiles: str = ""
    score: float | None = None
    score_property: str = ""
    detail: dict[str, Any] = Field(default_factory=dict)


class FlagFact(BaseModel):
    """An assertion a calculation made that is not a measurement.

    A warning, a hazard alert, an imaginary-frequency notice. **The line against
    `PropertyFact.value_bool` is cardinality, not type**: a fixed 0..1 attribute of every result of
    its kind (`converged`, `is_minimum`, `veber_pass`) is a property, because every such calculation
    has exactly one; an open-ended emitted set is a flag, because a calculation may raise none or
    six and nobody can enumerate them in advance.
    """

    model_config = _STORABLE

    ordinal: int = Field(ge=0)
    flag: str = Field(min_length=1)
    severity: str = "info"
    message: str = ""
    detail: dict[str, Any] = Field(default_factory=dict)


class Publication(BaseModel):
    """Who ran a calculation, under which tenant, and why.

    Separate from the record because **a calculation's identity excludes its requester**: two
    chemists asking the same question share one `calc_ref`, and putting the actor on that row would
    make them collide. N publications per record is the correct cardinality, and it is where a
    site's grants and row-level security attach.

    `rationale` is the field `job_records` added for the same reason (D-157): notes record what a
    run produced and the audit trail records that a tool was called, and neither says what question
    the run was meant to answer.

    `note_id` is the other half of that, and it is the one *structured* link: the note a finished
    connector job's envelope produced, which `job_records.note_id` already holds and both publish
    paths used to drop (`D-2026-09-13-a-publication-carries-the-link-the-system-already-holds`).
    """

    model_config = _STORABLE

    # Empty (the normal case) means "whatever the sink calls this deployment": a record goes to
    # every
    # enabled sink, and `dialect.rows_for` substitutes the manifest's `tenant_id`. Non-empty is a
    # deliberate override.
    tenant_id: str = ""
    actor: str = ""
    session_id: str = ""
    correlation_id: str = ""
    job_id: str = ""
    rationale: str = ""
    # The knowledge-graph note this run produced, or empty (as `job_records.note_id`). Not the ELN
    # run
    # that motivated the calculation, which nothing records.
    note_id: str = ""


class ResultRecord(BaseModel):
    """One computed result, in the shape it is published in.

    The whole cross-system contract: what the projection produces, what a driver writes, and what
    the shipped DDL stores. Frozen and `extra="forbid"`, so a field added on one side of that
    contract and not the other fails loudly here rather than silently downstream.
    """

    model_config = _STORABLE

    # --- identity -------------------------------------------------------------------------
    # The flat cache key `calc_type@calc_version:input_hash:params_hash`, the same string a
    # knowledge
    # note cites, so both name the calculation identically.
    calc_ref: str = Field(min_length=1)
    calc_type: str = Field(min_length=1)
    calc_version: str = ""
    input_hash: str = ""
    params_hash: str = ""

    # --- what it was about ----------------------------------------------------------------
    subject: Subject
    conditions: Conditions = Field(default_factory=Conditions)
    level: TheoryLevel
    # The geometry the calculation ran **on**, never the one it produced. Empty for a molecule-keyed
    # calculator ("not recorded").
    structure_id: str = ""

    # --- the facts ------------------------------------------------------------------------
    properties: list[PropertyFact] = Field(default_factory=list)
    sites: list[SiteFact] = Field(default_factory=list)
    points: list[PointFact] = Field(default_factory=list)
    conformers: list[ConformerFact] = Field(default_factory=list)
    candidates: list[CandidateFact] = Field(default_factory=list)
    flags: list[FlagFact] = Field(default_factory=list)
    # No `artifacts` list: nothing produces one. The local `calculation_artifacts` table holds a
    # calculation's by-products; restoring the field means shipping its producer in the same change
    # (`tests/test_publish_dialect.py` enforces it).

    # --- provenance -----------------------------------------------------------------------
    provenance: str = "computed"  # computed | measured | imported
    compute_seconds: float | None = None
    computed_at: datetime | None = None
    # The calculations this one rested on, published as edges because staleness propagation walks
    # them
    # in reverse and array columns are not portable.
    depends_on: list[str] = Field(default_factory=list)
    publications: list[Publication] = Field(default_factory=list)

    # --- the original ---------------------------------------------------------------------
    # The payload exactly as stored, never a predicate, so a projector bug is a replay, not data
    # loss.
    payload: dict[str, Any] = Field(default_factory=dict)
    payload_kind: str = ""  # the pydantic model name, for choosing a schema to validate against
    contract_version: int = CONTRACT_VERSION

    @property
    def subject_id(self) -> str:
        """The subject's content address, for the columns that denormalize it."""
        return self.subject.subject_id

    @model_validator(mode="after")
    def _facts_address_real_members(self) -> "ResultRecord":
        """Reject a member-scoped fact naming a member the subject does not have.

        Caught here because the far end may not enforce foreign keys (Snowflake accepts `REFERENCES`
        and never checks it).
        """
        ordinals = {member.ordinal for member in self.subject.members}
        for fact in self.properties:
            if fact.member_ordinal is not None and fact.member_ordinal not in ordinals:
                raise ValueError(
                    f"property {fact.property!r} names member {fact.member_ordinal}, but this "
                    f"subject has members {sorted(ordinals)}"
                )
        return self
