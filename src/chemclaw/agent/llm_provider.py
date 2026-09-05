"""The one place a chat-model class is imported — the LLM provider seam (plan Phase F0).

`build_chat_model` selects the model from config (`settings.llm_provider`), so pointing Chemclaw at
the internal OpenAI-compatible ("OpenLLM-like") endpoint versus the Anthropic dev path is a single
config change, never a code edit at a call site (KISS/DRY, mirroring the ELN adapter registry).
Provider classes are imported **only here** — `agent/langgraph_agent.py` calls this factory and
stays provider-agnostic. `prompt_caching_middleware` is here for the same reason and not because
it is a model: a cache breakpoint is spelled `cache_control` on Anthropic and has no counterpart on
the internal endpoint, so *which* middleware a deployment gets is a provider question, and this is
where provider questions are answered.

The internal endpoint is reached with **one generic API credential** (`settings.llm_api_key`),
deliberately not per-user Entra: the raw inference call is not a user-scoped resource access (see
`docs/archive/plans/foundation-plan.md` §0). Entra scoping applies to *who* is taking the turn and
*which* authorized workflow runs (Phase F4), not to this credential. Transport concerns (private-CA
TLS, timeout, retry budget) come from config so a firewalled internal endpoint works with no code
change.
"""

import logging
from functools import cache
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain_core.callbacks import BaseCallbackHandler

from chemclaw.core.config import settings
from chemclaw.core.metrics_bridge import record_metric

logger = logging.getLogger(__name__)

# A non-empty placeholder for endpoints that accept any bearer (some internal OpenAI-compatible
# servers ignore the key): the OpenAI SDK refuses to construct with an empty api_key, so a keyless
# internal endpoint still needs a stand-in. A real generic credential (`llm_api_key`) overrides it.
_KEYLESS_PLACEHOLDER = "not-required"


