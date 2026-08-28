"""Provider failover was silent, and its whole operational value is knowing that it fired.

The decision is `D-2026-08-27-a-refusal-is-not-a-crash`. `RunnableWithFallbacks.ainvoke` catches the
primary's exception and moves to the next runnable with **no log line, no metric and no callback for
the attempt that failed** — so the primary internal endpoint dying and the fallback absorbing 100%
of the fleet's traffic looked exactly like a healthy deployment.

Nothing can be hooked on the failure, so the fix hooks the consequence: the fallback model is
constructed with a callback handler attached, and the fallback is invoked only after the primary
raised. One `on_chat_model_start` therefore *is* one failover, with no inference — which is the
claim this file drives against a real `RunnableWithFallbacks` rather than asserting off the source.
"""

from typing import Any

import httpx2
import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableLambda

from chemclaw.agent.llm_provider import (
    _failover_exceptions,
    _FallbackObserved,
    _openai_compatible_model,
    _with_failover,
)
from chemclaw.core.config import settings
from chemclaw.core.metrics import METRICS


def _endpoint_is_down() -> Exception:
    """A real `openai.APIConnectionError` — one of the two families the failover handles."""
    import openai

    return openai.APIConnectionError(
        request=httpx2.Request("POST", "https://primary.internal/v1/chat/completions")
    )


def test_a_failover_is_counted_and_logged_when_the_fallback_is_actually_asked(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Driven through a real `RunnableWithFallbacks`, because the claim is about *its* behaviour.

    A test that called `on_chat_model_start` directly would prove the handler works and say nothing
    about whether upstream ever reaches it — which is precisely the question, since upstream offers
    no hook on the failure itself.
    """
    before = METRICS.value("chemclaw_model_fallbacks_total")
    primary = RunnableLambda(lambda _messages: (_ for _ in ()).throw(_endpoint_is_down()))
    fallback = GenericFakeChatModel(
        messages=iter([AIMessage(content="answered by the second endpoint")]),
        callbacks=[_FallbackObserved()],
    )

    with caplog.at_level("WARNING"):
        answer = primary.with_fallbacks(
            [fallback], exceptions_to_handle=_failover_exceptions()
        ).invoke("what is the pKa")

    assert answer.content == "answered by the second endpoint"
    assert METRICS.value("chemclaw_model_fallbacks_total") == before + 1
    assert "model failover" in caplog.text


def test_a_healthy_primary_books_no_failover(caplog: pytest.LogCaptureFixture) -> None:
    """The negative case, and the one that makes the series mean anything.

    A counter that also moved when nothing failed over would report a permanent outage on every
    deployment that configured a fallback and never needed it.
    """
    before = METRICS.value("chemclaw_model_fallbacks_total")
    primary = GenericFakeChatModel(messages=iter([AIMessage(content="answered by the primary")]))
    fallback = GenericFakeChatModel(
        messages=iter([AIMessage(content="unused")]), callbacks=[_FallbackObserved()]
    )

    with caplog.at_level("WARNING"):
        primary.with_fallbacks([fallback], exceptions_to_handle=_failover_exceptions()).invoke("x")

    assert METRICS.value("chemclaw_model_fallbacks_total") == before
    assert "model failover" not in caplog.text


def test_only_the_fallback_endpoint_carries_the_observer(monkeypatch: pytest.MonkeyPatch) -> None:
    """The asymmetry *is* the signal, so it is pinned rather than left to the reader.

    An observer on the primary would fire on every model call and report a fleet permanently on its
    fallback — the same wrong answer as no observer at all, arrived at from the other side.
    """
    monkeypatch.setattr(settings, "llm_fallback_base_url", "https://fallback.internal/v1")
    primary = _openai_compatible_model("some-model")
    wrapped: Any = _with_failover(primary, "some-model")

    assert not _observers(primary), "the primary must not book a failover on every call"
    assert len(wrapped.fallbacks) == 1
    assert _observers(wrapped.fallbacks[0]) == 1


def test_no_configured_fallback_leaves_the_model_untouched(monkeypatch: pytest.MonkeyPatch) -> None:
    """Inert until an operator names a second endpoint — the shipped default is no fallback."""
    monkeypatch.setattr(settings, "llm_fallback_base_url", "")
    primary = _openai_compatible_model("some-model")
    assert _with_failover(primary, "some-model") is primary


def _observers(model: Any) -> int:
    """How many failover observers a constructed chat model carries."""
    return sum(
        1
        for handler in (getattr(model, "callbacks", None) or [])
        if isinstance(handler, _FallbackObserved)
    )
