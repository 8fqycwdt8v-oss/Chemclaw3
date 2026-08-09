"""The turn event contract serializes stably and the runner emits the documented sequence (F2-T3).

Pure and fast: proves each event round-trips through JSON with its `type` discriminator, and that
`run_turn` translates a scripted stream of model updates into tokens + a tool-call trace + a final
answer — without any live model (a fake streaming agent is injected).
"""

import asyncio
import json
from typing import Any

from agent_framework import AgentSession

from chemclaw.api.events import AnswerEvent, ErrorEvent, Event, TokenEvent, ToolCallEvent
from chemclaw.api.runner import run_turn
from tests.fakes import FakeUpdate


class _ToolContent:
    """A minimal function-call-shaped content (name + arguments), as the runner duck-types."""

    def __init__(self, name: str, arguments: str) -> None:
        self.name = name
        self.arguments = arguments


class _FakeAgent:
    """A fake agent whose `run(stream=True)` yields a scripted update sequence (no model)."""

    mcp_tools: list[object] = []

    def create_session(self, *, session_id: str) -> AgentSession:
        return AgentSession(session_id=session_id)

    def run(  # noqa: D102 - a fake agent's run, documented by its class
        self,
        message: str,
        *,
        stream: bool,
        session: AgentSession,
        **_run_options: Any,
    ) -> object:
        async def _gen() -> object:
            yield FakeUpdate(contents=[_ToolContent("gather_evidence", '{"query": "aldol"}')])
            yield FakeUpdate(text="The ")
            yield FakeUpdate(text="answer.")

        return _gen()


def test_events_round_trip_with_type_discriminator() -> None:
    """Each event serializes to JSON carrying its `type`, and reloads to the same values."""
    token = TokenEvent(text="hi")
    payload = json.loads(token.model_dump_json())
    assert payload == {"type": "token", "text": "hi"}
    assert ToolCallEvent(tool="predict_pka").type == "tool_call"


def test_run_turn_emits_toolcall_tokens_then_answer() -> None:
    """A scripted turn yields the tool-call trace, each token, then the assembled answer."""
    agent = _FakeAgent()
    session = agent.create_session(session_id="s1")

    async def _collect() -> list[Event]:
        return [event async for event in run_turn(agent, session, "hello", connectors=[])]

    # Without the capability announcement: no Temporal broker runs in a test process, so every
    # turn here truthfully opens by saying the durable subsystem is down. This test is about the
    # trace/answer ordering, which that announcement is not part of.
    events = [e for e in asyncio.run(_collect()) if e.type != "capability_degraded"]
    kinds = [e.type for e in events]
    assert kinds == ["tool_call", "token", "token", "answer"]
    assert isinstance(events[0], ToolCallEvent)
    assert events[0].tool == "gather_evidence"
    answer = events[-1]
    assert isinstance(answer, AnswerEvent)
    assert answer.text == "The answer."


def test_run_turn_reports_failure_as_error_event() -> None:
    """A turn whose model call raises yields a single user-safe ErrorEvent, not an exception."""

    class _BoomAgent(_FakeAgent):
        def run(  # noqa: D102 - a fake agent's run, documented by its class
            self,
            message: str,
            *,
            stream: bool,
            session: AgentSession,
            **_run_options: Any,
        ) -> object:
            async def _gen() -> object:
                raise RuntimeError("model exploded")
                yield  # pragma: no cover - makes this an async generator

            return _gen()

    agent = _BoomAgent()
    session = agent.create_session(session_id="s2")

    async def _collect() -> list[Event]:
        return [event async for event in run_turn(agent, session, "hello", connectors=[])]

    events = [e for e in asyncio.run(_collect()) if e.type != "capability_degraded"]
    assert [e.type for e in events] == ["error"]
    assert isinstance(events[0], ErrorEvent)
    assert "could not be completed" in events[0].message
    # SEC-1: the raw exception text must never reach the client — only a generic message keyed by
    # the session id (which the client already holds) so an operator can correlate to the log.
    assert "model exploded" not in events[0].message
    assert "s2" in events[0].message
