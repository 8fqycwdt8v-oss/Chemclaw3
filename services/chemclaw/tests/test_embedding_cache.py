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

from chemclaw import embeddings
from chemclaw.config import settings
from chemclaw.embeddings import clear_embedding_cache, embed_texts


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
    assert len(embeddings._CACHE) <= 4  # noqa: SLF001 — the bound is the property under test


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
    direct = embeddings._hash_embedding("acetonitrile")  # noqa: SLF001
    assert embed_texts(["acetonitrile"])[0] == direct
    assert embed_texts(["acetonitrile"])[0] == direct


def test_an_empty_batch_is_not_a_cache_lookup() -> None:
    """Cheap, and it keeps the zip-strict pairing below from ever seeing an empty provider call."""
    assert embed_texts([]) == []
