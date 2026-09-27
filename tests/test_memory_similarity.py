"""`memory/similarity.cluster_by_similarity` against the pairwise loop it replaced.

The clustering used to be n²/2 Python-level `tanimoto_bits` calls feeding a NetworkX graph. It is
now a sparse bit-matrix product feeding `scipy.sparse.csgraph.connected_components` — same
comparisons, same asymptotics, a few times faster per comparison. **A speed change to a function
whose output is a grouping is only safe if the grouping is identical**, because nothing downstream
would notice a quietly different one: a playbook is distilled from whatever cluster it is handed,
and `stable_id` anchors on the cluster's smallest member, so a single moved reaction mints a
different note with no error anywhere.

So the readable pairwise form is kept here as a **differential oracle**, exactly as
`InMemoryFingerprintStore` is kept for the SQL ranker
(`D-2026-09-07-a-reference-implementation-is-a-test-oracle-not-a-backend`): it is the definition of
the answer, and the fast path is checked against it on a real corpus rather than against a
hand-written expectation that would encode whatever the fast path happens to do.

The corpus is real DRFP fingerprints of real reaction SMILES rather than random bitstrings, because
the two things that decide whether the vectorised form agrees — how many bits a row sets, and how
many pairs share at least one bit — are properties of the chemistry, not of the code. Random rows
at 50% density would exercise a regime this function never sees.
"""

import tracemalloc

import pytest

from chemclaw.core.config import settings
from chemclaw.memory.similarity import cluster_by_similarity
from chemclaw.science.fingerprints.rxnfp.fingerprint import drfp_bitstring
from chemclaw.science.fingerprints.store import FingerprintError, tanimoto

_SUBSTITUENTS = ["", "C", "CC", "Cl", "F", "OC", "C(F)(F)F", "N(C)C", "C#N"]


def _reference_clusters(fingerprints: dict[str, str], threshold: float) -> list[list[str]]:
    """The pairwise single-linkage grouping, written for readability rather than for speed.

    Deliberately the *simplest* thing that can be right: every pair, the shared `tanimoto` helper,
    and a union-find over the links. It is not the code that was deleted — it is a third statement
    of what single linkage means, so agreement is evidence about the definition rather than about
    one transcription of it.
    """
    ids = list(fingerprints)
    parent = {key: key for key in ids}

    def root(key: str) -> str:
        while parent[key] != key:
            key = parent[key]
        return key

    for index, left in enumerate(ids):
        for right in ids[index + 1 :]:
            if tanimoto(fingerprints[left], fingerprints[right]) >= threshold:
                parent[root(left)] = root(right)

    grouped: dict[str, list[str]] = {}
    for key in ids:
        grouped.setdefault(root(key), []).append(key)
    clusters = [sorted(group) for group in grouped.values()]
    clusters.sort(key=lambda cluster: cluster[0])
    return clusters


@pytest.fixture(scope="module")
def corpus() -> dict[str, str]:
    """~250 amide couplings, each a distinct reaction, fingerprinted the way ingest does.

    Built once for the module: DRFP encoding is ~6 ms per reaction, which is the bulk of this
    file's runtime and has nothing to do with what it asserts.
    """
    acids = ["C" * length + "C(=O)O" for length in range(1, 9)]
    acids += [f"c1ccc({sub})cc1C(=O)O" if sub else "c1ccccc1C(=O)O" for sub in _SUBSTITUENTS]
    amines = ["N" + "C" * length for length in range(1, 9)]
    amines += [f"Nc1ccc({sub})cc1" if sub else "Nc1ccccc1" for sub in _SUBSTITUENTS]

    fingerprints: dict[str, str] = {}
    for index, acid in enumerate(acids):
        for offset, amine in enumerate(amines):
            product = acid[:-1] + "N" + amine[1:]
            fingerprints[f"rxn-{index}-{offset}"] = drfp_bitstring(f"{acid}.{amine}>>{product}")
    return fingerprints


@pytest.mark.parametrize("threshold", [0.1, 0.3, 0.5, 0.7, 0.9])
def test_the_vectorised_clustering_is_the_pairwise_one(
    corpus: dict[str, str], threshold: float
) -> None:
    """The grouping is *identical* to the pairwise definition, not merely similar.

    Five thresholds because the interesting failures are at the boundaries rather than in the
    middle: a float that rounds differently only matters for a pair sitting exactly on the
    threshold, and the low end is where one wrong link merges two large components into one. At
    0.1 this corpus is nearly one cluster and at 0.9 it is nearly all singletons, so both
    degenerate shapes are covered as well.

    This is what the `shared / union` arithmetic has to hold: integer counts divided as float64,
    which is bit-for-bit what `tanimoto_bits` computes in Python. Comparing at a rounded similarity
    — or thresholding a float32 matmul's output — would pass most of these and fail a pair.
    """
    assert cluster_by_similarity(corpus, threshold) == _reference_clusters(corpus, threshold)


def test_every_reaction_appears_exactly_once(corpus: dict[str, str]) -> None:
    """A partition, which the reference's union-find guarantees and the sparse path must too.

    `connected_components` labels every row, including one with no edges, so a reaction that shares
    no bits with anything is its own cluster rather than missing. Asserted separately because the
    equality above would also pass if *both* implementations dropped the same id.
    """
    clusters = cluster_by_similarity(corpus, 0.5)
    flat = [key for cluster in clusters for key in cluster]
    assert sorted(flat) == sorted(corpus)
    assert len(flat) == len(set(flat))


