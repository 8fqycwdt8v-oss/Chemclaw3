"""One facet query, and the honest denominator every answer over it must carry.

The six precedent questions look like six searches and are one, asked with different fields filled
in. "Has this substrate been used as starting material" is a species plus a role. "Which ligands
for Buchwald couplings" is a name plus a role. "Workhorse conditions for a Buchwald whose product
carries this group" is a name plus a functional group. "How do we work this up with this reagent"
is a species. So there is one `Facet`, one selection over it, and five presentations above — which
is the difference between five queries that agree and five that drift.

**Every answer carries `CorpusCoverage`, and it is scoped to the facet's rows.** A count over a
half-labelled corpus is a lower bound, and a lower bound presented as a total is the failure mode
this whole subsystem is most exposed to: "which ligands were used for Buchwald couplings" answered
from 3% of the corpus reads exactly like the complete answer. Scoping to the corpus rather than to
the facet would be a different lie, because the drain does not proceed uniformly across reaction
types.
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
    sources: frozenset[str] = Field(
        default_factory=frozenset,
        description="Restrict to these registry sources. Empty means all.",
    )

    def is_open(self) -> bool:
        """Whether this facet narrows nothing — the whole-index case, meaningful only as a total."""
        return not any(
            (
                self.named_reaction,
                self.rxno_id,
                self.species_smiles,
                self.product_smiles,
                self.product_functional_group,
                self.sources,
            )
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
    in the denominator would make every ligand look rarer than it is.
    """

    model_config = ConfigDict(frozen=True)

    role: SpeciesRole
    smiles: str
    count: int = Field(ge=1)
    share: float = Field(ge=0.0, le=1.0)
    median_yield_percent: float | None = None


class FrequencyReport(BaseModel):
    """A roll-up of what the facet's reactions actually used, by role, most common first."""

    model_config = ConfigDict(frozen=True)

    agents: list[AgentCount] = Field(default_factory=list)
    reactions_in_scope: int = Field(
        default=0, ge=0, description="Labelled reactions the roll-up counted over."
    )
    coverage: CorpusCoverage
    truncated: bool = False

    @computed_field  # type: ignore[prop-decorator]
    @property
    def verdict(self) -> str:
        """What the reader must know before quoting a number from this table.

        A `computed_field` and not a bare property, the same call `FingerprintSearch.verdict` makes
        and for the same measured reason: a plain property is not serialized, so the sentence
        explaining what the counts mean never leaves this process.
        """
        if not self.agents:
            return (
                "NO COUNTS: no labelled reaction matching this facet recorded a species in the "
                f"requested role(s). {self.coverage.verdict} Do not report this as 'no ligand was "
                "used' or 'none is known' — it means nothing matching was labelled and recorded."
            )
        head = (
            f"Counted over {self.reactions_in_scope} labelled reaction(s). Popularity is not "
            "suitability: a frequent reagent is the field's default, not a recommendation for this "
            "substrate."
        )
        if self.truncated:
            return (
                f"PARTIAL: {head} The selection hit its row cap, so these counts are a lower bound "
                f"over a sample rather than totals. {self.coverage.verdict}"
            )
        return f"{head} {self.coverage.verdict}"
