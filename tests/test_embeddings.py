"""The embedding provider seam builds vectors per config, and only here (plan F10-A1).

Offline: the `hash` embedder is deterministic, correctly sized, orthogonal for disjoint text, and
more similar for token-overlapping text (the property retrieval relies on). Wiring: the
`openai_compatible` path calls the endpoint with the configured model and returns its vectors,
with the client classes faked so no network happens.
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
    """Every text keeps its own vector when the provider answers a batch out of order.

    The OpenAI embeddings response carries a per-item `index` precisely because `data` order is not
    part of the contract, and batching servers reorder: vLLM, TEI and gateway proxies all do. Read
    positionally, a reordered batch assigns each text its neighbour's vector — and nothing
    downstream can see it. `embed_texts` pairs the vectors with the de-duplicated texts by `zip(...,
    strict=True)`, which catches a *count* mismatch and cannot see a *permutation*; the stored
    vectors are then all wrong, `embedding_key` still reads as current, and the only symptom is bad
    recall, which is what `embedding_config_key`'s docstring calls corrupting every similarity
    silently.
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
    """Sorting is only a fix while `index` says what it is supposed to say.

    A provider that omits the field, repeats a value, or numbers a chunk against the whole request
    leaves `sorted` stable — which is arrival order again, silently. The corruption this prevents
    is unrecoverable and invisible once written, so an index set that is not the chunk's own
    positions is refused rather than sorted on trust.
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
    """The defect: `provider:model:dim` read as identical across two different endpoints.

    Model names are not globally unique — `text-embedding-3-large` is served by the vendor and by
    any gateway that proxies it, and two of them need not be the same weights. Repointing
    `llm_base_url` therefore has to invalidate the stored vectors, or every key already on record
    keeps reading as current and nothing is ever re-embedded.
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
    """The key is written into two durable columns, so it may not *be* the endpoint.

    `document_chunks.embedding_key` and `note_index.embedding_key` get one copy of this string per
    row, in tables nothing prunes and the runtime role can read. `llm_base_url` is a plain `str`
    with no validator forbidding userinfo, so `https://svc:s3cr3t@llm.internal/v1` is a
    configuration this deployment accepts — and the verbatim form persisted the password. Even
    without one, the internal hostname does not belong in every row of the corpus.

    A digest identifies the endpoint without carrying it, which is all the invalidation property
    needs — and that property is asserted separately, immediately below.
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
    """An operator reads this string out of two durable columns, so no slot may be empty.

    The default deployment rendered `hash:::1536` — two empty slots, because `embedding_model`
    defaults to `""` as well as the endpoint — which reads as truncated or corrupt rather than as
    "no endpoint, no model name". Filled and prefixed, every field names itself.
    """
    _use_settings(monkeypatch, embedding_provider="hash")
    assert provider.embedding_config_key() == "hash:ep-none:d1536:model-none"


def test_a_colon_in_the_model_name_cannot_be_read_as_a_separator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The one free-form field is last, so Ollama/vLLM naming cannot make the key unparseable.

    `nomic-embed-text:v1.5` is an ordinary model name. With the dimension after it the key had five
    colon-separated fields and no way to tell which was which; with the model last, everything after
    the third colon is the model and nothing else can be.
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
