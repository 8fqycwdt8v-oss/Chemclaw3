"""The one place a chat-client class is imported — the LLM provider seam (plan Phase F0).

Two builders, one seam: `build_chat_client` for the MAF engine and `build_chat_model` for the
LangGraph one. They are separate functions because they return different protocols, and identical
in everything that is genuinely about the *provider* — endpoint, credential, per-task model route,
and transport. That sharing is the point: F0's promise is that pointing Chemclaw at the internal
endpoint is one config change, and a seam that forked per engine would drift on the one thing it
exists to keep single.

`build_chat_client` selects the agent's MAF chat client from config (`settings.llm_provider`), so
pointing Chemclaw at the internal OpenAI-compatible ("OpenLLM-like") endpoint versus the Anthropic
dev path is a single config change, never a code edit at a call site (KISS/DRY, mirroring the ELN
adapter registry). Provider client classes are imported **only here** — `agent/chemclaw_agent.py`
calls this factory and stays provider-agnostic.

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
    """Build the configured LangChain chat model — the LangGraph engine's half of this seam.

    The twin of `build_chat_client`, and deliberately a second function rather than a branch inside
    it. Both return `Any` so the types cannot tell them apart, but they return objects of different
    protocols (a MAF chat client versus a LangChain `BaseChatModel`), and the caller always knows
    which engine it is: `chemclaw_agent.build_agent` wants the first and
    `langgraph_agent.build_langgraph_agent` the second. One name covering two protocols would make
    every call site's intent unreadable at exactly the point where getting it wrong is an obscure
    attribute error inside a model call.

    What is shared is everything that is actually about *the provider*: which endpoint, which
    credential, the per-task model route, and the transport (private-CA TLS, timeout, retry
    budget). That is the property F0 asked for — pointing Chemclaw at the internal endpoint is a
    config change — and it must not fork per engine, or the two would drift on the one thing this
    module exists to keep single.

    Args:
        task: The routing key for per-task model selection (F10-E), exactly as
            `build_chat_client` uses it.

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
        # and out of any log line that prints the model object — an improvement over the raw string
        # the MAF client took.
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
