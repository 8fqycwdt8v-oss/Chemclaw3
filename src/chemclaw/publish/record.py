"""The canonical shape a computed result takes on its way out of this system.

**Why this exists.** `calculation_results` is a cache: `key TEXT PRIMARY KEY` onto an opaque
`result JSONB`, and `CalculationQuery`'s own docstring says the opacity is deliberate — *"a
`total_energy_hartree > x` predicate would put one calculator's schema inside the thing that
persists all of them."* That is right for a store whose job is exact-key lookup, and it is exactly
why the cache cannot also be the scientific record. This module is the other shape: the same
science, projected into something a chemist can put a `WHERE` clause on.

**The central abstraction: a calculation is an event with a subject, and results are typed facts
about that subject.** Three parts, and the split between them is load-bearing:

- a **spine** (`ResultRecord`) carrying identity, subject, conditions and provenance;
- a **governed fact layer** — every fact names a `property` that must exist in the shipped
  registry, so a value cannot be written under a name nobody defined;
- the **verbatim payload**, carried untouched. That is what makes the projection safe to be wrong:
  the truth is retained, and every fact can be rebuilt from it by re-projecting.

**One subject shape for five cases, rather than five special cases.** A subject is an identity plus
1..N members with roles. One molecule is one member; a reaction is N members with
`reactant`/`product` roles; a complex is three (`monomer`, `monomer`, `complex`); an ensemble is one
member naming the *seed* geometry, because the conformers it found are outputs rather than
subjects. A continuum solvent is deliberately **not** a member — it is a parameter of the
Hamiltonian, not a species — while an explicit solvent molecule is, and the two stay
distinguishable because they are genuinely different calculations.

**Subject identity excludes solvent, temperature and method**, and that exclusion is what makes
"compare ΔG for this reaction across every solvent we ran it in" a `GROUP BY subject_id` rather
than a fuzzy join over two text arrays that happen to be spelled the same way.

**A calculation's identity excludes who asked for it.** `qm_job_key` already states the rule — *"the
result of a calculation does not depend on who asked for it, so identical science shares one key
across users."* So the actor does not live on the record; it lives on `Publication`, of which there
are N per record. Two chemists running the same calculation produce one `ResultRecord` and two
publications, which is also what makes re-delivery idempotent.
"""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from chemclaw.core.ids import stable_hash
from chemclaw.publish.solvents import canonical_solvent

# The contract version this writer builds records against. Stamped on every record so a consumer
# can tell an absent *measurement* from an absent *column* — the question "why is `in_domain` NULL
# for everything before March" is unanswerable without it.
#
# Bump when the meaning of an already-published field changes. A field merely *added* does not need
# one: an older row simply has the default, which reads correctly as "not recorded".
CONTRACT_VERSION = 1

# What a member is to the subject. Closed, because an unknown role is a projection bug rather than
# a new kind of chemistry, and silently accepting one would put an unqueryable value in the column
# every reaction query filters on.
MemberRole = Literal[
    "subject",  # the single molecule or geometry a calculation is about
    "reactant",
    "product",
    "monomer",  # one half of a non-covalent complex
    "complex",  # the associated pair itself
    "solvent",  # an EXPLICIT solvent molecule; a continuum model is a condition, not a member
    "catalyst",
]

# The five subject shapes, named. `system` is the escape hatch for a future multi-component subject
# that is none of the four specific ones; it is deliberately last and deliberately vague, because a
# projection reaching for it is a signal that a real kind is missing.
SubjectKind = Literal["molecule", "geometry", "ensemble", "reaction", "complex", "system"]

# Where a fact attaches. `calculation` is a fact about the whole run (a reaction's ΔG); `member` is
# a fact about one participant (a species' absolute Gibbs energy). One table answers both.
FactScope = Literal["calculation", "member"]


