"""The two rows of the reaction-label index, and what each phase of writing them may set.

A reaction reaches this index twice:

* The **record phase** is written by the ingester from the canonical record in hand. It must be: the
  fingerprint's transformation string drops solvent and catalyst, which a label index exists to
  answer about, and `ElnAdapter` cannot read one entry back by id.
* The **derived phase** is written later by the background labeller from the record phase's
  `record_smiles`.

`labeller_version` separates them: NULL means never derived, a value below the current one means
derived by a superseded labeller, and both are found by one indexed `WHERE` scan. `withheld` belongs
to neither phase: it follows the entry, because the app role cannot delete a row.

A citation-only record (`D-2026-10-23-a-facet-on-a-drawn-species-counts-the-citation-tier`) has a
row of the record phase only: its drawn species, conditions and yield, no `record_smiles`, and a
`LabelTier` that keeps the labeller away from it and the facet tools counting it apart.
"""

import math
from datetime import date, datetime
from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, computed_field, model_validator

from chemclaw.science.fingerprints.store import CitationOnlyRecords
from chemclaw.science.labels.vocabulary import SpeciesRole


class LabelTier(StrEnum):
    """The evidence tier of the entry a label row records (values as `ingest.eln.ord.RecordTier`).

    `science/` may not import `ingest/`, so the two spellings are held equal by
    `tests/test_label_vocabulary.py`.
    """

    STRUCTURED = "structured"
    CITATION_ONLY = "citation-only"


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
    tier: LabelTier = Field(
        default=LabelTier.STRUCTURED,
        description=(
            "`citation-only` for a record whose source named a species without its structure: its "
            "row holds what was drawn and stated, and nothing is ever derived for it."
        ),
    )
    record_smiles: str | None = Field(
        default=None,
        min_length=1,
        description=(
            "`reactants>agents>products` with the agents **kept** — the record form, not the "
            "fingerprint form. See this module's docstring for why the distinction is the design. "
            "None exactly when the row is citation-only: a reaction nobody ran has no string."
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
    # True while no facet may serve this row: its ELN entry was withdrawn by the source, or it is
    # the structured row an entry left behind when it was amended to name a species without its
    # structure (the next ingest of the entry replaces it). A corpus row is never withheld.
    withheld: bool = False

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

    @property
    def reaction_string(self) -> str:
        """`record_smiles` of a structured row; a citation-only row has none to hand a labeller."""
        if self.record_smiles is None:
            raise ValueError("a citation-only label row has no reaction string")
        return self.record_smiles

    @model_validator(mode="after")
    def _the_tier_decides_what_the_row_holds(self) -> Self:
        """A structured row has a reaction string; a citation-only row has that and nothing derived.

        The same rule the table's CHECK holds for the string, and the one the labeller's scan and
        `store_labels` rely on: an inference from a partial reaction never reaches the row.
        """
        if self.tier is LabelTier.STRUCTURED:
            if self.record_smiles is None:
                raise ValueError("a structured label row needs its record_smiles")
            return self
        derived = (
            self.record_smiles,
            self.mapped_smiles,
            self.named_reaction,
            self.reaction_class,
            self.rxno_id,
            self.confidence,
            self.method,
            self.labeller_version,
            self.labelled_at,
        )
        if any(value is not None for value in derived):
            raise ValueError(
                "a citation-only label row holds no reaction string and nothing derived: "
                "the source named a species without its structure"
            )
        return self


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

    labelled: int = Field(
        ge=0, description="Structured rows in scope carrying the current labeller version."
    )
    total: int = Field(
        ge=0, description="Structured rows in scope at all, labelled or not (not the other tier)."
    )
    sources: list[str] = Field(default_factory=list, description="Which sources the scope spans.")
    citation_only: int = Field(
        default=0,
        ge=0,
        description=(
            "Citation-only rows in scope. Counted in the answer beside the structured ones, never "
            "in `labelled` or `total`: nothing is derived for them. 0 whenever the facet cannot "
            "reach the tier (`citation_tier_searched` false)."
        ),
    )
    citation_tier_searched: bool = Field(
        default=False,
        description=(
            "Whether this facet looked in the citation-only tier. True for a question keyed on a "
            "drawn species or a role; false for one keyed on a named reaction, an atom-mapped or "
            "similar product, or a similarity neighbour, none of which a citation-only row can "
            "answer."
        ),
    )
    # ELN records the facet did not count (`None` means not asked): the tier when the facet cannot
    # reach it, or the records of it that have no label row yet.
    unsearched: CitationOnlyRecords | None = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def verdict(self) -> str:
        """What the reader must know about this answer's denominator before quoting it.

        `_labelled_verdict` covers the structured tier, `_tier_verdict` the citation-only one the
        facet counted, and the clause after them names the citation-only records it did not count,
        so a COMPLETE count is not read as the whole ELN.
        """
        parts = [self._labelled_verdict(), self._tier_verdict()]
        if self.unsearched is not None:
            parts.append(self.unsearched.verdict)
        return " ".join(part for part in parts if part)

    def _tier_verdict(self) -> str:
        """The sentence about the citation-only tier a facet counted; empty when it counted none."""
        if not (self.citation_tier_searched and self.citation_only):
            return ""
        return (
            f"CITATION-ONLY TIER: {self.citation_only} citation-only record(s) were searched "
            "beside the structured ones, and each one counted is marked `tier: citation-only`. "
            "The source named a species of each without its structure, so nothing is derived for "
            "them: no named reaction, atom map or refined role (a species keeps the source's own "
            "role — starting-material, reagent, catalyst or solvent — so a ligand, base or "
            "additive filter does not match one), and a species the source only named is never "
            "matched. "
            "Quote their numbers apart from the structured ones, and say which tier a figure is "
            "from."
        )

    def _labelled_verdict(self) -> str:
        """The sentence about the label index's own denominator."""
        if self.total == 0 and self.citation_tier_searched and self.citation_only:
            return (
                "NO STRUCTURED ROWS IN SCOPE: no structured reaction in the reaction-label index "
                "matched this facet's scope, so every figure here comes from the citation-only "
                "tier alone."
            )
        if self.total == 0:
            return (
                "NO ROWS IN SCOPE: nothing in the reaction-label index matched this facet at all, "
                "so the question was not answered. This is NOT evidence that no such reaction "
                "exists — report that the corpus may not be indexed and say which sources are "
                "configured."
            )
        if self.labelled == 0:
            return (
                f"NOT ANSWERABLE YET: {self.total} structured reaction(s) match this facet and "
                "NONE of them have been labelled at the current labeller version, so no role, name "
                "or structure feature could be read from them. Report that the labelling backfill "
                "has not reached these rows. Do not present this as a finding about the chemistry."
            )
        if self.labelled < self.total:
            # Truncate to a tenth and clamp to [0.1, 99.9], so a PARTIAL share never prints as 100%
            # or 0%.
            share = min(99.9, max(0.1, math.floor(1000 * self.labelled / self.total) / 10))
            return (
                f"PARTIAL: this answer is drawn from {self.labelled} of {self.total} matching "
                f"structured reaction(s) ({share:.1f}%) — the rest are not yet labelled at the "
                "current version. Treat structured counts as a lower bound and say so; a reagent "
                "absent here may simply live in an unlabelled row."
            )
        counted = (
            f" and {self.citation_only} citation-only record(s) are counted beside them"
            if self.citation_tier_searched and self.citation_only
            else ""
        )
        return (
            f"COMPLETE: all {self.total} matching structured reaction(s) in the label index are "
            f"labelled at the current version{counted}, so counts over this facet are totals "
            "over the structured records the index holds rather than lower bounds."
        )
