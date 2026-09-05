"""The one place a chat-model class is imported — the LLM gateway seam (plan Phase F0).

`build_chat_model` builds one client, `ChatOpenAI`, against the one OpenAI-compatible gateway
`settings.llm_base_url` names. **There is no provider selection**
(`D-2026-09-04-a-gateway-is-the-only-provider`): which vendor answers behind that address is the
gateway's business, and a codebase that could not name a second vendor cannot send a prompt to one
by accident. The seam F0 asked for survives intact — pointing Chemclaw at a different endpoint is a
config change, never a code edit at a call site — it simply has one arm instead of two.

**What the second arm cost.** `_anthropic_model` passed no `base_url`, so a deployment that set
`llm_base_url` to an internal gateway *and* left the provider at its shipped default built a client
resolving to `api.anthropic.com`; `api/middleware._refuse_public_llm_exposure` returned early on
that same truthy `llm_base_url` and its docstring said the combination was satisfied; and
`core/netguard.derive_allowed` added the public host to the egress allowlist. Three controls, all
reading the same field, all agreeing that a pod exfiltrating every prompt was correctly configured.
Prompt caching went with it — `cache_control` breakpoints are Anthropic's spelling and there is no
counterpart on the gateway — which is a real, accepted cost recorded in that ADR.

The gateway is reached with **one generic API credential** (`settings.llm_api_key`), deliberately
not per-user Entra: the raw inference call is not a user-scoped resource access (see
`docs/archive/plans/foundation-plan.md` §0). Entra scoping applies to *who* is taking the turn and
*which* authorized workflow runs (Phase F4), not to this credential. Transport concerns (private-CA
TLS, timeout, retry budget) come from config so a firewalled internal endpoint works with no code
change.
"""

import logging
from functools import cache
from typing import Any

from langchain_core.callbacks import BaseCallbackHandler

from chemclaw.core.config import settings
from chemclaw.core.http import private_ca_transport
from chemclaw.core.metrics_bridge import record_metric

logger = logging.getLogger(__name__)

# A non-empty placeholder for endpoints that accept any bearer (some internal OpenAI-compatible
# servers ignore the key): the OpenAI SDK refuses to construct with an empty api_key, so a keyless
# internal endpoint still needs a stand-in. A real generic credential (`llm_api_key`) overrides it.
_KEYLESS_PLACEHOLDER = "not-required"


def build_chat_model(task: str = "agent", *, effort: str | None = None) -> Any:
    """Build the gateway chat model — the whole of this seam.

    Everything that is actually about *the endpoint* is decided here and only here: which address,
    which credential, the per-task model route, and the transport (private-CA TLS, timeout, retry
    budget). That is the property F0 asked for — pointing Chemclaw at a different endpoint is a
    config change, not a code edit — and it is why every caller comes through this entry point
    rather than constructing a client.

    Returns `Any` rather than `BaseChatModel` so this module stays the only one naming a provider
    class: a return annotation would put `langchain_core` in every caller's import graph for a type
    none of them narrows.

    Args:
        task: The routing key for per-task model selection (F10-E).
        effort: How hard to ask the model to think, overriding `llm_effort` for this build.
            `None` takes the deployment's setting — which is itself usually `None`, meaning the
            parameter is absent from the request. Keyword-only, because `build_chat_model("agent",
            "high")` reads as a second routing key and this is not one.

    Returns:
        A LangChain `BaseChatModel` ready for `create_agent(model=...)`. Construction only, no
        network call.
    """
    model = settings.model_routes.get(task)
    # Resolved here rather than inside `_generation_options`, so that both the primary and the
    # failover instance below are built from the same answer. A profile that narrows effort must
    # narrow the fallback endpoint too, or a degraded turn would quietly think harder than the
    # profile asked for.
    chosen = effort if effort is not None else settings.llm_effort
    primary = _openai_compatible_model(model, effort=chosen)
    return _with_failover(primary, model, effort=chosen)


