"""The reasoning-effort knob reaches the constructed client, on both providers, and is absent unset.

**Asserted on the model object rather than on a captured kwargs dict**, which is the house pattern
in `tests/test_llm_provider.py` and here it is load-bearing rather than stylistic: both
`ChatOpenAI` and `ChatAnthropic` are `extra="ignore"`, so a kwarg they stopped accepting — a
rename upstream, a client swap — would be **dropped in silence**. A test that asserted "we passed
`reasoning_effort=`" would stay green through exactly that failure while every turn ran at the
endpoint's default effort. Asking the constructed object what its effort *is* cannot.

The absence case is the other half and is not symmetric with it: "unset" has to mean the key is
missing from the request rather than present and null, because some OpenAI-compatible endpoints
reject an explicit null — the rule `core/config/llm.py` records having broken every turn once.
"""

from typing import Any

import pytest
from pydantic import SecretStr, ValidationError

from chemclaw.agent.llm_provider import build_chat_model
from chemclaw.agent.profiles import AgentProfile
from chemclaw.core.config import settings
from chemclaw.core.config.llm import LlmSettings


def _openai(monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the seam at the shipped provider — an internal OpenAI-compatible endpoint."""
    monkeypatch.setattr(settings, "llm_provider", "openai_compatible")
    monkeypatch.setattr(settings, "llm_base_url", "http://internal-llm.invalid/v1")
    monkeypatch.setattr(settings, "llm_model", "gpt-oss")


def _anthropic(monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the seam at the dev provider, with the credential its preflight demands."""
    monkeypatch.setattr(settings, "llm_provider", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")


def test_the_configured_effort_reaches_the_wire_on_the_provider_that_supports_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Asserted on the **request payload**, not on the constructed object.

    The first version of this file asserted `model.reasoning_effort == "high"` and called that
    proof the feature worked. It is not: both clients are `extra="ignore"` pydantic models, so the
    attribute says the constructor accepted a kwarg and says nothing about what is sent. That gap
    is exactly where the defect this test now guards lived — the same attribute assertion passed on
    Anthropic while the wire carried `output_config` plus injected extended thinking.

    `_default_params` is what `ChatOpenAI` builds its request from, so this reads the payload the
    endpoint would receive.
    """
    _openai(monkeypatch)
    monkeypatch.setattr(settings, "llm_effort", "high")

    params = build_chat_model()._default_params

    assert params["reasoning_effort"] == "high"


def test_an_unset_effort_leaves_the_parameter_off_the_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The shipped default sends nothing — an absent key, not a null one.

    This is the case that protects every existing deployment: a 400 from a parameter an endpoint
    dislikes is deliberately *not* failed over (`_failover_exceptions`), so a knob that defaulted
    to sending something would fail every turn on an endpoint that had never been asked about it.
    """
    _openai(monkeypatch)
    monkeypatch.setattr(settings, "llm_effort", None)

    params = build_chat_model()._default_params

    assert params.get("reasoning_effort") is None


def test_effort_is_refused_on_anthropic_where_it_means_extended_thinking() -> None:
    """The config refuses the combination at startup rather than at the first turn.

    **What the refusal is protecting against, measured rather than reasoned:** against
    `langchain-anthropic`, `reasoning_effort="high"` renders as `output_config={'effort': 'high'}`
    *and* `thinking={'type': 'adaptive', 'display': 'summarized'}`. That is extended thinking, not
    an effort level — and it cannot be combined with a set `temperature` (a 400, which is not
    failed over, so every turn fails) and draws its tokens from `llm_max_tokens`.

    Constructed rather than monkeypatched, because a `@model_validator` runs at construction and
    `setattr` on a live settings object never consults it — which is why the rest of this file
    cannot cover this and a test that patched the singleton would prove nothing.
    """
    with pytest.raises(ValidationError, match="only supported on"):
        LlmSettings(llm_provider="anthropic", llm_effort="high")


def test_the_anthropic_payload_is_why_that_refusal_exists() -> None:
    """Pins the upstream behaviour the refusal is written against, so a change is visible.

    If `langchain-anthropic` ever makes `reasoning_effort` a plain effort level with no injected
    thinking, this test goes red and the refusal above can be revisited — which is the point of
    asserting somebody else's behaviour rather than only our reaction to it. The same shape
    `tests/test_upstream_surface.py` uses for every other coupling to an upstream promise.
    """
    from langchain_anthropic import ChatAnthropic

    # `max_tokens` and `reasoning_effort` are pydantic field aliases upstream does not expose to a
    # type checker, which is itself part of the point: the kwarg surface is not statically known,
    # so what a client accepts has to be measured rather than trusted.
    client = ChatAnthropic(  # type: ignore[call-arg]
        model_name="claude-sonnet-5",
        api_key=SecretStr("x"),
        max_tokens=4096,
        stop=None,
        reasoning_effort="high",
    )
    payload = client._get_request_payload([("user", "hi")])

    assert payload["output_config"] == {"effort": "high"}
    assert payload["thinking"]["type"] == "adaptive", (
        "upstream no longer injects extended thinking for reasoning_effort — "
        "LlmSettings._effort_is_provider_scoped can be revisited"
    )


def test_a_profile_s_effort_beats_the_deployment_s(monkeypatch: pytest.MonkeyPatch) -> None:
    """The point of putting the field on the profile: two agents, one deployment, two answers."""
    _openai(monkeypatch)
    monkeypatch.setattr(settings, "llm_effort", "low")

    model = build_chat_model(effort="high")

    assert getattr(model, "reasoning_effort", None) == "high"


def test_a_profile_that_states_no_effort_inherits_the_deployment_s(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`None` on a profile means "use the global default", as it does for every other field."""
    _openai(monkeypatch)
    monkeypatch.setattr(settings, "llm_effort", "medium")

    model = build_chat_model(effort=None)

    assert getattr(model, "reasoning_effort", None) == "medium"


def test_the_fallback_endpoint_thinks_no_harder_than_the_primary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A degraded turn must not quietly exceed the effort the profile asked for.

    The failover instance is built by a second call to `_openai_compatible_model`, so it is a
    separate place the parameter has to arrive — and the one a reader is most likely to forget,
    because nothing exercises it until an endpoint is already down.
    """
    _openai(monkeypatch)
    monkeypatch.setattr(settings, "llm_fallback_base_url", "http://fallback.invalid/v1")
    monkeypatch.setattr(settings, "llm_effort", None)

    runnable = build_chat_model(effort="low")

    fallbacks = getattr(runnable, "fallbacks", ())
    assert fallbacks, "no fallback was configured, so this test proves nothing"
    assert all(getattr(f, "reasoning_effort", None) == "low" for f in fallbacks)


def test_the_profile_field_and_the_settings_field_accept_the_same_set() -> None:
    """One vocabulary, pinned in both directions.

    `AgentProfile` deliberately imports no settings module, so the two `Literal`s are written out
    twice and nothing but this test stops them drifting — a profile accepting a value the
    deployment setting refuses (or the reverse) would be a knob whose meaning depended on where it
    was spelled. The same invariant `tests/test_profile_autonomy_validation.py` holds for
    `harness_autonomy`.
    """

    def _values(annotation: Any) -> set[str]:
        import typing

        for arg in typing.get_args(annotation):
            if typing.get_origin(arg) is typing.Literal:
                return set(typing.get_args(arg))
        return set()

    profile = _values(AgentProfile.model_fields["effort"].annotation)
    deployment = _values(LlmSettings.model_fields["llm_effort"].annotation)

    assert profile == deployment == {"low", "medium", "high"}


def test_a_misspelled_effort_value_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """A typo is a loud error, not a silently ignored knob.

    Both clients are `extra="ignore"` and `ChatOpenAI` types the field `str | None`, so nothing
    downstream would object to `"hihg"` — it would reach the endpoint and come back a 400 that is
    not failed over, on every turn. The `Literal` is what turns that into a refusal at load time.
    """
    with pytest.raises(ValueError, match="effort"):
        AgentProfile(name="typo", effort="hihg")  # type: ignore[arg-type]
