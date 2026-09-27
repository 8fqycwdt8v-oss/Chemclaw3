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

from chemclaw.core.config import settings
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


#: Bytes the block pipeline below holds per pair that shares a bit — what turns the byte budget into
#: a row count. **Measured, not counted off the source.** Adding up the arrays one entry passes
#: through gives 29 (the CSR product's `data` and `indices`, the COO view's `row`, `col` and `data`,
#: the float64 quotient, the bool mask), and the honest figure is nearly twice that: driven on a
#: worst-case corpus where every pair shares every bit, at n=4,000 with the budget stepped 8 → 128
#: MB, peak traced allocation rose 29.2 → 202.0 MB against 208k → 3,352k pairs — a slope of
#: **55.0 bytes per pair**, the rest being the temporaries `coo_matrix`, the fancy-index and
#: `tocsr()` take that no source reading enumerates. The intercept of that fit, 17.8 MB, is the
#: *linear* cost of decoding the fingerprints (~4.4 kB per note at a 2,048-bit width), which this
#: budget does not cover and does not need to: it is what a caller already pays for the corpus, and
#: it is what bounding the quadratic half leaves behind.
_BYTES_PER_PAIR = 56


def _block_rows(size: int) -> int:
    """How many rows of the pairwise product fit inside `memory_similarity_block_bytes`.

    Sized against the **worst** case — every pair in the block sharing a bit — because the density
    is a property of the corpus and the bound must not be. At least one row, so a corpus wider than
    the whole budget still makes progress rather than dividing by zero.
    """
    return max(1, settings.memory_similarity_block_bytes // (size * _BYTES_PER_PAIR))


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
    The sparse product is single-threaded C and allocates one entry per bit-sharing pair, which is
    cheaper than a dense n² float64 matrix only at a density this corpus does not have.

    **That last clause is a correction, and the sentence it replaces was a memory argument for the
    sparse form that ran the wrong way.** It claimed this avoided "an n² float64 matrix, which at
    10⁴ reactions is 800 MB" — but a bit-sharing *pair* is not rare at DRFP's density: measured over
    10,000 synthetic 30-of-2,048-bit fingerprints, **36% of all pairs share at least one bit**, and
    a real DRFP corpus is about 55%, so the stored entries outweigh the dense matrix the arithmetic
    was comparing against — the stored `data`/`indices` alone are 288 MB at n=10,000 where the dense
    matrix is 800 MB, and each pair is then carried through a COO `row`/`col`/`data` triple, a
    quotient and a mask. Driven at threshold 0.3, the whole-corpus form peaked at **130 MB of traced
    allocation at n=3,000 and 1,339 MB at n=10,000**, where the pairwise loop it replaced retained
    nothing per pair at all. `memory_corpus_max_reactions` defaults to 100,000 and all three miners
    cluster the whole capped corpus, so the linear ~40 kB-per-reaction figure that cap was sized on
    stopped bounding this job.

    **So the product is taken in row blocks, and each block is folded into a partition rather than
    into an edge list.** The block is sized from `memory_similarity_block_bytes` against the worst
    case — every pair in the block sharing a bit — so the bound holds whatever the corpus looks
    like. After each block the partition found so far is re-expressed as one edge per node (each
    node to its component's lowest-indexed member), which is O(n) however much linked: carrying the
    linked pairs forward instead would restore the quadratic on a corpus where many reactions are
    the same reaction. Measured at threshold 0.3, clusters bit-identical: traced peak
    **130 MB → 26 MB at n=3,000 and 1,339 MB → 57 MB at n=10,000**, and it is *faster* rather than
    slower — 5.02 s → 1.78 s at n=10,000, 0.15 s → 0.13 s at n=3,000, unchanged at 784. Which was
    the surprise: blocking the product was expected to cost wall clock for the bound, and at 10⁴
    the whole-corpus form spends its time allocating and touching 1.3 GB. The speed-up figures
    above stand for the same reason — it is the same arithmetic in the same C routines, taken a
    slice at a time. `tests/test_memory_similarity.py` pins the peak as well as the clusters,
    because a correctness test passes at any memory.

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
    # int32 throughout, not int64: a shared-bit count and a fingerprint's population are both at
    # most the width, and a node index is at most `len(ids)`. Every array below is one per *pair*,
    # so the dtype is half the memory bound, and the division these feed is IEEE double either way
    # — the exactness argument above is about the values being integers, not about their width.
    counts = bits.sum(axis=1, dtype=np.int32)
    # Each node's edge to its component's representative: the whole partition in n entries, which is
    # what the blocks below fold into, and what makes the peak independent of how much links.
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
        # The block's new links plus the partition so far, in one graph. `connected_components`
        # numbers components 0..count-1 in no useful order, so each is mapped back to a node index
        # — the lowest-indexed member, assigned by writing the node indices in reverse so the first
        # one wins — and that node is the representative the next block folds against.
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
