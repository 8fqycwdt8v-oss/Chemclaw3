"""Reading a session's plan back from the checkpointer, between turns (M13 Step 5).

The plan gate reads the plan *during* a call, off `request.state`. `GET /sessions/{id}/plan` and
the CLI's `/plan` read it when no turn is running, and under MAF that came off the in-process
`AgentSession` the front door held — the object an LRU eviction or a pod roll dropped, which is
half of why a rehydrated session used to propose the empty plan and meet its own already-spent
approval.

These drive a real graph with a real checkpointer, because the property under test is precisely
that the plan *survives the turn that wrote it*. A fake saver would prove the dict access.
"""

import asyncio
from typing import Any, TypedDict

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph

from chemclaw.agent.plan_state import session_todos


class _State(TypedDict):
    """Just the field `TodoListMiddleware` owns, which is all this read looks at."""

    todos: list[dict[str, str]]


def _graph(saver: Any) -> Any:
    """A one-node graph that writes a todo list and stops — a turn, reduced to its plan."""

    async def node(state: _State, config: RunnableConfig) -> dict[str, Any]:
        return {
            "todos": [
                {"content": "screen the species", "status": "pending"},
                {"content": "compute the barrier", "status": "pending"},
            ]
        }

    graph = StateGraph(_State)
    graph.add_node("plan", node)
    graph.add_edge(START, "plan")
    graph.add_edge("plan", END)
    return graph.compile(checkpointer=saver)


def test_a_plan_written_in_a_turn_is_readable_after_it() -> None:
    """The whole point: the read happens between turns, so the plan has to outlive one.

    Asserted through a *separate* `session_todos` call rather than off the invoke's return value,
    which is the difference that matters — the return value proves the node ran, the checkpointer
    read proves the plan is still there when the chemist asks for it.
    """
    saver = InMemorySaver()

    async def _run() -> list[str]:
        await _graph(saver).ainvoke({"todos": []}, {"configurable": {"thread_id": "sess-plan-1"}})
        return await session_todos("sess-plan-1", saver=saver)

    assert asyncio.run(_run()) == ["screen the species", "compute the barrier"]


def test_a_session_that_never_took_a_turn_has_no_plan() -> None:
    """No checkpoint is "no plan yet", not an error.

    The route renders that as an empty plan and `/approve` refuses to act on it, so returning `[]`
    is what lets both callers treat "never asked anything" and "asked, proposed nothing" the same
    way — which is correct, because to a chemist they are the same thing.
    """
    assert asyncio.run(session_todos("sess-never-used", saver=InMemorySaver())) == []


def test_an_unreadable_checkpointer_reads_as_no_plan_rather_than_failing() -> None:
    """A plan is a display concern; failing to read it must not fail the request that asked.

    Deliberately the same posture the runner takes for the plan event it yields mid-turn. The cost
    is stated rather than hidden: the WARNING this logs is the only thing that distinguishes "the
    database hiccuped" from "there is no plan", and that difference decides whether a chemist is
    offered an approve button.
    """

    class _BrokenSaver:
        async def aget_tuple(self, config: dict[str, Any]) -> Any:
            raise ConnectionError("Postgres unreachable at postgresql://h/db")

    assert asyncio.run(session_todos("sess-broken", saver=_BrokenSaver())) == []


def test_a_todo_without_content_is_skipped_rather_than_crashing_the_read() -> None:
    """The checkpoint is somebody else's shape, so the read cannot assume every row is well-formed.

    `TodoListMiddleware` owns `todos`, and a version of it that added a row kind without `content`
    would otherwise turn a plan display into a 500. Skipping is right rather than substituting an
    empty string: an unnameable item is not a plan item, and showing a blank line invites approving
    something nobody can read.
    """

    class _SaverWithJunk:
        async def aget_tuple(self, config: dict[str, Any]) -> Any:
            class _Tuple:
                checkpoint = {
                    "channel_values": {
                        "todos": [
                            {"content": "a real item"},
                            {"status": "pending"},
                            "not a dict at all",
                        ]
                    }
                }

            return _Tuple()

    assert asyncio.run(session_todos("sess-junk", saver=_SaverWithJunk())) == ["a real item"]
