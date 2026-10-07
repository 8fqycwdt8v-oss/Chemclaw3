"""The reasoning-effort knob reaches the request payload, and is absent from it when unset.

Asserted on the payload, because `ChatOpenAI` is `extra="ignore"`: a kwarg it stopped accepting
would be dropped silently while an attribute check stayed green. "Unset" means the key is
missing, not null, because some OpenAI-compatible endpoints reject an explicit null.
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
    """The configured effort reaches the request payload built by `_default_params`.

    An attribute assertion only proves the constructor accepted the kwarg, not what is sent.
    """
    _openai(monkeypatch)
    monkeypatch.setattr(settings, "llm_effort", "high")

    params = build_chat_model()._default_params

    assert params["reasoning_effort"] == "high"


def test_an_unset_effort_leaves_the_parameter_off_the_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The shipped default sends nothing: an absent key, not a null one.

    A 400 for a disliked parameter is deliberately not failed over, so a default that sent something
    would break every turn on an endpoint that never expected it.
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
    """The fallback endpoint gets the same effort as the primary.

    The failover instance is built by a second `_openai_compatible_model` call, a separate place the
    parameter must arrive, and exercised only when an endpoint is down.
    """
    _openai(monkeypatch)
    monkeypatch.setattr(settings, "llm_fallback_base_url", "http://fallback.invalid/v1")
    monkeypatch.setattr(settings, "llm_effort", None)

    runnable = build_chat_model(effort="low")

    fallbacks = getattr(runnable, "fallbacks", ())
    assert fallbacks, "no fallback was configured, so this test proves nothing"
    assert all(getattr(f, "reasoning_effort", None) == "low" for f in fallbacks)


def test_the_profile_field_and_the_settings_field_accept_the_same_set() -> None:
    """The profile field and the settings field accept the same effort vocabulary.

    `AgentProfile` imports no settings module, so the two `Literal`s are written twice and only this
    test stops them drifting.
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
    """A misspelled effort value is refused at load time.

    `ChatOpenAI` types the field `str | None`, so a typo would reach the endpoint and 400 on every
    turn; the `Literal` turns it into a load-time refusal.
    """
    with pytest.raises(ValueError, match="effort"):
        AgentProfile(name="typo", effort="hihg")  # type: ignore[arg-type]


def test_effort_is_no_longer_refused_anywhere_and_reaches_the_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Effort from both the settings and a profile reaches the payload without refusal.

    Asserted from both inputs, on `_default_params`, since a gateway that does not understand
    `reasoning_effort` would drop it silently.
    """
    _openai(monkeypatch)
    monkeypatch.setattr(settings, "llm_effort", None)

    # The profile input — the one the settings validator could never see.
    assert build_chat_model(effort="high")._default_params["reasoning_effort"] == "high"

    # And the deployment setting accepts it at construction.
    assert LlmSettings(llm_effort="high").llm_effort == "high"
