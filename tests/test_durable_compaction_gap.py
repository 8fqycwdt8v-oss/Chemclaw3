"""What the durable-compaction fix deliberately did *not* do (REV-4, D-151).

This file used to pin an open gap. The gap is closed — `save_messages` now runs the context
compaction strategy against the table (`chemclaw.agent.session_store._compact`) — but the two
facts that
shaped the fix are still true, and both are load-bearing:

**MAF's `after_run` still cannot reach the durable provider, and never will.**
`CompactionProvider.after_run` reads `session.state[history_source_id]["messages"]`, the slot
`InMemoryHistoryProvider` writes and `PostgresHistoryProvider` deliberately does not. Nothing short
of reintroducing the in-process thread the provider exists to abolish would change that, which is
why the fix lives in the provider rather than in the `CompactionProvider` wiring. If these tests
ever go red, someone has started populating that slot and the durable path needs re-deciding, not
patching.

**`get_messages` still has no `LIMIT`, and must not grow one** — though the reason changed under
it. It was a data-safety rule while the read repaired pairings and wrote the repair back; with the
repair gone it is a rendering rule, because the one caller left is the transcript route and a
window makes a reloaded conversation look like it began later than it did. Compaction is what
bounds the table, deleting only whole pairing components (`droppable_rows`, D-145).
"""

import asyncio

from agent_framework import AgentSession, CompactionProvider, Message

from chemclaw.agent.session_store import PostgresHistoryProvider
from chemclaw.core import db
from chemclaw.core.config import settings
from tests.pg import migrated_db_or_skip


def test_the_durable_provider_writes_nothing_where_compaction_looks() -> None:
    """The mechanical cause: the slot `after_run` reads is never populated by this provider."""
    durable = PostgresHistoryProvider()
    session = AgentSession(session_id="rev4")
    assert session.state.get(durable.source_id) is None, (
        "the durable provider now writes messages into session state; if that is deliberate, "
        "after-run compaction may finally apply and this test should become the opposite assertion"
    )


def test_compaction_after_run_is_a_no_op_without_that_state() -> None:
    """The consequence: the strategy is never called, so nothing is trimmed.

    Asserted by giving the provider a strategy that records being called — a mock that asserted
    "compaction was configured" would have passed against the broken wiring, which is exactly how
    this survived. The question is whether the strategy *runs*.
    """
    called: list[int] = []

    async def _recording_strategy(messages: list[Message]) -> bool:
        """A `CompactionStrategy` that only records that it ran (it changes nothing)."""
        called.append(len(messages))
        return False

    durable = PostgresHistoryProvider()
    provider = CompactionProvider(
        before_strategy=None,
        after_strategy=_recording_strategy,
        history_source_id=durable.source_id,
    )
    session = AgentSession(session_id="rev4-after")

    async def _drive() -> None:
        await provider.after_run(agent=None, session=session, context=None, state={})
        assert called == [], (
            "the after-run strategy ran, so the durable provider's messages are reachable from "
            "session state after all — re-check `agent/session_store.py` before trusting it"
        )
        # The same provider *does* fire once the messages are where MAF looks. That contrast is
        # what makes the assertion above a statement about the durable store rather than about a
        # strategy that was never going to run under any conditions.
        session.state[durable.source_id] = {"messages": [Message("user", ["hello"])]}
        await provider.after_run(agent=None, session=session, context=None, state={})
        assert called == [1]

    asyncio.run(_drive())


def test_the_transcript_read_returns_the_whole_session_not_a_window() -> None:
    """`get_messages` still has no `LIMIT`, for a reason that changed under it.

    It used to be a data-safety rule: the read repaired orphaned pairings and *wrote the repair
    back*, so over a windowed read a `tool_result` whose `tool_use` merely fell outside the window
    was indistinguishable from a real orphan and would be stripped and committed. That repair is
    gone — nothing feeds this back to a model any more — and the previous version of this test said
    in as many words that its own deletion should turn it into a different test. This is that test.

    The surviving reason is the reader. The one caller is `GET /sessions/{id}/messages`, rendered
    for a person reloading a conversation, and a transcript that silently drops its own beginning
    is worse than a slow one: it does not look truncated, it looks like the conversation started
    later than it did. Compaction is what bounds this table, and it deletes whole pairing
    components (`droppable_rows`, D-145) so what remains is always coherent.

    Asserted behaviorally rather than by grepping the SQL, which is what the old version had to do
    (the write-back was unobservable without a database). A window would show up here as a short
    list, however it were implemented.
    """

    async def _run() -> None:
        await migrated_db_or_skip()
        provider = PostgresHistoryProvider()
        session_id = "sess-no-window"
        async with db.connection(settings.postgres_dsn) as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "DELETE FROM session_messages WHERE session_id = %s", (session_id,)
                )
        turns = 80  # comfortably past any plausible default window
        for index in range(turns):
            await provider.save_messages(session_id, [Message("user", [f"question {index}"])])

        loaded = await provider.get_messages(session_id)
        assert [m.text for m in loaded] == [f"question {index}" for index in range(turns)], (
            f"the transcript read returned {len(loaded)} of {turns} messages — a window would "
            "make a reloaded conversation look like it began later than it did"
        )

    asyncio.run(_run())
