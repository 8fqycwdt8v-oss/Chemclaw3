"""Ambient session-id plumbing for job push-back (plan Phase F3-T3).

Proves the contextvar carrier, that `submit_qm_job` stamps the current session onto the durable
job (so the completing workflow knows whom to notify), and that the runner sets/clears the ambient
id around a turn — all offline with fakes (no Temporal, no database).
"""

import asyncio
from typing import Any

from agent_framework import AgentSession

import agents.qm_tools as qm_tools
from agents.session_context import (
    get_current_session_id,
    reset_current_session_id,
    set_current_session_id,
)
from service.runner import run_turn


def test_session_id_does_not_affect_the_job_cache_key() -> None:
    """Two jobs differing only by session share one key — identical science is deduped (D-011)."""
    from workflows.models import QMJobInput, qm_job_key

    base = {"molecule_smiles": "CCO", "method": "B3LYP", "basis_set": "def2-SVP"}
    a = QMJobInput(**base, session_id="sess-1")
    b = QMJobInput(**base, session_id="sess-2")
    assert qm_job_key(a) == qm_job_key(b)


def test_contextvar_set_get_reset() -> None:
    """The session id sets, reads back, and resets to the prior value."""
    assert get_current_session_id() is None
    token = set_current_session_id("sess-A")
    assert get_current_session_id() == "sess-A"
    reset_current_session_id(token)
    assert get_current_session_id() is None


class _FakeHandle:
    def __init__(self, workflow_id: str) -> None:
        self.id = workflow_id


class _CapturingClient:
    """A fake Temporal client that records the job input handed to start_workflow."""

    def __init__(self) -> None:
        self.started: list[Any] = []

    async def start_workflow(self, _run: Any, job: Any, *, id: str, **_: Any) -> _FakeHandle:
        self.started.append(job)
        return _FakeHandle(id)


def test_submit_qm_job_stamps_current_session(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """`submit_qm_job` copies the ambient session id onto the durable job input."""
    client = _CapturingClient()

    async def _fake_connect() -> _CapturingClient:
        return client

    monkeypatch.setattr(qm_tools, "connect", _fake_connect)

    async def _run() -> None:
        token = set_current_session_id("sess-42")
        try:
            await qm_tools.submit_qm_job("CCO", "B3LYP", "def2-SVP")
        finally:
            reset_current_session_id(token)

    asyncio.run(_run())
    assert client.started and client.started[0].session_id == "sess-42"


class _Update:
    def __init__(self, text: str = "") -> None:
        self.text = text
        self.contents: list[object] = []
        self.user_input_requests: list[object] = []


class _EchoSessionAgent:
    """A fake agent whose turn echoes the ambient session id the runner stamped."""

    mcp_tools: list[object] = []

    def create_session(self, *, session_id: str) -> AgentSession:
        return AgentSession(session_id=session_id)

    def run(self, message: str, *, stream: bool, session: AgentSession) -> object:
        async def _gen() -> object:
            yield _Update(text=get_current_session_id() or "NONE")

        return _gen()


def test_runner_stamps_and_clears_session() -> None:
    """`run_turn` makes the session id ambient during the turn and clears it afterward."""
    agent = _EchoSessionAgent()
    session = agent.create_session(session_id="sess-run")

    async def _collect() -> list[Any]:
        return [event async for event in run_turn(agent, session, "hi")]

    events = asyncio.run(_collect())
    answer = next(e for e in events if e.type == "answer")
    assert answer.text == "sess-run"  # the tool-facing ambient id was set during the turn
    assert get_current_session_id() is None  # and cleared after
