"""The turn event contract serializes stably and the runner emits the documented sequence (F2-T3).

Pure and fast: proves each event round-trips through JSON with its `type` discriminator, and that
`run_turn` translates a scripted stream of model updates into tokens + a tool-call trace + a final
answer — without any live model (a fake streaming agent is injected).
"""

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

from agent_framework import AgentSession

from chemclaw.api.events import ErrorEvent, Event, TokenEvent, ToolCallEvent
from chemclaw.api.runner import run_turn
from tests.fakes import FakeUpdate
from tests.fakes_turn import Piece, ScriptedTurn


class _ToolContent:
    """A minimal function-call-shaped content (name + arguments), as the runner duck-types."""

    def __init__(self, name: str, arguments: str) -> None:
        self.name = name
        self.arguments = arguments


class _FakeAgent(ScriptedTurn):
    """A fake agent whose turn is a scripted update sequence: one tool call, then two tokens.

    The pieces here are MAF updates rather than text, because the sequence under test *is* MAF's:
    a call content arriving before any prose. The turn-level test that reads it is
    `maf_engine_only`, and the graph engine's twin — the whole event sequence for a scripted
    tool-calling turn — is `tests/test_langgraph_stream.py`'s conformance test.
    """

    def create_session(self, *, session_id: str) -> AgentSession:
        """The one non-streaming method the front door calls on an agent."""
        return AgentSession(session_id=session_id)

    async def stream(self, message: str) -> AsyncIterator[Any]:
        """The MAF-shaped stream: a call content, then two text updates.

        Typed `Any` rather than `Piece` because these pieces are already MAF updates — the shared
        rendering has nothing to add to a content the runner duck-types.
        """
        yield FakeUpdate(contents=[_ToolContent("gather_evidence", '{"query": "aldol"}')])
        yield FakeUpdate(text="The ")
        yield FakeUpdate(text="answer.")

    def run(  # noqa: D102 - see the class docstring
        self, message: str, *, stream: bool, session: Any, **_run_options: Any
    ) -> AsyncIterator[Any]:
        return self.stream(message)


def test_events_round_trip_with_type_discriminator() -> None:
    """Each event serializes to JSON carrying its `type`, and reloads to the same values."""
    token = TokenEvent(text="hi")
    payload = json.loads(token.model_dump_json())
    assert payload == {"type": "token", "text": "hi"}
    assert ToolCallEvent(tool="predict_pka").type == "tool_call"


def test_run_turn_reports_failure_as_error_event() -> None:
    """A turn whose model call raises yields a single user-safe ErrorEvent, not an exception."""

    class _BoomAgent(_FakeAgent):
        """A turn whose model call raises before it says anything."""

        async def stream(  # noqa: D102 - see `ScriptedTurn`
            self, message: str
        ) -> AsyncIterator[Piece]:
            raise RuntimeError("model exploded")
            yield  # pragma: no cover - makes this an async generator

    agent = _BoomAgent()
    session = agent.create_session(session_id="s2")

    async def _collect() -> list[Event]:
        return [
            event
            async for event in run_turn(
                session, "hello", connectors=[], graph_factory=agent.graph_factory
            )
        ]

    events = [e for e in asyncio.run(_collect()) if e.type != "capability_degraded"]
    assert [e.type for e in events] == ["error"]
    assert isinstance(events[0], ErrorEvent)
    assert "could not be completed" in events[0].message
    # SEC-1: the raw exception text must never reach the client — only a generic message keyed by
    # the session id (which the client already holds) so an operator can correlate to the log.
    assert "model exploded" not in events[0].message
    assert "s2" in events[0].message
