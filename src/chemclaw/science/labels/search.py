"""The precedent questions, as thin shapings of one facet query.

Each of these is a thin shaping of `LabelIndex.select` or `agent_counts`, and that is the point:
several presentations of one query cannot drift the way several queries would. What each function
contributes is the *pre-pass* that turns a chemist's question into a facet — a similarity search
over `corpus_molecules` for "products like this", a DRFP search over `corpus_reactions` for
"transformations like this", a substructure screen for "products containing this", a role for "as
starting material".

Every answer carries a coverage sentence. On a half-labelled corpus a count is a lower bound, and
a lower bound presented as a total is the failure this subsystem is most exposed to.
"""

from pydantic import BaseModel, ConfigDict, Field, computed_field

from chemclaw.core.config import settings
from chemclaw.science.fingerprints.molfp.fingerprint import ecfp_bitstring
from chemclaw.science.fingerprints.rxnfp.fingerprint import drfp_bitstring
from chemclaw.science.fingerprints.store import FingerprintStore, find_matches
from chemclaw.science.labels.facets import Facet, FrequencyReport
from chemclaw.science.labels.molecules import CorpusMolecules
from chemclaw.science.labels.records import CorpusCoverage, ReactionLabel
from chemclaw.science.labels.store import LabelIndex
from chemclaw.science.labels.vocabulary import SpeciesRole


class Precedent(BaseModel):
    """One recorded reaction, in the shape an answer quotes it: what, under what, cite it here."""

    model_config = ConfigDict(frozen=True)

    source: str
    reaction_id: str
    citation: str
    reaction_smiles: str
    named_reaction: str | None = None
    temperature_c: float | None = None
    time_h: float | None = None
    yield_percent: float | None = None
    workup_text: str | None = None
    agents: dict[str, list[str]] = Field(
        default_factory=dict,
        description=(
            "The species that were not substrates or products, grouped by derived role — the "
            "recipe. This is the whole reason the record phase keeps the agents the fingerprint "
            "drops."
        ),
    )

    @classmethod
    def of(cls, row: ReactionLabel) -> "Precedent":
        """Shape one stored row into the answer form, grouping its agents by role."""
        agents: dict[str, list[str]] = {}
        for species in row.species:
            role = species.derived_role
            if role is None or role in _SUBSTRATE_ROLES:
                continue
            agents.setdefault(role.value, []).append(species.smiles)
        return cls(
            source=row.source,
            reaction_id=row.reaction_id,
            citation=row.citation,
            reaction_smiles=row.record_smiles,
            named_reaction=row.named_reaction,
            temperature_c=row.temperature_c,
            time_h=row.time_h,
            yield_percent=row.yield_percent,
            workup_text=row.workup_text,
            agents=agents,
        )


class PrecedentSearch(BaseModel):
    """Precedents, whether the page was capped, and what fraction of the scope was labelled."""

    model_config = ConfigDict(frozen=True)

    question: str = Field(description="What was asked, restated — so the answer cannot drift.")
    hits: list[Precedent] = Field(default_factory=list)
    coverage: CorpusCoverage
    truncated: bool = False

    @computed_field  # type: ignore[prop-decorator]
    @property
    def verdict(self) -> str:
        """The one sentence the model must read before writing an answer from this.

        A `computed_field` rather than a bare property, for the reason `FingerprintSearch.verdict`
        records at length: a plain property is not serialized, so the caveat never leaves this
        process — a lesson learned on a hazard screen that told a chemist "no hazards detected"
        six times.
        """
        if not self.hits:
            return (
                "NO PRECEDENT FOUND IN THE LABELLED CORPUS. "
                f"{self.coverage.verdict} Say which of those two this is before concluding "
                "anything: an unlabelled corpus cannot answer, and an empty answer from a labelled "
                "one is a genuine negative."
            )
        head = f"{len(self.hits)} precedent(s) found."
        if self.truncated:
            return (
                f"PARTIAL: {head} The result hit its page cap, so this is a sample rather than the "
                f"complete set. {self.coverage.verdict}"
            )
        return f"{head} {self.coverage.verdict}"


