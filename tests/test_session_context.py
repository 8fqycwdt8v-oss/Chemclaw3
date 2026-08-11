"""Ambient session-id plumbing for job push-back (plan Phase F3-T3).

Proves the contextvar carrier, that a durable launcher stamps the current session onto the job (so
the completing workflow knows whom to notify), and that the runner sets/clears the ambient id
around a turn — all offline with fakes (no Temporal, no database).

The launcher under test is the *generated* one (`chemclaw.connectors.jobs`), which is now the only
kind: the
hand-written QM launcher that used to carry this plumbing became a declared job in D-118.
"""

import asyncio
from collections.abc import AsyncIterator
from typing import Any

from agent_framework import AgentSession

import chemclaw.connectors.jobs as connector_jobs
from chemclaw.api.runner import run_turn
from chemclaw.connectors.jobs import build_job_tool, job_workflow_id
from chemclaw.connectors.manifest import JobSpec
from chemclaw.core.session_context import (
    get_current_session_id,
    reset_current_session_id,
    set_current_session_id,
)
from tests.fakes_turn import Piece, ScriptedTurn

_SPEC = JobSpec.model_validate(
    {
        "name": "compute_dft_energy",
        "workflow": "QMJobWorkflow",
        "summary": "Run a durable DFT calculation.",
        "params_model": "chemclaw.connectors.qm.specs:QmJobSpec",
    }
)


def test_session_id_does_not_affect_the_job_id() -> None:
    """Two launches differing only by session share one id — identical science is deduped (D-011).

    The id is derived from the *payload*, which is exactly the model-authored arguments; the
    session is ambient and never enters it. So two chemists asking the same question in two chats
    join one run, and each still gets woken (the wrapper carries the session beside the payload).
    """
    payload = {"molecule_smiles": "CCO", "method": "B3LYP", "basis_set": "def2-SVP"}
    assert job_workflow_id("qm", "compute_dft_energy", payload) == job_workflow_id(
        "qm", "compute_dft_energy", dict(payload)
    )


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


def test_a_durable_launch_stamps_the_current_session(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """The generated launcher copies the ambient session id onto the wrapper's input.

    Without it the completing job has no chat to wake, so the chemist would have to poll a
    long-running calculation by hand — the push-back channel exists precisely to avoid that.
    """
    client = _CapturingClient()

    async def _fake_connect() -> _CapturingClient:
        return client

    monkeypatch.setattr(connector_jobs, "connect", _fake_connect)
    tool = build_job_tool("qm", _SPEC)
    params = tool.__annotations__["params"]

    async def _run() -> None:
        token = set_current_session_id("sess-42")
        try:
            await tool(
                params(molecule_smiles="CCO", method="B3LYP", basis_set="def2-SVP"),
                "confirm the barrier before proposing the route",
            )
        finally:
            reset_current_session_id(token)

    asyncio.run(_run())
    assert client.started and client.started[0].session_id == "sess-42"


class _EchoSessionAgent(ScriptedTurn):
    """A fake agent whose turn echoes the ambient session id the runner stamped."""

    def create_session(self, *, session_id: str) -> AgentSession:
        """The one non-streaming method the front door calls on an agent."""
        return AgentSession(session_id=session_id)

    async def stream(self, message: str) -> AsyncIterator[Piece]:  # noqa: D102 - see the base class
        yield get_current_session_id() or "NONE"


def test_runner_stamps_and_clears_session() -> None:
    """`run_turn` makes the session id ambient during the turn and clears it afterward."""
    agent = _EchoSessionAgent()
    session = agent.create_session(session_id="sess-run")

    async def _collect() -> list[Any]:
        return [
            event
            async for event in run_turn(
                session, "hi", connectors=[], graph_factory=agent.graph_factory
            )
        ]

    events = asyncio.run(_collect())
    answer = next(e for e in events if e.type == "answer")
    assert answer.text == "sess-run"  # the tool-facing ambient id was set during the turn
    assert get_current_session_id() is None  # and cleared after
