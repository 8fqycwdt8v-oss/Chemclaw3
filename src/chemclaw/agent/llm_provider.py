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

from typing import Any

from chemclaw.core.config import settings

# A non-empty placeholder for endpoints that accept any bearer (some internal OpenAI-compatible
# servers ignore the key): the OpenAI SDK refuses to construct with an empty api_key, so a keyless
# internal endpoint still needs a stand-in. A real generic credential (`llm_api_key`) overrides it.
_KEYLESS_PLACEHOLDER = "not-required"


def build_chat_model(task: str = "agent") -> Any:
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

    Returns:
        A LangChain `BaseChatModel` ready for `create_agent(model=...)`. Construction only, no
        network call.

    Raises:
        RuntimeError: When the selected provider's credential is absent, naming what to set.
    """
    model = settings.model_routes.get(task)
    if settings.llm_provider == "openai_compatible":
        return _openai_compatible_model(model)
    return _anthropic_model(model)


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

    **Below the minimum cacheable prefix it degrades silently, and that is a property of the API
    rather than a check here.** Anthropic requires a prefix of roughly 1,024 tokens to create an
    entry at all (2,048–4,096 on some models — it is per-model and not monotonic across
    generations). Under that, the breakpoint is accepted, no entry is created, `cache_creation` and
    `cache_read` both come back zero, and the request is answered normally at full price. There is
    no error to handle and nothing here counts tokens to pre-empt one: a threshold copied into this
    repo would be a second, staler statement of a number only the provider knows. What makes the
    difference *visible* rather than assumed is the ledger — `api/runner_usage.graph_usage_tokens`
    reads `cache_read`/`cache_creation` off every chunk and `turn_costs` stores both columns, so a
    prefix that is not caching reads as zeros there instead of as a belief.

    **Two gates, because they answer different questions.** `settings.llm_provider` decides whether
    the *deployment* is on Anthropic at all, so the production `openai_compatible` target gets an
    empty list and never imports `langchain_anthropic` — the guarantee is structural rather than
    resting on somebody else's isinstance check. `unsupported_model_behavior="ignore"` covers the
    other case: an Anthropic-configured deployment handed an injected model (every test that passes
    `build_langgraph_agent(model=fake)`), where upstream's default would emit a `UserWarning` per
    model call for a situation that is entirely expected.

    Returns:
        A one-element middleware list on the Anthropic path with caching enabled, else `[]`. The
        list shape is what `build_langgraph_agent` splices, matching its three other middleware
        groups.
    """
    if not settings.llm_prompt_caching or settings.llm_provider != "anthropic":
        return []
    from langchain_anthropic.middleware import AnthropicPromptCachingMiddleware

    # `ttl` is left at upstream's 5-minute default deliberately. The 1-hour cache doubles the write
    # premium (2x rather than 1.25x) to buy survival across idle gaps, which is a trade a
    # deployment with measured traffic can make and this seam cannot make for it — and a second
    # setting with no reader is what `agent/compaction.py` records the cost of.
    return [AnthropicPromptCachingMiddleware(unsupported_model_behavior="ignore")]


def _openai_compatible_model(model: str | None = None) -> Any:
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

    return ChatOpenAI(
        model=model or settings.llm_model,
        base_url=settings.llm_base_url,
        # `ChatOpenAI` takes the credential as a `SecretStr`, which keeps the key out of a repr
        # and out of any log line that prints the model object.
        api_key=SecretStr(settings.llm_api_key or _KEYLESS_PLACEHOLDER),
        timeout=settings.llm_timeout_seconds,
        max_retries=settings.llm_max_retries,
        http_async_client=_tls_http_client(),
        stream_usage=settings.llm_stream_usage,
        **_generation_options(),
    )


def _anthropic_model(model: str | None = None) -> Any:
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
        **_generation_options(),
    )


def _generation_options() -> dict[str, Any]:
    """The deployment's generation caps, as constructor kwargs both providers accept.

    **Shared because a per-response cap that applies on one provider and not the other is not a
    cap.** These were lost in the rebuild — the agent builder that used to thread them was deleted
    and neither replacement passed them on — and the failure was silent in the expensive direction:
    with `llm_max_tokens=4096` configured, the Anthropic model resolved to the library's own
    default, measured at 128000. A deployment that had bounded its worst-case answer no longer had.

    `temperature` is omitted rather than sent as `None` when unset. That is the rule
    `core/config/llm.py` records having broken every turn once: some OpenAI-compatible endpoints
    reject an explicit null, so "unset" has to mean *absent from the request*, not present-and-null.
    """
    options: dict[str, Any] = {"max_tokens": settings.llm_max_tokens}
    if settings.llm_temperature is not None:
        options["temperature"] = settings.llm_temperature
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


def _tls_http_client() -> Any | None:
    """An httpx client pinned to the internal CA when one is configured, else None (system store).

    Returning None lets the OpenAI SDK build its own default client — the right behavior for a
    publicly-trusted endpoint; only a private-CA internal endpoint needs the explicit bundle.
    """
    if not settings.llm_tls_ca_bundle:
        return None
    import httpx

    return httpx.AsyncClient(verify=settings.llm_tls_ca_bundle)
