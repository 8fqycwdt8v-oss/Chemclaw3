"""The durable session store persists and resumes a conversation (plan Phase F3-T1).

The round-trip test runs against a real database (CI provides Postgres; the offline sandbox has
none, so it skips). The provider-selection test is a pure unit test with no database — it proves
`build_agent` swaps the history provider by config, which is the wiring that makes sessions durable.
"""

import asyncio

from agent_framework import Content, InMemoryHistoryProvider, Message

from agents.chemclaw_agent import history_provider
from agents.message_pairing import unmatched_call_ids
from agents.session_store import PostgresHistoryProvider, SessionOwnerStore
from chemclaw.config import settings
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


def test_messages_survive_a_new_provider_instance() -> None:
    """Saved messages reload through a fresh provider over the same DSN (proxy for a restart)."""

    async def _run() -> None:
        writer = await _provider_or_skip()
        session_id = "sess-f3-roundtrip"
        turn = [Message(role="user", contents=["what is the pKa of phenol?"])]
        await writer.save_messages(session_id, turn)

        # A brand-new provider instance (as a restarted pod would build) sees the persisted turn.
        reader = PostgresHistoryProvider()
        loaded = await reader.get_messages(session_id)
        assert any("phenol" in m.text for m in loaded)

    asyncio.run(_run())


def test_an_orphaned_tool_call_is_repaired_on_read() -> None:
    """A stored call with no result is dropped *and* removed from the table, not just filtered.

    This is the poison-pill case: the model rejects a thread whose `tool_use` has no
    `tool_result`, so without this the session fails on every later turn — permanently, since a
    `SIGKILL` or pod eviction between the two writes runs no rollback handler at all.
    """

    async def _run() -> None:
        provider = await _provider_or_skip()
        session_id = "sess-orphan-repair"
        await provider.rollback_to(session_id, None)  # a clean slate for a rerun
        await provider.save_messages(
            session_id,
            [
                Message(role="user", contents=["screen sodium azide"]),
                Message(
                    role="assistant",
                    contents=[
                        Content.from_text("Checking that now."),
                        Content.from_function_call(
                            call_id="c-orphan", name="screen_hazards", arguments={}
                        ),
                    ],
                ),
            ],
        )

        loaded = await provider.get_messages(session_id)
        assert unmatched_call_ids(loaded) == set()  # the caller never sees the orphan
        assert any("Checking that now." in m.text for m in loaded)  # the prose survived

        # ...and it is gone from storage, so the repair is paid once rather than on every read.
        fresh = PostgresHistoryProvider()
        assert unmatched_call_ids(await fresh.get_messages(session_id)) == set()

    asyncio.run(_run())


def test_a_matched_pair_is_never_touched_by_the_repair() -> None:
    """A complete call/result pair round-trips intact — the repair must not eat healthy history."""

    async def _run() -> None:
        provider = await _provider_or_skip()
        session_id = "sess-orphan-healthy"
        await provider.rollback_to(session_id, None)
        await provider.save_messages(
            session_id,
            [
                Message(
                    role="assistant",
                    contents=[
                        Content.from_function_call(call_id="c-ok", name="predict_pka", arguments={})
                    ],
                ),
                Message(
                    role="tool",
                    contents=[Content.from_function_result(call_id="c-ok", result="9.95")],
                ),
            ],
        )
        loaded = await provider.get_messages(session_id)
        assert [c.type for m in loaded for c in m.contents] == ["function_call", "function_result"]

    asyncio.run(_run())


def test_rollback_deletes_only_what_the_turn_wrote() -> None:
    """The durable half of the disconnect rollback: rows past the watermark go, earlier ones stay.

    `session.state` is not where this provider keeps messages — `save_messages` has already
    committed them — so restoring the state alone left a half-written turn durably stored.
    """

    async def _run() -> None:
        provider = await _provider_or_skip()
        session_id = "sess-rollback"
        await provider.rollback_to(session_id, None)
        await provider.save_messages(session_id, [Message(role="user", contents=["turn one"])])

        watermark = await provider.latest_message_id(session_id)
        assert watermark is not None
        await provider.save_messages(session_id, [Message(role="user", contents=["turn two"])])

        assert await provider.rollback_to(session_id, watermark) == 1
        remaining = await provider.get_messages(session_id)
        assert [m.text for m in remaining] == ["turn one"]  # the committed turn is untouched

    asyncio.run(_run())


def test_watermark_is_none_for_a_session_with_no_history() -> None:
    """A first turn has nothing to roll back to, and rolling back to `None` clears the session."""

    async def _run() -> None:
        provider = await _provider_or_skip()
        assert await provider.latest_message_id("sess-never-used") is None

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
        assert await reader.lookup("sess-owner-1") == (True, "alice")
        assert await reader.lookup("sess-never-created") == (False, None)

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
        assert await store.lookup("sess-owner-null") == (True, None)

    asyncio.run(_run())
