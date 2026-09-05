"""The LLM gateway seam builds one client, against the configured address, and only here (F0).

These prove the *wiring* — that `build_chat_model` carries the endpoint, credential and transport
into the constructed client — without any network call. The client is constructed for real, because
a LangChain chat model exposes those values as attributes: the stronger assertion is available, and
it doubles as a live check of this module's "construction only, no network call" claim.

**Three of these are about a destination rather than a wiring**, and they are here because this is
the module that decides one. `D-2026-09-04-a-gateway-is-the-only-provider` removed the provider
concept after measuring that a second arm silently ignored `llm_base_url`; what makes that
irreversible is not the deletion but the assertions below that no first-party module can name a
second vendor's client again.
"""

import ast
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr

import chemclaw.agent.llm_provider as provider
from chemclaw.core.config import Settings, settings

_SRC = Path(__file__).resolve().parents[1] / "src" / "chemclaw"

# The distributions that ship a model client, and the *only* first-party modules that may name one.
# Each entry says what it builds and why nothing else may build it — the sentence
# `core/config/llm.py` used to make in prose ("No provider client class is imported outside
# `agent/llm_provider.py`"), which was false in three places and enforced by nothing.
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
    """The config comment's claim, as an assertion instead of a sentence.

    It read "No provider client class is imported outside `agent/llm_provider.py`" and was false in
    three places at once, with nothing checking it — `tests/test_third_party_layering.py` in fact
    *licensed* three packages to import the `llm` stack. A present-tense claim about a control that
    nothing enforces is the shape this repository has a standing rule against, so the sentence was
    narrowed to what the tree actually guarantees and this is what holds it there.
    """
    declared = set(_CLIENT_SEAMS) | set(_TYPES_ONLY)
    found = _provider_imports()
    assert set(found) == declared, (
        "a module gained or lost a provider-SDK import. Every one is a decision about where a "
        "prompt can go, so declare it in _CLIENT_SEAMS/_TYPES_ONLY with its reason. "
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


def test_no_first_party_module_imports_the_anthropic_sdk() -> None:
    """The absence, asserted, because a re-added import is how a second destination comes back.

    `anthropic` and `langchain-anthropic` are no longer declared in `pyproject.toml`, but they stay
    in the resolved closure because `deepagents` requires the wrapper — so "it is not installed" is
    not the control and never was. The control is that nothing here imports it —
    `evals/live_judge.py` was the last importer, and it posted that vendor's own protocol to
    `<gateway>/v1/messages`,
    which against an OpenAI-compatible gateway is a doubled path and a 404 degraded to `ungraded`
    on every probe.
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
    """The whole defect, as one assertion: the configured address is the address dialled.

    Measured on the pre-change tree, with `llm_base_url` set to an internal gateway and the
    provider left at its shipped default of `anthropic`::

        build_chat_model("agent").anthropic_api_url == 'https://api.anthropic.com'

    — the base URL was accepted by the config, passed every validator, and never reached a client.
    `api/middleware._refuse_public_llm_exposure` then returned early *because* it was set, and
    `core/netguard.derive_allowed` put the public host on the egress allowlist. There is no
    configuration that reproduces it now, which is why this asserts the positive: there is one
    branch, so the field either arrives or the test is red.
    """
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

    Many internal gateways ignore the bearer, and the OpenAI SDK refuses to construct with an empty
    `api_key` — so an empty `CHEMCLAW_LLM_API_KEY` is a legitimate configuration served by a
    placeholder, not a misconfiguration to fail at startup. That is what replaced D-037's eager
    `ANTHROPIC_API_KEY` check, and `cli/chat.py` says so where it used to promise the check.
    """
    _use_settings(
        monkeypatch,
        llm_base_url="https://llm.internal/v1",
        llm_model="internal-model",
        llm_api_key=SecretStr(""),
    )
    assert provider.build_chat_model().openai_api_key.get_secret_value()


def test_the_openai_compatible_model_asks_the_endpoint_for_token_usage() -> None:
    """Without this the cost ledger reads zero, and nothing else notices.

    `ChatOpenAI` default-enables `stream_usage` only when *no* custom base URL and *no* custom HTTP
    client are configured. `_openai_compatible_model` sets both — the gateway address and the
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


def test_the_gateway_clients_are_built_once_per_process(monkeypatch: pytest.MonkeyPatch) -> None:
    """A graph is compiled per turn, so an uncached client factory is a per-turn socket leak.

    `build_chat_model` runs on every graph build and reaches `_tls_http_clients`. Uncached, that
    built a fresh `AsyncClient` (its own pool, its own TLS context) per question asked, and nothing
    ever closed one. The verifier client already pays `@cache` for exactly this on a colder path;
    this is the hot one.
    """
    import certifi

    from chemclaw.agent.llm_provider import _tls_http_clients

    _tls_http_clients.cache_clear()
    # A real PEM, because httpx loads the bundle when the client is constructed — a made-up path
    # would fail in `ssl` before reaching the property under test. Which trust store it is does not
    # matter here; that it is a store the client accepts does.
    monkeypatch.setattr(settings, "llm_tls_ca_bundle", certifi.where())
    try:
        first = _tls_http_clients()
        assert _tls_http_clients() is first, "a second turn must reuse the process's clients"
        # The bundle is still reaching the context, asserted here because this file is the only
        # place that says so: an earlier version checked `is not None` against a factory that
        # returned `None` without one, and dropping that left the *configured* bundle pinned
        # nowhere but `tests/test_protocol_condense.py`'s unrelated `FileNotFoundError` path.
        for client in first:
            context = client._transport._pool._ssl_context
            assert context.get_ca_certs(), "the configured bundle produced an empty trust store"
    finally:
        _tls_http_clients.cache_clear()


def test_both_gateway_clients_exist_and_refuse_the_environment_with_no_bundle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The shipped configuration is the no-bundle one, and it used to be the unprotected one.

    **This test is the inverse of the one it replaces.** `test_no_bundle_leaves_the_sdk_its_own
    _client` asserted that no bundle yields `None` — "a publicly-trusted endpoint wants the SDK's
    own default" — which is true about TLS and was the whole defect about proxies. `None` means
    the SDK builds the client, and an SDK-built httpx client carries `trust_env=True`: measured on
    that configuration with `HTTP_PROXY` naming a local recorder, the recorder received
    `POST /v1/chat/completions` with the prompt body and the gateway `Authorization` bearer, on
    both `invoke` and `ainvoke`, while `netguard._refused` stayed at 0. A guard cannot see a
    proxied call, because the destination has left the address
    (`D-2026-09-05-a-proxy-moves-the-destination-out-of-the-address`).

    So both clients are ours on every branch, and the bundle decides only *verification*. Asserted
    on the sync client as well as the async one because the sync one was never passed at all —
    `ChatOpenAI` got `http_async_client=` alone, so `invoke` went out on a client this repository
    had never seen.
    """
    from chemclaw.agent.llm_provider import _tls_http_clients

    _tls_http_clients.cache_clear()
    monkeypatch.setattr(settings, "llm_tls_ca_bundle", "")
    try:
        sync_client, async_client = _tls_http_clients()
        assert sync_client is not None and async_client is not None
        for client in (sync_client, async_client):
            assert client.trust_env is False, "a client that trusts the env follows HTTP(S)_PROXY"
            proxy_mounts = [key for key in client._mounts if key.pattern is not None]
            assert proxy_mounts == [], f"proxy mounts resolved from the environment: {proxy_mounts}"
    finally:
        _tls_http_clients.cache_clear()


def test_the_chat_model_is_handed_both_of_this_process_s_clients(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """What the model is *built with*, not what the factory returns — the gap the defect lived in.

    The factory could be perfect and the constructor still pass one of its two results, which is
    exactly what happened: `http_async_client=` alone, so every `invoke` used an SDK-built client.
    Reads the objects off the constructed `ChatOpenAI` rather than the call, so an argument dropped
    in a refactor fails here.
    """
    _openai_endpoint(monkeypatch)
    from chemclaw.agent.llm_provider import _tls_http_clients, build_chat_model

    _tls_http_clients.cache_clear()
    try:
        model = build_chat_model()
        sync_client, async_client = _tls_http_clients()
        assert model.root_client._client is sync_client
        assert model.root_async_client._client is async_client
    finally:
        _tls_http_clients.cache_clear()


def _openai_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    """Configure a primary gateway endpoint."""
    monkeypatch.setattr(settings, "llm_base_url", "https://primary.internal/v1")
    monkeypatch.setattr(settings, "llm_model", "internal-large")
    monkeypatch.setattr(settings, "llm_api_key", SecretStr("primary-key"))


def test_no_fallback_configured_returns_the_model_itself(monkeypatch: pytest.MonkeyPatch) -> None:
    """The default is off, so an existing deployment's model object does not change shape.

    Asserted because wrapping unconditionally would be the easy mistake: every caller of
    `build_chat_model` would start receiving a `RunnableWithFallbacks`, and the one that noticed
    would be whichever code path calls a `ChatOpenAI`-only attribute.
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
    """A refused *request* must not be re-sent to the standby, and this is where that is decided.

    `with_fallbacks` defaults to catching every `Exception`. Under that default a malformed request
    is rejected by the primary, sent to the standby, and rejected identically — twice the latency
    for the same answer, and a 400 laundered into something that looks like an outage. It is the
    same distinction `connectors/calc/remote.py` draws between a refused call and an unreachable
    service: a retry fixes exactly one of them.

    Asserted on the handled set rather than by driving a live failure, because what can go wrong
    here is the *configuration* of the wrapper, and that is visible directly.
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
    """The failover answer must still be able to call tools, or it is worse than no failover.

    `create_agent` binds the turn's tools to whatever `build_chat_model` returned. If that binding
    reached only the primary, an outage would produce a model that answers fluently and cannot do
    anything — a degraded mode that looks like it is working. Measured here rather than assumed,
    because "wrapping a chat model loses its tool surface" is exactly the shape of upstream
    behaviour this repository has been caught by before.
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
