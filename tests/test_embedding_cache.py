"""Repeated queries are embedded once (STO-12).

The audit's finding on "tool result caching" was mostly that it is *not* a gap — every calculator
already routes through `run_cached`, and the RDKit chem tools are cheaper than the Postgres round
trip a cache would add. One genuine repetition survived that review: `embed_texts` re-embedded the
same query on every retrieval, and under a real provider that is a network round trip on the
interactive path, paid by all three graph-backed retrievers per query.

The cache's one hazard is serving a vector the current model would not produce, which is why the
configuration is part of the key and why that is the first thing asserted here.
"""

import pytest

from chemclaw.core import embeddings
from chemclaw.core.config import settings
from chemclaw.core.embeddings import clear_embedding_cache, embed_texts


@pytest.fixture(autouse=True)
def _empty_cache() -> None:
    """Start every test from an empty cache, or it measures the previous test's leftovers."""
    clear_embedding_cache()


class _Counting:
    """Stands in for the provider, counting how many texts actually reached it."""

    def __init__(self) -> None:
        """Start at zero."""
        self.calls = 0
        self.texts: list[str] = []

    def __call__(self, texts: list[str]) -> list[list[float]]:
        """Return a distinct deterministic vector per text, counting the batch."""
        self.calls += 1
        self.texts.extend(texts)
        return [[float(len(text)), float(index)] for index, text in enumerate(texts)]


def test_a_repeated_query_is_embedded_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """The saving, stated as the thing that is actually repeated: one query, many retrievals."""
    provider = _Counting()
    monkeypatch.setattr(embeddings, "_embed_uncached", provider)

    first = embed_texts(["suzuki coupling conditions"])
    second = embed_texts(["suzuki coupling conditions"])

    assert first == second
    assert provider.calls == 1


def test_only_the_misses_are_sent(monkeypatch: pytest.MonkeyPatch) -> None:
    """A half-cached batch costs half a request, not a whole one.

    This is what makes the cache worth having at index time too, where texts arrive in batches and
    a re-index after editing one note would otherwise re-embed the entire corpus.
    """
    provider = _Counting()
    monkeypatch.setattr(embeddings, "_embed_uncached", provider)

    embed_texts(["a", "b"])
    embed_texts(["b", "c"])

    assert provider.texts == ["a", "b", "c"]


def test_a_batch_naming_one_text_twice_embeds_it_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """Deduplicated before the call, so a duplicated input is not a duplicated round trip."""
    provider = _Counting()
    monkeypatch.setattr(embeddings, "_embed_uncached", provider)

    result = embed_texts(["same", "same"])
    assert provider.texts == ["same"]
    assert result[0] == result[1]


def test_changing_the_model_does_not_serve_the_old_models_vectors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The hazard the calculation cache learned the hard way, avoided by construction (D-011).

    A vector is only reusable for the configuration that produced it. Serving one model's
    embeddings after a switch would corrupt every similarity comparison silently — nothing would
    error, the numbers would simply stop meaning anything.
    """
    calls: list[str] = []

    def _provider(texts: list[str]) -> list[list[float]]:
        calls.append(settings.embedding_model)
        return [[1.0, 0.0] for _ in texts]

    monkeypatch.setattr(embeddings, "_embed_uncached", _provider)
    monkeypatch.setattr(settings, "embedding_model", "model-a")
    embed_texts(["query"])
    monkeypatch.setattr(settings, "embedding_model", "model-b")
    embed_texts(["query"])

    assert calls == ["model-a", "model-b"]


def test_changing_the_dimension_also_misses(monkeypatch: pytest.MonkeyPatch) -> None:
    """A vector of the wrong width would not merely be wrong, it would not index at all."""
    provider = _Counting()
    monkeypatch.setattr(embeddings, "_embed_uncached", provider)
    embed_texts(["query"])
    monkeypatch.setattr(settings, "embedding_dim", settings.embedding_dim + 1)
    embed_texts(["query"])
    assert provider.calls == 2


def test_the_cache_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unbounded map of every text ever embedded is a slow leak in a long-lived process."""
    provider = _Counting()
    monkeypatch.setattr(embeddings, "_embed_uncached", provider)
    monkeypatch.setattr(settings, "embedding_cache_size", 4)

    for index in range(10):
        embed_texts([f"text-{index}"])
    assert len(embeddings._CACHE) <= 4


