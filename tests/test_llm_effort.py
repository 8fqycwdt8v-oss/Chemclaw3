"""The reasoning-effort knob reaches the constructed client, and is absent from the request unset.

**Asserted on the request payload rather than on a captured kwargs dict**, which is the house
pattern in `tests/test_llm_provider.py` and here it is load-bearing rather than stylistic:
`ChatOpenAI` is `extra="ignore"`, so a kwarg it stopped accepting — a rename upstream, a client
swap — would be **dropped in silence**, as would one a gateway does not understand. A test that
asserted "we passed `reasoning_effort=`" would stay green through exactly that failure while every
turn ran at the endpoint's default effort.

The absence case is the other half and is not symmetric with it: "unset" has to mean the key is
missing from the request rather than present and null, because some OpenAI-compatible endpoints
reject an explicit null — the rule `core/config/llm.py` records having broken every turn once.

**Half this file used to be about a refusal, and the refusal is gone.** Two guards — a settings
validator and a check inside `build_chat_model` — existed because `langchain-anthropic` turned the
same kwarg into extended thinking. With one gateway there is one meaning
(`D-2026-09-04-a-gateway-is-the-only-provider`), so `effort` is unconditionally usable and the
tests that drove the refusal are deleted rather than inverted. What survives from them is the
*method*: read the payload, never the attribute.
"""

from typing import Any

import pytest

from chemclaw.agent.llm_provider import build_chat_model
from chemclaw.agent.profiles import AgentProfile
from chemclaw.core.config import settings
from chemclaw.core.config.llm import LlmSettings


def _openai(monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the seam at a gateway that is not the local mock."""
    monkeypatch.setattr(settings, "llm_base_url", "http://internal-llm.invalid/v1")
    monkeypatch.setattr(settings, "llm_model", "gpt-oss")


def test_the_configured_effort_reaches_the_wire(monkeypatch: pytest.MonkeyPatch) -> None:
    """Asserted on the **request payload**, not on the constructed object.

    The first version of this file asserted `model.reasoning_effort == "high"` and called that
    proof the feature worked. It is not: `ChatOpenAI` is an `extra="ignore"` pydantic model, so the
    attribute says the constructor accepted a kwarg and says nothing about what is sent. That gap
    is where a real defect lived — the same attribute assertion passed against the second client
    this seam used to have, while the wire carried `output_config` plus injected extended thinking.
    A gateway that silently ignores the parameter is the same gap, one hop further out.

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

    `ChatOpenAI` is `extra="ignore"` and types the field `str | None`, so nothing
    downstream would object to `"hihg"` — it would reach the endpoint and come back a 400 that is
    not failed over, on every turn. The `Literal` is what turns that into a refusal at load time.
    """
    with pytest.raises(ValueError, match="effort"):
        AgentProfile(name="typo", effort="hihg")  # type: ignore[arg-type]


def test_effort_is_no_longer_refused_anywhere_and_reaches_the_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The widening this collapse bought, asserted on the wire rather than announced in a comment.

    Two guards used to refuse a non-`None` effort: `LlmSettings._effort_is_provider_scoped` for the
    deployment setting, and a `RuntimeError` inside `build_chat_model` for `AgentProfile.effort`,
    which never passes through a settings validator. Both existed because `langchain-anthropic`
    folded the kwarg into `output_config` *and* injected `thinking={'type': 'adaptive'}` — a
    different feature, with a `temperature` conflict and a claim on `llm_max_tokens`.

    With one client there is one meaning, so a profile's effort now simply arrives. Asserted from
    *both* inputs, because the two guards were separate and their removal has to be too — and on
    `_default_params`, because a gateway that does not understand `reasoning_effort` drops it in
    silence and the attribute would say nothing about that.
    """
    _openai(monkeypatch)
    monkeypatch.setattr(settings, "llm_effort", None)

    # The profile input — the one the settings validator could never see.
    assert build_chat_model(effort="high")._default_params["reasoning_effort"] == "high"

    # And the deployment input, which the validator used to reject at construction.
    assert LlmSettings(llm_effort="high").llm_effort == "high"
