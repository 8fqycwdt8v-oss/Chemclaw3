"""A durable job's result can reach the same turn that launched it (gap AGT-2).

The system's defining interaction — "compute this, then reason about the result" — was split in
two: a tool returned a job id, the turn ended, and the result arrived as push-back the session only
picked up on its *next* turn. Both halves of the machinery existed (the F3-T3 mailbox, the D-058
todo flip); the missing piece was a bounded wait a live turn could perform.

The properties that matter are the failure modes, not the happy path: the wait is opt-in, bounded,
non-recursive, and degrades to the *previous* behavior (result on the next turn) rather than to an
error — a database blip must not cost a chemist an answer the model already produced.
"""

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from agent_framework import AgentSession

from agents.turn_signals import record_job_started
from chemclaw.config import settings
from service.runner import run_turn


class _JobLaunchingAgent:
    """Streams a first pass that launches a job, then a continuation pass."""

    mcp_tools: list[Any] = []

    def __init__(self, job_id: str) -> None:
        self._job_id = job_id
        self.messages: list[str] = []

    def run(  # noqa: D102 - a fake agent's run, documented by its class
        self,
        message: str,
        *,
        stream: bool,
        session: AgentSession,
        **_run_options: Any,
    ) -> Any:
        self.messages.append(message)
        first = len(self.messages) == 1
        job_id = self._job_id

        async def _gen() -> Any:
            if first:
                record_job_started(job_id, "qm")
                yield SimpleNamespace(text="starting", contents=[], user_input_requests=[])
            else:
                yield SimpleNamespace(
                    text=" the energy is -154.1", contents=[], user_input_requests=[]
                )

        return _gen()


def _events(agent: Any) -> list[Any]:
    async def _collect() -> list[Any]:
        return [e async for e in run_turn(agent, AgentSession(session_id="s1"), "compute it")]

    return asyncio.run(_collect())


@pytest.fixture
def enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """Turn the (default-off) resume on for the tests that exercise it."""
    monkeypatch.setattr(settings, "mid_turn_resume_enabled", True)
    monkeypatch.setattr(settings, "mid_turn_resume_timeout_seconds", 5.0)


def test_the_result_reaches_the_same_turn(monkeypatch: pytest.MonkeyPatch, enabled: None) -> None:
    """One exchange, not two — the whole point of the gap."""

    async def _fake_wait(session_id: str, job_ids: list[str], *, timeout_seconds: float) -> Any:
        return {job_ids[0]: {"energy_hartree": -154.1}}

    monkeypatch.setattr("service.runner.await_job_results", _fake_wait)
    agent = _JobLaunchingAgent("qm-1")
    events = _events(agent)

    assert len(agent.messages) == 2, "the turn did not continue after the job completed"
    answer = next(e for e in events if e.type == "answer")
    assert "starting" in answer.text and "-154.1" in answer.text


def test_the_result_is_handed_to_the_model_as_framed_data(
    monkeypatch: pytest.MonkeyPatch, enabled: None
) -> None:
    """A workflow result is untrusted input, so the same framing as retrieved notes applies."""

    async def _fake_wait(session_id: str, job_ids: list[str], *, timeout_seconds: float) -> Any:
        return {"qm-1": {"energy_hartree": -154.1}}

    monkeypatch.setattr("service.runner.await_job_results", _fake_wait)
    agent = _JobLaunchingAgent("qm-1")
    _events(agent)
    continuation = agent.messages[1]
    assert "<retrieved-note" in continuation or "job-results" in continuation


def test_a_timeout_degrades_to_the_previous_behavior(
    monkeypatch: pytest.MonkeyPatch, enabled: None
) -> None:
    """No result in time is not an error: push-back still delivers it on the next turn."""

    async def _no_results(session_id: str, job_ids: list[str], *, timeout_seconds: float) -> Any:
        return {}

    monkeypatch.setattr("service.runner.await_job_results", _no_results)
    agent = _JobLaunchingAgent("qm-1")
    events = _events(agent)
    assert len(agent.messages) == 1  # no continuation
    assert events[-1].type == "answer"
    assert not [e for e in events if e.type == "error"]


def test_the_wait_is_off_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Holding a turn open holds an admission permit, so a deployment opts in deliberately."""
    assert settings.mid_turn_resume_enabled is False
    called = []

    async def _spy(session_id: str, job_ids: list[str], *, timeout_seconds: float) -> Any:
        called.append(job_ids)
        return {}

    monkeypatch.setattr("service.runner.await_job_results", _spy)
    agent = _JobLaunchingAgent("qm-1")
    _events(agent)
    assert called == [], "the resume ran without being enabled"
    assert len(agent.messages) == 1


def test_a_turn_that_starts_no_job_never_waits(
    monkeypatch: pytest.MonkeyPatch, enabled: None
) -> None:
    """An ordinary question must not pay a wait for jobs it never launched."""
    called = []

    async def _spy(session_id: str, job_ids: list[str], *, timeout_seconds: float) -> Any:
        called.append(job_ids)
        return {}

    monkeypatch.setattr("service.runner.await_job_results", _spy)

    class _PlainAgent:
        mcp_tools: list[Any] = []
        messages: list[str] = []

        def run(  # noqa: D102 - a fake agent's run, documented by its class
            self,
            message: str,
            *,
            stream: bool,
            session: AgentSession,
            **_run_options: Any,
        ) -> Any:
            async def _gen() -> Any:
                yield SimpleNamespace(text="an answer", contents=[], user_input_requests=[])

            return _gen()

    _events(_PlainAgent())
    assert called == []


def test_the_resume_is_not_recursive(monkeypatch: pytest.MonkeyPatch, enabled: None) -> None:
    """A continuation that starts another job must not chain another wait.

    Otherwise one chemist turn could hold an admission permit indefinitely by launching a job from
    each continuation — the runaway the whole admission-control design exists to prevent.
    """
    waits = []

    async def _fake_wait(session_id: str, job_ids: list[str], *, timeout_seconds: float) -> Any:
        waits.append(job_ids)
        return {job_ids[0]: {"ok": True}}

    monkeypatch.setattr("service.runner.await_job_results", _fake_wait)

    class _AlwaysLaunching:
        mcp_tools: list[Any] = []

        def __init__(self) -> None:
            self.messages: list[str] = []

        def run(  # noqa: D102 - a fake agent's run, documented by its class
            self,
            message: str,
            *,
            stream: bool,
            session: AgentSession,
            **_run_options: Any,
        ) -> Any:
            self.messages.append(message)
            index = len(self.messages)

            async def _gen() -> Any:
                record_job_started(f"qm-{index}", "qm")
                yield SimpleNamespace(text=f"pass{index}", contents=[], user_input_requests=[])

            return _gen()

    agent = _AlwaysLaunching()
    _events(agent)
    assert len(waits) == 1, "the resume waited more than once in a single turn"
    assert len(agent.messages) == 2
