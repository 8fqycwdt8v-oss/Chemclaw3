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
from types import SimpleNamespace
from typing import Any, cast

from langchain_core.language_models import BaseChatModel, GenericFakeChatModel
from langchain_core.messages import AIMessage

from chemclaw.agent.langgraph_agent import build_langgraph_agent
from chemclaw.agent.resume import (
    Resumability,
    ResumeOutcome,
    resumability,
    resume_turn,
)
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


def test_a_resume_is_refused_while_somebody_else_holds_the_session() -> None:
    """The guard without which a resume forks the thread instead of continuing it.

    Wave 2 measured what a second writer does: not an error, a **fork** — 26 checkpoint rows with
    duplicate step numbers under different parents, last writer winning, and one pod's answer
    returned to its caller and absent from the session. So a resume takes the same lease a chat turn
    takes, and refuses when it cannot.

    Three arms, because each one alone passes for the wrong reason: held refuses and drives nothing;
    free resumes; and the lease is **released either way**, since a resume that kept it would leave
    the session reading "busy" for a turn nobody is running.
    """
    driven: list[str] = []
    released: list[str] = []

    class _Claims:
        """`SessionTurnClaims`' three-method shape, with the taken/free decision scripted."""

        def __init__(self, grant: bool) -> None:
            self._grant = grant

        async def claim(self, session_id: str, holder: str, lease_seconds: float) -> bool:
            return self._grant

        async def release(self, session_id: str, holder: str) -> None:
            released.append(session_id)

    class _Graph:
        """A graph that records being driven, and reports one node still to run."""

        async def aget_state(self, config: Any) -> Any:
            return SimpleNamespace(next=("model",), created_at="2026-09-14T00:00:00Z")

        async def ainvoke(self, value: Any, config: Any) -> Any:
            driven.append(str(config["configurable"]["thread_id"]))
            return {}

    held = asyncio.run(
        resume_turn(
            _Graph(), "s-held", claims=_Claims(grant=False), holder="pod-b", lease_seconds=60.0
        )
    )
    assert held == ResumeOutcome(state=Resumability.HELD, resumed=False)
    assert driven == [], "a resume that could not take the lease must not drive the graph"
    assert released == [], "it holds nothing, so it releases nothing"

    free = asyncio.run(
        resume_turn(
            _Graph(), "s-free", claims=_Claims(grant=True), holder="pod-a", lease_seconds=60.0
        )
    )
    assert free == ResumeOutcome(state=Resumability.RESUMABLE, resumed=True)
    assert driven == ["s-free"]
    assert released == ["s-free"], "the lease is given back when the turn ends"


def test_the_claim_is_taken_before_the_state_is_read() -> None:
    """The ordering is the correctness argument, so it is asserted rather than reasoned about.

    Reading first and claiming second leaves a window in which the thread changes between the two:
    another process finishing it, or starting a fresh turn on it, and the resume then drives a
    graph it has an out-of-date opinion about. Claiming first makes the read happen under
    exclusion.

    Both calls already happen, so no other assertion in this file can tell the two orders apart:
    swap them and everything else here still passes. This watches the sequence itself.
    """
    calls: list[str] = []

    class _Claims:
        async def claim(self, session_id: str, holder: str, lease_seconds: float) -> bool:
            calls.append("claim")
            return True

        async def release(self, session_id: str, holder: str) -> None:
            calls.append("release")

    class _Graph:
        async def aget_state(self, config: Any) -> Any:
            calls.append("read")
            return SimpleNamespace(next=("model",), created_at="2026-09-14T00:00:00Z")

        async def ainvoke(self, value: Any, config: Any) -> Any:
            calls.append("invoke")
            return {}

    asyncio.run(resume_turn(_Graph(), "s", claims=_Claims(), holder="pod-a", lease_seconds=60.0))

    assert calls == ["claim", "read", "invoke", "release"], (
        f"the claim must precede the read, so the read happens under exclusion; got {calls}"
    )


def test_a_finished_session_releases_the_lease_it_took_to_look() -> None:
    """Taking the claim before reading the state is deliberate, so the release has to be too.

    The claim comes first because reading first leaves a window in which the thread changes between
    the read and the claim. That makes the not-resumable path a case where the lease was taken and
    nothing was driven — and a lease kept there would make the session unusable until it lapsed,
    for a turn nobody is running.
    """
    released: list[str] = []

    class _Claims:
        async def claim(self, session_id: str, holder: str, lease_seconds: float) -> bool:
            return True

        async def release(self, session_id: str, holder: str) -> None:
            released.append(session_id)

    class _Finished:
        async def aget_state(self, config: Any) -> Any:
            return SimpleNamespace(next=(), created_at="2026-09-14T00:00:00Z")

        async def ainvoke(self, value: Any, config: Any) -> Any:
            raise AssertionError("a finished thread must not be driven")

    outcome = asyncio.run(
        resume_turn(_Finished(), "s-done", claims=_Claims(), holder="pod-a", lease_seconds=60.0)
    )

    assert outcome == ResumeOutcome(state=Resumability.FINISHED, resumed=False)
    assert released == ["s-done"]
