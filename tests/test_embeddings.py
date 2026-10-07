"""The embedding provider seam builds vectors per config, and only here.

Offline: the `hash` embedder is deterministic, correctly sized, orthogonal for disjoint text and
more similar for overlapping text. Wiring: the `openai_compatible` path calls the endpoint with the
configured model, with the client classes faked so no network happens.
"""

import math
import sys
from typing import Any

import pytest

import chemclaw.core.embeddings as provider
from chemclaw.core.config import Settings


def _use_settings(monkeypatch: pytest.MonkeyPatch, **overrides: Any) -> None:
    """Point the provider module at a fresh Settings built from explicit overrides."""
    monkeypatch.setattr(provider, "settings", Settings(**overrides))


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def test_hash_embedding_is_deterministic_and_sized(monkeypatch: pytest.MonkeyPatch) -> None:
    """The same text embeds identically, to a vector of the configured dimension."""
    _use_settings(monkeypatch, embedding_provider="hash", embedding_dim=256)
    one = provider.embed_texts(["acetylation of salicylic acid"])
    two = provider.embed_texts(["acetylation of salicylic acid"])
    assert len(one) == 1 and len(one[0]) == 256
    assert one[0] == two[0]  # deterministic


def test_hash_embedding_ranks_overlap_above_disjoint(monkeypatch: pytest.MonkeyPatch) -> None:
    """Token overlap yields higher cosine than disjoint text — the retrieval-relevant property."""
    _use_settings(monkeypatch, embedding_provider="hash", embedding_dim=512)
    query, overlap, disjoint = provider.embed_texts(
        ["amide coupling epimerization", "amide coupling temperature", "distillation column reflux"]
    )
    assert _cosine(query, overlap) > _cosine(query, disjoint)


def test_hash_embedding_of_tokenless_text_is_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    """Text with no tokens embeds to a zero vector (cosine 0 — no spurious match)."""
    _use_settings(monkeypatch, embedding_provider="hash", embedding_dim=64)
    assert provider.embed_texts(["   !!!   "])[0] == [0.0] * 64


def test_empty_input_returns_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """No texts in, no vectors out (no endpoint call)."""
    _use_settings(
        monkeypatch,
        embedding_provider="openai_compatible",
        embedding_model="m",
        llm_base_url="https://llm.internal/v1",
    )
    assert provider.embed_texts([]) == []


def test_openai_compatible_path_calls_the_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    """`openai_compatible` sends model + input to the endpoint and returns its vectors."""
    _use_settings(
        monkeypatch,
        embedding_provider="openai_compatible",
        embedding_model="internal-embed",
        llm_base_url="https://llm.internal/v1",
    )
    captured: dict[str, Any] = {}

    class _FakeEmbeddings:
        def create(self, *, model: str, input: list[str]) -> Any:
            captured["model"] = model
            captured["input"] = input
            data = [type("E", (), {"embedding": [float(i)], "index": i}) for i in range(len(input))]
            return type("R", (), {"data": data})

    class _FakeOpenAI:
        def __init__(self, **kwargs: Any) -> None:
            captured["init"] = kwargs
            self.embeddings = _FakeEmbeddings()

    fake_openai = type(sys)("openai")
    fake_openai.OpenAI = _FakeOpenAI  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "openai", fake_openai)

    vectors = provider.embed_texts(["a", "b"])
    assert captured["model"] == "internal-embed"
    assert captured["input"] == ["a", "b"]
    assert captured["init"]["base_url"] == "https://llm.internal/v1"
    assert vectors == [[0.0], [1.0]]


def test_a_reordered_batch_is_paired_by_index_and_not_by_position(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reordered batch is paired by `index`, not by position.

    `data` order is not part of the embeddings contract and batching servers reorder. Read
    positionally each text gets its neighbour's vector, which `zip(strict=True)` cannot detect and
    nothing downstream can see.
    """
    _use_settings(
        monkeypatch,
        embedding_provider="openai_compatible",
        embedding_model="internal-embed",
        # A distinct endpoint per test on purpose: `_openai_client` is an `lru_cache` keyed on the
        # transport config, so two tests sharing a `base_url` would share one client — and
        # therefore each other's fake.
        llm_base_url="https://llm-reordering.internal/v1",
    )

    class _ReorderingEmbeddings:
        """Answers correctly, in the reverse order — every item still carrying its own index."""

        def create(self, *, model: str, input: list[str]) -> Any:
            data = [type("E", (), {"embedding": [float(i)], "index": i}) for i in range(len(input))]
            return type("R", (), {"data": list(reversed(data))})

    class _FakeOpenAI:
        def __init__(self, **kwargs: Any) -> None:
            self.embeddings = _ReorderingEmbeddings()

    fake_openai = type(sys)("openai")
    fake_openai.OpenAI = _FakeOpenAI  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "openai", fake_openai)

    assert provider.embed_texts(["a", "b", "c"]) == [[0.0], [1.0], [2.0]]


def test_a_batch_whose_indices_are_not_its_own_positions_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A batch whose indices are not its own positions is refused.

    A missing, repeated or request-relative `index` leaves `sorted` in arrival order, and the
    resulting corruption is invisible once written.
    """
    _use_settings(
        monkeypatch,
        embedding_provider="openai_compatible",
        embedding_model="internal-embed",
        llm_base_url="https://llm-flat-index.internal/v1",
    )

    class _FlatIndexEmbeddings:
        """Numbers every item 0 — the shape a stable sort cannot tell from a correct answer."""

        def create(self, *, model: str, input: list[str]) -> Any:
            data = [type("E", (), {"embedding": [float(i)], "index": 0}) for i in range(len(input))]
            return type("R", (), {"data": data})

    class _FakeOpenAI:
        def __init__(self, **kwargs: Any) -> None:
            self.embeddings = _FlatIndexEmbeddings()

    fake_openai = type(sys)("openai")
    fake_openai.OpenAI = _FakeOpenAI  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "openai", fake_openai)

    with pytest.raises(ValueError, match="index"):
        provider.embed_texts(["a", "b"])


