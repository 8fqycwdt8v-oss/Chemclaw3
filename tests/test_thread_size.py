"""A turn is not admitted onto a conversation larger than the front door can load twelve of.

Every turn loads its whole checkpointed thread — compaction trims only what is *sent* — so what an
admitted turn costs the pod grows with the conversation it continues, and twelve permits on 10 MB
threads OOM-killed a 1Gi front door at turn 76
(`D-2026-09-24-a-turn-costs-the-thread-it-loads`). `session_max_thread_bytes` is the bound, read off
the stored thread so a restart or a second replica cannot hand it a fresh allowance the way it
hands the in-process turn caps one.
"""

from collections.abc import AsyncIterator
from typing import Any, cast

import pytest
from langchain_core.messages import HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, MessagesState, StateGraph

from chemclaw.agent import checkpointer as checkpointer_module
from chemclaw.agent.checkpointer import checkpointer, close_checkpointer, stored_thread_bytes
from chemclaw.agent.state import turn_config
from chemclaw.api.budget import BudgetExceeded, check_thread_size
from chemclaw.core.config import settings
from chemclaw.core.metrics import METRICS
from tests.fakes_turn import Piece
from tests.pg import create_checkpoint_tables, migrated_db_or_skip


def _echo_graph(saver: Any) -> Any:
    """The smallest graph that writes a `messages` channel through the real saver."""
    graph = StateGraph(MessagesState)
    graph.add_node("noop", lambda state: {})
    graph.add_edge(START, "noop")
    graph.add_edge("noop", END)
    return graph.compile(checkpointer=saver)


async def test_the_stored_size_is_the_newest_blob_a_turn_would_load(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Measured off real checkpoint writes: it grows with the thread and names the *newest* copy.

    The newest, because `checkpoint_retain_per_thread` keeps superseded copies beside it and a sum
    over them would refuse a thread for history no turn loads. And 0 for a thread with no
    checkpoint, which is every session's first turn.
    """
    monkeypatch.setattr(settings, "session_store", "postgres")
    await migrated_db_or_skip()
    await create_checkpoint_tables()
    await close_checkpointer()
    thread = "sess-thread-size"
    try:
        assert await stored_thread_bytes(thread) == 0
        graph = _echo_graph(await checkpointer())
        config = cast("RunnableConfig", turn_config(thread))
        await graph.ainvoke({"messages": [HumanMessage("x" * 50_000)]}, config)
        first = await stored_thread_bytes(thread)
        await graph.ainvoke({"messages": [HumanMessage("y" * 50_000)]}, config)
        second = await stored_thread_bytes(thread)
    finally:
        await close_checkpointer()

    assert 50_000 <= first < 60_000, f"one 50,000-char message stored as {first} bytes"
    assert 100_000 <= second < 120_000, (
        f"two 50,000-char messages stored as {second} bytes — the read is not the newest blob, or "
        "it is summing the superseded copies the prune keeps beside it"
    )


async def test_a_thread_at_its_ceiling_is_refused_and_one_below_it_is_not(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The comparison, in both directions, and the message that tells the chemist what to do."""
    monkeypatch.setattr(settings, "session_max_thread_bytes", 1000)

    async def _stored(size: int) -> Any:
        async def _read(thread_id: str) -> int:
            return size

        return _read

    monkeypatch.setattr(checkpointer_module, "stored_thread_bytes", await _stored(999))
    await check_thread_size("s")
    monkeypatch.setattr(checkpointer_module, "stored_thread_bytes", await _stored(1000))
    with pytest.raises(BudgetExceeded, match="Start a new session"):
        await check_thread_size("s")


async def test_it_binds_with_budgets_off_and_is_disabled_only_by_its_own_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A memory bound, not a cost one: `budget_enabled` does not switch it off; its own 0 does."""

    async def _huge(thread_id: str) -> int:
        return 10**9

    monkeypatch.setattr(checkpointer_module, "stored_thread_bytes", _huge)
    monkeypatch.setattr(settings, "budget_enabled", False)
    monkeypatch.setattr(settings, "session_max_thread_bytes", 1)
    with pytest.raises(BudgetExceeded):
        await check_thread_size("s")
    monkeypatch.setattr(settings, "session_max_thread_bytes", 0)
    await check_thread_size("s")


async def test_a_size_that_cannot_be_read_admits_the_turn_and_is_counted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The load that follows reads the same database, so refusing here would add an outage."""

    async def _down(thread_id: str) -> int:
        raise ConnectionError("database unreachable")

    monkeypatch.setattr(checkpointer_module, "stored_thread_bytes", _down)
    monkeypatch.setattr(settings, "session_max_thread_bytes", 1)
    before = METRICS.value("chemclaw_degraded_total")
    await check_thread_size("s")
    assert METRICS.value("chemclaw_degraded_total") == before + 1


def test_the_front_door_refuses_an_oversize_thread_as_a_spent_session_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On the open stream, `budget_exhausted` and not retryable — and the agent never runs.

    `budget_exhausted` rather than a new code because it already means "this session was refused
    before the turn started and has no answer with it", which is exactly this, and a surface that
    switches on it already stops offering a retry that would fail identically.
    """
    from tests.test_service import _client, _FakeAgent

    asked: list[str] = []

    class _Recording(_FakeAgent):
        """Remembers every message it was asked to answer."""

        async def stream(self, message: str) -> AsyncIterator[Piece]:
            asked.append(message)
            yield "answered anyway"

    async def _huge(thread_id: str) -> int:
        return 10**9

    monkeypatch.setattr(checkpointer_module, "stored_thread_bytes", _huge)
    agent = _Recording()
    with _client(agent) as client:
        session_id = client.post("/sessions").json()["session_id"]
        with client.stream(
            "POST", f"/sessions/{session_id}/messages", json={"message": "hi"}
        ) as res:
            body = "".join(res.iter_lines())
    assert '"code":"budget_exhausted"' in body.replace(" ", ""), body
    assert '"retryable":false' in body.replace(" ", ""), body
    assert "Start a new session" in body
    assert asked == [], "the refused turn still ran"
