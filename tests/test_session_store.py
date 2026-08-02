"""The durable session store persists and resumes a conversation (plan Phase F3-T1).

The round-trip test runs against a real database (CI provides Postgres; the offline sandbox has
none, so it skips). The provider-selection test is a pure unit test with no database — it proves
`build_agent` swaps the history provider by config, which is the wiring that makes sessions durable.
"""

import asyncio

import pytest
from agent_framework import Content, InMemoryHistoryProvider, Message

from chemclaw.agent.chemclaw_agent import history_provider
from chemclaw.agent.message_pairing import unmatched_call_ids, unmatched_result_ids
from chemclaw.agent.session_store import (
    PostgresHistoryProvider,
    SessionOwnerStore,
    SessionTurnClaims,
    _crossed_new_compaction_bucket,
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
        await provider.rollback_to(session_id, 0)  # a clean slate for a rerun
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


def test_repair_rewrites_the_right_rows_when_an_earlier_one_is_dropped() -> None:
    """A dropped row must not shift the rewrite onto a later row's message.

    Regression guard: pairing the stored row ids against the repaired list *positionally* works
    only while the two are the same length. The moment one message is discarded outright, every
    row after it lines up against the wrong message — so a trimmed message would be written over
    an unrelated row, corrupting history the repair was supposed to be saving.
    """

    async def _run() -> None:
        provider = await _provider_or_skip()
        session_id = "sess-orphan-shift"
        await provider.rollback_to(session_id, 0)
        await provider.save_messages(
            session_id,
            [
                Message(role="user", contents=["first, a question"]),
                # Dropped entirely (nothing but the orphan call) — this is what shifts the list.
                Message(
                    role="assistant",
                    contents=[
                        Content.from_function_call(
                            call_id="c-gone", name="predict_pka", arguments={}
                        )
                    ],
                ),
                # Trimmed, not dropped: its prose must survive, on its own row.
                Message(
                    role="assistant",
                    contents=[
                        Content.from_text("and some prose worth keeping"),
                        Content.from_function_call(
                            call_id="c-trim", name="screen_hazards", arguments={}
                        ),
                    ],
                ),
            ],
        )

        await provider.get_messages(session_id)  # triggers the repair + write-back
        reloaded = await PostgresHistoryProvider().get_messages(session_id)

        assert [m.text for m in reloaded] == ["first, a question", "and some prose worth keeping"]
        assert unmatched_call_ids(reloaded) == set()

    asyncio.run(_run())


def test_a_matched_pair_is_never_touched_by_the_repair() -> None:
    """A complete call/result pair round-trips intact — the repair must not eat healthy history."""

    async def _run() -> None:
        provider = await _provider_or_skip()
        session_id = "sess-orphan-healthy"
        await provider.rollback_to(session_id, 0)
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
        await provider.rollback_to(session_id, 0)
        await provider.save_messages(session_id, [Message(role="user", contents=["turn one"])])

        watermark = await provider.latest_message_id(session_id)
        assert watermark is not None
        await provider.save_messages(session_id, [Message(role="user", contents=["turn two"])])

        assert await provider.rollback_to(session_id, watermark) == 1
        remaining = await provider.get_messages(session_id)
        assert [m.text for m in remaining] == ["turn one"]  # the committed turn is untouched

    asyncio.run(_run())


def test_watermark_is_none_for_a_session_with_no_history() -> None:
    """A first turn reads no watermark; the caller decides that means 0, the store never guesses.

    `None` here is the store saying "no history yet" — and only that. The runner maps it to a
    watermark of 0 itself, because `rollback_to` deliberately no longer accepts `None`: the same
    value used to also mean "the read failed", and defaulting it to 0 turned a failed read plus a
    disconnect into a full history wipe.
    """

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


# --- R4.2: `_compact` replans only on a fresh floor-bucket crossing, not every turn above it ----


def test_below_the_floor_never_crosses() -> None:
    """Neither count sits at or past the floor: no replan, whatever the two counts are."""
    assert _crossed_new_compaction_bucket(5, 9, floor=12) is False


def test_first_time_reaching_the_floor_crosses() -> None:
    """The turn whose insert pushes the count from under the floor to at/over it must replan."""
    assert _crossed_new_compaction_bucket(195, 200, floor=200) is True


def test_staying_in_the_same_bucket_above_the_floor_does_not_cross() -> None:
    """This is the defect: growing from 201 to 202 must not repeat the full read + replan."""
    assert _crossed_new_compaction_bucket(201, 202, floor=200) is False


def test_growing_into_the_next_bucket_crosses_again() -> None:
    """Once the count reaches the *next* multiple of the floor, it is due again."""
    assert _crossed_new_compaction_bucket(399, 400, floor=200) is True


def test_a_multi_message_turn_that_skips_straight_past_a_bucket_still_crosses() -> None:
    """A multi-row turn can jump the count clean over a boundary; it must still trigger.

    Bucket membership is what matters, not landing exactly on a multiple.
    """
    assert _crossed_new_compaction_bucket(190, 210, floor=200) is True


def test_a_negative_before_count_clamps_rather_than_crashing() -> None:
    """`inserted` could in principle exceed `count` under a racing write.

    Must not raise or go negative through the bucket math.
    """
    assert _crossed_new_compaction_bucket(-5, 200, floor=200) is True  # bucket 0 -> bucket 1
    assert _crossed_new_compaction_bucket(-5, 50, floor=200) is False  # both still bucket 0


# --- D-151: the stored history stops growing without bound -------------------------------------


# Long enough for the window to bind several times over, so the band is visible rather than a
# single sample that could be a local peak.
_TURNS = 60


def _compaction_turn(index: int) -> list[Message]:
    """One turn's worth of stored messages, with a payload big enough to matter."""
    return [
        Message(role="user", contents=[Content.from_text(f"question {index}")]),
        Message(
            role="assistant",
            contents=[
                Content.from_function_call(call_id=f"k{index}", name="predict_pka", arguments={})
            ],
        ),
        Message(
            role="tool",
            contents=[
                Content.from_function_result(call_id=f"k{index}", result="payload " + "z" * 800)
            ],
        ),
        Message(role="assistant", contents=[Content.from_text(f"answer {index}")]),
    ]


def test_durable_compaction_bounds_a_long_session(monkeypatch: pytest.MonkeyPatch) -> None:
    """The defect: without this the row count grows by four per turn, forever.

    Every turn re-reads and deserialises the whole history before the model call, so the cost of
    turn N is O(all turns so far). Retention does not bound it — it prunes by age, is off by
    default, and an age window does not cap one long-running session at all.

    Asserted as *boundedness*, not as a monotone plateau. Measured over 60 turns the count sits in
    a band (14 → 23 → 22 → 18) rather than settling on one number: the sliding window keeps a fixed
    number of conversation groups, and a collapsed group leaves a summary row that is itself evicted
    a few turns later, so the total breathes. A single before/after ratio would catch a local peak
    and flake. What must be true is that the size is a function of the window and not of the number
    of turns — so this compares the second half against the first and against the linear count.
    """
    monkeypatch.setattr(settings, "agent_durable_compaction_enabled", True)
    monkeypatch.setattr(settings, "agent_durable_compaction_min_rows", 12)
    monkeypatch.setattr(settings, "agent_context_token_budget", 2000)

    async def _run() -> tuple[list[int], set[str], set[str]]:
        await migrated_db_or_skip()
        provider = PostgresHistoryProvider()
        session_id = "d146-plateau"
        async with db.connection(settings.postgres_dsn) as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "DELETE FROM session_messages WHERE session_id = %s", (session_id,)
                )
            await conn.commit()

        sizes: list[int] = []
        for index in range(_TURNS):
            await provider.save_messages(session_id, _compaction_turn(index))
            if (index + 1) % 10 == 0:
                sizes.append(len(await provider.get_messages(session_id)))

        final_messages = await provider.get_messages(session_id)
        return (
            sizes,
            unmatched_call_ids(final_messages),
            unmatched_result_ids(final_messages),
        )

    sizes, orphan_calls, orphan_results = asyncio.run(_run())
    linear = _TURNS * 4  # what the table would hold if nothing ever compacted
    assert max(sizes) < linear // 2, (
        f"the history never compacted: peaked at {max(sizes)} rows against {linear} uncompacted"
    )
    # The property that matters: size tracks the window, not the turn count. The second half must
    # not be systematically larger than the first — a growing history fails here even though its
    # absolute size might still be under the bound above.
    half = len(sizes) // 2
    assert max(sizes[half:]) <= max(sizes[:half]) + 12, (
        f"the history is still growing with turn count: {sizes}"
    )
    # And every pass left a thread that can still be sent.
    assert orphan_calls == set(), "compaction left a call without its result"
    assert orphan_results == set(), "compaction stranded a result — the session is bricked"


def test_durable_compaction_is_off_until_a_deployment_asks(monkeypatch: pytest.MonkeyPatch) -> None:
    """Deleting conversation history is a stated policy, never one inherited on upgrade."""
    monkeypatch.setattr(settings, "agent_durable_compaction_enabled", False)
    monkeypatch.setattr(settings, "agent_durable_compaction_min_rows", 4)
    monkeypatch.setattr(settings, "agent_context_token_budget", 500)

    async def _run() -> int:
        await migrated_db_or_skip()
        provider = PostgresHistoryProvider()
        session_id = "d146-off"
        async with db.connection(settings.postgres_dsn) as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "DELETE FROM session_messages WHERE session_id = %s", (session_id,)
                )
            await conn.commit()
        for index in range(6):
            await provider.save_messages(session_id, _compaction_turn(index))
        return len(await provider.get_messages(session_id))

    assert asyncio.run(_run()) == 24, "rows went missing with compaction disabled"
