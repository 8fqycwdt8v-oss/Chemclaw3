"""Reading a session's plan back from the checkpointer, between turns.

`GET /sessions/{id}/plan` and the CLI's `/plan` read when no turn is running, so the plan must
survive in the checkpointer rather than in an evictable in-process session. Driven with a real
graph and checkpointer.
"""

import asyncio
from typing import Any, TypedDict

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph

from chemclaw.agent.plan_state import session_plan


class _State(TypedDict):
    """Just the field `TodoListMiddleware` owns, which is all this read looks at."""

    todos: list[dict[str, Any]]


def _graph(saver: Any) -> Any:
    """A one-node graph that writes a todo list and stops — a turn, reduced to its plan."""

    async def node(state: _State, config: RunnableConfig) -> dict[str, Any]:
        return {
            "todos": [
                {
                    "content": "screen the species",
                    "status": "pending",
                    "tools": ["gather_evidence"],
                },
                {
                    "content": "compute the barrier",
                    "status": "pending",
                    "tools": ["run_xtb_energy"],
                },
            ]
        }

    graph = StateGraph(_State)
    graph.add_node("plan", node)
    graph.add_edge(START, "plan")
    graph.add_edge("plan", END)
    return graph.compile(checkpointer=saver)


def test_a_plan_written_in_a_turn_is_readable_after_it() -> None:
    """A plan written in a turn is readable after it, through a separate `session_plan` call.

    Steps come back whole, declaration included, since identities and decisions are taken over them.
    """
    saver = InMemorySaver()

    async def _run() -> list[dict[str, Any]] | None:
        await _graph(saver).ainvoke({"todos": []}, {"configurable": {"thread_id": "sess-plan-1"}})
        return await session_plan("sess-plan-1", saver=saver)

    assert asyncio.run(_run()) == [
        {"content": "screen the species", "status": "pending", "tools": ["gather_evidence"]},
        {"content": "compute the barrier", "status": "pending", "tools": ["run_xtb_energy"]},
    ]


def test_a_session_that_never_took_a_turn_reads_as_unreadable_not_as_an_empty_plan() -> None:
    """No checkpoint reads as `None`, not as an empty plan.

    `[]` hashes to a real identity a decision can match; `None` cannot, so the approval-spending
    caller can tell them apart. Read-only callers coalesce with `or []`.
    """
    assert asyncio.run(session_plan("sess-never-used", saver=InMemorySaver())) is None


def test_an_unreadable_checkpointer_reads_as_no_plan_rather_than_failing() -> None:
    """An unreadable checkpointer reads as `None` rather than failing the request.

    The return value distinguishes an outage from no plan, so the approval-spending path does not
    treat an outage as a session that proposed nothing.
    """

    class _BrokenSaver:
        async def aget_tuple(self, config: dict[str, Any]) -> Any:
            raise ConnectionError("Postgres unreachable at postgresql://h/db")

    assert asyncio.run(session_plan("sess-broken", saver=_BrokenSaver())) is None


def test_a_todo_without_content_is_skipped_rather_than_crashing_the_read() -> None:
    """A todo without `content` is skipped rather than crashing the read.

    `todos` is `TodoListMiddleware`'s shape, and an unnameable item is not a plan item anyone could
    approve.
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

    assert asyncio.run(session_plan("sess-junk", saver=_SaverWithJunk())) == [
        {"content": "a real item"}
    ]