# Roles a species holds when it is *what the reaction is about* rather than how it was run. The
# recipe is everything else, which is what `Precedent.agents` reports.
_SUBSTRATE_ROLES = frozenset({SpeciesRole.STARTING_MATERIAL, SpeciesRole.PRODUCT})


async def substrate_precedents(
    index: LabelIndex,
    version: str,
    smiles: str,
    *,
    role: SpeciesRole | None = None,
    limit: int | None = None,
) -> PrecedentSearch:
    """Answers: has this substrate been used in other reactions, and as what?

    Exact on the standardized structure, because the question is about *this* compound. For "like
    this", the caller runs a similarity pass first and asks about each neighbour — kept separate so
    that a hit is never a near-miss the answer presents as a match.
    """
    facet = Facet(
        species_smiles=smiles,
        species_roles=frozenset({role}) if role is not None else frozenset(),
    )
    asked = f"reactions using {smiles}" + (f" as {role.value}" if role else " in any role")
    return await _search(index, facet, version, asked, limit)


async def conditions_for_similar_products(
    index: LabelIndex,
    fingerprints: FingerprintStore,
    version: str,
    product_smiles: str,
    *,
    threshold: float | None = None,
    limit: int | None = None,
) -> PrecedentSearch:
    """Answers: give me conditions that worked for similar products.

    Two passes, and the split is what makes the answer honest. Structural neighbours are found in
    fingerprint space — where "similar" is defined and measurable — and only then are their
    reactions looked up. A single query cannot do that, because similarity is not a SQL predicate;
    and doing it the other way round (select reactions, then rank) would rank whatever the row cap
    happened to admit.
    """
    matches, _ = await find_matches(fingerprints, ecfp_bitstring(product_smiles), limit, threshold)
    if not matches:
        # An empty neighbour set is not an empty answer, and the difference has to survive: the
        # facet below would otherwise be open and select the whole corpus.
        coverage = await index.coverage(version)
        return PrecedentSearch(
            question=f"conditions for products similar to {product_smiles}", coverage=coverage
        )
    facet = Facet(product_smiles=frozenset(m.id for m in matches))
    asked = f"conditions for products similar to {product_smiles}"
    return await _search(index, facet, version, asked, limit)


async def conditions_for_similar_reactions(
    index: LabelIndex,
    fingerprints: FingerprintStore,
    version: str,
    reaction_smiles: str,
    *,
    threshold: float | None = None,
    limit: int | None = None,
) -> PrecedentSearch:
    """Answers: has this *transformation* been done, and under what conditions?

    The transformation-space twin of `conditions_for_similar_products`, and the reason
    `corpus_reactions` is written at all: without it a bulk source is searchable by structure and
    never by what the reaction actually does, so "have I got precedent for this coupling" answers
    from product similarity alone — which cannot tell a Buchwald from a Suzuki that happens to make
    the same biaryl.

    Same two passes and the same reason: neighbours are found in DRFP space, where similarity is
    defined and measurable, and only then are their recorded conditions looked up.

    `fingerprints` is the *corpus* reaction index (`science.labels.reactions.corpus_reactions`),
    never `reaction_fingerprints`. The two are keyed differently on purpose — this one by
    `<source>:<reaction_id>`, which is what `Facet.reaction_keys` narrows on, while the ELN index
    is keyed on the bare id and its hits are note ids `similar_reactions` resolves instead.
    """
    matches, _ = await find_matches(fingerprints, drfp_bitstring(reaction_smiles), limit, threshold)
    asked = f"conditions recorded for reactions similar to {reaction_smiles}"
    if not matches:
        # An empty neighbour set is not an empty answer — the same distinction the product twin
        # makes, and for the same reason: an open facet would select the whole corpus.
        return PrecedentSearch(question=asked, coverage=await index.coverage(version))
    facet = Facet(reaction_keys=frozenset(m.id for m in matches))
    return await _search(index, facet, version, asked, limit)


