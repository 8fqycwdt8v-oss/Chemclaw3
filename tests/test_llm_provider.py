"""The LLM gateway seam builds one client, against the configured address, and only here.

These prove the wiring (`build_chat_model` carries endpoint, credential and transport into a really
constructed client) without network calls. Some assert a destination: no first-party module may
name a second vendor's client.
"""

import ast
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr

import chemclaw.agent.llm_provider as provider
from chemclaw.core.config import Settings, settings

_SRC = Path(__file__).resolve().parents[1] / "src" / "chemclaw"

# The distributions that ship a model client, and the only first-party modules that may name one,
# each with what it builds and why nothing else may.
_PROVIDER_ROOTS = frozenset({"openai", "anthropic", "langchain_openai", "langchain_anthropic"})

_CLIENT_SEAMS: dict[str, str] = {
    "agent/llm_provider.py": (
        "the seam: `ChatOpenAI` against the gateway, plus the SDK exception classes the failure "
        "taxonomy is written from"
    ),
    "core/embeddings.py": (
        "the parallel embedding seam. It cannot go through `build_chat_model` — that builds a "
        "*chat* model and `ChatOpenAI` cannot embed — so it builds `openai.OpenAI` against the "
        "same gateway, with the same transport rule (`core/http.gateway_client_kwargs`)"
    ),
}

# Modules that name a provider distribution for its *response types* and never a client. The mock
# gateway is the server side of this protocol: it emits the frames `langchain_openai` deserializes,
# so it needs the types and would be wrong to hold a client.
_TYPES_ONLY: dict[str, str] = {
    "cli/mock_llm.py": "the mock gateway serves the protocol; it emits frames, it does not dial",
}

# Modules that name a provider distribution for one *function* and neither dial nor deserialise a
# frame. Kept separate from `_TYPES_ONLY`, whose grant rests on the target being a `.types` module.
_HELPERS_ONLY: dict[str, str] = {
    "agent/turn_usage.py": (
        "reads `_create_usage_metadata` to normalise the usage block of a call the gateway billed "
        'and nothing metered: with `method="json_schema"` the SDK parses inside `_agenerate`, so '
        "a judge reply that fails validation raises before `on_llm_end` and booked 1,100 tokens of "
        "a served 6,600 — the verifier's own documented degrade path. Re-implementing that "
        "normalisation would put a silently-drifting copy of upstream's shape here, including the "
        "cache-token detail keys a `service_tier` response prefixes; "
        "`tests/test_upstream_surface.py` drives the real function so a rename turns red there"
    ),
}


def _provider_imports() -> dict[str, list[str]]:
    """Every first-party import of a provider distribution, as {relative path: [targets]}."""
    found: dict[str, list[str]] = {}
    for path in sorted(_SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        targets: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                targets += [a.name for a in node.names if a.name.split(".")[0] in _PROVIDER_ROOTS]
            elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
                if node.module.split(".")[0] in _PROVIDER_ROOTS:
                    targets.append(node.module)
        if targets:
            found[str(path.relative_to(_SRC))] = sorted(set(targets))
    return found


def test_a_provider_client_class_is_imported_only_at_the_two_declared_seams() -> None:
    """A provider client class is imported only at the two declared seams."""
    declared = set(_CLIENT_SEAMS) | set(_TYPES_ONLY) | set(_HELPERS_ONLY)
    found = _provider_imports()
    assert set(found) == declared, (
        "a module gained or lost a provider-SDK import. Every one is a decision about where a "
        "prompt can go, so declare it in _CLIENT_SEAMS/_TYPES_ONLY/_HELPERS_ONLY with its "
        "reason. "
        f"unexpected: {sorted(set(found) - declared)}; "
        f"stale rows: {sorted(declared - set(found))}"
    )


def test_a_types_only_module_holds_no_client() -> None:
    """`cli/mock_llm.py` may name the wire format; it may not name something that dials.

    The distinction is what makes the row above safe to grant: a server that imports response types
    is implementing the protocol, and a server that imports a client is a second destination.
    """
    found = _provider_imports()
    for path in _TYPES_ONLY:
        for target in found[path]:
            assert ".types" in target, (
                f"{path} imports {target!r}, which is not a response-type module. "
                f"{_TYPES_ONLY[path]} — a client here would be a second way out of the pod."
            )


def test_a_helper_only_module_holds_no_client() -> None:
    """A module granted one helper function may not also import something that dials.

    Asserted by imported symbol rather than module path, because a helper lives beside its client
    class (`_create_usage_metadata` and `ChatOpenAI` share a module).
    """
    for path, reason in _HELPERS_ONLY.items():
        tree = ast.parse((_SRC / path).read_text(encoding="utf-8"), filename=path)
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom) or node.level:
                continue
            if not node.module or node.module.split(".")[0] not in _PROVIDER_ROOTS:
                continue
            for alias in node.names:
                # Leading underscores stripped first: the question is whether the name is a
                # function or a class, and `_create_usage_metadata` is private *and* a function.
                assert alias.name.lstrip("_")[:1].islower(), (
                    f"{path} imports {alias.name!r} from {node.module!r}, which names a class "
                    f"rather than a function. {reason} — a client here would be a second way out "
                    "of the pod."
                )


