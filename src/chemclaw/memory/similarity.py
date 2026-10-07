"""Shared reaction-similarity clustering for the memory layers.

The one place reactions are DRFP-fingerprinted and grouped by single-linkage similarity, used by
both playbooks and optimization campaigns. Pure and deterministic: no store, no LLM, no I/O.
"""

import numpy as np
from scipy.sparse import coo_matrix, csr_matrix
from scipy.sparse.csgraph import connected_components

from chemclaw.core.config import settings
from chemclaw.ingest.eln.ord import OrdReaction
from chemclaw.science.fingerprints.rxnfp.fingerprint import drfp_bitstring
from chemclaw.science.fingerprints.store import FingerprintError


def reaction_fingerprints(reactions: list[OrdReaction]) -> dict[str, str]:
    """Map each reaction id to its DRFP bitstring, dropping any that cannot be fingerprinted.

    A degenerate or unparseable reaction is skipped, never fatal, so one bad record cannot abort
    clustering for the whole corpus.
    """
    fingerprints: dict[str, str] = {}
    for reaction in reactions:
        try:
            # The transformation, not the record form: a solvent left in the string would make
            # clustering
            # ask "same flask?" instead of "same chemistry?".
            fingerprints[reaction.reaction_id] = drfp_bitstring(reaction.transformation_smiles())
        except FingerprintError:
            continue
    return fingerprints


#: Bytes the block pipeline holds per bit-sharing pair, turning the byte budget into a row count.
#: Measured empirically (the temporaries of the sparse pipeline roughly double the naive count).
#: The linear cost of decoding the fingerprints is the caller's and is not covered here.
_BYTES_PER_PAIR = 56


def _block_rows(size: int) -> int:
    """How many rows of the pairwise product fit inside `memory_similarity_block_bytes`.

    Sized against the worst case (every pair in the block sharing a bit) so the bound does not
    depend on corpus density. At least one row, so progress is always made.
    """
    return max(1, settings.memory_similarity_block_bytes // (size * _BYTES_PER_PAIR))


def cluster_by_similarity(fingerprints: dict[str, str], threshold: float) -> list[list[str]]:
    """Single-linkage clusters of ids whose DRFP Tanimoto reaches `threshold`.

    Two reactions are linked when their similarity is >= `threshold`; a cluster is a connected
    component, so similarity is transitive. Each cluster is a sorted id list and clusters are sorted
    by their first id, so the result is deterministic and order-independent.

    Still O(n^2) comparisons, done in C: the bitstrings become an `(n, width)` 0/1 matrix, a sparse
    product yields each pair's shared-bit count, and `connected_components` finds the clusters. The
    product is taken in row blocks sized from `memory_similarity_block_bytes`, and after each block
    the partition is folded back to one edge per node, so peak memory is bounded regardless of how
    much links.

    The result is exact: `shared / union` over integer counts is the same IEEE division
    `tanimoto_bits` performs. A pair sharing no bits never appears in the sparse product, so a
    non-positive threshold (which links everything) is answered up front; for every stored pair
    `union >= 1`.
    """
    ids = list(fingerprints)
    if not ids:
        return []
    widths = {len(bits) for bits in fingerprints.values()}
    if len(widths) > 1:
        # The check `tanimoto` makes per pair, made once for the corpus — the matrix below has one
        # width by construction, so after it is built there is nothing left to compare.
        raise FingerprintError("cannot compare fingerprints of different widths")
    if threshold <= 0.0:
        # Every pair reaches a non-positive threshold, including two that share nothing, so the
        # whole corpus is one component and the sparse product below would say otherwise.
        return [sorted(ids)]

    flat = np.frombuffer("".join(fingerprints[key] for key in ids).encode("ascii"), dtype=np.uint8)
    bits = (flat - ord("0")).reshape(len(ids), widths.pop())
    if bits.max() > 1:
        # Raised in this module's vocabulary: a non-bit character means the index is corrupt, not
        # the
        # query.
        raise FingerprintError("a fingerprint carries a character that is not a bit")

    rows = csr_matrix(bits, dtype=np.int32)
    # int32 throughout: counts are bounded by the width and node indices by `len(ids)`, and the
    # arrays are per pair, so the dtype halves the memory bound without affecting exactness.
    counts = bits.sum(axis=1, dtype=np.int32)
    # Each node's edge to its component's representative: the whole partition in n entries.
    # Initially every node is its own component.
    nodes = np.arange(len(ids), dtype=np.int32)
    component = nodes
    labels = nodes
    count = len(ids)
    block = _block_rows(len(ids))
    for start in range(0, len(ids), block):
        stop = min(start + block, len(ids))
        shared = coo_matrix(rows[start:stop] @ rows.T)
        left = shared.row + np.int32(start)
        linked = shared.data / (counts[left] + counts[shared.col] - shared.data) >= threshold
        if not linked.any():
            continue
        # The block's new links plus the partition so far, in one graph. Each component maps back to
        # its
        # lowest-indexed member (writing indices in reverse so the first wins), which becomes the
        # representative the next block folds against.
        edges = coo_matrix(
            (
                np.ones(int(linked.sum()) + len(ids), dtype=bool),
                (
                    np.concatenate((left[linked], component)),
                    np.concatenate((shared.col[linked], nodes)),
                ),
            ),
            shape=(len(ids), len(ids)),
        )
        count, labels = connected_components(edges.tocsr(), directed=False)
        representative = np.zeros(count, dtype=np.int32)
        representative[labels[::-1]] = nodes[::-1]
        component = representative[labels]
    clusters: list[list[str]] = [[] for _ in range(count)]
    for key, label in zip(ids, labels, strict=True):
        clusters[label].append(key)
    for cluster in clusters:
        cluster.sort()
    clusters.sort(key=lambda cluster: cluster[0])
    return clusters
