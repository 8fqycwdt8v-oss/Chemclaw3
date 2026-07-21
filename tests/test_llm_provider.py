"""The LLM provider seam builds the right client per config, and only here (plan Phase F0).

These prove the *wiring* — that `build_chat_client` selects the configured provider and carries the
endpoint/credential/transport into the constructed client — without any network call. The provider
client classes are monkeypatched so the test asserts on what they were constructed with, not on live
model behavior.
"""

import sys
from typing import Any

import pytest

import agents.llm_provider as provider
from chemclaw.config import Settings


def _use_settings(monkeypatch: pytest.MonkeyPatch, **overrides: Any) -> Settings:
    """Point the provider module at a fresh Settings built from explicit overrides."""
    cfg = Settings(**overrides)
    monkeypatch.setattr(provider, "settings", cfg)
    return cfg


def test_openai_compatible_client_carries_endpoint_and_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`openai_compatible` builds an OpenAIChatClient over an AsyncOpenAI with our base_url/key."""
    _use_settings(
        monkeypatch,
        llm_provider="openai_compatible",
        llm_base_url="https://llm.internal/v1",
        llm_model="internal-model",
        llm_api_key="generic-key",
        llm_timeout_seconds=12.0,
        llm_max_retries=5,
    )

    captured: dict[str, Any] = {}

    class FakeAsyncOpenAI:
        def __init__(self, **kwargs: Any) -> None:
            captured["openai"] = kwargs

    class FakeOpenAIChatClient:
        def __init__(self, **kwargs: Any) -> None:
            captured["maf"] = kwargs

    # The provider imports these lazily inside the function, from their real modules.
    monkeypatch.setitem(sys.modules, "openai", type(sys)("openai"))
    sys.modules["openai"].AsyncOpenAI = FakeAsyncOpenAI  # type: ignore[attr-defined]
    fake_af_openai = type(sys)("agent_framework.openai")
    fake_af_openai.OpenAIChatClient = FakeOpenAIChatClient  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "agent_framework.openai", fake_af_openai)

    provider.build_chat_client()

    assert captured["openai"]["base_url"] == "https://llm.internal/v1"
    assert captured["openai"]["api_key"] == "generic-key"
    assert captured["openai"]["timeout"] == 12.0
    assert captured["openai"]["max_retries"] == 5
    assert captured["maf"]["model"] == "internal-model"


def test_keyless_endpoint_gets_placeholder(monkeypatch: pytest.MonkeyPatch) -> None:
    """An internal endpoint with no configured key still constructs (non-empty placeholder key)."""
    _use_settings(
        monkeypatch,
        llm_provider="openai_compatible",
        llm_base_url="https://llm.internal/v1",
        llm_model="internal-model",
        llm_api_key="",
    )
    captured: dict[str, Any] = {}

    class FakeAsyncOpenAI:
        def __init__(self, **kwargs: Any) -> None:
            captured.update(kwargs)

    fake_openai = type(sys)("openai")
    fake_openai.AsyncOpenAI = FakeAsyncOpenAI  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "openai", fake_openai)
    fake_af_openai = type(sys)("agent_framework.openai")
    fake_af_openai.OpenAIChatClient = lambda **k: None  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "agent_framework.openai", fake_af_openai)

    provider.build_chat_client()

    assert captured["api_key"]  # non-empty, so the OpenAI SDK will not refuse to construct


def test_anthropic_path_preflights_missing_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """The Anthropic dev path fails clearly when its key is absent (unchanged pre-seam behavior)."""
    _use_settings(monkeypatch, llm_provider="anthropic")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        provider.build_chat_client()
