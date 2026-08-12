"""Anthropic prompt caching: the breakpoints go on the wire, and the write lands in the ledger.

Three things are proved here, and only the first is about wiring.

**The seam decides.** `prompt_caching_middleware` returns a middleware on the Anthropic path and
nothing on `openai_compatible` — the production target, which has no `cache_control` parameter to
be handed.

**The breakpoints reach the request.** Not "the flag is set": the graph is built and its Anthropic
payload captured, so what is asserted is the three `cache_control` markers as the provider will
receive them. The paired negative (caching off → no marker anywhere) is what makes the positive
mean something.

**The ledger books a write.** `graph_usage_tokens` is asserted against the *recorded* shape of a
live cached call, because that shape is the one this repo got wrong: LangChain publishes cache
writes under a per-TTL key and zeroes the flat one, so the obvious reader books every write as
full-price input and reports that caching never happens.

`test_live_second_call_reads_from_cache` is the only one that spends money, and it is the only one
that proves the provider *honoured* any of the above. It skips without a credential so `make test`
stays offline.
"""

import os
import warnings
from typing import Any

import pytest

import chemclaw.agent.llm_provider as provider
from chemclaw.api.runner_usage import graph_usage_tokens
from chemclaw.core.config import Settings

# The recorded `input_token_details` of a live `claude-haiku-4-5` call that wrote a fresh cache
# entry over the default profile's prefix. Copied verbatim from the run, including the zeroed
# `cache_creation` that is the whole point: the 6,208 written tokens are only under the TTL key.
LIVE_WRITE_DETAILS = {
    "cache_read": 15136,
    "cache_creation": 0,
    "ephemeral_5m_input_tokens": 6208,
    "ephemeral_1h_input_tokens": 0,
}


class _Chunk:
    """The one attribute `graph_usage_tokens` reads off a streamed chunk."""

    def __init__(self, usage_metadata: dict[str, Any]) -> None:
        self.usage_metadata = usage_metadata


def _use_settings(monkeypatch: pytest.MonkeyPatch, **overrides: Any) -> None:
    """Point the provider seam at a fresh Settings built from explicit overrides."""
    monkeypatch.setattr(provider, "settings", Settings(_env_file=None, **overrides))  # type: ignore[call-arg]