def _with_failover(primary: Any, model: str | None, *, effort: str | None = None) -> Any:
    """`primary`, or a runnable that tries a second endpoint when the first one is *down* (AG-12).

    Returns `primary` unchanged when no fallback is configured, which is the default — so this is
    inert until an operator names a second endpoint, and no existing deployment changes shape.

    **Only transport failures fail over, and that is the whole design.** `with_fallbacks` defaults
    to catching every `Exception`, which would send a malformed request to the second endpoint to
    be rejected identically — twice the latency for the same answer, and a 400 laundered into
    something that looks like an outage. The distinction is the one `connectors/calc/remote.py`
    already draws between a refused request and an unreachable service: a retry fixes exactly one
    of them. So the handled set is connection, timeout and 5xx, and a `BadRequestError` or a 401
    still fails immediately on the endpoint that produced it.

    **`bind_tools` survives this, measured rather than assumed** — the obvious worry is that
    wrapping a chat model in a `RunnableWithFallbacks` loses the tool surface `create_agent`
    binds. It does not: `bind_tools` on the wrapper returns a `RunnableWithFallbacks` whose
    primary *and* fallback both carry the tools, so the failover answer can still call them. A
    fallback that answered without tools would be worse than no fallback, because it would look
    like a working degraded mode while quietly being unable to do the work.
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

    **`RunnableWithFallbacks` tells nobody.** Its `ainvoke` catches the primary's exception and
    moves to the next runnable with no log line, no metric and no callback for the attempt that
    failed. So the primary internal endpoint dying and the fallback silently absorbing 100% of the
    fleet's traffic looked exactly like a healthy deployment — for a feature whose entire
    operational value is knowing whether it has fired.

    Nothing can be hooked on the *failure*, so this hooks the consequence instead: the fallback
    model is constructed with this handler attached, and the fallback model is invoked only after
    the primary raised. One `on_chat_model_start` therefore *is* one failover, exactly, with no
    inference. It rides on the model instance rather than on a run config because `bind_tools`
    rebuilds the wrapper and keeps the constructor's callbacks — which is what makes it survive
    `create_agent`'s binding.

    Beside the counter is a WARNING, because the two audiences differ: the series answers "how much
    of today ran on the fallback", and the line is what someone reading logs at 03:00 needs in order
    to stop looking at the fallback endpoint for the cause of the outage.
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

    Imported lazily and by name so this module stays the only one that knows a provider SDK exists.
    `APIConnectionError` covers `APITimeoutError` (its subclass) and every DNS/TLS/refused-socket
    case; `InternalServerError` is the 5xx family. Everything else — 400, 401, 404, 422 — is about
    the request and is left to fail where it was made.

    Explicit `from openai import …` rather than a lookup by name, deliberately: a rename upstream
    must fail loudly here, because the silent alternative is a failover that quietly handles nothing
    while the deployment still believes it has one. `classify_model_failure` reuses this set as its
    `transport` family for exactly that reason — the taxonomy is stated once.
    """
    from openai import APIConnectionError, InternalServerError

    return (APIConnectionError, InternalServerError)


# What an endpoint says when the request was fine and the *thread* was too long. Matched on the
# message because no SDK exposes it as a distinct class: it arrives as an ordinary
# `BadRequestError`, which is how it came to be classified `("internal", False)` by
# `api/runner._classify` — "internal error, do not retry" told to a chemist about the one failure
# mode `agent/compaction.py` exists to prevent, and the one that a shorter question fixes.
#
# **`prompt is too long` is Anthropic's wording and it stays, with the SDK gone.** A gateway is a
# proxy: when the vendor behind it refuses a thread, the vendor's own sentence is what arrives in
# the 400 body, so the spellings this must recognise are the *vendors'* rather than the client
# library's. Dropping it because no `anthropic` package is installed would confuse whose string
# this is — and would silently reclassify the one failure mode compaction exists to prevent, on
# every deployment whose gateway fronts that vendor.
#
# Substrings rather than a setting, because these are somebody else's wording rather than a
# threshold: a deployment cannot tune what its gateway relays. An unrecognised phrasing falls
# through to `error`, which is the honest degradation — it is not counted as something it might not
# be. `tests/test_agent_observability_model.py` pins the live spellings.
_CONTEXT_LENGTH_MARKERS: tuple[str, ...] = (
    "context_length_exceeded",
    "maximum context length",
    "context window",
    "prompt is too long",
)


@cache
def _openai_exceptions(*names: str) -> tuple[type[BaseException], ...]:
    """The named exception classes from the OpenAI SDK, skipping any this version does not define.

    Tolerant where `_failover_exceptions` is strict, and the asymmetry is the point: that function
    configures a *control* (which failures fail over), so a missing name must break the build. This
    one feeds a *label*, and a classifier that raised would replace the failure it was called to
    describe with an `AttributeError` about its own lookup — at the moment a model call has already
    failed. A renamed class degrades that call's outcome to `error`, which is what
    `tests/test_agent_observability_model.py` drives.

    There is no `try: import` here, and there used to be: this took a module name and asked both
    provider SDKs, because a process might be configured for either. One gateway means one client
    library, and `openai` is a hard dependency of `langchain-openai` — so the import-guard branch
    was unreachable, wore a `# pragma: no cover`, and would have become live code the moment the
    second SDK was uninstalled. Removing the second caller is what removed the pragma.
    """
    import openai

    found = (getattr(openai, name, None) for name in names)
    return tuple(k for k in found if isinstance(k, type) and issubclass(k, BaseException))


