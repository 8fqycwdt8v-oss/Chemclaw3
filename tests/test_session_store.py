"""The durable session store persists and resumes a conversation (plan Phase F3-T1).

The round-trip test runs against a real database (CI provides Postgres; the offline sandbox has
none, so it skips). The provider-selection test is a pure unit test with no database — it proves
`build_agent` swaps the history provider by config, which is the wiring that makes sessions durable.
"""

import asyncio

from agent_framework import InMemoryHistoryProvider, Message

from agents.chemclaw_agent import _history_provider
from agents.session_store import PostgresHistoryProvider, SessionOwnerStore
from chemclaw.config import settings
from tests.pg import migrated_db_or_skip


def test_history_provider_selected_by_config(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """`session_store` picks the durable Postgres provider or the in-memory default."""
    monkeypatch.setattr(settings, "session_store", "memory")
    assert isinstance(_history_provider(), InMemoryHistoryProvider)
    monkeypatch.setattr(settings, "session_store", "postgres")
    assert isinstance(_history_provider(), PostgresHistoryProvider)


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


def test_session_owner_records_null_owner() -> None:
    """A session with no Entra oid (the shared dev principal) is still recorded and found."""

    async def _run() -> None:
        await migrated_db_or_skip()
        store = SessionOwnerStore()
        await store.record("sess-owner-null", None)
        assert await store.lookup("sess-owner-null") == (True, None)

    asyncio.run(_run())


def test_listing_returns_only_the_owners_sessions_newest_first() -> None:
    """`list_for_owner` is the index behind `GET /sessions` — owner-scoped, ordered, labelled.

    A session id was returned once at creation and never listed again, so a client that lost it
    could only start over, orphaning durable history. This is the query that makes it reachable,
    and it must not leak anyone else's conversations while doing so.
    """

    async def _run() -> None:
        await migrated_db_or_skip()
        history = PostgresHistoryProvider()
        owners = SessionOwnerStore()

        await owners.record("list-alice-used", "list-alice")
        await owners.record("list-alice-empty", "list-alice")
        await owners.record("list-bob", "list-bob")
        await history.save_messages(
            "list-alice-used", [Message(role="user", contents=["what is the pKa of phenol?"])]
        )

        alice = await owners.list_for_owner("list-alice", limit=50)
        assert {session_id for session_id, _, _ in alice} == {
            "list-alice-used",
            "list-alice-empty",
        }

        titles = {session_id: title for session_id, _, title in alice}
        assert "pKa of phenol" in titles["list-alice-used"]
        # Created but never used: no opening message, so no label — not an error.
        assert titles["list-alice-empty"] == ""

        stamps = [created_at for _, created_at, _ in alice]
        assert stamps == sorted(stamps, reverse=True)  # newest first

        assert len(await owners.list_for_owner("list-alice", limit=1)) == 1

    asyncio.run(_run())


def test_listing_matches_a_null_owner_on_the_dev_path() -> None:
    """A NULL owner (the shared dev principal) lists its own sessions.

    `owner = NULL` is never true in SQL, so an `=` comparison would return nothing and the
    listing would be silently empty exactly where `entra_required` is off — i.e. in development,
    where it would look like the feature simply does not work. `IS NOT DISTINCT FROM` is what
    makes the dev path behave like every other.
    """

    async def _run() -> None:
        await migrated_db_or_skip()
        owners = SessionOwnerStore()
        await owners.record("list-devnull", None)

        listed = await owners.list_for_owner(None, limit=50)
        assert "list-devnull" in {session_id for session_id, _, _ in listed}

    asyncio.run(_run())
