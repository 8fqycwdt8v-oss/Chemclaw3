"""The two rows of the reaction-label index, and what each phase of writing them may set.

**The two-phase split is the design, not an implementation detail.** A reaction reaches this index
twice:

* The **record phase** is written by whoever ingested the reaction, from the canonical record in
  hand. It has to be. `OrdReaction.transformation_smiles()` — the string `reaction_fingerprints`
  stores — deliberately drops solvent and catalyst, because leaving them in let a solvent swap
  dominate DRFP similarity (measured: 0.82 for one coupling in THF vs 2-MeTHF, 1.00 once excluded).
  That is right for a fingerprint and fatal for a label index, whose whole job is to answer *which
  solvent, which ligand, which base*. And there is no second chance to ask: `ElnAdapter` offers
  `fetch_new_entries(since)` and nothing that reads one entry back by id.
* The **derived phase** is written later by the background labeller, from the record phase's own
  `record_smiles`.

`labeller_version` is what separates them, and it is the reason "which entries are missing labels"
is a `WHERE` clause instead of a flag somebody has to remember to set. NULL means never derived; a
value below the current one means derived by a superseded labeller. Both are stale, and both are
found by one indexed scan. This is `note_index.fingerprint` (`infra/sql/035`) and
`document_chunks.embedding_key` (`038`) applied to a third kind of derived data.
"""

from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field, computed_field

from chemclaw.science.labels.vocabulary import SpeciesRole


class SpeciesLabel(BaseModel):
    """One species of one reaction: what it is, what it was recorded as, what it was derived as.

    No fingerprint bits, no InChIKey, no formula, no molecular weight — deliberately. A 13M-reaction
    corpus is ~65M of these rows over ~4M *distinct* structures, so a per-row fingerprint is a 16x
    duplication and an HNSW index that will not build; the bits live once per structure in
    `corpus_molecules`, joined by `smiles` (already `standard_smiles`, so it joins by value with no
    surrogate key). The identity fields have no caller among the questions this index exists to
    answer, and this tree deletes dead fields on sight.

    `scaffold` and `functional_groups` are the exceptions that earn their place: a Bemis-Murcko
    scaffold buys an exact `GROUP BY` that similarity cannot, and the Ertl group names answer
    "a product containing this functional group" by array containment with no SMARTS matching at
    query time.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    ordinal: int = Field(ge=0, description="Position within the reaction; part of the row key.")
    smiles: str = Field(min_length=1, description="`standard_smiles` of the species.")
    role: str = Field(
        min_length=1,
        description="The recorded `Role` value, verbatim — what the source said, never a guess.",
    )
    derived_role: SpeciesRole | None = Field(
        default=None,
        description=(
            "The refined role, or None until a labeller has looked. `SpeciesRole.UNKNOWN` is a "
            "different answer: it means a labeller looked and could not decide."
        ),
    )
    scaffold: str | None = Field(default=None, description="Bemis-Murcko scaffold, once derived.")
    functional_groups: list[str] = Field(
        default_factory=list, description="Ertl functional-group names, once derived."
    )


class ReactionLabel(BaseModel):
    """One reaction in the label index: its record phase, and whatever has been derived of it.

    `(source, reaction_id)` is the key, and the source half is not decoration: two ELNs may
    legitimately use one entry id, which `reaction_fingerprints` — keyed on the bare id — cannot
    represent.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    # --- identity ---
    source: str = Field(min_length=1, description="The registry source name (the folder name).")
    reaction_id: str = Field(min_length=1, description="The source's own id for this reaction.")

    # --- record phase ---
    record_smiles: str = Field(
        min_length=1,
        description=(
            "`reactants>agents>products` with the agents **kept** — the record form, not the "
            "fingerprint form. See this module's docstring for why the distinction is the design."
        ),
    )
    citation: str = Field(
        min_length=1,
        description=(
            "What an answer cites for this row: a note id for an ELN entry, a patent number for a "
            "corpus row. A precedent the chemist cannot follow back is not a precedent."
        ),
    )
    performed_on: date | None = None
    temperature_c: float | None = None
    time_h: float | None = Field(default=None, ge=0.0)
    yield_percent: float | None = Field(default=None, ge=0.0, le=100.0)
    workup_text: str | None = Field(
        default=None,
        description=(
            "The workup steps' verbatim instructions. Stored because 'how do we work this up' is "
            "the one precedent question no structural index can answer at all."
        ),
    )
    species: list[SpeciesLabel] = Field(default_factory=list)

    # --- derived phase ---
    mapped_smiles: str | None = None
    named_reaction: str | None = None
    reaction_class: str | None = None
    rxno_id: str | None = Field(
        default=None,
        description=(
            "The RXNO ontology id. The vocabulary-independent key: NameRxn, Rxn-INSIGHT and RXNO "
            "are three different name strings for one transformation, and matching on the string "
            "answers from whichever fraction of the corpus happened to use that one."
        ),
    )
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    method: str | None = Field(
        default=None, description="How the name was arrived at: 'source', 'smirks' or 'model'."
    )
    labeller_version: str | None = Field(
        default=None,
        description="NULL = never derived. Below the current value = derived by a superseded run.",
    )
    labelled_at: datetime | None = None


class CorpusCoverage(BaseModel):
    """How much of the row set an answer was drawn from is actually labelled — and the sentence.

    Scoped to the **facet's** rows, never to the whole corpus, because the two are different claims
    and only one of them is useful: "3% of the patent corpus is labelled" read as "3% of the
    Buchwalds are labelled" is a different lie, and the labelling drain does not proceed uniformly
    across reaction types.

    `verdict` is a `computed_field` and not a bare property for the reason
    `FingerprintSearch.verdict` spells out at length: a plain property is not serialized, so the
    one sentence that explains what the numbers mean never leaves this process. That lesson was
    learned on a hazard screen that told a chemist "no hazards detected" six times.
    """

    labelled: int = Field(ge=0, description="Rows in scope carrying the current labeller version.")
    total: int = Field(ge=0, description="Rows in scope at all, labelled or not.")
    sources: list[str] = Field(default_factory=list, description="Which sources the scope spans.")

    @computed_field  # type: ignore[prop-decorator]
    @property
    def verdict(self) -> str:
        """What the reader must know about this answer's denominator before quoting it."""
        if self.total == 0:
            return (
                "NO ROWS IN SCOPE: nothing in the reaction-label index matched this facet at all, "
                "so the question was not answered. This is NOT evidence that no such reaction "
                "exists — report that the corpus may not be indexed and say which sources are "
                "configured."
            )
        if self.labelled == 0:
            return (
                f"NOT ANSWERABLE YET: {self.total} reaction(s) match this facet and NONE of them "
                "have been labelled at the current labeller version, so no role, name or "
                "structure feature could be read. Report that the labelling backfill has not "
                "reached these rows. Do not present this as a finding about the chemistry."
            )
        if self.labelled < self.total:
            share = 100 * self.labelled / self.total
            return (
                f"PARTIAL: this answer is drawn from {self.labelled} of {self.total} matching "
                f"reaction(s) ({share:.0f}%) — the rest are not yet labelled at the current "
                "version. Treat counts as a lower bound and say so; a reagent absent here may "
                "simply live in an unlabelled row."
            )
        return (
            f"COMPLETE: all {self.total} matching reaction(s) are labelled at the current version, "
            "so counts over this facet are totals rather than lower bounds."
        )
