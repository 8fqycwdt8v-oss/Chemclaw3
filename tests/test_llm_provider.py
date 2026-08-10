"""The LLM provider seam builds the right client per config, and only here (plan Phase F0).

These prove the *wiring* — that `build_chat_client` selects the configured provider and carries the
endpoint/credential/transport into the constructed client — without any network call. The provider
client classes are monkeypatched so the test asserts on what they were constructed with, not on live
model behavior.
"""

import sys
from typing import Any

import pytest

import chemclaw.agent.llm_provider as provider
from chemclaw.core.config import Settings


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


def _fake_openai_client_capture(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Wire fake AsyncOpenAI + OpenAIChatClient and return the dict they capture kwargs into."""
    captured: dict[str, Any] = {}
    fake_openai = type(sys)("openai")
    fake_openai.AsyncOpenAI = lambda **k: None  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "openai", fake_openai)
    fake_af_openai = type(sys)("agent_framework.openai")
    fake_af_openai.OpenAIChatClient = lambda **k: captured.update(k)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "agent_framework.openai", fake_af_openai)
    return captured


def test_model_routes_select_the_task_model(monkeypatch: pytest.MonkeyPatch) -> None:
    """A routed task uses its mapped model; an unrouted task falls back to the default model."""
    _use_settings(
        monkeypatch,
        llm_provider="openai_compatible",
        llm_base_url="https://llm.internal/v1",
        llm_model="internal-large",
        model_routes={"verifier": "internal-small"},
    )
    captured = _fake_openai_client_capture(monkeypatch)

    provider.build_chat_client("verifier")
    assert captured["model"] == "internal-small"  # routed to the cheap model

    provider.build_chat_client("agent")
    assert captured["model"] == "internal-large"  # unrouted → default llm_model


def test_default_task_is_unchanged_without_routes(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no routes, the default `agent` task keeps the provider's default model."""
    _use_settings(
        monkeypatch,
        llm_provider="openai_compatible",
        llm_base_url="https://llm.internal/v1",
        llm_model="internal-model",
    )
    captured = _fake_openai_client_capture(monkeypatch)
    provider.build_chat_client()  # default task, empty model_routes
    assert captured["model"] == "internal-model"


# --- the LangGraph half of the seam (D-2026-08-10, phase M1) ------------------------------------
#
# These construct the *real* `ChatOpenAI`/`ChatAnthropic` rather than faking them through
# `sys.modules` as the MAF tests above must. That is not a style difference: the MAF clients are
# faked because asserting on them means asserting on constructor kwargs, while a LangChain chat
# model exposes the same values as attributes — so the stronger assertion is available, and it
# doubles as a live check of this module's "construction only, no network call" claim.


def test_openai_compatible_model_carries_endpoint_and_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`build_chat_model` points ChatOpenAI at the internal endpoint and honours the task route."""
    _use_settings(
        monkeypatch,
        llm_provider="openai_compatible",
        llm_base_url="https://llm.internal/v1",
        llm_model="internal-large",
        llm_api_key="generic-key",
        llm_timeout_seconds=12.0,
        llm_max_retries=5,
        model_routes={"verifier": "internal-small"},
    )

    default = provider.build_chat_model()
    assert str(default.openai_api_base) == "https://llm.internal/v1"
    assert default.model_name == "internal-large"
    assert default.request_timeout == 12.0
    assert default.max_retries == 5

    # The route is the same dial both halves read, so a deployment cannot end up with the verifier
    # on the cheap model under one engine and the expensive one under the other.
    assert provider.build_chat_model("verifier").model_name == "internal-small"


def test_keyless_endpoint_gets_placeholder_for_the_model_half(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A keyless internal endpoint still constructs — the placeholder applies to both halves."""
    _use_settings(
        monkeypatch,
        llm_provider="openai_compatible",
        llm_base_url="https://llm.internal/v1",
        llm_model="internal-model",
        llm_api_key="",
    )
    assert provider.build_chat_model().openai_api_key.get_secret_value()


def test_anthropic_model_path_preflights_missing_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """The graph engine gets the same eager credential failure, for a sharper reason.

    Under MAF a missing key surfaced before the turn began. Under the graph engine the model is
    built inside `build_graph`, so without this preflight the failure would be an opaque 401 in the
    middle of a stream that has already emitted events — a turn that looks like it started working
    and then died. Same message, same `_require_anthropic_key`, shared by both halves.
    """
    _use_settings(monkeypatch, llm_provider="anthropic")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        provider.build_chat_model()
