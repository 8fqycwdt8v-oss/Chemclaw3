"""The durable session store persists and resumes a conversation (plan Phase F3-T1).

The round-trip test runs against a real database (CI provides Postgres; the offline sandbox has
none, so it skips). The provider-selection test is a pure unit test with no database — it proves
`build_agent` swaps the history provider by config, which is the wiring that makes sessions durable.
"""

import asyncio

from langchain_core.messages import HumanMessage

from chemclaw.agent.chemclaw_agent import history_provider
from chemclaw.agent.session_store import (
    InMemoryHistoryProvider,
    PostgresHistoryProvider,
    SessionOwnerStore,
    SessionTurnClaims,
)
from chemclaw.core import db
from chemclaw.core.config import settings
from tests.pg import migrated_db_or_skip


def test_history_provider_selected_by_config(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """`session_store` picks the durable Postgres provider or the in-memory default."""
    monkeypatch.setattr(settings, "session_store", "memory")
    assert isinstance(history_provider(), InMemoryHistoryProvider)
    monkeypatch.setattr(settings, "session_store", "postgres")
    assert isinstance(history_provider(), PostgresHistoryProvider)


async def _provider_or_skip() -> PostgresHistoryProvider:
    """Return a provider over a migrated database, or skip if none is reachable."""
    await migrated_db_or_skip()
    return PostgresHistoryProvider()


async def _clear(session_id: str) -> None:
    """Empty one session's rows, so a rerun starts from the state the test describes.

    Written here rather than borrowed from the provider: the store used to expose `rollback_to`,
    and these tests reached for it as a truncate because it happened to be there. It was the
    disconnect rollback's delete, it is gone with that rollback, and a fixture concern should never
    have been resting on a production method's side effect anyway.
    """
    async with db.connection(settings.postgres_dsn) as conn:
        async with conn.cursor() as cur:
            await cur.execute("DELETE FROM session_messages WHERE session_id = %s", (session_id,))


def test_messages_survive_a_new_provider_instance() -> None:
    """Saved messages reload through a fresh provider over the same DSN (proxy for a restart)."""

    async def _run() -> None:
        writer = await _provider_or_skip()
        session_id = "sess-f3-roundtrip"
        turn = [HumanMessage(content="what is the pKa of phenol?")]
        await writer.save_messages(session_id, turn)

        # A brand-new provider instance (as a restarted pod would build) sees the persisted turn.
        reader = PostgresHistoryProvider()
        loaded = await reader.get_messages(session_id)
        assert any("phenol" in str(m.content) for m in loaded)

    asyncio.run(_run())


def test_unknown_session_loads_empty() -> None:
    """A session with no rows (or a None id) loads to an empty thread, never an error."""

    async def _run() -> None:
        provider = await _provider_or_skip()
        assert await provider.get_messages("sess-does-not-exist") == []
        assert await provider.get_messages(None) == []

    asyncio.run(_run())


def test_session_owner_records_and_reattaches() -> None:
    """Ownership persists and a fresh store instance looks it up — the reattach path (F3)."""

    async def _run() -> None:
        await migrated_db_or_skip()
        writer = SessionOwnerStore()
        await writer.record("sess-owner-1", "alice")
        await writer.record("sess-owner-1", "mallory")  # idempotent: first writer wins

        reader = SessionOwnerStore()  # a restarted pod would build a fresh instance
        assert await reader.lookup("sess-owner-1") == (True, "alice", None)
        assert await reader.lookup("sess-never-created") == (False, None, None)

    asyncio.run(_run())


def test_session_owner_lists_only_its_own_sessions_newest_first() -> None:
    """Listing is owner-scoped and newest-first — what `GET /sessions` renders as the sidebar.

    A dedicated owner string per test: the table is shared across this module's cases, so scoping
    to a real owner is also what keeps the assertion independent of the other rows in there.
    """

    async def _run() -> None:
        await migrated_db_or_skip()
        store = SessionOwnerStore()
        await store.record("sess-list-a", "owner-list-test")
        await store.record("sess-list-b", "owner-list-test")
        await store.record("sess-list-other", "someone-else")

        listed = await store.list_for_owner("owner-list-test")
        assert {session_id for session_id, _ in listed} == {"sess-list-a", "sess-list-b"}
        # created_at defaults to now(), so newest-first is a descending sort on it.
        assert [created for _, created in listed] == sorted(
            (created for _, created in listed), reverse=True
        )
        assert await store.list_for_owner("owner-with-no-sessions") == []

    asyncio.run(_run())


def test_session_owner_lists_the_null_owner_sessions() -> None:
    """A NULL owner matches itself when listing — `owner = NULL` would silently return nothing.

    The shared dev principal records a real SQL NULL, and three-valued logic makes `= %s` false for
    every row, so the dev/no-Entra deployment would show an empty conversation list with sessions
    sitting right there in the table. `IS NOT DISTINCT FROM` is what makes this row come back.
    """

    async def _run() -> None:
        await migrated_db_or_skip()
        store = SessionOwnerStore()
        await store.record("sess-list-null", None)
        listed = await store.list_for_owner(None)
        assert "sess-list-null" in {session_id for session_id, _ in listed}

    asyncio.run(_run())


def test_session_owner_records_null_owner() -> None:
    """A session with no Entra oid (the shared dev principal) is still recorded and found."""

    async def _run() -> None:
        await migrated_db_or_skip()
        store = SessionOwnerStore()
        await store.record("sess-owner-null", None)
        assert await store.lookup("sess-owner-null") == (True, None, None)

    asyncio.run(_run())


async def _claims_or_skip() -> SessionTurnClaims:
    """Return a turn-claim store over a migrated database, or skip if none is reachable."""
    await migrated_db_or_skip()
    return SessionTurnClaims()


def test_a_second_process_cannot_claim_a_session_that_is_already_running() -> None:
    """Two *separate* claim stores — the model of two workers — cannot both hold one session.

    This is the guarantee the in-process `active_turns` set could not give: the shipped chart runs
    two front-door replicas, so the second POST for a session can arrive at a process that has
    never heard of the first. The claim is one statement so the check and the take cannot be
    interleaved; releasing hands the slot to the next caller.
    """

    async def _run() -> None:
        worker_a = await _claims_or_skip()
        worker_b = SessionTurnClaims()
        session_id = "sess-d120-exclusive"
        await worker_a.release(session_id, "a")  # a previous run's residue must not decide this

        assert await worker_a.claim(session_id, "a", 60.0) is True
        assert await worker_b.claim(session_id, "b", 60.0) is False
        await worker_a.release(session_id, "a")
        assert await worker_b.claim(session_id, "b", 60.0) is True
        await worker_b.release(session_id, "b")

    asyncio.run(_run())


def test_a_crashed_workers_claim_ages_out_and_a_refresh_holds_it() -> None:
    """An expired lease is takeable; a refreshed one is not — the two halves of the lease.

    Expiry is why this is a lease and not a lock: a worker SIGKILLed mid-turn runs no cleanup, and
    without expiry its session would 409 forever. Refresh is the other half — a turn that
    legitimately outlives one lease must not be declared dead while it is still streaming.
    """

    async def _run() -> None:
        claims = await _claims_or_skip()
        session_id = "sess-d120-lease"
        await claims.release(session_id, "dead")

        # A lease that has already elapsed: the holder is gone and nothing released it.
        assert await claims.claim(session_id, "dead", -1.0) is True
        assert await claims.claim(session_id, "live", 60.0) is True  # taken over, not blocked

        # Now the live holder keeps it, and a refresh by the *dead* holder cannot steal it back.
        assert await claims.claim(session_id, "other", 60.0) is False
        await claims.refresh(session_id, "dead", 600.0)
        await claims.release(session_id, "dead")  # wrong holder: must not free someone else's slot
        assert await claims.claim(session_id, "other", 60.0) is False

        await claims.release(session_id, "live")
        assert await claims.claim(session_id, "other", 60.0) is True
        await claims.release(session_id, "other")

    asyncio.run(_run())


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
            await provider.save_messages(session_id, [HumanMessage(content=f"question {index}")])

        loaded = await provider.get_messages(session_id)
        assert [m.content for m in loaded] == [f"question {index}" for index in range(turns)], (
            f"the transcript read returned {len(loaded)} of {turns} messages — a window would "
            "make a reloaded conversation look like it began later than it did"
        )

    asyncio.run(_run())
