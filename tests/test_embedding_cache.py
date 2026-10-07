"""Repeated queries are embedded once.

Under a real provider an embedding is a network round trip on the interactive path. The cache's
one hazard is serving a vector the current model would not produce, so the configuration is part
of the key, and that is asserted first.
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
    """Changing the model does not serve the old model's vectors.

    A vector is reusable only for the configuration that produced it; anything else corrupts
    similarity silently.
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
    """A batch larger than the bound still returns every vector.

    The trim may evict a key this call inserted, so the answer is assembled from what the call
    holds, not re-read from `_CACHE`. `reindex_notes` embeds the whole corpus in one batch.
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
    """Concurrent batches do not race on the cache.

    Retrieval embeds through `asyncio.to_thread`, so concurrent turns share the cache from several
    threads. Without the lock, a trim evicts a key between another thread's insert and read
    (`KeyError`), or two trims mutate the dict together (`RuntimeError`). The workload is large and
    overlapping to widen the race window.

    The 600 s timeout covers coverage tracing, which slows this loop about thirty-fold; what the cap
    must catch is a deadlock on `_CACHE_LOCK`, which never finishes at either speed.
    """
    import concurrent.futures

    def embed_a_batch(worker: int) -> int:
        return len(embed_texts([f"text-{worker}-{index}" for index in range(600)]))

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        counts = list(pool.map(embed_a_batch, range(8)))

    # A raised KeyError/RuntimeError fails the test by propagating out of `map`; this pins that
    # every caller also got a complete answer rather than a short one.
    assert counts == [600] * 8