@cache
def _failure_families() -> tuple[tuple[str, tuple[type[BaseException], ...]], ...]:
    """The gateway client's failure taxonomy, most specific first.

    **The taxonomy was already known and simply not recorded.** `_failover_exceptions` proved it:
    this seam has always distinguished "the endpoint is down" from "the request is wrong", because
    failover depends on the difference. Nothing else did — no metric, no log, no span named a
    provider failure at all — so a 429, a dead endpoint and a context-length overflow were the same
    invisible event.

    Order is the classification. `APITimeoutError` subclasses `APIConnectionError` in both SDKs, so
    a linear scan that tested transport first would report every timeout as `transport`; the same
    ordering argument `agent/audit._refusal_types` makes for the refusals.

    Cached because the tuple is fixed for the life of the process and this is only ever reached
    from a failed model call.
    """
    return (
        ("timeout", _openai_exceptions("APITimeoutError")),
        ("rate_limited", _openai_exceptions("RateLimitError")),
        # The failover set *is* the transport family — the same sentence read for a different
        # purpose, which is why it is imported rather than restated.
        ("transport", _failover_exceptions()),
    )


def classify_model_failure(exc: BaseException) -> str:
    """What kind of provider failure this is: the outcome label a model call is counted under.

    One of `rate_limited`, `context_length`, `timeout`, `transport` or `error` — the label space
    `chemclaw_model_calls_total` declares beside `ok`. Anything unrecognised is `error` rather than
    a guess, because the point of the series is that a deployment can tell these apart, and a
    mislabelled 401 would put an operator on the wrong runbook.

    `context_length` is tested first: it arrives as a `BadRequestError`, so any test of the request
    families would have to run after it anyway, and it is the one label with a specific remedy —
    the thread is too long, and `agent/compaction.py` is the mechanism that is supposed to prevent
    it. It could not be counted at all before this, which is to say the failure mode compaction
    exists for was the one nobody could measure.
    """
    if _is_context_length(exc):
        return "context_length"
    for label, kinds in _failure_families():
        if kinds and isinstance(exc, kinds):
            return label
    return "error"


