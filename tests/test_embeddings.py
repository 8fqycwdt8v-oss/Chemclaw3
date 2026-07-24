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

import agents.embedding_provider as provider
from chemclaw.config import Settings


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
            data = [type("E", (), {"embedding": [float(i)]}) for i in range(len(input))]
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


def test_openai_compatible_half_config_is_rejected_at_build_time() -> None:
    """A missing endpoint/model fails when Settings is built, before any embed call happens."""
    with pytest.raises(ValueError, match="embedding_model"):
        Settings(embedding_provider="openai_compatible", llm_base_url="x")