async def workup_precedents(
    index: LabelIndex, version: str, reagent_smiles: str, *, limit: int | None = None
) -> PrecedentSearch:
    """Answers: how do we best work up reactions with this reagent?

    The one question no structural index can answer at all: it is answered by showing a chemist
    what other people actually did, so the hits are filtered to those that recorded a workup. A
    reaction that used the reagent and wrote nothing down is not a workup precedent, and returning
    it with an empty field would pad the answer with rows that say nothing.
    """
    facet = Facet(species_smiles=reagent_smiles)
    found = await _search(
        index, facet, version, f"workups recorded for reactions using {reagent_smiles}", limit
    )
    return found.model_copy(update={"hits": [h for h in found.hits if h.workup_text]})


async def agent_frequency(
    index: LabelIndex,
    version: str,
    *,
    named_reaction: str | None = None,
    rxno_id: str | None = None,
    product_functional_group: str | None = None,
    product_smiles: frozenset[str] = frozenset(),
    roles: frozenset[SpeciesRole] = frozenset(),
    limit: int | None = None,
) -> FrequencyReport:
    """Answers both "which ligands for Buchwald couplings" and "what are the workhorse conditions".

    One function for both, because they are one query with a different role filter: the first names
    `roles={LIGAND}`, the second leaves `roles` empty and gets every role back, which is what
    "conditions" means. Narrowing by `product_functional_group` is what makes the second question's
    "with a product carrying this group" a facet rather than a second search.
    """
    facet = Facet(
        named_reaction=named_reaction,
        rxno_id=rxno_id,
        product_functional_group=product_functional_group,
        product_smiles=product_smiles,
    )
    return await index.agent_counts(facet, version, roles, _page(limit))


async def reactions_with_product_substructure(
    index: LabelIndex,
    molecules: CorpusMolecules,
    version: str,
    smarts: str,
    *,
    named_reaction: str | None = None,
    limit: int | None = None,
) -> PrecedentSearch:
    """Reactions whose *product* contains a SMARTS pattern, optionally of one named reaction.

    Screen-then-verify over `corpus_molecules` finds the structures; the facet finds their
    reactions. The screen's truncation is carried through rather than dropped, because a capped
    screen that found nothing is not a corpus that contains nothing.
    """
    page = _page(limit)
    products, screen_truncated = await molecules.containing(
        smarts, settings.substructure_scan_max_records
    )
    asked = f"reactions making a product matching {smarts}"
    if not products:
        coverage = await index.coverage(version)
        return PrecedentSearch(question=asked, coverage=coverage, truncated=screen_truncated)
    facet = Facet(product_smiles=frozenset(products), named_reaction=named_reaction)
    found = await _search(index, facet, version, asked, page)
    return found.model_copy(update={"truncated": found.truncated or screen_truncated})


async def _search(
    index: LabelIndex, facet: Facet, version: str, question: str, limit: int | None
) -> PrecedentSearch:
    """Run one facet and shape it, so every entry point above reports identically."""
    selection = await index.select(facet, version, _page(limit))
    return PrecedentSearch(
        question=question,
        hits=[Precedent.of(row) for row in selection.rows],
        coverage=selection.coverage,
        truncated=selection.truncated,
    )


def _page(limit: int | None) -> int:
    """The page size, clamped — the one chokepoint a model-supplied `limit` passes through.

    Reuses the fingerprint knobs rather than introducing `precedent_top_k`/`precedent_max_top_k`,
    because they bound the same hazard (a value from a tool argument landing in a SQL `LIMIT`) and
    a second pair that means almost the same thing is how two expressions of one condition come to
    disagree.
    """
    page = limit if limit is not None else settings.fingerprint_top_k
    return min(max(page, 1), settings.fingerprint_max_top_k)