def test_an_all_zero_fingerprint_clusters_alone() -> None:
    """Two fingerprints sharing nothing are 0.0 similar, which is the `union == 0` guard's case.

    `tanimoto_bits` defines two all-zero fingerprints as 0.0 rather than as NaN or 1.0, and the
    sparse product expresses the same thing by never storing an entry for them. This pins the
    agreement, because it is the one place where "no shared bits" and "no data" have to mean the
    same thing.
    """
    zero = "0" * 16
    fingerprints = {"empty-a": zero, "empty-b": zero, "set": "1" * 16}
    assert cluster_by_similarity(fingerprints, 0.5) == [["empty-a"], ["empty-b"], ["set"]]


def test_a_non_positive_threshold_still_links_everything() -> None:
    """`>= 0.0` is true of every pair, including the ones that share no bits at all.

    The old loop got this for free: it compared every pair and 0.0 reaches a threshold of 0.0. The
    sparse product never *visits* a pair that shares nothing, so this case is answered before it —
    and it is asserted rather than assumed, because a misconfigured threshold of zero silently
    turning a corpus into singletons is the opposite of what it used to do, in the direction where
    every downstream note changes.
    """
    fingerprints = {"a": "1000", "b": "0100", "c": "0000"}
    assert cluster_by_similarity(fingerprints, 0.0) == [["a", "b", "c"]]
    assert cluster_by_similarity(fingerprints, 0.5) == [["a"], ["b"], ["c"]]


def test_an_empty_corpus_is_no_clusters() -> None:
    """Nothing in, nothing out — and no `reshape` of an empty buffer on the way."""
    assert cluster_by_similarity({}, 0.5) == []


def test_fingerprints_of_different_widths_are_refused() -> None:
    """The corpus-wide width check, which is the only place an int-parsed corpus can still make it.

    `FingerprintError` rather than its `FingerprintInputError` subclass, deliberately: a mixed-width
    index is an outage, not a bad query, and a caller that reports it as "nothing found" tells a
    chemist the company has no precedent.
    """
    with pytest.raises(FingerprintError, match="different widths"):
        cluster_by_similarity({"a": "1010", "b": "10101010"}, 0.5)


def test_a_bitstring_that_is_not_bits_is_refused() -> None:
    """What `int(bits, 2)` used to catch per fingerprint, now caught once for the matrix.

    It used to surface as a bare `ValueError` from the parse; it is this module's own error class
    now, beside the width check, because both say the same thing to a caller — the stored index is
    corrupt.
    """
    with pytest.raises(FingerprintError, match="not a bit"):
        cluster_by_similarity({"a": "1010", "b": "10x0"}, 0.5)


@pytest.mark.parametrize("threshold", [0.1, 0.3, 0.5, 0.7, 0.9])
def test_taking_the_product_a_block_at_a_time_is_the_same_grouping(
    corpus: dict[str, str], threshold: float, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The blocking must be invisible in the answer, including when nearly every block is one row.

    The product is taken in row blocks sized from `memory_similarity_block_bytes`, and each block is
    folded into a running partition rather than into an edge list — so a link found in block 40
    between a node in block 3 and one in block 12 has to merge the components those blocks had
    already built. That is the part a single-block corpus never exercises: at the shipped budget
    this fixture is one block, so every test above it would pass with the fold broken.

    One byte of budget forces one row per block — ~250 blocks over this corpus — which is the most
    adversarial arrangement of the same arithmetic.
    """
    monkeypatch.setattr(settings, "memory_similarity_block_bytes", 1)
    assert cluster_by_similarity(corpus, threshold) == _reference_clusters(corpus, threshold)


def test_the_peak_memory_of_clustering_grows_with_the_corpus_and_not_with_its_square(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bound the shipped corpus cap was calibrated on, which the sparse product had removed.

    `cluster_by_similarity`'s own docstring argued the sparse product against "an n² float64 matrix,
    which at 10⁴ reactions is 800 MB" — but 36% of synthetic DRFP pairs and ~55% of real ones share
    at least one bit, so it stored more than the matrix it was contrasted with. Driven at
    threshold 0.3 on the whole-corpus form: **130 MB of traced allocation at n=3,000 and 1,339 MB at
    n=10,000**, while `memory_corpus_max_reactions` defaults to 100,000 and all three miners cluster
    the whole capped corpus — so the ~40 kB-per-reaction figure that cap was sized on had stopped
    bounding the job.

    Asserted as a *doubling ratio* rather than as a byte count, because a byte count is a fact about
    this interpreter on this box and would be re-baselined the first time it moved, which is how a
    resource guard stops guarding. Quadratic growth doubles the corpus and quadruples the peak;
    measured on this worst case, where every pair shares every bit, the whole-corpus form went
    139.3 MB → 544.1 MB (3.91x) and the blocked form goes 10.7 MB → 17.3 MB (1.61x). The ceiling
    below sits between the two and well clear of both.

    The budget is set small so the bound bites at a corpus size this suite can afford; it is the
    same code path the default exercises at ~25x the size.
    """
    monkeypatch.setattr(settings, "memory_similarity_block_bytes", 4 * 1024 * 1024)
    one_fingerprint = "1" * 30 + "0" * 2018

    def peak_bytes(size: int) -> int:
        """Traced peak of clustering `size` copies of one fingerprint — every pair links."""
        fingerprints = {f"rxn-{index}": one_fingerprint for index in range(size)}
        tracemalloc.start()
        try:
            clusters = cluster_by_similarity(fingerprints, 0.5)
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
        assert clusters == [sorted(fingerprints)], "the premise: the whole corpus is one cluster"
        return peak

    small = peak_bytes(1500)
    large = peak_bytes(3000)
    assert large / small < 2.6, (
        f"doubling the corpus took the peak from {small / 1e6:.1f} MB to {large / 1e6:.1f} MB "
        f"({large / small:.2f}x) — a ratio near 4 means the pairwise product is being held whole "
        "again, which is what puts a capped corpus over the pod's memory"
    )
