"""The awaiting-job todo bridge flips the right item, and only once (BACKLOG.md F3-T3 follow-up).

No agent, no LLM, no Temporal — a real `AgentSession` is all `mark_awaiting_job`/
`complete_awaiting_job` need, since they operate directly on the harness's `TodoSessionStore`.
"""

import asyncio

from agent_framework import DEFAULT_TODO_SOURCE_ID, AgentSession, TodoSessionStore

from agents.harness_todo import complete_awaiting_job, mark_awaiting_job


def test_mark_then_complete_flips_only_the_matching_todo() -> None:
    """Two awaiting todos in flight: completing one job's leaves the other's untouched."""

    async def _run() -> None:
        session = AgentSession(session_id="s1")
        await mark_awaiting_job(session, "qm-1", title="Await QM job qm-1")
        await mark_awaiting_job(session, "qm-2", title="Await QM job qm-2")

        found = await complete_awaiting_job(session, "qm-1", reason="QM job qm-1 completed")

        assert found is True
        items = await TodoSessionStore().load_items(session, source_id=DEFAULT_TODO_SOURCE_ID)
        by_title = {item.title: item for item in items}
        assert by_title["Await QM job qm-1"].is_complete is True
        assert by_title["Await QM job qm-1"].description == "QM job qm-1 completed"
        assert by_title["Await QM job qm-2"].is_complete is False

    asyncio.run(_run())


def test_complete_for_unknown_job_is_a_safe_no_op() -> None:
    """No awaiting todo matches → False, and nothing in the store changes."""

    async def _run() -> None:
        session = AgentSession(session_id="s1")
        await mark_awaiting_job(session, "qm-1", title="Await QM job qm-1")

        found = await complete_awaiting_job(session, "qm-does-not-exist", reason="x")

        assert found is False
        items = await TodoSessionStore().load_items(session, source_id=DEFAULT_TODO_SOURCE_ID)
        assert items[0].is_complete is False

    asyncio.run(_run())


def test_complete_is_idempotent_and_never_reopens() -> None:
    """A second push-back for the same job id (should not happen, but be defensive) is a no-op."""

    async def _run() -> None:
        session = AgentSession(session_id="s1")
        await mark_awaiting_job(session, "qm-1", title="Await QM job qm-1")

        first = await complete_awaiting_job(session, "qm-1", reason="first notification")
        second = await complete_awaiting_job(session, "qm-1", reason="duplicate notification")

        assert (first, second) == (True, False)
        items = await TodoSessionStore().load_items(session, source_id=DEFAULT_TODO_SOURCE_ID)
        assert items[0].description == "first notification"  # the duplicate never overwrote it

    asyncio.run(_run())


def test_mark_awaiting_leaves_other_session_state_untouched() -> None:
    """Marking a job only touches the todo source id, not unrelated session state."""

    async def _run() -> None:
        session = AgentSession(session_id="s1")
        session.state["agent_mode"] = {"current_mode": "execute"}

        await mark_awaiting_job(session, "qm-1", title="Await QM job qm-1")

        assert session.state["agent_mode"] == {"current_mode": "execute"}

    asyncio.run(_run())
