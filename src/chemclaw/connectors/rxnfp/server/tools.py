"""The `rxnfp` bundle's MCP tool surface: reaction similarity, and the faceted precedent search.

Declaration, not logic: each function delegates to `chemclaw.science.fingerprints.rxnfp` or
`chemclaw.science.labels`. `app.py` serves it over HTTP; `main()` runs it over stdio. Judgment
stays out (see the `molfp` twin). The facet tools live here because they share the store, RDKit,
the pool, the pod and the `reaction-search` skill with `similar_reactions`.

Every result also states what it did not search: citation-only ELN records have no fingerprint
and no label row, so each carries `ReactionRecordStore.citation_only` and its verdict says so.
Facet answers are over the corpus at the index's current label version, asked of the index rather
than the labelling server so search does not depend on a background service.
"""

from mcp.server.fastmcp import FastMCP

from chemclaw.ingest.eln.records import ReactionRecordStore, default_record_store
from chemclaw.kg.note import note_id_for_reaction
from chemclaw.science.fingerprints.rxnfp.search import find_similar_reactions
from chemclaw.science.fingerprints.store import (
    FingerprintSearch,
    FingerprintStore,
    Match,
    default_reaction_store,
    log_index_size,
)
from chemclaw.science.labels.facets import FrequencyReport
from chemclaw.science.labels.molecules import CorpusMolecules, corpus_fingerprints
from chemclaw.science.labels.reactions import corpus_reactions
from chemclaw.science.labels.records import CorpusCoverage
from chemclaw.science.labels.search import (
    PrecedentSearch,
    agent_frequency,
    conditions_for_similar_products,
    conditions_for_similar_reactions,
    reactions_with_product_substructure,
    substrate_precedents,
    workup_precedents,
)
from chemclaw.science.labels.store import LabelIndex, default_label_index
from chemclaw.science.labels.vocabulary import SpeciesRole

server = FastMCP("mcp-rxnfp")
_store: FingerprintStore = default_reaction_store()
# The transcription store, for the one question the fingerprint index cannot answer: whether the
# run a hit stands for may be served at all — withdrawn by its source, or citation-only.
_records: ReactionRecordStore = default_record_store()
_labels: LabelIndex = default_label_index()
_molecules = CorpusMolecules()


@server.tool()
async def similar_reactions(
    reaction_smiles: str, top_k: int | None = None, threshold: float | None = None
) -> FingerprintSearch[Match]:
    """Find stored reactions similar to `reaction_smiles`, most similar first.

    Each hit's `id` is the reaction's **note id**, so it can be passed straight to `expand_note`
    for the full recipe. `top_k` and `threshold` (Tanimoto floor) default to the configured values.

    **Read `verdict` before answering.** Empty `hits` with `index_empty: true` means no reaction
    has been indexed and the question was not answered — never report it as "we have no precedent".
    `hits_truncated: true` means more reactions cleared the threshold than `top_k` could return, so
    the count is a lower bound on the precedent on file, not the amount of it.
    And `approximate: true` means this deployment searched the index approximately — the
    neighbours returned are the best it proposed, not provably the best on file, so a closer
    precedent may exist and the list is not definitive. The two are independent: a result can
    be both, and `verdict` says so when it is.
    """
    search = await find_similar_reactions(_store, reaction_smiles, top_k, threshold)
    # The index knows bits and a label; whether the run still stands is the record store's. A hit
    # whose
    # record is missing is still served (unindexed is not withdrawn); a citation-only record, which
    # can
    # only be a stale row here, is dropped.
    withdrawn = await _records.structurally_withheld(
        [(match.source, match.id) for match in search.hits]
    )
    return search.model_copy(
        update={
            "unsearched": await _records.citation_only(reaction_smiles),
            # The cited id names its source, because fingerprints are keyed by `(source, id)` and a
            # bare id two
            # sites hold resolves to neither.
            "hits": [
                match.model_copy(update={"id": note_id_for_reaction(match.id, match.source)})
                for match in search.hits
                if (match.source, match.id) not in withdrawn
            ],
        }
    )


async def _disclosed(coverage: CorpusCoverage, query: str | None) -> CorpusCoverage:
    """`coverage` with the citation-only records outside the label index stated beside it.

    On the coverage because `CorpusCoverage.verdict` is the denominator sentence every facet answer
    quotes.
    """
    return coverage.model_copy(update={"unsearched": await _records.citation_only(query)})


