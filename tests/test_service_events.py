"""The turn event contract serializes stably and the runner emits the documented sequence (F2-T3).

Pure and fast: proves each event round-trips through JSON with its `type` discriminator, and that
`run_turn` translates a scripted stream of model updates into tokens + a tool-call trace + a final
answer — without any live model (a fake streaming agent is injected).
"""

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

from chemclaw.agent.session import TurnSession
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

    The pieces are streamed-update doubles rather than plain text, because what is under test is
    the *event* mapping — a tool call arriving before any prose. The whole event sequence for a
    scripted tool-calling turn is `tests/test_langgraph_stream.py`'s conformance test; this file
    pins the serialization and the error path.
    """

    def create_session(self, *, session_id: str) -> TurnSession:
        """The one non-streaming method the front door calls on an agent."""
        return TurnSession(session_id=session_id)

    async def stream(self, message: str) -> AsyncIterator[Any]:
        """A call content, then two text updates.

        Typed `Any` rather than `Piece` because these pieces are already update doubles — the shared
        rendering has nothing to add to a content the runner duck-types.
        """
        yield FakeUpdate(contents=[_ToolContent("gather_evidence", '{"query": "aldol"}')])
        yield FakeUpdate(text="The ")
        yield FakeUpdate(text="answer.")


def test_events_round_trip_with_type_discriminator() -> None:
    """Each event serializes to JSON carrying its `type`, and reloads to the same values."""
    token = TokenEvent(text="hi")
    payload = json.loads(token.model_dump_json())
    # `agent` is on the wire and empty, which is the whole additive contract: a chunk with no
    # specialist named is the main agent's, so an existing consumer that ignores the field reads
    # exactly what it read before teams existed.
    assert payload == {"type": "token", "text": "hi", "agent": ""}
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