class SubjectMember(BaseModel):
    """One participant in what a calculation was about.

    `stoichiometry` is carried rather than inferred, but the tools' own convention is to list a
    species once per equivalent (`["O", "O"]` for two waters), so the faithful projection of a
    reaction is N members at stoichiometry 1 rather than one member at 2. The field exists for a
    source that states coefficients instead.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    ordinal: int = Field(ge=0)
    role: MemberRole
    # The molecule, as this system already identifies one: `core.chem.compound_id`, a hash over the
    # *standardized* SMILES. Reused rather than minting an InChIKey, because it is already the join
    # key between the knowledge graph, the fingerprint search and the QM notes — so a published
    # result meets the note about the same compound with no second naming scheme.
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

        Such a row would join to nothing and would make every "what do we hold for compound X"
        query silently under-return, which is the failure this whole module exists to avoid.
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

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: SubjectKind
    members: list[SubjectMember] = Field(min_length=1)
    # A human-readable form (a reaction SMILES, or the compound's SMILES), so a published row is
    # legible without a join — the same argument migration 030 makes for `measurements.subject`.
    label: str = ""

    @property
    def subject_id(self) -> str:
        """The content address of this subject, excluding solvent, temperature and method.

        Excluding them is the point: it is what lets one reaction's runs across five solvents be
        found by grouping on a single column.
        """
        parts = sorted(
            (
                member.role,
                member.compound_id or member.smiles or member.structure_id,
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

    model_config = ConfigDict(extra="forbid", frozen=True)

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

        An unrecognized name is normalized and kept rather than refused — a solvent this registry
        has not heard of is still a fact about the run, and losing a finished calculation to
        protect a lookup table would be the wrong trade. An empty or whitespace name reads as gas
        phase, which is what `canonical_solvent` already decides for the calculation layer.
        """
        return canonical_solvent(value)

    @property
    def condition_id(self) -> str:
        """The content address of this condition set.

        A record with nothing set resolves to one shared id rather than to a null, so "ran in the
        gas phase with nothing else stated" and "this calculator has no conditions" are one
        queryable value instead of a `LEFT JOIN` in every consumer.
        """
        return f"cond_{stable_hash(self.model_dump(mode='json'))}"


