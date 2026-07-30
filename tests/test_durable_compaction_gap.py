"""What the durable-compaction fix deliberately did *not* do (REV-4, D-149).

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

**`get_messages` still has no `LIMIT`, and must not grow one.** Windowing the read looks like the
cheaper fix and silently corrupts data: the repair on that path *writes back*, and over a partial
read a `tool_result` whose `tool_use` merely fell outside the window is indistinguishable from a
real orphan. Compaction sidesteps the whole class by deleting only whole pairing components
(`droppable_rows`, D-145) — it never reads a partial history.
"""

import asyncio
from pathlib import Path

from agent_framework import AgentSession, CompactionProvider, Message

from chemclaw.agent.session_store import PostgresHistoryProvider


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


def test_the_load_repair_writes_back_which_is_why_a_limit_is_unsafe() -> None:
    """The trap: `get_messages` heals orphaned pairings by *deleting and rewriting stored rows*.

    That is right for a full read — a `SIGKILL` between a tool call and its result leaves a genuine
    orphan that breaks every later turn, and healing it on read fixes sessions already broken in the
    wild. It is wrong for a *windowed* read, and the difference is invisible to the repair: a
    `tool_result` whose `tool_use` sits just outside the window looks precisely like one whose
    `tool_use` never arrived. Adding a `LIMIT` would therefore commit the stripping of pairings that
    were intact on disk.

    Pinned against the source, because the hazard is the write-back and there is no way to observe
    it without a database. A future change that makes the repair in-memory-only is what unlocks
    bounding the read, and it should turn this test into a different one.
    """
    source = (
        Path(__file__).resolve().parents[1] / "src" / "chemclaw" / "agent" / "session_store.py"
    ).read_text()
    assert "await self._persist_repair(" in source, (
        "the read-repair no longer writes back — which is exactly the change that makes bounding "
        "the history load safe, so re-derive REV-4's fix rather than deleting this test"
    )
    select = next(line for line in source.splitlines() if line.startswith("_SELECT_WITH_ID ="))
    assert "LIMIT" not in select.upper(), (
        "the history load grew a LIMIT while the repair still persists: a tool_result whose "
        "tool_use fell outside the window will be stripped and committed (REV-4)"
    )
