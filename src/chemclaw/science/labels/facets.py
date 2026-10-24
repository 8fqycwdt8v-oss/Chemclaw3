"""One facet query, and the honest denominator every answer over it must carry.

The precedent questions are one search with different fields filled in (species, role, named
reaction, functional group), so there is one `Facet`, one selection over it, and several
presentations, rather than several queries that drift.

Every answer carries `CorpusCoverage` scoped to the facet's rows: a count over a half-labelled
corpus is a lower bound, and the drain does not proceed uniformly across reaction types, so
corpus-wide coverage would mislead.
"""

from pydantic import BaseModel, ConfigDict, Field, computed_field

from chemclaw.science.labels.records import CorpusCoverage, ReactionLabel
from chemclaw.science.labels.vocabulary import SpeciesRole


class Facet(BaseModel):
    """Which reactions an answer is about. Every field is a narrowing; all of them AND together.

    A facet with nothing set selects the whole index, which is meaningful exactly once — as the
    denominator of an operator's "how much of this corpus is labelled".
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    named_reaction: str | None = Field(
        default=None, description="Exact match on the stored name, case-insensitive."
    )
    rxno_id: str | None = Field(
        default=None,
        description=(
            "Exact match on the RXNO ontology id — the vocabulary-independent key. Prefer it over "
            "`named_reaction` wherever the caller has one: NameRxn, Rxn-INSIGHT and RXNO are three "
            "name strings for one transformation, so matching the string answers from whichever "
            "fraction of the corpus happened to use that spelling, silently."
        ),
    )
    species_smiles: str | None = Field(
        default=None, description="A standardized structure the reaction must contain."
    )
    species_roles: frozenset[SpeciesRole] = Field(
        default_factory=frozenset,
        description="Roles `species_smiles` may hold. Empty means any role.",
    )
    product_smiles: frozenset[str] = Field(
        default_factory=frozenset,
        description=(
            "Restrict to reactions making one of these products — how a similarity pre-pass feeds "
            "this query: the neighbours are found in fingerprint space, then their reactions here."
        ),
    )
    product_functional_group: str | None = Field(
        default=None,
        description=(
            "An Ertl functional-group name the *product* must carry. Answered by array containment "
            "on `reaction_species.functional_groups`, so no SMARTS matching happens at query time."
        ),
    )
    reaction_keys: frozenset[tuple[str, str]] = Field(
        default_factory=frozenset,
        description=(
            "Restrict to these reactions, each a `(source, reaction_id)` pair — how a DRFP "
            "similarity pre-pass over `corpus_reactions` feeds this query, the way an ECFP "
            "pre-pass feeds `product_smiles`. The pair and not the bare id, because two sources "
            "may use one entry id and this is the narrowing that would otherwise merge them; a "
            "`Match` has carried both halves since "
            "`D-2026-08-27-a-fingerprint-is-keyed-by-its-source`, so nothing composes or splits a "
            "string to build it."
        ),
    )
    sources: frozenset[str] = Field(
        default_factory=frozenset,
        description="Restrict to these registry sources. Empty means all.",
    )

    @property
    def reaches_citation_only(self) -> bool:
        """Whether a citation-only row can answer this facet: it narrows on nothing derived.

        A citation-only row has a drawn species, a role the source recorded, conditions and a
        yield, and nothing derived: no named reaction, no product structure, no fingerprint for a
        neighbour to be found by. A facet narrowing on any of those is a structure or similarity
        question, which stays closed to the tier; every other facet counts it.
        """
        return not (
            self.named_reaction
            or self.rxno_id
            or self.product_smiles
            or self.product_functional_group
            or self.reaction_keys
        )


class FacetSelection(BaseModel):
    """The reactions a facet selected, what fraction of them is labelled, and whether it truncated.

    `truncated` is carried in the payload rather than logged for the reason
    `FingerprintSearch.scan_truncated` spells out: a truncation known only to the log cannot reach
    the model that writes the answer, and a capped page read as a total is the same defect in a
    different place.
    """

    model_config = ConfigDict(frozen=True)

    rows: list[ReactionLabel] = Field(default_factory=list)
    coverage: CorpusCoverage
    truncated: bool = False


class AgentCount(BaseModel):
    """One species in one role, and how often the facet's reactions used it.

    `share` is of the reactions that *named a species in this role*, not of all matching reactions —
    a reaction whose ligand nobody recorded is not evidence that no ligand was used, so counting it
    in the denominator would make every ligand look rarer than it is. The two tiers are counted in
    their own fields and medianed apart, because a citation-only record's role is the source's own
    word and a structured one's is refined from the structure: one number over both would not say
    which it describes.
    """

    model_config = ConfigDict(frozen=True)

    role: SpeciesRole
    smiles: str
    count: int = Field(ge=1, description="Reactions in both tiers: structured + citation-only.")
    structured_count: int = Field(ge=0, description="Of `count`, the structured reactions.")
    citation_only_count: int = Field(
        ge=0,
        description=(
            "Of `count`, the citation-only records: the source's own recorded role, nothing "
            "derived, a species it only named never counted."
        ),
    )
    share: float = Field(ge=0.0, le=1.0)
    median_yield_percent: float | None = Field(
        default=None, description="Median over the structured reactions' recorded yields only."
    )
    citation_only_median_yield_percent: float | None = Field(
        default=None, description="Median over the citation-only records' recorded yields only."
    )


class FrequencyReport(BaseModel):
    """A roll-up of what the facet's reactions actually used, by role, most common first."""

    model_config = ConfigDict(frozen=True)

    agents: list[AgentCount] = Field(default_factory=list)
    reactions_in_scope: int = Field(
        default=0,
        ge=0,
        description="Reactions counted over, in both tiers: structured + citation-only.",
    )
    structured_reactions: int = Field(
        default=0, ge=0, description="Of `reactions_in_scope`, the labelled structured ones."
    )
    citation_only_reactions: int = Field(
        default=0, ge=0, description="Of `reactions_in_scope`, the citation-only records."
    )
    coverage: CorpusCoverage
    truncated: bool = False

    @computed_field  # type: ignore[prop-decorator]
    @property
    def verdict(self) -> str:
        """What the reader must know before quoting a number from this table.

        A `computed_field` so the sentence is serialized with the result.
        """
        if not self.agents:
            return (
                "NO COUNTS: no labelled reaction matching this facet recorded a species in the "
                f"requested role(s). {self.coverage.verdict} Do not report this as 'no ligand was "
                "used' or 'none is known' — it means nothing matching was labelled and recorded."
            )
        head = (
            f"Counted over {self.reactions_in_scope} reaction(s): {self.structured_reactions} "
            f"labelled structured and {self.citation_only_reactions} citation-only, each agent's "
            "count split the same way. Popularity is not suitability: a frequent reagent is the "
            "field's default, not a recommendation for this substrate."
        )
        if self.truncated:
            return (
                f"PARTIAL: {head} The selection hit its row cap, so these counts are a lower bound "
                f"over a sample rather than totals. {self.coverage.verdict}"
            )
        return f"{head} {self.coverage.verdict}"
