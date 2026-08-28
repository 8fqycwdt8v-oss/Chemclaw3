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

import ast
import functools
import os
import warnings
from pathlib import Path
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


def _captured_payload(
    monkeypatch: pytest.MonkeyPatch, profile: Any | None = None
) -> dict[str, Any]:
    """The Anthropic request payload `build_langgraph_agent` produces. No network call.

    The graph is invoked for real up to the point the provider would open a socket, and the payload
    is taken there — so this reads what the assembled middleware chain actually built rather than
    what re-assembling it by hand would build.

    Args:
        monkeypatch: Used to install the capture and a dummy credential.
        profile: The agent profile to build, or `None` for the default. Passed through rather than
            given a second helper, because a narrowed profile's payload differs from the default's
            in exactly the way the floor test measures — its tool schemas.
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
    agent = (
        build_langgraph_agent(model=model, profile=profile)
        if profile
        else build_langgraph_agent(model=model)
    )
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
    """A deployment that has measured caching to be a loss can switch it off from config.

    **"Off" stopped being an empty list, and the payload test below is what found out.**
    `create_deep_agent` composes an `AnthropicPromptCachingMiddleware` whenever the model is an
    Anthropic one, so returning nothing here no longer means "no caching" — it means "upstream's
    default". Off is now a *named placeholder* that occupies that slot and overrides no hook.

    Asserted on the name rather than on the type, because the name is the whole mechanism: it is
    what `_apply_custom_middleware` matches to replace upstream's entry instead of landing beside
    it. `test_payload_carries_no_breakpoints_when_caching_is_off` is the one that proves the effect.
    """
    _use_settings(monkeypatch, llm_provider="anthropic", llm_prompt_caching=False)
    middleware = provider.prompt_caching_middleware()
    assert [m.name for m in middleware] == ["AnthropicPromptCachingMiddleware"]
    assert not isinstance(middleware[0], _real_caching_middleware()), (
        "the disabled path returned the real caching middleware"
    )


def _real_caching_middleware() -> type:
    """Upstream's caching middleware class, imported where the layering gate allows it.

    Function-scope for the reason `prompt_caching_middleware` keeps its own import there:
    `tests/test_third_party_layering.py` lists `langchain_anthropic` as a function-scope row.
    """
    from langchain_anthropic.middleware import AnthropicPromptCachingMiddleware

    return AnthropicPromptCachingMiddleware


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
# The credential, and the difference between not having one and having one that is refused.
# --------------------------------------------------------------------------------------------

_CREDENTIAL_ENV = "API-KEY"


@functools.cache
def _live_credential() -> str | None:
    """The credential, but only if it actually works. `None` — with a reason — otherwise.

    **The guard this replaced tested for the variable rather than for the credential**, and the two
    come apart in the environment this repository is developed in. A Claude Code Remote box for
    this repo carries `API-KEY` set to a value `api.anthropic.com` rejects; measured 2026-08-25,
    both tests below therefore *ran* where the intent was plainly to skip, and `make test` returned
    `3 failed, 4251 passed` on an unmodified tree. A red suite that means "your credential is
    stale" is a red suite nobody can read.

    `count_tokens` is the probe because it is **not billed**, which is also why the second test
    uses it for its real work. One round trip, cached for the session by `functools.cache`, so a
    suite with thirty guarded tests still costs one call.

    **Only a credential that does not work becomes a skip** — a rejected key
    (`AuthenticationError`), or a key on an account with no balance (a 400 whose body is "credit
    balance is too low"). Both are the operator being unconfigured. A 429 or a 529 is the provider
    being *busy*, not unconfigured, and folding those into "skipped" is how a suite quietly stops
    testing; they propagate, and the test fails as it should. Any other 400 is a real fault in this
    fixed probe and propagates too.

    **Called from a fixture, never from `skipif`.** `skipif` evaluates its condition at
    *collection*, and an exception there is not a failing test — it is `Interrupted: N errors
    during collection`, which abandons the entire session. Measured: with `API-KEY` set and the
    provider unreachable, `pytest tests/test_prompt_caching.py tests/test_context_floor.py`
    collected neither file and ran 0 tests. That turned the paragraph above inside out — "the test
    fails as it should" was the intent, and what actually happened was that every *other* test
    stopped running too. The probe now runs inside `live_credential`, where a propagating 429 fails
    these two tests and nothing else, which is what was meant in the first place.
    """
    key = os.environ.get(_CREDENTIAL_ENV)
    if not key:
        return None
    import anthropic

    try:
        anthropic.Anthropic(api_key=key).messages.count_tokens(
            model="claude-haiku-4-5-20251001",
            messages=[{"role": "user", "content": "probe"}],
        )
    except anthropic.AuthenticationError:
        return None
    except anthropic.BadRequestError as exc:
        # An unfunded account is the operator being unconfigured, not the provider being busy:
        # `count_tokens` on a zero-balance key answers 400 "credit balance is too low", which is a
        # credential that does not work in exactly the sense the docstring means. Fold *only* that
        # one signal into a skip; any other 400 is a real fault in this fixed probe and propagates.
        if "credit balance is too low" in str(exc).lower():
            return None
        raise
    return key


@pytest.fixture
def live_credential() -> str:
    """The working credential, or skip *this test* — the guard that used to be a `skipif`.

    A fixture rather than a mark because it runs at call time. Collection then cannot fail, so a
    provider that is down costs these two tests and leaves the other 4,500 alone.
    """
    key = _live_credential()
    if key is None:
        pytest.skip(_no_credential_reason())
    return key


def _no_credential_reason() -> str:
    """Why the guarded tests are skipping, in the words an operator needs.

    Absent and rejected are different problems with different remedies, and the skip line is the
    only place anybody reads which one they have.
    """
    if not os.environ.get(_CREDENTIAL_ENV):
        return f"no {_CREDENTIAL_ENV} in the environment"
    return (
        f"{_CREDENTIAL_ENV} is set but the provider will not serve it — the credential is stale "
        "or its account has no balance"
    )


# --------------------------------------------------------------------------------------------
# The only test that proves the provider honoured any of it.
# --------------------------------------------------------------------------------------------


def test_live_second_call_reads_from_cache(
    monkeypatch: pytest.MonkeyPatch, live_credential: str
) -> None:
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
    monkeypatch.setenv("ANTHROPIC_API_KEY", live_credential)
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


# The floors, measured 2026-08-12 by bisecting a synthetic prefix to ±1 token and reading
# `cache_creation_input_tokens`. They live in a test rather than in `src/` deliberately:
# `llm_provider.prompt_caching_middleware` argues that a provider threshold copied into the code
# path would be a second, staler statement of a number only the provider knows, and that argument
# survived the measurement. A test is where such a number belongs — when the provider moves it,
# this fails and someone re-measures, which is the opposite of a constant nobody rechecks.
#
# Note the shape: the *smaller* model has the *higher* floor. Any rule of the form "the newer or
# cheaper the model, the lower the minimum" is wrong here.
_MEASURED_FLOORS = {"claude-sonnet-5": 1024, "claude-haiku-4-5-20251001": 4096}

# Profiles known to sit below the haiku floor, and therefore never to cache on it. What this pins is
# that the set does not *grow* silently — a prompt edit that pushes a profile under 4,096 is a cost
# regression with no symptom except a bill.
#
# **It is now empty, and the reason is a side effect rather than an improvement.** It held
# `property-lookup` and `safety` until the scratchpad landed (D-2026-08-15): registering the six
# filesystem tools added their schemas to every profile's prefix, worth roughly 1,800 tokens, and
# that carried both over the floor. Re-measured live against `count_tokens` on the day:
# `safety` 2,958 -> 4,751 and `property-lookup` -> 4,842, against `design` 7,286, `evidence` 7,749,
# `computation` 10,542, `reporting` 9,240 and `default` 23,309.
#
# Adjudicated rather than updated, because the direction is not obviously good. Both profiles now
# cache, which is a real saving on a narrow agent that used to pay full price on *every* call. But
# the same 1,800 tokens are added to the five profiles that already cleared the floor, where they
# buy nothing after the first call and cost a larger cache write. The set is the thing worth
# ratcheting; the prefix growth is recorded here so that a future reading of "0 below the floor"
# is not mistaken for a prompt that got leaner.
_BELOW_HAIKU_FLOOR: set[str] = set()


def test_which_shipped_profiles_clear_the_cache_floor(live_credential: str) -> None:
    """Every profile's real prefix, against the floor of the model it runs on.

    **This is the check the original reasoning could not make, because its number was wrong.** The
    docstring said the minimum was "roughly 1,024 tokens" and the ADR said every prefix was "far
    above every model's minimum"; measured, haiku's floor is 4,096 and two of the seven shipped
    profiles are under it. Confirmed live at the time: `safety` sent 2,958 input tokens on two
    identical back-to-back calls with both cache counters zero on each.

    Costs nothing in tokens — `count_tokens` is not billed — so this runs wherever a credential
    does. What it counts is `tools` + `system`, which is the prefix upstream's breakpoints cover.
    """
    import anthropic

    from chemclaw.agent.profile_discovery import load_profiles
    from chemclaw.agent.profiles import get_profile, registered_profile_names

    load_profiles()
    client = anthropic.Anthropic(api_key=live_credential)
    model = "claude-haiku-4-5-20251001"
    floor = _MEASURED_FLOORS[model]

    below: set[str] = set()
    for name in registered_profile_names():
        with pytest.MonkeyPatch.context() as patch:
            payload = _captured_payload(patch, get_profile(name))
        # `tools` is omitted rather than passed a sentinel when a profile advertises none: the
        # SDK's absent-value type is not the same as an empty list, and every shipped profile has
        # tools anyway — this is the branch that keeps an out-of-tree toolless profile counting.
        counted: dict[str, Any] = {
            "model": model,
            "system": [{"type": "text", "text": block["text"]} for block in payload["system"]],
            "messages": [{"role": "user", "content": "hi"}],
        }
        if payload.get("tools"):
            counted["tools"] = payload["tools"]
        prefix = client.messages.count_tokens(**counted).input_tokens
        if prefix < floor:
            below.add(name)

    assert below == _BELOW_HAIKU_FLOOR, (
        f"the set of profiles that cannot cache on {model} changed: {sorted(below)} "
        f"(recorded: {sorted(_BELOW_HAIKU_FLOOR)}). A profile that dropped under {floor} tokens "
        "now pays full price on every model call, and the only symptom is the bill."
    )


# --------------------------------------------------------------------------------------------
# The guard on the guard.
# --------------------------------------------------------------------------------------------
# The guard on the guard.
# --------------------------------------------------------------------------------------------


def test_no_module_level_call_dials_the_provider_at_collection() -> None:
    """Collecting this module must never dial anything, whatever the environment holds.

    The defect this pins cost the whole suite, not this file: `_live_credential()` was evaluated
    inside `@pytest.mark.skipif(...)`, so an unreachable provider raised during *collection*, and a
    collection error is `Interrupted` — sibling files are never collected and never run. Measured
    with the provider pointed at a closed port: 0 tests ran from two files, one of which was
    `test_context_floor.py`.

    Asserting the absence rather than the fix, because there are several ways to reintroduce it
    (a `skipif`, a module-level constant, a decorator argument) and only one property that matters:
    nothing evaluated at import time asks the network a question.
    """
    tree = ast.parse(Path(__file__).read_text())
    nested = {
        id(inner)
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        for stmt in node.body
        for inner in ast.walk(stmt)
    }
    offenders = sorted(
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_live_credential"
        and id(node) not in nested
    )
    assert not offenders, (
        f"_live_credential() is called outside a function body at line(s) {offenders}. It performs "
        "a network round trip, so at module scope — including inside a `skipif` condition — it "
        "runs at collection, where an exception aborts the entire pytest session rather than "
        "failing one test. Request the `live_credential` fixture instead."
    )