def _is_context_length(exc: BaseException) -> bool:
    """Whether this is the provider refusing a thread that no longer fits.

    A `BadRequestError` first, so a message that merely quotes a context-length phrase — a chemist
    asking about context windows, echoed back in some other error — cannot be classified by its
    words alone. `code` is read where it is set (OpenAI's `context_length_exceeded`); a gateway
    relaying a vendor that sets none leaves the message as all there is.
    """
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

    `http_async_client` is where the private-CA bundle goes: `ChatOpenAI` builds its own
    `AsyncOpenAI` internally, so the bundle has to be handed in as a client rather than set on one
    we construct. Passing `None` (no configured bundle) leaves the SDK's own default in place,
    which is right for a publicly-trusted endpoint.

    **`stream_usage` is passed explicitly, and leaving it to the default metered every turn at
    zero.** `ChatOpenAI` default-enables it only when *all* of `stream_usage`, `openai_proxy`, the
    four client fields, `http_client` and `http_async_client` are unset **and** no custom base URL
    is configured — upstream turns it off otherwise, on the stated grounds that "many non-OpenAI
    endpoints do not support streaming token usage". This function trips that check twice over: it
    sets a base URL *and* hands in a client for the CA bundle. So the endpoint was never asked to
    report usage, no usage chunk ever arrived, and `runner_usage.graph_usage_tokens` correctly read
    nothing from it.

    Measured against the local mock: 15 turns on the graph engine wrote `turn_costs` rows totalling
    **0** tokens while the same traffic on the other engine wrote 2,040 per session. That is the
    failure `usage_tokens`'s own docstring records — 50 turns of 15,000 real tokens booked as zero
    while the budget guard went on allowing the next one — reached by a different route. A
    runaway-cost guard that meters zero is not conservative, it is disarmed.

    It is a *setting* rather than a hardcoded `True` because upstream's caution is about real
    endpoints: an OpenAI-compatible server that rejects `stream_options` needs a way out that is not
    a code change. The default is on, because metering silently at zero is the worse failure.
    """
    from langchain_openai import ChatOpenAI
    from pydantic import SecretStr

    # The fallback endpoint reuses the primary's model and credential unless it names its own:
    # the common case is a second replica of one internal deployment, not a different vendor.
    base_url = settings.llm_fallback_base_url if fallback else settings.llm_base_url
    chosen = model or (settings.llm_fallback_model if fallback else "") or settings.llm_model
    # Unwrapped here and nowhere earlier: both settings are `SecretStr`, so `or` on them would
    # compare wrappers (always truthy) rather than the keys inside.
    fallback_key = settings.llm_fallback_api_key.get_secret_value() if fallback else ""
    key = fallback_key or settings.llm_api_key.get_secret_value()

    return ChatOpenAI(
        model=chosen,
        base_url=base_url,
        # Only the fallback instance gets one, and that asymmetry is the whole signal: this
        # endpoint is asked only after the primary raised (`_FallbackObserved`).
        callbacks=[observer] if observer is not None else None,
        # `ChatOpenAI` takes the credential as a `SecretStr`, which keeps the key out of a repr
        # and out of any log line that prints the model object — the same guarantee `Settings` now
        # makes, so the value is a `SecretStr` on both sides of this call and plain only between.
        api_key=SecretStr(key or _KEYLESS_PLACEHOLDER),
        timeout=settings.llm_timeout_seconds,
        max_retries=settings.llm_max_retries,
        http_async_client=_tls_http_client(),
        stream_usage=settings.llm_stream_usage,
        **_generation_options(effort),
    )


def _generation_options(effort: str | None = None) -> dict[str, Any]:
    """The deployment's generation caps, as constructor kwargs both providers accept.

    **Every model call gets them, and losing them once was silent in the expensive direction.**
    They went missing in the LangGraph rebuild — the agent builder that used to thread them was
    deleted and neither replacement passed them on — and with `llm_max_tokens=4096` configured the
    client resolved to its library's own default, measured at 128000. A deployment that had bounded
    its worst-case answer no longer had.

    `temperature` is omitted rather than sent as `None` when unset. That is the rule
    `core/config/llm.py` records having broken every turn once: some OpenAI-compatible endpoints
    reject an explicit null, so "unset" has to mean *absent from the request*, not present-and-null.

    **`reasoning_effort` is a plain pass-through, and it took two guards to get here.** With two
    clients it was scoped to one of them: `langchain-anthropic` folded the same kwarg into
    `output_config.effort` and injected `thinking={'type': 'adaptive'}`, i.e. extended thinking,
    with the `temperature` conflict and the `max_tokens` draw that implies — so a setting and a
    profile field that read as one knob were two parameters wearing one name. Both guards went with
    the second client. The lesson survives them and is the general one: an attribute that
    round-trips on a client proves the constructor accepted a kwarg, and nothing at all about what
    reaches the wire, which is why `tests/test_llm_effort.py` reads the request payload.

    The same absent-when-unset rule, and it binds harder here: a rejected parameter is a 400, and
    `_failover_exceptions` deliberately does not fail those over.

    Args:
        effort: The resolved reasoning effort, or `None` to send none. Resolved by the caller —
            a profile's answer beats the deployment's, and this function is not where that is
            decided.

    Returns:
        The constructor kwargs shared by both providers.
    """
    options: dict[str, Any] = {"max_tokens": settings.llm_max_tokens}
    if settings.llm_temperature is not None:
        options["temperature"] = settings.llm_temperature
    if effort is not None:
        options["reasoning_effort"] = effort
    return options


@cache
def _tls_http_client() -> Any | None:
    """An httpx client pinned to the internal CA when one is configured, else None (system store).

    Returning None lets the OpenAI SDK build its own default client — the right behavior for a
    publicly-trusted endpoint; only a private-CA internal endpoint needs the explicit bundle.

    **Cached, because this is per *process*, not per turn.** `build_chat_model` runs on every graph
    build and a graph is compiled per turn (M7), so an uncached factory built a fresh
    `AsyncClient` — a fresh connection pool, a fresh TLS context — for every question asked, and
    nothing ever closed one: the sockets waited on the garbage collector. That is on the
    `openai_compatible` + private-CA path, which is the documented production target. It is also
    the cost `agent/verifier.py::_default_client` already pays `@cache` to avoid, on a colder path
    than this one; the main agent's client was the only one building per turn. That sentence named
    `agent/challenge._default_client` beside it until 2026-08-27 — a module
    `D-2026-08-15-a-capability-that-ships-off-is-not-a-capability` deleted, so the "two callers
    already do this" argument rested on one caller and a ghost. It rests on one caller and a
    measurement now: an uncached factory built a fresh `AsyncClient` per turn, which is the reason
    here regardless of how many other modules do the same.

    Process-scoped, so the pool binds to the first loop that uses it. Production runs one loop.
    """
    transport = private_ca_transport(settings.llm_tls_ca_bundle)
    if transport is None:
        return None
    import httpx

    # What pins the CA and refuses an ambient proxy is `core/http.private_ca_transport`, stated
    # once for both LLM seams: the embedding client reaches the same gateway with the same bundle
    # and had written the same two lines. Only the class differs, and only the caller knows it.
    return httpx.AsyncClient(**transport)