def test_no_first_party_module_imports_the_anthropic_sdk() -> None:
    """No first-party module imports the Anthropic SDK.

    `deepagents` keeps `anthropic` in the resolved closure, so "not installed" is not the control;
    the control is that nothing here imports it.
    """
    offenders = {
        path: targets
        for path, targets in _provider_imports().items()
        if any(t.split(".")[0] in {"anthropic", "langchain_anthropic"} for t in targets)
    }
    assert not offenders, (
        "a first-party module imports the Anthropic SDK again. Every model call goes through "
        "`build_chat_model` to one OpenAI-compatible gateway "
        f"(D-2026-09-04-a-gateway-is-the-only-provider): {offenders}"
    )


def test_a_configured_gateway_is_where_the_model_is_built(monkeypatch: pytest.MonkeyPatch) -> None:
    """The configured gateway address is the address the model is built against."""
    _use_settings(
        monkeypatch,
        llm_base_url="https://gateway.internal/v1",
        llm_model="whatever-the-gateway-serves",
    )
    model = provider.build_chat_model("agent")
    assert str(model.openai_api_base) == "https://gateway.internal/v1"
    assert not hasattr(model, "anthropic_api_url"), (
        "a vendor client was built; the gateway address would be ignored"
    )


def _use_settings(monkeypatch: pytest.MonkeyPatch, **overrides: Any) -> Settings:
    """Point the provider module at a fresh Settings built from explicit overrides."""
    cfg = Settings(**overrides)
    monkeypatch.setattr(provider, "settings", cfg)
    return cfg


def test_openai_compatible_model_carries_endpoint_and_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`build_chat_model` points ChatOpenAI at the internal endpoint and honours the task route."""
    _use_settings(
        monkeypatch,
        llm_base_url="https://llm.internal/v1",
        llm_model="internal-large",
        llm_api_key=SecretStr("generic-key"),
        llm_timeout_seconds=12.0,
        llm_max_retries=5,
        model_routes={"verifier": "internal-small"},
    )

    default = provider.build_chat_model()
    assert str(default.openai_api_base) == "https://llm.internal/v1"
    assert default.model_name == "internal-large"
    assert default.request_timeout == 12.0
    assert default.max_retries == 5

    # One dial for every task, so the verifier cannot end up on a different model than the one a
    # deployment routed it to.
    assert provider.build_chat_model("verifier").model_name == "internal-small"


def test_keyless_endpoint_gets_placeholder_for_the_model_half(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A keyless gateway still constructs, which is why there is no credential preflight.

    The OpenAI SDK refuses an empty `api_key`, so an empty `CHEMCLAW_LLM_API_KEY` is served by a
    placeholder: many internal gateways ignore the bearer.
    """
    _use_settings(
        monkeypatch,
        llm_base_url="https://llm.internal/v1",
        llm_model="internal-model",
        llm_api_key=SecretStr(""),
    )
    assert provider.build_chat_model().openai_api_key.get_secret_value()