async def _precedents(search: PrecedentSearch, query: str | None) -> PrecedentSearch:
    """`search` with its coverage `_disclosed` for `query`."""
    return search.model_copy(update={"coverage": await _disclosed(search.coverage, query)})


async def _unlabelled(question: str, query: str | None) -> PrecedentSearch:
    """The answer when nothing in the index has been labelled yet.

    Never a bare empty list, which reads as "no such reaction". Coverage is asked under a version
    nothing carries, so it reports the real corpus size with zero labelled.
    """
    coverage = await _labels.coverage("never-labelled")
    return PrecedentSearch(question=question, coverage=await _disclosed(coverage, query))


def _roles(names: list[str] | None) -> frozenset[SpeciesRole]:
    """Role names as members, refusing one this vocabulary does not have.

    Strict, unlike `merge._role` for stored roles: a mistyped query role must not silently match
    nothing.
    """
    if not names:
        return frozenset()
    return frozenset(SpeciesRole(name) for name in names)


@server.tool()
async def substrate_precedent(
    smiles: str, role: str | None = None, top_k: int | None = None
) -> PrecedentSearch:
    """Reactions that used this exact structure, optionally only in one role.

    Answers "has this substrate been used in other reactions as starting material?". `role` is one
    of `starting-material`, `product`, `reagent`, `solvent`, `catalyst`, `ligand`, `base`,
    `additive`; omit it for any role. Matching is exact on the standardized structure — for "like
    this", use `similar_molecules` first and ask about each neighbour, so a hit is never a
    near-miss presented as a match.

    **Read `verdict` before answering.** It says whether an empty result means "no precedent" or
    "nothing matching has been labelled yet", and what fraction of the matching corpus the counts
    were drawn from.
    """
    version = await _labels.current_version()
    if version is None:
        return await _unlabelled(f"reactions using {smiles}", smiles)
    search = await substrate_precedents(
        _labels, version, smiles, role=SpeciesRole(role) if role else None, limit=top_k
    )
    return await _precedents(search, smiles)


@server.tool()
async def conditions_for_similar_product(
    product_smiles: str, threshold: float | None = None, top_k: int | None = None
) -> PrecedentSearch:
    """Recorded conditions from reactions that made structurally similar products.

    Answers "give me conditions that worked for similar products". Two passes: neighbours are found
    in ECFP4 fingerprint space, then their reactions are looked up — so "similar" means a Tanimoto
    a chemist can check, not whatever a text filter happened to admit. Each hit carries its recipe
    grouped by role (`agents`), its temperature, time and yield, and the document to cite.

    **Read `verdict`.** An empty result with no neighbours is not the same as no precedent.
    """
    version = await _labels.current_version()
    if version is None:
        return await _unlabelled(
            f"conditions for products similar to {product_smiles}", product_smiles
        )
    search = await conditions_for_similar_products(
        _labels, corpus_fingerprints(), version, product_smiles, threshold=threshold, limit=top_k
    )
    return await _precedents(search, product_smiles)


@server.tool()
async def conditions_for_similar_reaction(
    reaction_smiles: str, threshold: float | None = None, top_k: int | None = None
) -> PrecedentSearch:
    """Recorded conditions from reactions that ran a structurally similar *transformation*.

    Answers "has this reaction been done, and what worked". Two passes: neighbours are found in DRFP
    reaction-fingerprint space, then their recorded conditions are looked up, so each hit carries
    its recipe grouped by role, its temperature, time and yield, and the document to cite.

    **Query with `reactants>>products` — the two core substrates and the product, no reagents.**
    The index is built with the agent slot *excluded*, because DRFP folds agents onto the reactants
    and a solvent swap otherwise dominates the score. So naming a ligand, base or solvent in the
    query adds features the indexed rows do not have and pushes a real precedent *below* the
    threshold. A three-part `reactants>agents>products` string is accepted and its agents are folded
    in, which is exactly the case to avoid here. Measured on one Buchwald against itself indexed
    without agents: naming **one solvent** scores 0.72-0.85 across six common ones (DMF 0.72, THF
    and toluene 0.76, dioxane 0.80, t-BuOH 0.82, MeCN 0.85), and a **realistic recipe** — ligand,
    base and solvent — scores **0.61**. So a query written the way a chemist describes a reaction
    can miss the identical precedent at any sensible threshold.

    **Prefer this over `conditions_for_similar_product` when you have the whole reaction.** Product
    similarity cannot tell a Buchwald from a Suzuki that happens to make the same biaryl; this can.
    Prefer `similar_reactions` when you want *our own* runs rather than the literature corpus —
    the two search different indexes and cite different things.

    **Read `verdict`.** An empty result with no neighbours is not the same as no precedent.
    """
    version = await _labels.current_version()
    if version is None:
        return await _unlabelled(
            f"conditions for reactions similar to {reaction_smiles}", reaction_smiles
        )
    search = await conditions_for_similar_reactions(
        _labels, corpus_reactions(), version, reaction_smiles, threshold=threshold, limit=top_k
    )
    return await _precedents(search, reaction_smiles)


