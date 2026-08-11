"""The one place a chat-model class is imported — the LLM provider seam (plan Phase F0).

`build_chat_model` selects the model from config (`settings.llm_provider`), so pointing Chemclaw at
the internal OpenAI-compatible ("OpenLLM-like") endpoint versus the Anthropic dev path is a single
config change, never a code edit at a call site (KISS/DRY, mirroring the ELN adapter registry).
Provider classes are imported **only here** — `agent/langgraph_agent.py` calls this factory and
stays provider-agnostic.

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
    )


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
