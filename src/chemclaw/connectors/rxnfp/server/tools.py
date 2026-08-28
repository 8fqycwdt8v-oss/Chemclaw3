"""The `rxnfp` bundle's MCP tool surface: reaction similarity, and the faceted precedent search.

Declaration, not logic: each function delegates to `chemclaw.science.fingerprints.rxnfp` or
`chemclaw.science.labels`, and what this file contributes is the `@server.tool()` decoration the
agent sees. `app.py` serves it over HTTP; `main()` runs the same tools over stdio for running the
capability by hand. Judgment stays out (G6) — see the `molfp` twin for the full note.

**Why the facet tools are here and not in a new bundle.** They share the store, RDKit, the Postgres
pool, the pod and the `reaction-search` skill with `similar_reactions`; a second bundle would cost
a Deployment, a Service, a token, a chart entry, a port and a runbook paragraph for no isolation
(Rule of Three, and the third caller does not exist). The honest cost is that "rxnfp" now names
more than fingerprints, which is written down here and in
`D-2026-08-25-a-label-is-derived-not-recorded` rather than left for a reader to notice.

**Every one of them answers over the labelled corpus and says so.** The version searched is the one
the index is currently labelled at (`current_version`), asked of the index rather than of the
labelling server — the question "what is this corpus labelled at" is about our data, and a remote
call on the read path would make search depend on a background service being up.
"""

from mcp.server.fastmcp import FastMCP

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
    """
    search = await find_similar_reactions(_store, reaction_smiles, top_k, threshold)
    return search.model_copy(
        update={
            "hits": [
                match.model_copy(update={"id": note_id_for_reaction(match.id)})
                for match in search.hits
            ]
        }
    )


async def _unlabelled(question: str) -> PrecedentSearch:
    """The answer when nothing in the index has been labelled yet.

    Every facet tool routes through this rather than inventing its own way to say it, and the one
    thing none of them may do is return a bare empty list — which reads as "no such reaction
    exists". The coverage is asked for under a version nothing carries, so `total` is the real
    corpus size and `labelled` is zero: the sentence then says the corpus holds N reactions and
    none of them can answer yet, which is the true state.
    """
    return PrecedentSearch(question=question, coverage=await _labels.coverage("never-labelled"))


def _roles(names: list[str] | None) -> frozenset[SpeciesRole]:
    """Role names as members, refusing one this vocabulary does not have.

    Strict where `merge._role` is lenient, and the asymmetry is deliberate: a *stored* role from a
    newer labeller must degrade quietly, but a role a model typed into a query must not silently
    match nothing — that is a filter that returns "no precedent" for a spelling mistake.
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
        return await _unlabelled(f"reactions using {smiles}")
    return await substrate_precedents(
        _labels, version, smiles, role=SpeciesRole(role) if role else None, limit=top_k
    )


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
        return await _unlabelled(f"conditions for products similar to {product_smiles}")
    return await conditions_for_similar_products(
        _labels, corpus_fingerprints(), version, product_smiles, threshold=threshold, limit=top_k
    )


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
        return await _unlabelled(f"conditions for reactions similar to {reaction_smiles}")
    return await conditions_for_similar_reactions(
        _labels, corpus_reactions(), version, reaction_smiles, threshold=threshold, limit=top_k
    )


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
        return FrequencyReport(coverage=await _labels.coverage("never-labelled"))
    return await agent_frequency(
        _labels,
        version,
        named_reaction=named_reaction,
        rxno_id=rxno_id,
        product_functional_group=product_functional_group,
        roles=_roles(roles),
        limit=top_k,
    )


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
        return await _unlabelled(f"reactions making a product matching {smarts}")
    return await reactions_with_product_substructure(
        _labels, _molecules, version, smarts, named_reaction=named_reaction, limit=top_k
    )


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
        return await _unlabelled(f"workups recorded for reactions using {reagent_smiles}")
    return await workup_precedents(_labels, version, reagent_smiles, limit=top_k)


async def report_index_size() -> None:
    """Log this connector's index size at startup (see the `molfp` twin for the full note)."""
    await log_index_size(_store, "reaction")


def main() -> None:
    """Run the server over stdio (the default MCP transport)."""
    server.run()


if __name__ == "__main__":
    main()