@server.tool()
async def reagent_frequency(
    named_reaction: str | None = None,
    rxno_id: str | None = None,
    product_functional_group: str | None = None,
    roles: list[str] | None = None,
    top_k: int | None = None,
) -> FrequencyReport:
    """What the corpus actually used, counted by role — the "workhorse conditions" question.

    Answers both "which ligands were used for Buchwald couplings" (`roles=["ligand"]`) and "which
    workhorse conditions were used for a Buchwald whose product carries this group"
    (`product_functional_group=...`, `roles` omitted so every role comes back). Prefer `rxno_id`
    over `named_reaction` when you have one: NameRxn, Rxn-INSIGHT and RXNO are three name strings
    for one transformation, so matching the string answers from whichever fraction of the corpus
    used that spelling.

    **Popularity is not suitability, and `verdict` says so.** A frequent reagent is the field's
    default, not a recommendation for this substrate — and on a partly-labelled corpus the counts
    are a lower bound.
    """
    version = await _labels.current_version()
    if version is None:
        return FrequencyReport(
            coverage=await _disclosed(await _labels.coverage("never-labelled"), None)
        )
    # No structure in the question, so the count alone: a frequency over the label index cannot
    # name a record outside it, only say how many there are.
    report = await agent_frequency(
        _labels,
        version,
        named_reaction=named_reaction,
        rxno_id=rxno_id,
        product_functional_group=product_functional_group,
        roles=_roles(roles),
        limit=top_k,
    )
    return report.model_copy(update={"coverage": await _disclosed(report.coverage, None)})


@server.tool()
async def reactions_making_substructure(
    smarts: str, named_reaction: str | None = None, top_k: int | None = None
) -> PrecedentSearch:
    """Reactions whose *product* contains a SMARTS pattern, optionally of one named reaction.

    Answers "search for the same reaction part based on this SMARTS", from the product side. The
    corpus is screened with a pattern fingerprint and then verified exactly with RDKit, so a hit
    genuinely contains the motif and a miss genuinely does not — but the screen is capped, and
    `verdict` says when the cap was reached.
    """
    version = await _labels.current_version()
    if version is None:
        return await _unlabelled(f"reactions making a product matching {smarts}", smarts)
    search = await reactions_with_product_substructure(
        _labels, _molecules, version, smarts, named_reaction=named_reaction, limit=top_k
    )
    return await _precedents(search, smarts)


@server.tool()
async def workup_precedent(reagent_smiles: str, top_k: int | None = None) -> PrecedentSearch:
    """Verbatim workup instructions from reactions that used this reagent.

    Answers "how do we best work up reactions with this reagent?" — the one precedent question no
    structural index can answer, because it is answered by showing what other people actually did.
    Only reactions that recorded a workup are returned; one that used the reagent and wrote nothing
    down is not a workup precedent.
    """
    version = await _labels.current_version()
    if version is None:
        return await _unlabelled(
            f"workups recorded for reactions using {reagent_smiles}", reagent_smiles
        )
    search = await workup_precedents(_labels, version, reagent_smiles, limit=top_k)
    return await _precedents(search, reagent_smiles)


async def report_index_size() -> None:
    """Log this connector's index size at startup (see the `molfp` twin for the full note)."""
    await log_index_size(_store, "reaction")


def main() -> None:
    """Run the server over stdio (the default MCP transport)."""
    server.run()


if __name__ == "__main__":
    main()