def _captured_payload(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """The Anthropic request payload `build_langgraph_agent` produces. No network call.

    The graph is invoked for real up to the point the provider would open a socket, and the payload
    is taken there — so this reads what the assembled middleware chain actually built rather than
    what re-assembling it by hand would build.
    """
    import asyncio

    from langchain_anthropic import ChatAnthropic

    from chemclaw.agent.langgraph_agent import build_langgraph_agent

    monkeypatch.setenv("ANTHROPIC_API_KEY", "not-used-no-request-is-sent")
    captured: list[dict[str, Any]] = []
    original = ChatAnthropic._get_request_payload

    def capture(self: Any, input_: Any, **kwargs: Any) -> dict[str, Any]:
        captured.append(original(self, input_, **kwargs))
        raise _StopBeforeRequest

    monkeypatch.setattr(ChatAnthropic, "_get_request_payload", capture)
    model = ChatAnthropic(
        model_name="claude-haiku-4-5-20251001",
        max_tokens_to_sample=16,
        timeout=None,
        stop=None,
    )
    agent = build_langgraph_agent(model=model)
    with pytest.raises(_StopBeforeRequest):
        asyncio.run(agent.ainvoke({"messages": [{"role": "user", "content": "hi"}]}))
    return captured[0]


class _StopBeforeRequest(Exception):
    """Raised once the payload is in hand, so no request is ever sent."""


# --------------------------------------------------------------------------------------------
# The seam's decision.
# --------------------------------------------------------------------------------------------


def test_anthropic_path_supplies_a_caching_middleware(monkeypatch: pytest.MonkeyPatch) -> None:
    """On Anthropic with caching enabled, the seam yields exactly one middleware."""
    from langchain_anthropic.middleware import AnthropicPromptCachingMiddleware

    _use_settings(monkeypatch, llm_provider="anthropic", llm_prompt_caching=True)
    middleware = provider.prompt_caching_middleware()
    assert len(middleware) == 1
    assert isinstance(middleware[0], AnthropicPromptCachingMiddleware)


def test_openai_compatible_path_supplies_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """The production target gets no caching middleware, whatever the caching flag says.

    `cache_control` is an Anthropic parameter; the internal endpoint has no counterpart. Asserted
    with the flag *on* so the emptiness is the provider gate and not the feature switch.
    """
    _use_settings(
        monkeypatch,
        llm_provider="openai_compatible",
        llm_base_url="https://llm.internal/v1",
        llm_model="internal-large",
        llm_prompt_caching=True,
    )
    assert provider.prompt_caching_middleware() == []


def test_the_setting_turns_it_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """A deployment that has measured caching to be a loss can switch it off from config."""
    _use_settings(monkeypatch, llm_provider="anthropic", llm_prompt_caching=False)
    assert provider.prompt_caching_middleware() == []


def test_an_injected_non_anthropic_model_is_ignored_silently() -> None:
    """A fake model under an Anthropic-configured deployment must not warn on every model call.

    Every test that calls `build_langgraph_agent(model=fake)` is this case, so upstream's default
    `unsupported_model_behavior="warn"` would make the suite noisy about an expected situation.
    """
    from langchain.agents.middleware import ModelRequest

    (middleware,) = provider.prompt_caching_middleware()
    request = ModelRequest(
        model=object(),  # type: ignore[arg-type]
        messages=[],
        system_prompt="unused",
        tool_choice=None,
        tools=[],
        response_format=None,
    )
    with warnings.catch_warnings(record=True) as recorded:
        warnings.simplefilter("always")
        applied = middleware._should_apply_caching(request)
    assert applied is False
    assert not [w for w in recorded if issubclass(w.category, UserWarning)]


# --------------------------------------------------------------------------------------------
# The request shape.
# --------------------------------------------------------------------------------------------


def test_payload_carries_the_three_breakpoints(monkeypatch: pytest.MonkeyPatch) -> None:
    """The assembled graph marks the system prompt, the tool schemas and the message tail.

    The Anthropic wire order is `tools` → `system` → `messages`, so the system breakpoint is the
    one that caches the whole static prefix; the tool breakpoint caches the schemas alone (so a
    changed system prompt does not cost them), and the top-level marker caches the growing tail.
    """
    payload = _captured_payload(monkeypatch)

    system = payload["system"]
    assert isinstance(system, list) and system, "system prompt should be blocks, not a bare string"
    assert "cache_control" not in system[0], "only the last system block carries the breakpoint"
    assert system[-1]["cache_control"] == {"type": "ephemeral", "ttl": "5m"}

    tools = payload["tools"]
    marked = [tool for tool in tools if "cache_control" in tool]
    assert len(marked) == 1, "one trailing tool breakpoint caches the whole contiguous tool block"
    assert marked[0] is tools[-1]

    assert payload["cache_control"] == {"type": "ephemeral", "ttl": "5m"}

    # Anthropic allows four explicit breakpoints per request; three is what this uses.
    explicit = sum("cache_control" in block for block in system) + len(marked)
    assert explicit <= 4


def test_payload_carries_no_breakpoints_when_caching_is_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With the setting off, nothing in the request mentions caching.

    The pair to the test above: it is what proves those markers come from this feature rather than
    from something the framework was doing anyway.
    """
    import json

    _use_settings(monkeypatch, llm_provider="anthropic", llm_prompt_caching=False)
    payload = _captured_payload(monkeypatch)
    assert "cache_control" not in json.dumps(payload, default=str)


# --------------------------------------------------------------------------------------------
# The ledger.
# --------------------------------------------------------------------------------------------


def test_cache_write_is_read_from_the_per_ttl_key() -> None:
    """A write reported only under `ephemeral_5m_input_tokens` is booked as a write.

    This is the recorded shape of a real cached call. Reading `cache_creation` alone — which is
    what upstream zeroes here — booked all 6,208 written tokens as full-price `input` and left
    `cache_write` at 0, so `turn_costs` claimed a deployment that caches on every turn had never
    written a cache entry.
    """
    usage = graph_usage_tokens(
        _Chunk(
            {
                "input_tokens": 21347,
                "output_tokens": 16,
                "total_tokens": 21363,
                "input_token_details": LIVE_WRITE_DETAILS,
            }
        )
    )
    assert usage.cache_write == 6208
    assert usage.cache_read == 15136
    # The uncached remainder, not the write folded back into it.
    assert usage.input == 21347 - 15136 - 6208 == 3
    assert usage.total == 21363


def test_cache_write_still_reads_the_flat_key() -> None:
    """A provider that reports only the flat `cache_creation` is unchanged.

    The per-TTL breakdown is Anthropic's; the fallback keeps every other provider — and any future
    version that stops sending it — reading exactly as before.
    """
    usage = graph_usage_tokens(
        _Chunk(
            {
                "input_tokens": 1200,
                "output_tokens": 10,
                "total_tokens": 1210,
                "input_token_details": {"cache_read": 200, "cache_creation": 900},
            }
        )
    )
    assert usage.cache_write == 900
    assert usage.cache_read == 200
    assert usage.input == 100


def test_no_cache_activity_still_meters_zero() -> None:
    """An uncached provider response reports no cache tokens and full input, as it always did."""
    usage = graph_usage_tokens(
        _Chunk({"input_tokens": 500, "output_tokens": 20, "total_tokens": 520})
    )
    assert (usage.cache_read, usage.cache_write) == (0, 0)
    assert usage.input == 500


# --------------------------------------------------------------------------------------------
# The only test that proves the provider honoured any of it.
# --------------------------------------------------------------------------------------------


@pytest.mark.skipif("API-KEY" not in os.environ, reason="needs a live Anthropic credential")
def test_live_second_call_reads_from_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two back-to-back calls with our own payload: the second is served from cache.

    A payload-shape assertion proves the marker was sent, never that it was honoured — the
    provider silently ignores a breakpoint under its minimum cacheable prefix. So this captures
    the payload the graph builds, replays it verbatim twice through `ChatAnthropic`, and reads the
    result back through `graph_usage_tokens`: wire format, provider behaviour and ledger in one
    pass. Roughly 21,000 prefix tokens on the cheapest model, twice.
    """
    import uuid

    from langchain_anthropic import ChatAnthropic
    from langchain_core.messages import HumanMessage

    payload = _captured_payload(monkeypatch)
    # After the capture, which deliberately installs a dummy key so it can never reach the network.
    monkeypatch.setenv("ANTHROPIC_API_KEY", os.environ["API-KEY"])
    payload["max_tokens"] = 16
    # A unique tail, so this run writes its own entry instead of reading a previous run's.
    payload["messages"] = [{"role": "user", "content": f"Reply with: OK {uuid.uuid4().hex}"}]

    model = ChatAnthropic(
        model_name="claude-haiku-4-5-20251001",
        max_tokens_to_sample=16,
        timeout=None,
        stop=None,
    )
    monkeypatch.setattr(
        ChatAnthropic, "_get_request_payload", lambda self, input_, **kw: dict(payload)
    )
    ignored = [HumanMessage("ignored — the captured payload is what is sent")]

    first = graph_usage_tokens(model.invoke(ignored))
    second = graph_usage_tokens(model.invoke(ignored))

    assert first.cache_write > 0, f"first call created no cache entry: {first}"
    assert second.cache_read > 0, f"second call read nothing from cache: {second}"
    # The prefix is served from cache rather than re-billed: what is left at full price is the
    # handful of tokens after the last breakpoint.
    assert second.input < first.input + first.cache_write
    assert second.cache_read >= first.cache_write
