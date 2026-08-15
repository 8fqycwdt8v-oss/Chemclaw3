"""Shared reaction-similarity clustering for the memory layers (plan Phase 5).

The one place reactions are fingerprinted and grouped by structural similarity. Two memory
groupings need it — cross-project **playbooks** (5.4) and same-transformation **optimization
campaigns** — so the DRFP computation and the single-linkage clustering live here once (DRY,
the Rule-of-Three extraction that the second and third callers made real), not copy-pasted per
job. Pure and deterministic: no store, no LLM, no I/O.
"""

import networkx as nx

from chemclaw.ingest.eln.ord import OrdReaction
from chemclaw.science.fingerprints.rxnfp.fingerprint import drfp_bitstring
from chemclaw.science.fingerprints.store import FingerprintError, tanimoto_bits


def reaction_fingerprints(reactions: list[OrdReaction]) -> dict[str, str]:
    """Map each reaction id to its DRFP bitstring, dropping any that cannot be fingerprinted.

    A degenerate or unparseable reaction (e.g. `CCO>>CCO`) is skipped, never fatal: one bad
    reaction must not abort clustering for the whole corpus (G4). Only fingerprintable
    reactions can participate in a similarity grouping.
    """
    fingerprints: dict[str, str] = {}
    for reaction in reactions:
        try:
            # The transformation, not the record form: clustering asks "is this the same
            # chemistry?", and a solvent left in the string answers "was it run in the same
            # flask?" instead — see `OrdReaction.transformation_smiles`.
            fingerprints[reaction.reaction_id] = drfp_bitstring(reaction.transformation_smiles())
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

    **The n² is in the comparisons; it does not have to be in the parsing too.** Each bitstring is
    2,048 characters and `tanimoto` parses both of its arguments, so this loop turned n distinct
    fingerprints into n² string parses to make n²/2 comparisons. Parsing once up front leaves the
    quadratic term as two `bit_count`s, which is what the O(n²) note above is actually claiming.
    """
    ids = list(fingerprints)
    widths = {len(bits) for bits in fingerprints.values()}
    if len(widths) > 1:
        # The check `tanimoto` makes per pair, made once for the corpus — an int has no width, so
        # after parsing there is nothing left to compare. Same error, same class, raised earlier.
        raise FingerprintError("cannot compare fingerprints of different widths")
    parsed = {key: int(bits, 2) for key, bits in fingerprints.items()}
    graph: nx.Graph = nx.Graph()
    graph.add_nodes_from(ids)
    for i, a in enumerate(ids):
        for b in ids[i + 1 :]:
            if tanimoto_bits(parsed[a], parsed[b]) >= threshold:
                graph.add_edge(a, b)
    clusters = [sorted(component) for component in nx.connected_components(graph)]
    clusters.sort(key=lambda c: c[0])
    return clusters
