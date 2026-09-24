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
        # The newest checkpoint's blob gone — the torn state a sweep racing a turn leaves — must
        # read as nothing, never as the older copy the prune kept beside it.
        async with (await checkpointer_module._checkpoint_pool()).connection() as conn:
            await conn.execute(
                "DELETE FROM checkpoint_blobs b USING (SELECT checkpoint FROM checkpoints"
                " WHERE thread_id = %(t)s AND checkpoint_ns = '' ORDER BY checkpoint_id DESC"
                " LIMIT 1) AS newest WHERE b.thread_id = %(t)s AND b.channel = 'messages'"
                " AND b.version = newest.checkpoint -> 'channel_versions' ->> 'messages'",
                {"t": thread},
            )
        torn = await stored_thread_bytes(thread)
    finally:
        await close_checkpointer()

    assert 50_000 <= first < 60_000, f"one 50,000-char message stored as {first} bytes"
    assert 100_000 <= second < 120_000, (
        f"two 50,000-char messages stored as {second} bytes — the read is not the newest blob, or "
        "it is summing the superseded copies the prune keeps beside it"
    )
    assert torn == 0, (
        f"a newest checkpoint with no blob read as {torn} bytes — an older copy's size"
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


def _recording_client(monkeypatch: pytest.MonkeyPatch, sizes: list[int]) -> tuple[Any, list[str]]:
    """A front door whose thread-size reads answer `sizes` in turn, and the messages it answered."""
    from tests.test_service import _client, _FakeAgent

    asked: list[str] = []

    class _Recording(_FakeAgent):
        """Remembers every message it was asked to answer."""

        async def stream(self, message: str) -> AsyncIterator[Piece]:
            asked.append(message)
            yield "answered anyway"

    reads = iter(sizes)

    async def _stored(thread_id: str) -> int:
        return next(reads)

    monkeypatch.setattr(checkpointer_module, "stored_thread_bytes", _stored)
    monkeypatch.setattr(settings, "session_max_thread_bytes", 1000)
    return _client(_Recording()), asked


def test_a_spent_thread_is_refused_at_the_door_before_it_claims_or_queues(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A clean 429 at request entry, counted apart from the token budget, and nothing runs.

    Before the entry check it took the durable claim and could wait for a permit — and be shed
    `at_capacity`, which names the wrong limit — before the stream said anything true.
    """
    client, asked = _recording_client(monkeypatch, [10**9])
    before = METRICS.value("chemclaw_turns_refused_thread_size_total")
    budget_before = METRICS.value("chemclaw_turns_refused_budget_total")
    with client:
        session_id = client.post("/sessions").json()["session_id"]
        res = client.post(f"/sessions/{session_id}/messages", json={"message": "hi"})
    assert res.status_code == 429
    assert "Start a new session" in res.text
    assert asked == [], "the refused turn still ran"
    assert METRICS.value("chemclaw_turns_refused_thread_size_total") == before + 1
    assert METRICS.value("chemclaw_turns_refused_budget_total") == budget_before, (
        "a thread-size refusal was booked on the token budget's counter, whose alert tells an "
        "operator to raise a window that does not clear it"
    )


def test_the_check_under_the_permit_is_the_one_that_binds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A thread that crosses its ceiling while its turn queued is refused on the stream.

    `budget_exhausted` and not retryable — the code already means "this session was refused before
    the turn started and has no answer with it", so a surface stops offering a retry that would
    fail identically — and the agent never runs, so the thread is never loaded.
    """
    client, asked = _recording_client(monkeypatch, [0, 10**9])
    with client:
        session_id = client.post("/sessions").json()["session_id"]
        with client.stream(
            "POST", f"/sessions/{session_id}/messages", json={"message": "hi"}
        ) as res:
            body = "".join(res.iter_lines()).replace(" ", "")
    assert '"code":"budget_exhausted"' in body, body
    assert '"retryable":false' in body, body
    assert "Startanewsession" in body
    assert asked == [], "the refused turn still ran"