def test_the_openai_compatible_model_asks_the_endpoint_for_token_usage() -> None:
    """The OpenAI-compatible model asks the endpoint for token usage.

    `ChatOpenAI` enables `stream_usage` only with no custom base URL and no custom HTTP client;
    `_openai_compatible_model` sets both, so without forcing it no usage chunk arrives and the cost
    ledger reads zero, disarming the runaway-cost guard. Asserted on the built model.
    """
    from chemclaw.agent.llm_provider import _openai_compatible_model

    assert _openai_compatible_model("m").stream_usage is True


def test_an_endpoint_that_cannot_report_usage_can_be_told_so(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An endpoint that rejects `stream_options` can turn usage reporting off with a setting.

    The ledger then reads zero as a stated consequence rather than a silent one.
    """
    from chemclaw.agent.llm_provider import _openai_compatible_model

    monkeypatch.setattr(settings, "llm_stream_usage", False)
    assert _openai_compatible_model("m").stream_usage is False


def _reset_gateway_clients() -> None:
    """Close the process-scoped gateway clients, then drop them from the cache.

    `_tls_http_clients` is `@cache`d and holds a pair of live pools; a test that drops the only
    reference should close them first. Production never clears the cache. A helper rather than an
    autouse fixture, because the tests that clear are the ones asserting on the cache.
    """
    from chemclaw.agent.llm_provider import _tls_http_clients

    if _tls_http_clients.cache_info().currsize:
        sync_client, async_client = _tls_http_clients()
        sync_client.close()
        # The async client's pool is closed by its own `__del__`; `aclose()` needs a loop this
        # helper is not running in, and forcing one here would bind the pool to a loop that ends.
        del async_client
    _tls_http_clients.cache_clear()


def test_the_gateway_clients_are_built_once_per_process(monkeypatch: pytest.MonkeyPatch) -> None:
    """The gateway clients are built once per process.

    A graph is compiled per turn and reaches `_tls_http_clients`, so an uncached factory would leak
    a client (pool and TLS context) per turn.
    """
    import certifi

    from chemclaw.agent.llm_provider import _tls_http_clients

    _reset_gateway_clients()
    # A real PEM, because httpx loads the bundle when the client is constructed — a made-up path
    # would fail in `ssl` before reaching the property under test. Which trust store it is does not
    # matter here; that it is a store the client accepts does.
    monkeypatch.setattr(settings, "llm_tls_ca_bundle", certifi.where())
    try:
        first = _tls_http_clients()
        assert _tls_http_clients() is first, "a second turn must reuse the process's clients"
        # The configured CA bundle still reaches the TLS context; this file is the only place that
        # pins it.
        for client in first:
            context = client._transport._pool._ssl_context
            assert context.get_ca_certs(), "the configured bundle produced an empty trust store"
    finally:
        _reset_gateway_clients()


def test_both_gateway_clients_exist_and_refuse_the_environment_with_no_bundle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no CA bundle, both gateway clients are still ours and refuse the proxy environment.

    An SDK-built httpx client has `trust_env=True`, so an ambient `HTTP_PROXY` would receive the
    prompt and the bearer while the egress guard sees nothing. The bundle decides only verification.
    Asserted on the sync and async clients, since both are passed.
    """
    from chemclaw.agent.llm_provider import _tls_http_clients

    _reset_gateway_clients()
    monkeypatch.setattr(settings, "llm_tls_ca_bundle", "")
    try:
        sync_client, async_client = _tls_http_clients()
        assert sync_client is not None and async_client is not None
        for client in (sync_client, async_client):
            assert client.trust_env is False, "a client that trusts the env follows HTTP(S)_PROXY"
            proxy_mounts = [key for key in client._mounts if key.pattern is not None]
            assert proxy_mounts == [], f"proxy mounts resolved from the environment: {proxy_mounts}"
    finally:
        _reset_gateway_clients()


def test_the_chat_model_is_handed_both_of_this_process_s_clients(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The chat model is constructed with both of this process's clients.

    Read off the constructed `ChatOpenAI`, so a constructor argument dropped in a refactor fails
    here even when the factory is correct.
    """
    _openai_endpoint(monkeypatch)
    from chemclaw.agent.llm_provider import _tls_http_clients, build_chat_model

    _reset_gateway_clients()
    try:
        model = build_chat_model()
        sync_client, async_client = _tls_http_clients()
        assert model.root_client._client is sync_client
        assert model.root_async_client._client is async_client
    finally:
        _reset_gateway_clients()


def _openai_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    """Configure a primary gateway endpoint."""
    monkeypatch.setattr(settings, "llm_base_url", "https://primary.internal/v1")
    monkeypatch.setattr(settings, "llm_model", "internal-large")
    monkeypatch.setattr(settings, "llm_api_key", SecretStr("primary-key"))


def test_no_fallback_configured_returns_the_model_itself(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no fallback configured, `build_chat_model` returns the model itself, not a wrapper.

    Callers using `ChatOpenAI`-only attributes would break on a `RunnableWithFallbacks`.
    """
    _openai_endpoint(monkeypatch)
    monkeypatch.setattr(settings, "llm_fallback_base_url", "")

    model = provider.build_chat_model()
    assert type(model).__name__ == "ChatOpenAI"


def test_a_configured_fallback_wraps_the_model_and_reuses_the_primarys_model_and_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A second endpoint is enough; the model name and credential default to the primary's.

    The common case is a second replica of one internal deployment rather than a different vendor,
    and requiring all three would make that case verbose enough that somebody skips it.
    """
    _openai_endpoint(monkeypatch)
    monkeypatch.setattr(settings, "llm_fallback_base_url", "https://standby.internal/v1")
    monkeypatch.setattr(settings, "llm_fallback_model", "")
    monkeypatch.setattr(settings, "llm_fallback_api_key", SecretStr(""))

    model = provider.build_chat_model()
    assert type(model).__name__ == "RunnableWithFallbacks"
    standby = model.fallbacks[0]
    assert str(standby.openai_api_base) == "https://standby.internal/v1"
    assert standby.model_name == "internal-large", "the fallback should reuse the primary's model"
    assert standby.openai_api_key.get_secret_value() == "primary-key"


def test_the_fallback_may_name_its_own_model_and_credential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A genuinely different endpoint needs its own two values, and they win when set."""
    _openai_endpoint(monkeypatch)
    monkeypatch.setattr(settings, "llm_fallback_base_url", "https://other.example/v1")
    monkeypatch.setattr(settings, "llm_fallback_model", "other-model")
    monkeypatch.setattr(settings, "llm_fallback_api_key", SecretStr("other-key"))

    standby = provider.build_chat_model().fallbacks[0]
    assert standby.model_name == "other-model"
    assert standby.openai_api_key.get_secret_value() == "other-key"


def test_only_an_endpoint_that_is_down_fails_over(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only an endpoint that is down fails over; a refused request is not re-sent to the standby.

    `with_fallbacks` catches every `Exception` by default, which would double latency for a
    malformed request and disguise a 400 as an outage. Asserted on the handled exception set.
    """
    from openai import APIConnectionError, APITimeoutError, BadRequestError, InternalServerError

    _openai_endpoint(monkeypatch)
    monkeypatch.setattr(settings, "llm_fallback_base_url", "https://standby.internal/v1")

    handled = provider.build_chat_model().exceptions_to_handle
    assert APIConnectionError in handled
    assert issubclass(APITimeoutError, tuple(handled)), "a timeout is an outage"
    assert InternalServerError in handled
    assert not issubclass(BadRequestError, tuple(handled)), (
        "a malformed request must fail where it was made, not be retried against the standby"
    )


def test_binding_tools_reaches_the_fallback_too(monkeypatch: pytest.MonkeyPatch) -> None:
    """Binding tools reaches the fallback too.

    `create_agent` binds tools to whatever `build_chat_model` returns; a failover model without
    tools would answer fluently and do nothing.
    """
    from langchain_core.tools import StructuredTool

    _openai_endpoint(monkeypatch)
    monkeypatch.setattr(settings, "llm_fallback_base_url", "https://standby.internal/v1")
    tool = StructuredTool.from_function(
        name="find_notes", description="d", func=lambda: "", infer_schema=True
    )

    bound = provider.build_chat_model().bind_tools([tool])
    assert type(bound).__name__ == "RunnableWithFallbacks", "the fallback survived binding"
    assert "tools" in bound.runnable.kwargs, "the primary carries the tools"
    assert "tools" in bound.fallbacks[0].kwargs, "so does the standby"