def test_a_size_of_zero_disables_it(monkeypatch: pytest.MonkeyPatch) -> None:
    """The escape hatch works, and costs exactly what it did before the cache existed."""
    provider = _Counting()
    monkeypatch.setattr(embeddings, "_embed_uncached", provider)
    monkeypatch.setattr(settings, "embedding_cache_size", 0)

    embed_texts(["query"])
    embed_texts(["query"])
    assert provider.calls == 2


def test_the_real_hash_embedder_still_round_trips_through_the_cache() -> None:
    """No provider stub: the cached value must be the value the provider would have returned."""
    direct = embeddings._hash_embedding("acetonitrile")
    assert embed_texts(["acetonitrile"])[0] == direct
    assert embed_texts(["acetonitrile"])[0] == direct


def test_an_empty_batch_is_not_a_cache_lookup() -> None:
    """Cheap, and it keeps the zip-strict pairing below from ever seeing an empty provider call."""
    assert embed_texts([]) == []


def test_a_batch_larger_than_the_bound_still_returns_every_vector(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The trim may evict a key this very call inserted; the caller still gets its vector.

    The deterministic half of the concurrency defect below, and the one worth pinning hardest
    because it needs no threads to state: the answer is assembled from what the call holds, not
    re-read from `_CACHE` after the trim has run. `reindex_notes` embeds one text per note in a
    single batch, so any corpus larger than `embedding_cache_size` takes this path.
    """
    monkeypatch.setattr(settings, "embedding_cache_size", 4)
    texts = [f"text-{index}" for index in range(20)]

    vectors = embed_texts(texts)

    assert len(vectors) == len(texts)
    assert all(
        vector == embeddings._hash_embedding(text)
        for text, vector in zip(texts, vectors, strict=True)
    )
    assert len(embeddings._CACHE) <= 4


@pytest.mark.timeout(600)
def test_concurrent_batches_do_not_race_on_the_cache() -> None:
    """`_CACHE` is reached from several threads, and was a plain dict with no lock.

    **Its own timeout, because the global 180 s cap fails it under `make cov` and nowhere else.**
    The workload is deliberately large — 8 threads x 600 texts against a 2,048-entry cache — and
    coverage tracing multiplies a tight 4,800-iteration loop by roughly thirty: measured on this
    machine, 4.8 s bare and 132-216 s traced. Every observed run under 180 s passed and every run
    over it failed, which is the cap and not the race. That made the whole gate red on a
    sufficiently loaded machine while `pytest tests/test_embedding_cache.py` stayed green, so the
    failure looked like a flake in the thing this test is about. 600 s is ~2.8x the slowest run
    seen; shrinking the batch instead would have narrowed the race window the docstring below
    explains was chosen to be wide.

    Every retrieval runs its embedding through `asyncio.to_thread`, so concurrent turns land on the
    default executor together. Two races followed and both are reproduced by this shape at the
    shipped `embedding_cache_size` of 2048: a trim evicting a key between another thread's insert
    and its read (`KeyError`, naming nothing, on the interactive path), and two trims mutating the
    dict together (`RuntimeError: dictionary changed size during iteration`).

    Measured on the pre-fix tree with these parameters: 6 of 6 trials failed, 1-3 of the 8 threads
    each time. A race test cannot promise to fail every run, so this is written to make the window
    as wide as the real workload does rather than to be a coin flip — batches that overlap, and a
    total well past the bound.
    """
    import concurrent.futures

    def embed_a_batch(worker: int) -> int:
        return len(embed_texts([f"text-{worker}-{index}" for index in range(600)]))

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        counts = list(pool.map(embed_a_batch, range(8)))

    # A raised KeyError/RuntimeError fails the test by propagating out of `map`; this pins that
    # every caller also got a complete answer rather than a short one.
    assert counts == [600] * 8
