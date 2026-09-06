"""The two records of one conversation, observed together across one teardown.

`session_messages` is what a chemist sees on reload; the checkpointer's thread is what the *model*
is built from next turn. `tests/test_turn_cancellation.py` pins the transcript half — "the
transcript is all-or-nothing across a teardown" — on a `ScriptedTurn` fake that has no checkpointer
at all, so nothing in this suite had ever looked at **both** records after one teardown, and the
runner carried two present-tense docstrings saying they could not disagree ("there is no third
outcome").

They can, and this is the third outcome: a teardown landing between the graph run and
`_record_transcript` leaves the exchange in the model's record and out of the chemist's. Measured
before it was counted at `checkpoints: 8, session_messages: 0`, with the runner logging "the
committed turn is kept".

**The window is instrumented rather than waited for**, the way the review that found it did:
`build_answer_event` is wrapped to block, which is the window the verifier's judge call really
occupies under `verifier_enabled`. Nothing else is patched — the graph, the saver, the transcript
provider and `run_turn` itself are the real ones.
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
from chemclaw.agent.session_store import PostgresHistoryProvider, SessionOwnerStore
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


def test_a_teardown_after_the_run_leaves_the_checkpoint_ahead_of_the_transcript(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both records, after one teardown — and the counter that now says they parted company.

    The turn is *not* rolled back and must not be: the exchange it committed is complete and
    correctly paired, and deleting it because a client dropped is the expensive failure
    `_roll_back_unfinished` exists to avoid. What was missing is that nothing said the two records
    had diverged — the runner logged "the committed turn is kept" and moved no counter, and both
    docstrings on this path denied the outcome existed.
    """
    # Both engines are gated on this one setting: `_turn_checkpointer` returns `None` off the
    # Postgres store, which is the configuration in which this divergence cannot arise at all —
    # there is no second record to diverge from.
    monkeypatch.setattr(settings, "session_store", "postgres")

    async def _run() -> None:
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
        # Patched by string on the module `run_turn` looks the name up in, not where it is
        # defined: `runner` imports it, so patching the definition would not intercept the call,
        # and a direct attribute assignment is neither an export mypy follows nor a form ruff
        # allows. `MonkeyPatch` restores it.
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
        assert transcript == [], (
            f"the transcript is expected to be empty on this path; got {transcript}"
        )
        after = METRICS.value("chemclaw_transcript_thread_divergence_total")
        assert after == before + 1, (
            "a turn kept in the checkpointer and missing from the transcript must be counted; "
            f"the counter went {before} -> {after}"
        )

    asyncio.run(_run())
