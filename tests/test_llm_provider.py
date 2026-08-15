"""The LLM provider seam builds the right client per config, and only here (plan Phase F0).

These prove the *wiring* — that `build_chat_model` selects the configured provider and carries the
endpoint/credential/transport into the constructed client — without any network call. The provider
client classes are monkeypatched so the test asserts on what they were constructed with, not on live
model behavior.
"""

import sys
from typing import Any

import pytest

import chemclaw.agent.llm_provider as provider
from chemclaw.core.config import Settings, settings


def _use_settings(monkeypatch: pytest.MonkeyPatch, **overrides: Any) -> Settings:
    """Point the provider module at a fresh Settings built from explicit overrides."""
    cfg = Settings(**overrides)
    monkeypatch.setattr(provider, "settings", cfg)
    return cfg


def test_anthropic_path_preflights_missing_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """The Anthropic dev path fails clearly when its key is absent (unchanged pre-seam behavior)."""
    _use_settings(monkeypatch, llm_provider="anthropic")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        provider.build_chat_model()


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


def test_the_openai_compatible_model_asks_the_endpoint_for_token_usage() -> None:
    """Without this the cost ledger reads zero, and nothing else notices.

    `ChatOpenAI` default-enables `stream_usage` only when *no* custom base URL and *no* custom HTTP
    client are configured. `_openai_compatible_model` sets both — the internal endpoint and the
    private-CA bundle — so upstream turns it off, the endpoint is never asked to report usage, no
    usage chunk arrives, and `runner_usage.graph_usage_tokens` correctly reads nothing.

    Measured before the fix: 15 turns through the graph engine wrote `turn_costs` rows totalling
    **0** tokens against 2,040 per session on the other engine. That is the same failure
    `usage_tokens`'s docstring records from the other direction, and it disarms the runaway-cost
    guard rather than making it conservative.

    Asserted on the built model rather than on a live stream, because the defect is a construction
    argument — and a test needing a real endpoint is a test that would not have run.
    """
    from chemclaw.agent.llm_provider import _openai_compatible_model

    assert _openai_compatible_model("m").stream_usage is True


def test_an_endpoint_that_cannot_report_usage_can_be_told_so(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The escape hatch is a setting, because upstream's caution is about real endpoints.

    LangChain disables the default on the stated grounds that "many non-OpenAI endpoints do not
    support streaming token usage". A deployment whose endpoint rejects `stream_options` needs a
    way out that is not a code change — and the ledger reading zero is then a stated consequence
    rather than a silent one.
    """
    from chemclaw.agent.llm_provider import _openai_compatible_model

    monkeypatch.setattr(settings, "llm_stream_usage", False)
    assert _openai_compatible_model("m").stream_usage is False


def test_the_private_ca_client_is_built_once_per_process(monkeypatch: pytest.MonkeyPatch) -> None:
    """A graph is compiled per turn, so an uncached client factory is a per-turn socket leak.

    `build_chat_model` runs on every graph build, and on the `openai_compatible` + private-CA
    path — the documented production target — it reaches `_tls_http_client`. Uncached, that built a
    fresh `AsyncClient` (its own pool, its own TLS context) per question asked, and nothing ever
    closed one. The verifier and challenge clients already pay `@cache` for exactly this on colder
    paths; this was the hot one.
    """
    import certifi

    from chemclaw.agent.llm_provider import _tls_http_client

    _tls_http_client.cache_clear()
    # A real PEM, because httpx loads the bundle when the client is constructed — a made-up path
    # would fail in `ssl` before reaching the property under test. Which trust store it is does not
    # matter here; that it is a store the client accepts does.
    monkeypatch.setattr(settings, "llm_tls_ca_bundle", certifi.where())
    try:
        first = _tls_http_client()
        assert first is not None, "a configured bundle must produce a pinned client"
        assert _tls_http_client() is first, "a second turn must reuse the process's client"
    finally:
        _tls_http_client.cache_clear()


def test_no_bundle_leaves_the_sdk_its_own_client(monkeypatch: pytest.MonkeyPatch) -> None:
    """The other half of the cache: caching must not turn "no bundle" into a client.

    A publicly-trusted endpoint wants the SDK's own default, which is what `None` asks for.
    """
    from chemclaw.agent.llm_provider import _tls_http_client

    _tls_http_client.cache_clear()
    monkeypatch.setattr(settings, "llm_tls_ca_bundle", "")
    try:
        assert _tls_http_client() is None
    finally:
        _tls_http_client.cache_clear()
