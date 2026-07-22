"""The per-turn runner's answer-verification wiring (plan F10-B2), driven with a fake agent.

Proves the runner stamps the verifier's confidence + unsupported claims on the final `AnswerEvent`
when verification is on, emits today's plain answer when it is off, and never lets a verifier
failure sink the turn. The verifier is faked here (it has its own offline tests) so no model runs.
"""

import asyncio
from typing import Any

import pytest
from agent_framework import AgentSession

import service.runner as runner
from agents.verifier import ClaimCheck, VerificationResult
from service.events import AnswerEvent


class _Update:
    def __init__(self, text: str = "") -> None:
        self.text = text
        self.contents: list[object] = []
        self.user_input_requests: list[object] = []


class _FakeAgent:
    """Yields a two-token answer; no MCP tools to open."""

    mcp_tools: list[object] = []

    def run(self, message: str, *, stream: bool, session: AgentSession) -> Any:
        async def _gen() -> Any:
            yield _Update(text="Yield was 90% ")
            yield _Update(text="[[reaction-a]].")

        return _gen()


def _run_turn(message: str = "q") -> list[Any]:
    async def _collect() -> list[Any]:
        session = AgentSession(session_id="s-1")
        return [event async for event in runner.run_turn(_FakeAgent(), session, message)]

    return asyncio.run(_collect())


def _answer(events: list[Any]) -> AnswerEvent:
    return next(e for e in events if isinstance(e, AnswerEvent))


def test_answer_is_unscored_when_verification_is_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verifier off (default): the final answer carries no confidence — today's behavior exactly."""
    from chemclaw.config import settings

    monkeypatch.setattr(settings, "verifier_enabled", False)
    answer = _answer(_run_turn())
    assert answer.text == "Yield was 90% [[reaction-a]]."
    assert answer.confidence is None and answer.unsupported_claims == []


def test_low_confidence_answer_is_flagged(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verifier on: a low-confidence verdict stamps confidence + the unsupported claim texts."""
    from chemclaw.config import settings

    monkeypatch.setattr(settings, "verifier_enabled", True)

    async def _fake_verify(answer: str, **_: Any) -> VerificationResult:
        return VerificationResult(
            claims=[ClaimCheck(text="Yield was 90%", supported=False)], confidence=0.2
        )

    monkeypatch.setattr(runner, "verify_turn_answer", _fake_verify)
    answer = _answer(_run_turn())
    assert answer.confidence == 0.2
    assert answer.unsupported_claims == ["Yield was 90%"]


def test_verifier_failure_degrades_to_plain_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verifier on but raising: the turn still returns its answer, unscored — never a sunk turn."""
    from chemclaw.config import settings

    monkeypatch.setattr(settings, "verifier_enabled", True)

    async def _boom(answer: str, **_: Any) -> VerificationResult:
        raise RuntimeError("verifier down")

    monkeypatch.setattr(runner, "verify_turn_answer", _boom)
    answer = _answer(_run_turn())
    assert answer.text == "Yield was 90% [[reaction-a]]."
    assert answer.confidence is None and answer.unsupported_claims == []
