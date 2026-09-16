"""Shared reaction-similarity clustering for the memory layers (plan Phase 5).

The one place reactions are fingerprinted and grouped by structural similarity. Two memory
groupings need it — cross-project **playbooks** (5.4) and same-transformation **optimization
campaigns** — so the DRFP computation and the single-linkage clustering live here once (DRY,
the Rule-of-Three extraction that the second and third callers made real), not copy-pasted per
job. Pure and deterministic: no store, no LLM, no I/O.
"""

import numpy as np
from scipy.sparse import coo_matrix, csr_matrix
from scipy.sparse.csgraph import connected_components

from chemclaw.ingest.eln.ord import OrdReaction
from chemclaw.science.fingerprints.rxnfp.fingerprint import drfp_bitstring
from chemclaw.science.fingerprints.store import FingerprintError


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
    and clusters are sorted by their first id — deterministic and order-independent.

    **Still O(n²) comparisons, done in C instead of in Python.** The bitstrings become an
    `(n, width)` matrix of 0/1 bytes, a sparse product gives every pair's shared-bit count in one
    call, and the thresholded matrix goes to `scipy.sparse.csgraph.connected_components`. Measured
    against the pairwise loop it replaced, on real DRFP fingerprints of real reaction SMILES, min
    of five runs each, three runs of the whole comparison: at **784 fingerprints, 348 ms → 75 ms**
    (0.3) and **241 ms → 45 ms** (0.6) — 3.4x to 5.3x across the runs — 3.2x to 5.6x at 289, and
    only 1.6x to 2.3x at 100, where the fixed numpy cost is still most of the work. (The box was
    shared and loaded, at a load average of 13-16 on four cores, which is why the figures are given
    as a range; both arms pay it.)

    It is *not* the hundredfold a "vectorise the inner loop" change sounds like, and the reason is
    in `tanimoto_bits`' own docstring: the parse was hoisted out of this loop in an earlier pass,
    so what was left per pair was two `int.bit_count()`s over 2,048-bit ints — a few dozen C word
    operations already, under about half a microsecond of Python call and dict-lookup overhead.
    What is removed here is the overhead, not an algorithm.

    **Sparse rather than a dense `X @ X.T`, on two measurements.** A DRFP row sets ~30 of 2,048
    bits, so the dense product spends 2,048 multiply-adds on a pair that shares at most 30 bits;
    measured side by side on 784 of them, the dense float32 form ran 201 ms against this one's
    55 ms. It also reaches BLAS, whose thread pool is the one part of this that is not a property
    of the code: timed alone on the same contended four-core box, that matmul cost a *flat* ~74 ms
    at n=100 and ~77 ms at n=600 — thread synchronisation, not arithmetic — which made the dense
    form slower than the Python loop it was replacing at both 100 and 300 fingerprints.
    The sparse product is single-threaded C and allocates one entry per bit-sharing pair rather
    than an n² float64 matrix, which is also what keeps it viable as n grows: at 10⁴ reactions that
    dense matrix alone is 800 MB.

    **Exact, not approximately exact**, which is the whole requirement — this is a *constant*
    change and the clusters must be the ones the pairwise loop produced. The comparison stays on
    the same rational arithmetic: `shared` and `union` are integer counts, and `shared / union` on
    two int64 arrays is IEEE double division of two exactly-represented integers, which is bit-for-
    bit what `tanimoto_bits`' `a / b` computes in Python. `tests/test_memory_similarity.py` asserts
    the two agree on a real corpus rather than trusting that sentence.

    Two shapes the pairwise form handled implicitly and this one has to state:

    * **A pair sharing no bits never appears in the sparse product**, so it is never linked. That
      is right for any positive threshold and wrong for a threshold of zero, where the old loop
      linked *everything* — two all-zero fingerprints included, since `tanimoto_bits` defines them
      as 0.0 and `0.0 >= 0.0`. A non-positive threshold is therefore answered before any of this.
    * **`union` is never zero here**, because a stored entry means at least one shared bit and
      `union = |a| + |b| - |a ∩ b| >= |a ∩ b| >= 1`. The `if union else 0.0` guard `tanimoto_bits`
      carries is about the all-zero pair, which is the case above.

    **This does not close `docs/planning/DEFERRED.md`'s "Sub-quadratic reaction clustering" row.**
    That row defers the *asymptotic* fix — an index that avoids comparing every pair at all, with a
    trigger of ~10⁴ reactions. Every pair is still compared here; they are merely compared several
    times faster, which moves the wall clock and not the exponent.
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
        # `int(bits, 2)` used to make this check per fingerprint, as a `ValueError`. Raised in this
        # module's own vocabulary instead, beside the width check, because both mean the same
        # thing to a caller: the index is corrupt, not the query (`FingerprintInputError`).
        raise FingerprintError("a fingerprint carries a character that is not a bit")

    rows = csr_matrix(bits, dtype=np.int32)
    shared = coo_matrix(rows @ rows.T)
    counts = bits.sum(axis=1, dtype=np.int64)
    intersection = shared.data.astype(np.int64)
    linked = intersection / (counts[shared.row] + counts[shared.col] - intersection) >= threshold
    edges = coo_matrix(
        (np.ones(int(linked.sum()), dtype=bool), (shared.row[linked], shared.col[linked])),
        shape=(len(ids), len(ids)),
    )
    count, labels = connected_components(edges.tocsr(), directed=False)
    clusters: list[list[str]] = [[] for _ in range(count)]
    for key, label in zip(ids, labels, strict=True):
        clusters[label].append(key)
    for cluster in clusters:
        cluster.sort()
    clusters.sort(key=lambda cluster: cluster[0])
    return clusters