class TheoryLevel(BaseModel):
    """How a calculation was run: the method, and what implemented it.

    Separate from `Conditions` because it answers a different question — *what approximation* rather
    than *what state* — and because "at GFN2" and "in THF" are independently varied in every screen
    a chemist runs.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

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
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    property: str = Field(min_length=1)
    scope: FactScope = "calculation"
    # Which member this is about, at member scope. None at calculation scope.
    member_ordinal: int | None = None
    # Exactly one of these three carries the value. A numeric fact fills `value`; a boolean
    # (`converged`, `is_minimum`) fills `value_bool`; a coded string (`site='acid'`) fills
    # `value_text`.
    value: float | None = None
    value_bool: bool | None = None
    value_text: str = ""
    # The unit the projection produced. Normalized to the registry's canonical unit at write time,
    # with the reported pair kept beside it so a conversion found wrong later is recoverable.
    unit: str = ""
    uncertainty: float | None = None
    uncertainty_kind: str = ""  # Estimate.method: reported | propagated | none
    in_domain: bool | None = None

    @model_validator(mode="after")
    def _carries_exactly_one_value(self) -> "PropertyFact":
        """Reject a fact with no value, or with more than one kind of value.

        A fact carrying none is a projection that dropped its number; a fact carrying two is one
        that could be read two ways. Both are silent in storage and loud here.
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

    model_config = ConfigDict(extra="forbid", frozen=True)

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

    model_config = ConfigDict(extra="forbid", frozen=True)

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

    model_config = ConfigDict(extra="forbid", frozen=True)

    ordinal: int = Field(ge=0)  # 0 = lowest, as `ensemble_from_members` orders them
    structure_id: str = Field(min_length=1)
    # The electronic state this geometry was computed at, when the payload states it. Carried
    # because `structure_id` is a hash *over* charge and multiplicity and so cannot be read back
    # for them, and because the `structure` row these become is what "show me every anionic
    # geometry we have optimised" filters on. `None` is "the payload did not say", which is not
    # the same as neutral — see `dialect.PRESERVE_ON_BLANK`.
    charge: int | None = None
    multiplicity: int | None = None
    energy_hartree: float | None = None
    relative_kcal: float | None = None
    population: float | None = None
    degeneracy: int = 1

    @model_validator(mode="after")
    def _at_least_one_energy(self) -> "ConformerFact":
        """Reject a member carrying neither an absolute nor a relative energy.

        A conformer with no energy at all is not a conformer anyone can rank, and storing one would
        put a row in the ensemble table that every ensemble query has to filter back out.
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

    model_config = ConfigDict(extra="forbid", frozen=True)

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

    model_config = ConfigDict(extra="forbid", frozen=True)

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
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    # **Empty means "whatever the sink calls this deployment", and empty is the normal case.** A
    # record is sink-agnostic by construction — the same projected record goes to every enabled
    # sink — so the tenant cannot be known when the record is built, only when it is written.
    # `dialect.rows_for` substitutes the manifest's `tenant_id` for an empty one. A non-empty value
    # here is a deliberate override, for a record being republished on behalf of another
    # deployment.
    tenant_id: str = ""
    actor: str = ""
    session_id: str = ""
    correlation_id: str = ""
    job_id: str = ""
    rationale: str = ""


class ResultRecord(BaseModel):
    """One computed result, in the shape it is published in.

    The whole cross-system contract: what the projection produces, what a driver writes, and what
    the shipped DDL stores. Frozen and `extra="forbid"`, so a field added on one side of that
    contract and not the other fails loudly here rather than silently downstream.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    # --- identity -------------------------------------------------------------------------
    # The flat cache key, `calc_type@calc_version:input_hash:params_hash` — the same string a
    # knowledge note cites and `find_calculations` resolves, so a published row and a note about it
    # name the calculation identically.
    calc_ref: str = Field(min_length=1)
    calc_type: str = Field(min_length=1)
    calc_version: str = ""
    input_hash: str = ""
    params_hash: str = ""

    # --- what it was about ----------------------------------------------------------------
    subject: Subject
    conditions: Conditions = Field(default_factory=Conditions)
    level: TheoryLevel
    # The geometry the calculation ran **on**, never the one it produced — migration 048's meaning,
    # kept, because that is the question a chemist holding a conformer's address actually asks.
    # Empty for a molecule-keyed calculator, which reads as "not recorded" rather than "none".
    structure_id: str = ""

    # --- the facts ------------------------------------------------------------------------
    properties: list[PropertyFact] = Field(default_factory=list)
    sites: list[SiteFact] = Field(default_factory=list)
    points: list[PointFact] = Field(default_factory=list)
    conformers: list[ConformerFact] = Field(default_factory=list)
    candidates: list[CandidateFact] = Field(default_factory=list)
    flags: list[FlagFact] = Field(default_factory=list)
    # **No `artifacts` list, deliberately.** One shipped here, with an `ArtifactFact` model, a
    # `calculation_artifact` table in `TABLE_ORDER` and a row builder to fill it — and no producer
    # at any layer: no projector returned an `artifacts` key and `project()` never read one, so
    # the list was empty on every record this system could build while a site was still required
    # to create the table for delivery to work at all. Deleted rather than left half-wired, on
    # `D-2026-08-26-an-attribution-nothing-can-write-is-not-an-attribution`'s rule; the local
    # `calculation_artifacts` table (migration 019) remains this deployment's own record of a
    # calculation's by-products. Restoring it means shipping the producer in the same change, and
    # `tests/test_publish_dialect.py` fails whoever does not.

    # --- provenance -----------------------------------------------------------------------
    provenance: str = "computed"  # computed | measured | imported
    compute_seconds: float | None = None
    computed_at: datetime | None = None
    # The calculations this one rested on. Published as edges rather than an array column, because
    # staleness propagation walks them in reverse and no array type indexes that direction — and
    # because neither Snowflake nor Oracle has one.
    depends_on: list[str] = Field(default_factory=list)
    publications: list[Publication] = Field(default_factory=list)

    # --- the original ---------------------------------------------------------------------
    # The payload exactly as it was stored, never a predicate. This is what makes the projection
    # safe to be wrong: every fact above can be rebuilt from it by re-projecting, so a projector bug
    # is a replay rather than a data loss.
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

        Caught here rather than by a foreign key at the far end, because the far end may be a
        warehouse that does not enforce one — Snowflake accepts a `REFERENCES` clause and never
        checks it. An off-by-one in a per-species projection would then land silently and make
        every per-species query under-return.
        """
        ordinals = {member.ordinal for member in self.subject.members}
        for fact in self.properties:
            if fact.member_ordinal is not None and fact.member_ordinal not in ordinals:
                raise ValueError(
                    f"property {fact.property!r} names member {fact.member_ordinal}, but this "
                    f"subject has members {sorted(ordinals)}"
                )
        return self
