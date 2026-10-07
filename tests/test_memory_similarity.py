"""`memory/similarity.cluster_by_similarity` against a pairwise reference.

The clustering is a sparse bit-matrix product feeding
`scipy.sparse.csgraph.connected_components`. Its output must equal the pairwise definition
exactly: playbooks are distilled from whatever cluster they get, and `stable_id` anchors on the
smallest member, so one moved reaction silently mints a different note. The readable pairwise form
is kept as a differential oracle. The corpus is real DRFP fingerprints, because bit density and the
share of overlapping pairs are properties of the chemistry that decide whether the forms agree.
"""

import tracemalloc

import pytest

from chemclaw.core.config import settings
from chemclaw.memory.similarity import cluster_by_similarity
from chemclaw.science.fingerprints.rxnfp.fingerprint import drfp_bitstring
from chemclaw.science.fingerprints.store import FingerprintError, tanimoto

_SUBSTITUENTS = ["", "C", "CC", "Cl", "F", "OC", "C(F)(F)F", "N(C)C", "C#N"]


def _reference_clusters(fingerprints: dict[str, str], threshold: float) -> list[list[str]]:
    """The pairwise single-linkage grouping, written for readability rather than speed.

    Every pair, the shared `tanimoto` helper, and a union-find over the links: the simplest
    statement of single linkage.
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
    """The vectorised grouping is identical to the pairwise definition, not merely similar.

    Five thresholds cover the boundaries: a pair exactly on the threshold, the low end where one
    wrong link merges large components, and the near-one-cluster and near-all-singleton extremes.
    `shared / union` must be integer counts divided as float64, bit-for-bit what `tanimoto_bits`
    computes; a float32 matmul would fail on a pair.
    """
    assert cluster_by_similarity(corpus, threshold) == _reference_clusters(corpus, threshold)


def test_every_reaction_appears_exactly_once(corpus: dict[str, str]) -> None:
    """Every reaction appears in exactly one cluster.

    `connected_components` labels edgeless rows too. Asserted separately because the equality above
    would pass if both implementations dropped the same id.
    """
    clusters = cluster_by_similarity(corpus, 0.5)
    flat = [key for cluster in clusters for key in cluster]
    assert sorted(flat) == sorted(corpus)
    assert len(flat) == len(set(flat))


def test_an_all_zero_fingerprint_clusters_alone() -> None:
    """An all-zero fingerprint clusters alone.

    `tanimoto_bits` defines two all-zero fingerprints as 0.0, and the sparse product stores no entry
    for them; this pins that "no shared bits" and "no data" agree.
    """
    zero = "0" * 16
    fingerprints = {"empty-a": zero, "empty-b": zero, "set": "1" * 16}
    assert cluster_by_similarity(fingerprints, 0.5) == [["empty-a"], ["empty-b"], ["set"]]


def test_a_non_positive_threshold_still_links_everything() -> None:
    """A non-positive threshold still links everything.

    `>= 0.0` holds for every pair, but the sparse product never visits pairs sharing no bits, so
    this case is answered before it rather than collapsing the corpus into singletons.
    """
    fingerprints = {"a": "1000", "b": "0100", "c": "0000"}
    assert cluster_by_similarity(fingerprints, 0.0) == [["a", "b", "c"]]
    assert cluster_by_similarity(fingerprints, 0.5) == [["a"], ["b"], ["c"]]


def test_an_empty_corpus_is_no_clusters() -> None:
    """Nothing in, nothing out — and no `reshape` of an empty buffer on the way."""
    assert cluster_by_similarity({}, 0.5) == []


def test_fingerprints_of_different_widths_are_refused() -> None:
    """Fingerprints of different widths are refused with `FingerprintError`.

    Not the `FingerprintInputError` subclass: a mixed-width index is an outage, not a bad query, and
    reporting it as "nothing found" would tell a chemist there is no precedent.
    """
    with pytest.raises(FingerprintError, match="different widths"):
        cluster_by_similarity({"a": "1010", "b": "10101010"}, 0.5)


def test_a_bitstring_that_is_not_bits_is_refused() -> None:
    """A bitstring that is not bits is refused once for the matrix, with this module's error class.

    Like the width check, it tells the caller the stored index is corrupt.
    """
    with pytest.raises(FingerprintError, match="not a bit"):
        cluster_by_similarity({"a": "1010", "b": "10x0"}, 0.5)


@pytest.mark.parametrize("threshold", [0.1, 0.3, 0.5, 0.7, 0.9])
def test_taking_the_product_a_block_at_a_time_is_the_same_grouping(
    corpus: dict[str, str], threshold: float, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Taking the product a block at a time gives the same grouping.

    Each block folds into a running partition, so a link between nodes in earlier blocks must merge
    components those blocks already built. One byte of budget forces one row per block, the most
    adversarial arrangement; at the shipped budget this fixture is a single block.
    """
    monkeypatch.setattr(settings, "memory_similarity_block_bytes", 1)
    assert cluster_by_similarity(corpus, threshold) == _reference_clusters(corpus, threshold)


def test_the_peak_memory_of_clustering_grows_with_the_corpus_and_not_with_its_square(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Peak clustering memory grows with the corpus, not with its square.

    Many real DRFP pairs share at least one bit, so an unblocked sparse product can store more than
    a dense matrix, while `memory_corpus_max_reactions` allows large corpora. Asserted as a doubling
    ratio rather than a byte count, which would be re-baselined the first time it moved: quadratic
    growth quadruples the peak when the corpus doubles. The budget is small so the bound bites at a
    size this suite can afford, on the same code path the default uses.
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
