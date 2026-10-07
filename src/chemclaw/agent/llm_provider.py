"""The one place a chat-model class is imported — the LLM gateway seam.

`build_chat_model` builds one client, `ChatOpenAI`, against the OpenAI-compatible gateway
`settings.llm_base_url` names. There is no provider selection
(D-2026-09-04-a-gateway-is-the-only-provider): which vendor answers is the gateway's business, and
pointing Chemclaw elsewhere is a config change. The gateway is reached with one generic credential
(`settings.llm_api_key`), not per-user Entra, because an inference call is not a user-scoped
resource access. Transport (private-CA TLS, timeout, retries) comes from config.
"""

import logging
from functools import cache
from typing import Any

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.exceptions import ContextOverflowError

from chemclaw.core.config import settings
from chemclaw.core.http import gateway_client_kwargs
from chemclaw.core.metrics_bridge import record_metric

logger = logging.getLogger(__name__)

# Placeholder for keyless internal endpoints: the OpenAI SDK refuses an empty api_key.
_KEYLESS_PLACEHOLDER = "not-required"


def build_chat_model(task: str = "agent", *, effort: str | None = None) -> Any:
    """Build the gateway chat model — the whole of this seam.

    Address, credential, per-task model route and transport are decided here and only here, so every
    caller comes through this entry point. Returns `Any` so no caller imports a provider type.

    Args:
        task: The routing key for per-task model selection.
        effort: Reasoning effort overriding `llm_effort` for this build; `None` takes the
            deployment's setting (usually absent from the request).

    Returns:
        A LangChain `BaseChatModel` ready for `create_agent(model=...)`. Construction only, no
        network call.
    """
    model = settings.model_routes.get(task)
    # Resolved once so the primary and the failover instance get the same effort.
    chosen = effort if effort is not None else settings.llm_effort
    primary = _openai_compatible_model(model, effort=chosen)
    return _with_failover(primary, model, effort=chosen)


def _with_failover(primary: Any, model: str | None, *, effort: str | None = None) -> Any:
    """`primary`, or a runnable that tries a second endpoint when the first one is *down*.

    Returns `primary` unchanged when no fallback is configured (the default). Only transport
    failures (connection, timeout, 5xx) fail over; a malformed request or 401 would be rejected
    identically by the second endpoint, so it fails where it was made. `bind_tools` on the wrapper
    binds tools on both primary and fallback.
    """
    if not settings.llm_fallback_base_url:
        return primary
    return primary.with_fallbacks(
        [
            _openai_compatible_model(
                model, fallback=True, observer=_FallbackObserved(), effort=effort
            )
        ],
        exceptions_to_handle=_failover_exceptions(),
    )


class _FallbackObserved(BaseCallbackHandler):
    """Notice that the *fallback* endpoint was asked — the only observable this failover has.

    `RunnableWithFallbacks` reports nothing when the primary fails, so this handler is attached to
    the fallback model: one `on_chat_model_start` is exactly one failover. It rides on the model
    instance because `bind_tools` keeps constructor callbacks. It increments a counter and logs a
    WARNING.
    """

    def on_chat_model_start(self, serialized: Any, messages: Any, **kwargs: Any) -> None:
        """Count and log one failover; never touch the call itself."""
        record_metric(lambda metrics: metrics.increment("chemclaw_model_fallbacks_total"))
        logger.warning(
            "model failover: the primary gateway did not answer, so this call was served by the "
            "configured fallback endpoint"
        )


def _failover_exceptions() -> tuple[type[BaseException], ...]:
    """The failures that mean *this endpoint is down* rather than *this request is wrong*.

    `APIConnectionError` (including `APITimeoutError`) and `InternalServerError` (5xx). Imported
    explicitly so an upstream rename fails loudly instead of silently disabling failover;
    `classify_model_failure` reuses this set as its `transport` family.
    """
    from openai import APIConnectionError, InternalServerError

    return (APIConnectionError, InternalServerError)


# What an endpoint says when the thread is too long. Matched on the message because it arrives as an
# ordinary `BadRequestError`. The spellings are the vendors' (a gateway relays the vendor's
# sentence), so Anthropic's `prompt is too long` stays. An unrecognised phrasing falls through to
# `error`; `tests/test_agent_observability_model.py` pins the live spellings.
_CONTEXT_LENGTH_MARKERS: tuple[str, ...] = (
    "context_length_exceeded",
    "maximum context length",
    "context window",
    "prompt is too long",
)


@cache
def _openai_exceptions(*names: str) -> tuple[type[BaseException], ...]:
    """The named exception classes from the OpenAI SDK, skipping any this version does not define.

    Tolerant, unlike `_failover_exceptions`, because this feeds a label: a classifier that raised
    would replace the model failure it describes. A renamed class degrades to `error`.
    """
    import openai

    found = (getattr(openai, name, None) for name in names)
    return tuple(k for k in found if isinstance(k, type) and issubclass(k, BaseException))


@cache
def _failure_families() -> tuple[tuple[str, tuple[type[BaseException], ...]], ...]:
    """The gateway client's failure taxonomy, most specific first.

    Order is the classification: `APITimeoutError` subclasses `APIConnectionError`, so timeout is
    tested before transport. Cached; fixed for the life of the process.
    """
    return (
        ("timeout", _openai_exceptions("APITimeoutError")),
        ("rate_limited", _openai_exceptions("RateLimitError")),
        # The gateway refused this deployment's credential (401/403); the remedy is an operator's
        # (`CHEMCLAW_LLM_API_KEY`), not a retry.
        ("auth", _openai_exceptions("AuthenticationError", "PermissionDeniedError")),
        # The failover set *is* the transport family — the same sentence read for a different
        # purpose, which is why it is imported rather than restated.
        ("transport", _failover_exceptions()),
    )


