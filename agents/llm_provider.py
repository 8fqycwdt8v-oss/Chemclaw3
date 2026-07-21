"""The one place a chat-client class is imported — the LLM provider seam (plan Phase F0).

`build_chat_client` selects the agent's MAF chat client from config (`settings.llm_provider`), so
pointing Chemclaw at the internal OpenAI-compatible ("OpenLLM-like") endpoint versus the Anthropic
dev path is a single config change, never a code edit at a call site (KISS/DRY, mirroring the ELN
adapter registry). Provider client classes are imported **only here** — `agents/chemclaw_agent.py`
calls this factory and stays provider-agnostic.

The internal endpoint is reached with **one generic API credential** (`settings.llm_api_key`),
deliberately not per-user Entra: the raw inference call is not a user-scoped resource access (see
`docs/foundation-plan.md` §0). Entra scoping applies to *who* is taking the turn and *which*
authorized workflow runs (Phase F4), not to this credential. Transport concerns (private-CA TLS,
timeout, retry budget) come from config so a firewalled internal endpoint works with no code change.
"""

from typing import Any

from chemclaw.config import settings

# A non-empty placeholder for endpoints that accept any bearer (some internal OpenAI-compatible
# servers ignore the key): the OpenAI SDK refuses to construct with an empty api_key, so a keyless
# internal endpoint still needs a stand-in. A real generic credential (`llm_api_key`) overrides it.
_KEYLESS_PLACEHOLDER = "not-required"


def build_chat_client() -> Any:
    """Build the configured MAF chat client (provider selected by `settings.llm_provider`).

    Returns:
        A MAF chat client ready to hand to `Agent(client=...)`. No network call happens here —
        construction only.

    Raises:
        RuntimeError: When the selected provider's required credential/config is absent, with a
            message naming exactly what to set (so a misconfiguration fails clearly at build time,
            not as an opaque 401/404 on the first model call).
    """
    if settings.llm_provider == "openai_compatible":
        return _openai_compatible_client()
    return _anthropic_client()


def _openai_compatible_client() -> Any:
    """Point MAF's OpenAI client at the internal endpoint (base_url + generic credential).

    Transport (private-CA TLS via `llm_tls_ca_bundle`, `llm_timeout_seconds`, `llm_max_retries`) is
    carried by an `AsyncOpenAI` we construct explicitly, since MAF's client constructor does not
    expose those — the model call must survive a slow or self-signed internal endpoint.
    """
    from agent_framework.openai import OpenAIChatClient
    from openai import AsyncOpenAI

    async_client = AsyncOpenAI(
        base_url=settings.llm_base_url,
        api_key=settings.llm_api_key or _KEYLESS_PLACEHOLDER,
        timeout=settings.llm_timeout_seconds,
        max_retries=settings.llm_max_retries,
        http_client=_tls_http_client(),
    )
    return OpenAIChatClient(model=settings.llm_model, async_client=async_client)


def _tls_http_client() -> Any | None:
    """An httpx client pinned to the internal CA when one is configured, else None (system store).

    Returning None lets the OpenAI SDK build its own default client — the right behavior for a
    publicly-trusted endpoint; only a private-CA internal endpoint needs the explicit bundle.
    """
    if not settings.llm_tls_ca_bundle:
        return None
    import httpx

    return httpx.AsyncClient(verify=settings.llm_tls_ca_bundle)


def _anthropic_client() -> Any:
    """Build the Anthropic dev-path client (unchanged behavior from the pre-seam default).

    Preflights the key so a missing credential fails here with a clear message rather than an opaque
    401 on the first call. `agent_model` (not `llm_model`) names the Anthropic model, keeping the
    two providers' model settings independent.
    """
    import os

    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise RuntimeError(
            "ANTHROPIC_API_KEY is not set — the Anthropic chat-client path needs it. "
            "Export it, set CHEMCLAW_LLM_PROVIDER=openai_compatible for the internal endpoint, "
            "or pass an explicit chat_client to build_agent (as the tests do)."
        )
    from agent_framework.anthropic import AnthropicClient

    return AnthropicClient(model=settings.agent_model)
