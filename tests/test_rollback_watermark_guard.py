"""A disarmed rollback guard must be countable, not only logged.

The rollback watermark (D-107) is the durable half of the turn snapshot: without it, a client
disconnect mid-tool-call leaves an orphaned `tool_use` in `session_messages` and every later turn
on that session is rejected by the model — one dropped connection permanently bricks the
conversation. Reading it is deliberately non-fatal, because failing the turn would trade a
conditional future fault for a certain immediate one.

The load test showed the cost of that being *silent*: 32 turns in 126 seconds ran unguarded and
only a WARNING said so. These tests pin the observable half — the counter moves, the log is an
ERROR, and the turn still succeeds.
"""

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest
from agent_framework import AgentSession

from chemclaw.api.events import AnswerEvent
from chemclaw.api.runner import run_turn
from chemclaw.core.metrics import METRICS
from tests.fakes_turn import Piece, ScriptedTurn


class _SilentAgent(ScriptedTurn):
    """A fake agent whose turn produces one chunk of text and no tool calls."""

    async def stream(self, message: str) -> AsyncIterator[Piece]:  # noqa: D102 - see the base class
        yield "done"


class _UnreachableHistory:
    """A durable history provider whose watermark read fails the way a busy database does."""

    async def latest_message_id(self, session_id: str) -> int | None:
        """Fail exactly as `chemclaw.core.db` does when a connection cannot be obtained in time."""
        raise ConnectionError("Postgres unreachable at postgresql://h/db: connection timeout")


def _run(history: Any) -> list[Any]:
    """Drive one turn to completion and return its events."""
    agent = _SilentAgent()

    async def _collect() -> list[Any]:
        return [
            event
            async for event in run_turn(
                AgentSession(session_id="s-watermark"),
                "hi",
                history=history,
                connectors=[],
                graph_factory=agent.graph_factory,
            )
        ]

    return asyncio.run(_collect())


def test_an_unreadable_watermark_is_counted_and_logged_as_an_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The turn still succeeds, but the counter moves and the log says the guard is off."""
    before = METRICS.value("chemclaw_rollback_watermark_unavailable_total")

    with caplog.at_level("ERROR"):
        events = _run(_UnreachableHistory())

    assert METRICS.value("chemclaw_rollback_watermark_unavailable_total") == before + 1
    assert any(isinstance(event, AnswerEvent) for event in events)  # non-fatal, by design
    assert any("WITHOUT the durable-history rollback guard" in r.message for r in caplog.records)


def test_a_readable_watermark_leaves_the_counter_alone() -> None:
    """The healthy path must not inflate the counter, or the signal is unalertable."""

    class _WorkingHistory:
        """A history provider whose watermark read succeeds."""

        async def latest_message_id(self, session_id: str) -> int | None:
            """Return a watermark, as a reachable session store does."""
            return 7

    before = METRICS.value("chemclaw_rollback_watermark_unavailable_total")
    _run(_WorkingHistory())
    assert METRICS.value("chemclaw_rollback_watermark_unavailable_total") == before
