"""Shared reaction-similarity clustering for the memory layers (plan Phase 5).

The one place reactions are fingerprinted and grouped by structural similarity. Two memory
groupings need it — cross-project **playbooks** (5.4) and same-transformation **optimization
campaigns** — so the DRFP computation and the single-linkage clustering live here once (DRY,
the Rule-of-Three extraction that the second and third callers made real), not copy-pasted per
job. Pure and deterministic: no store, no LLM, no I/O.
"""

import networkx as nx

from eln.ord import OrdReaction
from mcp_servers.fpstore import FingerprintError, tanimoto
from mcp_servers.rxnfp.fingerprint import drfp_bitstring


def reaction_fingerprints(reactions: list[OrdReaction]) -> dict[str, str]:
    """Map each reaction id to its DRFP bitstring, dropping any that cannot be fingerprinted.

    A degenerate or unparseable reaction (e.g. `CCO>>CCO`) is skipped, never fatal: one bad
    reaction must not abort clustering for the whole corpus (G4). Only fingerprintable
    reactions can participate in a similarity grouping.
    """
    fingerprints: dict[str, str] = {}
    for reaction in reactions:
        try:
            fingerprints[reaction.reaction_id] = drfp_bitstring(reaction.reaction_smiles())
        except FingerprintError:
            continue
    return fingerprints


def cluster_by_similarity(fingerprints: dict[str, str], threshold: float) -> list[list[str]]:
    """Single-linkage clusters of ids whose DRFP Tanimoto reaches `threshold`.

    Two reactions are linked when their similarity is >= `threshold`; a cluster is a
    connected component of that graph, so similarity is transitive (A~B, B~C groups A, B, C
    even if A and C are not directly similar). Each cluster is returned as a sorted id list,
    and clusters are sorted by their first id — deterministic and order-independent. Pairwise
    comparison is O(n²); fine at today's scale, and the Postgres HNSW index (Phase 3) is the
    escape hatch past ~10^4 reactions.
    """
    ids = list(fingerprints)
    graph: nx.Graph = nx.Graph()
    graph.add_nodes_from(ids)
    for i, a in enumerate(ids):
        for b in ids[i + 1 :]:
            if tanimoto(fingerprints[a], fingerprints[b]) >= threshold:
                graph.add_edge(a, b)
    clusters = [sorted(component) for component in nx.connected_components(graph)]
    clusters.sort(key=lambda c: c[0])
    return clusters
