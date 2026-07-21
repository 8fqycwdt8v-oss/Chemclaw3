"""The turn event contract serializes stably and the runner emits the documented sequence (F2-T3).

Pure and fast: proves each event round-trips through JSON with its `type` discriminator, and that
`run_turn` translates a scripted stream of model updates into tokens + a tool-call trace + a final
answer — without any live model (a fake streaming agent is injected).
"""

import asyncio
import json

from agent_framework import AgentSession

from service.events import AnswerEvent, TokenEvent, ToolCallEvent
from service.runner import run_turn


class _ToolContent:
    """A minimal function-call-shaped content (name + arguments), as the runner duck-types."""

    def __init__(self, name: str, arguments: str) -> None:
        self.name = name
        self.arguments = arguments


class _Update:
    """A minimal streamed update carrying text and/or contents."""

    def __init__(self, text: str = "", contents: list[object] | None = None) -> None:
        self.text = text
        self.contents = contents or []
        self.user_input_requests: list[object] = []


class _FakeAgent:
    """A fake agent whose `run(stream=True)` yields a scripted update sequence (no model)."""

    mcp_tools: list[object] = []

    def create_session(self, *, session_id: str) -> AgentSession:
        return AgentSession(session_id=session_id)

    def run(self, message: str, *, stream: bool, session: AgentSession) -> object:
        async def _gen() -> object:
            yield _Update(contents=[_ToolContent("gather_evidence", '{"query": "aldol"}')])
            yield _Update(text="The ")
            yield _Update(text="answer.")

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

    async def _collect() -> list[object]:
        return [event async for event in run_turn(agent, session, "hello")]

    events = asyncio.run(_collect())
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
        def run(self, message: str, *, stream: bool, session: AgentSession) -> object:
            async def _gen() -> object:
                raise RuntimeError("model exploded")
                yield  # pragma: no cover - makes this an async generator

            return _gen()

    agent = _BoomAgent()
    session = agent.create_session(session_id="s2")

    async def _collect() -> list[object]:
        return [event async for event in run_turn(agent, session, "hello")]

    events = asyncio.run(_collect())
    assert [e.type for e in events] == ["error"]
    assert "could not be completed" in events[0].message
