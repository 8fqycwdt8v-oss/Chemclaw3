"""Detect reaction chains: product of A = reactant of B (plan step 5.2).

The backbone of the episodic layer. Two experiments are causally linked when a product of
one is a reactant of another — the same structural identity the fingerprint index already
keys compounds by (`canonical_smiles`), so this reuses Phase 3's compound identity, no new
data. Reactions form a directed graph (edge A→B when a product of A is an input of B); each
weakly-connected component with ≥2 reactions is a chain (a campaign), returned topologically
ordered so the narrative reads reactant→product. Pure and deterministic — no LLM, no store.
"""

import networkx as nx
from pydantic import BaseModel

from eln.chem import canonical_smiles
from eln.ord import OrdReaction, Role


class ChainLink(BaseModel):
    """One causal edge: `from_reaction`'s product is `to_reaction`'s reactant."""

    from_reaction: str
    to_reaction: str
    via_compound: str  # canonical SMILES shared as product then reactant


class Chain(BaseModel):
    """A campaign: reactions linked product→reactant.

    `ordered` is True when the linkage is acyclic and `reaction_ids` is a genuine
    reactant→product topological order; False when the chain contains a cycle (a reversible
    pair), where the ids are only a stable listing, not a causal sequence — so a narrative
    must not present them as steps.
    """

    reaction_ids: list[str]
    links: list[ChainLink]
    ordered: bool = True


def _products(reaction: OrdReaction) -> set[str]:
    """Canonical SMILES of the reaction's products."""
    return {canonical_smiles(c.smiles) for c in reaction.outcomes}


def _reactant_inputs(reaction: OrdReaction) -> set[str]:
    """Canonical SMILES of the reaction's true reactant inputs (not reagent/solvent/catalyst)."""
    return {canonical_smiles(c.smiles) for c in reaction.inputs if c.role == Role.REACTANT}


def detect_chains(reactions: list[OrdReaction]) -> list[Chain]:
    """Return the reaction chains (>=2 linked reactions), each topologically ordered.

    An edge A→B is drawn when a product of A is a reactant of B. Chains are the weakly
    connected components of the resulting graph; singletons (unlinked reactions) are not
    campaigns and are omitted. Results are sorted by first reaction id for determinism.
    """
    graph: nx.DiGraph = nx.DiGraph()
    for reaction in reactions:
        graph.add_node(reaction.reaction_id)
    products = {r.reaction_id: _products(r) for r in reactions}
    reactants = {r.reaction_id: _reactant_inputs(r) for r in reactions}

    for producer in reactions:
        for consumer in reactions:
            if producer.reaction_id == consumer.reaction_id:
                continue
            shared = products[producer.reaction_id] & reactants[consumer.reaction_id]
            for compound in shared:
                graph.add_edge(producer.reaction_id, consumer.reaction_id, via=compound)

    chains: list[Chain] = []
    for component in nx.weakly_connected_components(graph):
        if len(component) < 2:
            continue
        subgraph = graph.subgraph(component)
        is_acyclic = nx.is_directed_acyclic_graph(subgraph)
        ordering = list(nx.topological_sort(subgraph)) if is_acyclic else sorted(subgraph.nodes())
        links = [
            ChainLink(from_reaction=u, to_reaction=v, via_compound=data["via"])
            for u, v, data in subgraph.edges(data=True)
        ]
        chains.append(Chain(reaction_ids=ordering, links=links, ordered=is_acyclic))
    chains.sort(key=lambda c: c.reaction_ids[0])
    return chains