def test_openai_compatible_half_config_is_rejected_at_build_time() -> None:
    """A missing endpoint/model fails when Settings is built, before any embed call happens."""
    with pytest.raises(ValueError, match="embedding_model"):
        Settings(embedding_provider="openai_compatible", llm_base_url="x")


def test_config_key_separates_two_endpoints_serving_the_same_model_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The config key separates two endpoints serving the same model name.

    Model names are not globally unique, so repointing `llm_base_url` must invalidate stored
    vectors.
    """
    endpoint = {
        "embedding_provider": "openai_compatible",
        "embedding_model": "text-embedding-3-large",
    }
    _use_settings(monkeypatch, **endpoint, llm_base_url="https://vendor.example/v1")
    vendor = provider.embedding_config_key()
    _use_settings(monkeypatch, **endpoint, llm_base_url="https://gateway.internal/v1")
    assert provider.embedding_config_key() != vendor


def test_config_key_ignores_a_trailing_slash_on_the_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`.../v1` and `.../v1/` are the same endpoint, so they must not force a corpus re-embed."""
    endpoint = {"embedding_provider": "openai_compatible", "embedding_model": "internal-embed"}
    _use_settings(monkeypatch, **endpoint, llm_base_url="https://llm.internal/v1")
    plain = provider.embedding_config_key()
    _use_settings(monkeypatch, **endpoint, llm_base_url="https://llm.internal/v1/")
    assert provider.embedding_config_key() == plain


def test_config_key_carries_no_part_of_the_endpoint_it_identifies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The config key carries no part of the endpoint it identifies.

    It is written into every row of two durable tables, and `llm_base_url` may carry userinfo or an
    internal hostname. A digest identifies the endpoint without carrying it; invalidation is
    asserted separately below.
    """
    _use_settings(
        monkeypatch,
        embedding_provider="openai_compatible",
        embedding_model="internal-embed",
        llm_base_url="https://svc:s3cr3t-token@llm.internal/v1",
    )
    key = provider.embedding_config_key()
    for leaked in ("s3cr3t-token", "svc", "llm.internal", "https"):
        assert leaked not in key, f"the key carries {leaked!r}: {key}"


def test_every_slot_of_the_config_key_says_what_it_is(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every slot of the config key says what it is.

    Operators read it out of durable columns, and empty slots read as truncated or corrupt.
    """
    _use_settings(monkeypatch, embedding_provider="hash")
    assert provider.embedding_config_key() == "hash:ep-none:d1536:model-none"


def test_a_colon_in_the_model_name_cannot_be_read_as_a_separator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A colon in the model name cannot be read as a separator.

    The model is the last field, so everything after the third colon is the model.
    """
    _use_settings(
        monkeypatch,
        embedding_provider="openai_compatible",
        embedding_model="nomic-embed-text:v1.5",
        llm_base_url="https://llm.internal/v1",
    )
    provider_name, endpoint, dimension, model = provider.embedding_config_key().split(":", 3)
    assert (provider_name, dimension, model) == (
        "openai_compatible",
        "d1536",
        "nomic-embed-text:v1.5",
    )
    assert endpoint.startswith("ep-")


def test_config_key_of_the_hash_provider_names_no_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The hash embedder never calls the endpoint, so `llm_base_url` cannot change its vectors."""
    _use_settings(monkeypatch, embedding_provider="hash", llm_base_url="https://one.example/v1")
    first = provider.embedding_config_key()
    _use_settings(monkeypatch, embedding_provider="hash", llm_base_url="https://two.example/v1")
    assert provider.embedding_config_key() == first