def classify_model_failure(exc: BaseException) -> str:
    """What kind of provider failure this is: the outcome label a model call is counted under.

    One of `rate_limited`, `context_length`, `timeout`, `transport`, `auth` or `error` (the label
    space of `chemclaw_model_calls_total` besides `ok`); unrecognised is `error`, not a guess.
    `context_length` is tested first because it arrives as a `BadRequestError` and has a specific
    remedy (`agent/compaction.py`).
    """
    if _is_context_length(exc):
        return "context_length"
    for label, kinds in _failure_families():
        if kinds and isinstance(exc, kinds):
            return label
    return "error"


def _is_context_length(exc: BaseException) -> bool:
    """Whether this is the provider refusing a thread that no longer fits.

    `ContextOverflowError` (raised by `langchain_openai` on both the plain and streamed paths) is
    read first. Otherwise only a `BadRequestError` qualifies, by `code` (`context_length_exceeded`)
    or by message, so an unrelated error quoting the phrase is not misread.
    """
    if isinstance(exc, ContextOverflowError):
        return True
    if not isinstance(exc, _openai_exceptions("BadRequestError")):
        return False
    text = f"{getattr(exc, 'code', '') or ''} {exc}".lower()
    return any(marker in text for marker in _CONTEXT_LENGTH_MARKERS)


def _openai_compatible_model(
    model: str | None = None,
    *,
    fallback: bool = False,
    observer: Any = None,
    effort: str | None = None,
) -> Any:
    """`ChatOpenAI` against the internal endpoint — same base URL, credential and transport.

    The private-CA bundle goes in through the httpx clients, since `ChatOpenAI` builds its own SDK
    client. `stream_usage` is passed explicitly: upstream disables it whenever a base URL or client
    is set, and without usage chunks every turn is metered at zero tokens and the spend cap is
    disarmed. It is a setting so an endpoint rejecting `stream_options` has a way out; it defaults
    on.
    """
    from langchain_openai import ChatOpenAI
    from pydantic import SecretStr

    # The fallback endpoint reuses the primary's model and credential unless it names its own: the
    # common case is a second replica of one internal deployment, not a different vendor.
    base_url = settings.llm_fallback_base_url if fallback else settings.llm_base_url
    chosen = model or (settings.llm_fallback_model if fallback else "") or settings.llm_model
    # Unwrapped here and nowhere earlier: both settings are `SecretStr`, so `or` on them would
    # compare wrappers (always truthy) rather than the keys inside.
    fallback_key = settings.llm_fallback_api_key.get_secret_value() if fallback else ""
    key = fallback_key or settings.llm_api_key.get_secret_value()

    return ChatOpenAI(
        model=chosen,
        base_url=base_url,
        # Only the fallback instance gets one, and that asymmetry is the whole signal: this endpoint
        # is asked only after the primary raised (`_FallbackObserved`).
        callbacks=[observer] if observer is not None else None,
        # A `SecretStr` keeps the key out of reprs and log lines.
        api_key=SecretStr(key or _KEYLESS_PLACEHOLDER),
        timeout=settings.llm_timeout_seconds,
        max_retries=settings.llm_max_retries,
        http_client=_tls_http_clients()[0],
        http_async_client=_tls_http_clients()[1],
        stream_usage=settings.llm_stream_usage,
        **_generation_options(effort),
    )


def _generation_options(effort: str | None = None) -> dict[str, Any]:
    """The deployment's generation caps, as `ChatOpenAI` constructor kwargs.

    Every model call gets them; without them the client falls back to its library default token
    limit. `temperature` and `reasoning_effort` are omitted rather than sent as `None` when unset,
    because some endpoints reject an explicit null and a 400 is not failed over.
    `tests/test_llm_effort.py` reads the request payload, since an attribute on the client proves
    nothing about the wire.

    Args:
        effort: The resolved reasoning effort, or `None` to send none; resolved by the caller.

    Returns:
        The constructor kwargs.
    """
    options: dict[str, Any] = {"max_tokens": settings.llm_max_tokens}
    if settings.llm_temperature is not None:
        options["temperature"] = settings.llm_temperature
    if effort is not None:
        options["reasoning_effort"] = effort
    return options


@cache
def _tls_http_clients() -> tuple[Any, Any]:
    """The sync and async httpx clients every chat call goes out on. Both, always.

    A client this process does not build trusts the environment: httpx's default `trust_env=True`
    would send prompts and the gateway bearer through an ambient `HTTP_PROXY`, past `netguard`. So
    both clients are built here with `trust_env=False`, CA bundle or not. Cached per process (not
    per turn) so each turn does not open a new connection pool; the async pool binds to the first
    loop that uses it.

    Returns:
        `(sync_client, async_client)`, in the order `ChatOpenAI` takes them.
    """
    import httpx

    # CA pinning and refusing an ambient proxy are `core/http.gateway_client_kwargs`, shared with
    # the embedding client.
    kwargs = gateway_client_kwargs(settings.llm_tls_ca_bundle)
    return httpx.Client(**kwargs), httpx.AsyncClient(**kwargs)