def build_chat_model(task: str = "agent", *, effort: str | None = None) -> Any:
    """Build the configured LangChain chat model — the whole of this seam.

    Everything that is actually about *the provider* is decided here and only here: which endpoint,
    which credential, the per-task model route, and the transport (private-CA TLS, timeout, retry
    budget). That is the property F0 asked for — pointing Chemclaw at the internal endpoint is a
    config change, not a code edit — and it is why the two provider branches below share this
    entry point rather than being reached directly.

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

    Raises:
        RuntimeError: When the selected provider's credential is absent, naming what to set.
    """
    model = settings.model_routes.get(task)
    # Resolved here rather than inside `_generation_options`, so that both provider branches and
    # the failover instance below are built from the same answer. A profile that narrows effort
    # must narrow the fallback endpoint too, or a degraded turn would quietly think harder than
    # the profile asked for.
    chosen = effort if effort is not None else settings.llm_effort
    # **The gate is here, at the seam, and putting it only in config was not enough.**
    # `LlmSettings._effort_is_provider_scoped` refuses `llm_effort` on the Anthropic path at
    # startup, which covers the *deployment* knob and nothing else: `AgentProfile.effort` is a
    # different input, reaching this function as the `effort` argument without passing through any
    # settings validator. Measured on the shipped code default (`llm_provider="anthropic"`,
    # `llm_effort=None`), a profile carrying `effort: high` produced exactly the payload the
    # validator exists to prevent — `output_config={'effort': 'high'}` plus
    # `thinking={'type': 'adaptive'}`.
    #
    # So the check belongs where every path converges rather than on one of them. This is the only
    # place that resolves the two inputs into one answer, and every client below is built from it.
    #
    # Raised rather than dropped. Dropping would leave a profile that says `effort: high` quietly
    # getting default effort — a control that reads as one and is not, which this repository has a
    # standing rule against and `agent/spend_cap.py` has its own scar from.
    if chosen is not None and settings.llm_provider == "anthropic":
        raise RuntimeError(
            f"agent effort {chosen!r} was requested on llm_provider='anthropic', where "
            "reasoning_effort enables extended thinking rather than setting an effort level "
            "(measured: it adds thinking={'type': 'adaptive'}, which conflicts with "
            "llm_temperature and draws from llm_max_tokens). Remove `effort:` from the profile, "
            "or run against llm_provider='openai_compatible'."
        )
    if settings.llm_provider == "openai_compatible":
        primary = _openai_compatible_model(model, effort=chosen)
        return _with_failover(primary, model, effort=chosen)
    return _anthropic_model(model, effort=chosen)


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
        provider = settings.llm_provider
        record_metric(
            lambda metrics: metrics.increment(
                "chemclaw_model_fallbacks_total", labels={"provider": provider}
            )
        )
        logger.warning(
            "model failover: the primary %s endpoint did not answer, so this call was served by "
            "the configured fallback endpoint",
            provider,
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


# What a provider says when the request was fine and the *thread* was too long, in the two SDKs
# this seam speaks to. Matched on the message because neither exposes it as a distinct class: it
# arrives as an ordinary `BadRequestError`, which is how it came to be classified `("internal",
# False)` by `api/runner._classify` — "internal error, do not retry" told to a chemist about the one
# failure mode `agent/compaction.py` exists to prevent, and the one that a shorter question fixes.
#
# Substrings rather than a setting, because these are somebody else's wording rather than a
# threshold: a deployment cannot tune what its provider writes. An unrecognised phrasing falls
# through to `error`, which is the honest degradation — it is not counted as something it might not
# be. `tests/test_agent_observability_model.py` pins the two live spellings.
_CONTEXT_LENGTH_MARKERS: tuple[str, ...] = (
    "context_length_exceeded",
    "maximum context length",
    "context window",
    "prompt is too long",
)


@cache
def _sdk_exceptions(module_name: str, *names: str) -> tuple[type[BaseException], ...]:
    """The named exception classes from a provider SDK, or nothing when it is not installed.

    Tolerant where `_failover_exceptions` is strict, and the asymmetry is the point: that function
    configures a *control* (which failures fail over), so a missing name must break the build. This
    one feeds a *label*, and a classifier that raised would replace the failure it was called to
    describe. Both SDKs are asked because a process may be configured for either and each is an
    optional dependency of the other's deployment.
    """
    import importlib

    try:
        module = importlib.import_module(module_name)
    except ImportError:  # pragma: no cover - both SDKs are installed in this workspace
        return ()
    found = (getattr(module, name, None) for name in names)
    return tuple(k for k in found if isinstance(k, type) and issubclass(k, BaseException))


@cache
def _failure_families() -> tuple[tuple[str, tuple[type[BaseException], ...]], ...]:
    """The provider SDKs' failure taxonomy, most specific first — one table, both providers.

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
        (
            "timeout",
            _sdk_exceptions("openai", "APITimeoutError")
            + _sdk_exceptions("anthropic", "APITimeoutError"),
        ),
        (
            "rate_limited",
            _sdk_exceptions("openai", "RateLimitError")
            + _sdk_exceptions("anthropic", "RateLimitError"),
        ),
        # The failover set *is* the transport family — the same sentence read for a different
        # purpose — plus the Anthropic SDK's twins, which have no failover to configure.
        (
            "transport",
            _failover_exceptions()
            + _sdk_exceptions("anthropic", "APIConnectionError", "InternalServerError"),
        ),
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
    words alone. `code` is read where the SDK sets one (OpenAI's `context_length_exceeded`);
    Anthropic sets none, so the message is all there is.
    """
    if not isinstance(
        exc,
        _sdk_exceptions("openai", "BadRequestError")
        + _sdk_exceptions("anthropic", "BadRequestError"),
    ):
        return False
    text = f"{getattr(exc, 'code', '') or ''} {exc}".lower()
    return any(marker in text for marker in _CONTEXT_LENGTH_MARKERS)


class _CachingDisabled(AgentMiddleware):
    """A middleware that does nothing, under the name of the one it keeps out.

    `AnthropicPromptCachingMiddleware` has no "off" constructor argument — `min_messages_to_cache`
    gates only the message-tail breakpoint, and the ones that actually appeared in the payload were
    on the system prompt and the tool definitions. So refusing it means occupying its slot rather
    than configuring it, and a bare `AgentMiddleware` overrides no hook and registers no tool: it is
    inert by construction rather than by a body that happens to be empty.

    Named for what it displaces rather than for itself: `_apply_custom_middleware` matches on
    `.name`, so a placeholder under its own class name would land *beside* upstream's and change
    nothing. `tests/test_prompt_caching.py` asserts the payload, which is the only place the
    difference is visible.
    """

    @property
    def name(self) -> str:
        """Upstream's name, so this replaces its middleware instead of joining it."""
        return "AnthropicPromptCachingMiddleware"


def prompt_caching_middleware() -> list[Any]:
    """The provider's prompt-caching middleware, or nothing — the second thing this seam decides.

    **It lives here rather than beside the middleware chain because caching is provider-specific,
    which is the same reason the chat-model class lives here.** Anthropic marks a cacheable prefix
    with `cache_control: {"type": "ephemeral"}` breakpoints; the internal OpenAI-compatible endpoint
    has no such parameter and would be handed a kwarg it does not understand. `langgraph_agent`
    splices whatever this returns and stays provider-agnostic, exactly as it does for the model.
    Keeping the `langchain_anthropic` import inside this function is also what
    `tests/test_third_party_layering.py` allows: `("chemclaw.agent", "llm")` is a *function-scope*
    row, so a module-level import here would fail the layering gate.

    **What gets marked, and why that is the whole win.** Upstream's middleware sets three
    breakpoints: the last block of the system prompt, the last tool definition, and — via a
    top-level `cache_control` on the request — the message tail. The Anthropic wire format renders
    `tools` → `system` → `messages`, so a breakpoint on the system prompt caches the tool schemas
    with it, and those two are the part that is byte-identical for the life of a profile. The
    conversation tail is not static, but the incremental breakpoint on it means each call reads the
    prefix the previous call wrote, which is what makes a long tool loop cheap rather than only the
    first hop of it. Four breakpoints is the API's limit; three is what this uses.

    **Below the minimum cacheable prefix it degrades silently, and two shipped profiles are below
    it.** The sentence that stood here said the minimum was "roughly 1,024 tokens" and the ADR
    beside it said every prefix was "far above every model's minimum". Both came from the spec;
    neither had been run. Measured on 2026-08-12 by bisecting the prefix to ±1 token and reading
    `cache_creation_input_tokens`:

    - `claude-sonnet-5` — **1,024**. The spec number, for the model `agent_model` defaults to.
    - `claude-haiku-4-5` — **4,096**. Four times that, for the model the live probe lane pins.

    So the floor is per-model and **not ordered by model size** — the smaller, cheaper model has the
    higher one, which is the shape "not monotonic" was gesturing at and the direction that makes it
    a trap. Against those floors the shipped prefixes (tools + system, which is what the breakpoint
    covers) are: `default` 21,321 · `computation` 8,708 · `reporting` 7,490 · `evidence` 5,803 ·
    `design` 5,625 · `property-lookup` 3,092 · `safety` 2,933. The last two are below haiku's floor
    and above sonnet's, so **whether a narrow profile caches is decided by `model_routes`, not by
    the profile** — and on haiku those two pay full price on every call, confirmed by sending each
    profile's real payload twice and getting `cache_read` and `cache_creation` of zero both times
    while `computation` wrote 8,734 and read all 8,734 back.

    Under the floor the breakpoint is accepted, no entry is created, both counters come back zero,
    and the request is answered normally at full price. There is still no error to handle, and
    still nothing here counts tokens to pre-empt one — that part of the original reasoning holds
    and is why the numbers above live in a docstring and a test rather than in an `if`: a threshold
    copied into the code would be a second, staler statement of a number only the provider knows,
    and this one moved by 4× between two models of the same generation. What makes the difference
    *visible* rather than assumed is the ledger — `api/runner_usage.graph_usage_tokens` reads
    `cache_read`/`cache_creation` off every chunk and `turn_costs` stores both columns — and, per
    profile, `chemclaw_cache_write_tokens_total{profile=...}`: a profile with input tokens and no
    cache series is one below the floor, which is the reading an operator can act on.

    **Two gates, because they answer different questions.** `settings.llm_provider` decides whether
    the *deployment* is on Anthropic at all, so the production `openai_compatible` target gets an
    empty list and never imports `langchain_anthropic` — the guarantee is structural rather than
    resting on somebody else's isinstance check. `unsupported_model_behavior="ignore"` covers the
    other case: an Anthropic-configured deployment handed an injected model (every test that passes
    `build_langgraph_agent(model=fake)`), where upstream's default would emit a `UserWarning` per
    model call for a situation that is entirely expected.

    **Off has to be spelled, because `create_deep_agent` turns it on.** Upstream's
    `append_prompt_caching_middleware` composes an `AnthropicPromptCachingMiddleware` whenever the
    model is an Anthropic one, so returning `[]` no longer means "no caching" — it means "whatever
    upstream decided". That was measured rather than reasoned about: with `llm_prompt_caching=False`
    the request payload still carried `cache_control` breakpoints, and the test asserting their
    absence is what caught it. So the disabled path now returns a *named placeholder* that occupies
    upstream's slot and does nothing, on the same argument and by the same `.name` splice as
    `compaction.disabled_summarizer`. This is the second default the harness supplies that this seam
    must actively refuse; the rule is that a decision belonging to `settings` may not be made by an
    upstream default, in either direction.

    The empty list is still right when the provider is not Anthropic: upstream composes nothing
    there either, so there is no slot to occupy and no `langchain_anthropic` import to make.

    Returns:
        A one-element list on the Anthropic path — the real middleware when caching is on, an inert
        placeholder holding its name when it is off — and `[]` on every other provider. The list
        shape is what `build_langgraph_agent` splices, matching its three other middleware groups.
    """
    if settings.llm_provider != "anthropic":
        return []
    if not settings.llm_prompt_caching:
        return [_CachingDisabled()]
    from langchain_anthropic.middleware import AnthropicPromptCachingMiddleware

    # `ttl` is left at upstream's 5-minute default deliberately. The 1-hour cache doubles the write
    # premium (2x rather than 1.25x) to buy survival across idle gaps, which is a trade a
    # deployment with measured traffic can make and this seam cannot make for it — and a second
    # setting with no reader is what `agent/compaction.py` records the cost of.
    return [AnthropicPromptCachingMiddleware(unsupported_model_behavior="ignore")]


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


def _anthropic_model(model: str | None = None, *, effort: str | None = None) -> Any:
    """`ChatAnthropic` on the dev path, with the same eager credential preflight.

    The preflight is kept for the reason `_anthropic_client` gives: a missing key should fail here,
    naming what to set, rather than as an opaque 401 on the first model call — which under the
    graph engine would surface mid-stream, after the turn has already emitted events.
    """
    _require_anthropic_key()
    from langchain_anthropic import ChatAnthropic

    return ChatAnthropic(
        model_name=model or settings.agent_model,
        timeout=settings.llm_timeout_seconds,
        max_retries=settings.llm_max_retries,
        stop=None,
        **_generation_options(effort),
    )


def _generation_options(effort: str | None = None) -> dict[str, Any]:
    """The deployment's generation caps, as constructor kwargs both providers accept.

    **Shared because a per-response cap that applies on one provider and not the other is not a
    cap.** These were lost in the rebuild — the agent builder that used to thread them was deleted
    and neither replacement passed them on — and the failure was silent in the expensive direction:
    with `llm_max_tokens=4096` configured, the Anthropic model resolved to the library's own
    default, measured at 128000. A deployment that had bounded its worst-case answer no longer had.

    `temperature` is omitted rather than sent as `None` when unset. That is the rule
    `core/config/llm.py` records having broken every turn once: some OpenAI-compatible endpoints
    reject an explicit null, so "unset" has to mean *absent from the request*, not present-and-null.

    **`reasoning_effort` is here, and it is scoped to one provider by config rather than by a
    branch in this function.** The first version of this said the two clients "both take it, so
    there is no translation to write", on the strength of both *accepting* the kwarg. They accept
    it and they do not mean the same thing by it — measured through `_get_request_payload` rather
    than off the constructed object, which is the check that would have caught it:
    `langchain-anthropic` folds it into `output_config.effort` and injects
    `thinking={'type': 'adaptive'}`, i.e. extended thinking, with the `temperature` conflict and
    the `max_tokens` draw that implies.

    So `LlmSettings._effort_is_provider_scoped` refuses the setting on the Anthropic path and this
    function stays a plain pass-through for the provider where the name means what it says. The
    lesson is the general one: an attribute that round-trips on a client proves the constructor
    accepted a kwarg, and nothing at all about what reaches the wire.

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


def _require_anthropic_key() -> None:
    """Fail with the one message both Anthropic paths owe a misconfigured deployment."""
    import os

    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise RuntimeError(
            "ANTHROPIC_API_KEY is not set — the Anthropic chat-client path needs it. "
            "Export it, set CHEMCLAW_LLM_PROVIDER=openai_compatible for the internal endpoint, "
            "or pass an explicit model to build_langgraph_agent (as the tests do)."
        )


def tls_verify() -> Any | None:
    """The TLS verification policy a client to the internal endpoint must use, or None.

    Split out of `_tls_http_client` so that the *policy* can be shared without sharing the
    *connection pool*. The judge (`evals/live_judge.py`) needs the same private CA and must not
    have the same client: `_tls_http_client` is a process-wide singleton the agent keeps for its
    whole life, and the Anthropic SDK stores a caller-supplied `http_client` unwrapped and
    `aclose()`s it from its own `close()`/`__aexit__` — so one idiomatic `async with
    AsyncAnthropic(...)` in the judge would close the agent's client for the rest of the process.
    Sharing the context has none of that: an `SSLContext` is immutable configuration, not a
    resource with an owner.

    None means the system trust store, which is right for a publicly-trusted endpoint; only a
    private-CA internal endpoint needs the explicit bundle.

    **Deliberately not `@cache`d, unlike the client below.** A second cache keyed off the same
    setting is a second thing every test that swaps `llm_tls_ca_bundle` has to clear, and the two
    caches disagreeing is a stale CA — which is what happened the moment this function was split
    out with `@cache` on it: `test_the_private_ca_client_is_built_once_per_process` passed alone
    and failed in file order, because its neighbour cleared one cache and not the other. The
    per-turn cost this saves lives in `_tls_http_client`'s cache, which is where it was measured;
    building a context per *grading call* is one file read against a network round trip.
    """
    if not settings.llm_tls_ca_bundle:
        return None
    import ssl

    # An `SSLContext`, not `verify="<path>"`: httpx deprecated the string form ("`verify=<str>` is
    # deprecated. Use `verify=ssl.create_default_context(cafile=...)`"), and building the context
    # here is also the only form that says what the bundle *is* — a CA file to verify the peer
    # against, rather than a path httpx has to guess the meaning of.
    return ssl.create_default_context(cafile=settings.llm_tls_ca_bundle)


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
    verify = tls_verify()
    if verify is None:
        return None
    import httpx

    return httpx.AsyncClient(
        verify=verify,
        # Never inherit an ambient proxy: HTTPS_PROXY/ALL_PROXY set on the pod would otherwise
        # redirect every prompt, completion and the Authorization bearer to a host of the env
        # setter's choosing, past the private-CA pinning above (the proxy re-terminates TLS).
        trust_env=False,
    )
