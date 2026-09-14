"""What a thread is before anything tries to continue it, driven against a real checkpointer.

`D-2026-09-14-a-turn-outlives-its-request-already-and-nothing-can-pick-it-up` measured that a fresh
process resumes a killed turn with `ainvoke(None, turn_config(T))`. What it also measured is that
the same call is a **silent no-op** on a finished thread — it returns the completed state with zero
model calls — and raises `EmptyInputError` on an unknown one. Neither is a failure and neither is a
resume, so a supervisor that read the return value alone would re-run finished work and report
recoveries that never happened.

`resumability` is that distinction, and it is asserted here rather than reasoned about: a thread
with a pending task, a thread that ran to completion, and an id nothing ever wrote. The three come
from the same compiled graph over the same saver in one test, because the property is that they are
*told apart*, which three separate tests could each satisfy while agreeing on the wrong answer.
"""

import asyncio
import contextlib
from typing import Any, cast

from langchain_core.language_models import BaseChatModel, GenericFakeChatModel
from langchain_core.messages import AIMessage

from chemclaw.agent.langgraph_agent import build_langgraph_agent
from chemclaw.agent.resume import Resumability, resumability
from chemclaw.agent.state import turn_config, turn_input
from chemclaw.core.config import settings
from tests.pg import create_checkpoint_tables, migrated_db_or_skip


class _Hanging(BaseChatModel):
    """A model that never answers, so the turn can be killed with its model node still pending."""

    @property
    def _llm_type(self) -> str:
        return "hanging"

    def _generate(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError

    async def _agenerate(self, *args: Any, **kwargs: Any) -> Any:
        await asyncio.sleep(3600)
        raise AssertionError("unreachable")

    def bind_tools(self, tools: Any, **kwargs: Any) -> Any:
        """Accept the binding and keep hanging."""
        return self


class _Model(GenericFakeChatModel):
    """A scripted model that can be bound, because `create_agent` binds tools on every request."""

    def bind_tools(self, tools: Any, **kwargs: Any) -> Any:
        """Accept the binding and keep replaying the script."""
        return self


def test_a_finished_thread_an_unknown_one_and_a_pending_one_are_told_apart() -> None:
    """The three states, from one graph over one saver, in one run.

    The pending thread is made by interrupting the graph *before* its model node runs — a
    `StateSnapshot` with a non-empty `next` is exactly what a pod death leaves behind, and it is
    what LangGraph itself would schedule. Reading `next` rather than querying the checkpoint tables
    is deliberate: the scheduler's own answer to "what would you run" is the question, and SQL would
    be a second implementation of it.
    """

    async def _run() -> tuple[str, str, str]:
        await migrated_db_or_skip()
        await create_checkpoint_tables()
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
        from psycopg_pool import AsyncConnectionPool

        pool = AsyncConnectionPool(
            conninfo=settings.postgres_dsn,
            kwargs={"autocommit": True},
            min_size=0,
            max_size=4,
            open=False,
        )
        await pool.open()
        try:
            saver = AsyncPostgresSaver(cast(Any, pool))
            graph = build_langgraph_agent(
                model=_Model(messages=iter([AIMessage(content="done")])),
                checkpointer=saver,
            )

            finished = "resume-finished"
            await graph.ainvoke(turn_input("a question"), config=turn_config(finished))

            # A thread with a checkpoint and a node still to run — made by *killing* the turn
            # mid-flight rather than by an interrupt, because that is what a pod death is and
            # `build_langgraph_agent` compiles no interrupt points. The model hangs, the task is
            # cancelled, and what is left on the thread is the pending model node.
            pending = "resume-pending"
            hanging = build_langgraph_agent(model=_Hanging(), checkpointer=saver)
            task = asyncio.create_task(
                hanging.ainvoke(turn_input("a question"), config=turn_config(pending))
            )
            await asyncio.sleep(0.4)
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

            return (
                str(await resumability(graph, pending)),
                str(await resumability(graph, finished)),
                str(await resumability(graph, "resume-never-written")),
            )
        finally:
            await pool.close()

    pending, finished, unknown = asyncio.run(_run())

    assert pending == Resumability.RESUMABLE, (
        "a thread with a node still to run is the only state that may be resumed"
    )
    assert finished == Resumability.FINISHED, (
        "a completed thread must not read as resumable: `ainvoke(None, ...)` on one is a silent "
        "no-op, so a supervisor would report a recovery that did not happen"
    )
    assert unknown == Resumability.UNKNOWN, (
        "an id nothing ever wrote must not read as finished: `ainvoke(None, ...)` raises there, "
        "and the two need different handling"
    )
