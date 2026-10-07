"""The two records of one conversation, observed together across one teardown.

`session_messages` is what a chemist sees; the checkpointer's thread is what the model is built
from next turn. A teardown between the graph run and `_record_transcript` leaves the answer in the
model's record only; the question, written ahead of the turn, is in both and marked `stopped`.
The window is held open by blocking `build_answer_event`; the graph, saver, transcript provider
and `run_turn` are real.
"""

import asyncio
from typing import Any, cast

import pytest
from langchain_core.language_models import GenericFakeChatModel
from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableConfig

from chemclaw.agent.checkpointer import checkpointer, close_checkpointer
from chemclaw.agent.langgraph_agent import build_langgraph_agent
from chemclaw.agent.session import TurnSession
from chemclaw.agent.session_store import (
    PostgresHistoryProvider,
    SessionOwnerStore,
    stored_turn_status,
)
from chemclaw.agent.state import turn_config
from chemclaw.api.runner import run_turn
from chemclaw.core.config import settings
from chemclaw.core.metrics import METRICS
from tests.pg import create_checkpoint_tables, migrated_db_or_skip

_SESSION = "sess-divergence"


class _Model(GenericFakeChatModel):
    """A model that answers once; `bind_tools` returns itself, as the graph build requires."""

    def bind_tools(self, tools: Any, **kwargs: Any) -> Any:
        """Bind nothing — this turn calls no tool."""
        return self


def _factory(**kwargs: Any) -> Any:
    """The real compiled graph, over a model that answers immediately."""
    kwargs.pop("model", None)
    return build_langgraph_agent(
        model=_Model(messages=iter([AIMessage(content="the answer")])), **kwargs
    )


async def test_a_teardown_after_the_run_leaves_the_checkpoint_ahead_of_the_transcript(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After a late teardown the checkpoint is ahead of the transcript, and a counter says so.

    The turn is not rolled back: its committed exchange is complete and correctly paired, and
    deleting it because a client dropped is the failure `_roll_back_unfinished` exists to avoid.
    """
    # Both engines are gated on this one setting: `_turn_checkpointer` returns `None` off the
    # Postgres store, which is the configuration in which this divergence cannot arise at all —
    # there is no second record to diverge from.
    monkeypatch.setattr(settings, "session_store", "postgres")

    await migrated_db_or_skip()
    await create_checkpoint_tables()
    # Any saver a previous test left published belongs to a closed loop and, possibly, another
    # DSN; the turn under test has to build its own on this one.
    await close_checkpointer()
    await SessionOwnerStore().record(_SESSION, "oid-divergence", None)
    history = PostgresHistoryProvider()

    reached = asyncio.Event()

    async def _slow_answer(*args: Any, **kwargs: Any) -> Any:
        """Hold the turn in the window between the graph run and the transcript write."""
        reached.set()
        await asyncio.sleep(30)
        raise AssertionError("unreachable")  # pragma: no cover

    before = METRICS.value("chemclaw_transcript_thread_divergence_total")
    # Patched on the module `run_turn` looks the name up in, since `runner` imports it.
    patch = pytest.MonkeyPatch()
    patch.setattr("chemclaw.api.runner.build_answer_event", _slow_answer)
    try:
        stream = run_turn(
            TurnSession(session_id=_SESSION),
            "the question the chemist asked",
            history=history,
            connectors=[],
            graph_factory=_factory,
        )

        async def _drain() -> None:
            async for _event in stream:
                pass

        task = asyncio.create_task(_drain())
        await asyncio.wait_for(reached.wait(), 20)
        # The graph run is over and committed; `_record_transcript` has not run.
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        await asyncio.sleep(0.2)
    finally:
        patch.undo()

    saver = await checkpointer()
    try:
        stored = await saver.aget_tuple(cast("RunnableConfig", turn_config(_SESSION)))
        assert stored is not None
        thread = [
            str(message.content) for message in stored.checkpoint["channel_values"]["messages"]
        ]
        transcript = await history.get_messages(_SESSION)
    finally:
        await close_checkpointer()

    # The measurement, pinned so the "no third outcome" claim cannot come back.
    assert "the question the chemist asked" in thread, thread
    assert "the answer" in thread, thread
    # The question is in both records and says how its turn ended; only the cut-off answer is the
    # model's alone.
    assert [str(message.content) for message in transcript] == ["the question the chemist asked"]
    assert stored_turn_status(transcript[0]) == "stopped", transcript[0].additional_kwargs
    after = METRICS.value("chemclaw_transcript_thread_divergence_total")
    assert after == before + 1, (
        "a turn kept in the checkpointer and missing from the transcript must be counted; "
        f"the counter went {before} -> {after}"
    )
