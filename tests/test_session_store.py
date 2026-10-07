"""The durable session store persists and resumes a conversation.

The round-trip tests need Postgres and skip without one; the provider-selection test is a pure
unit test of the wiring that makes sessions durable.
"""

import asyncio
import base64
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage, message_to_dict
from psycopg.types.json import Jsonb

from chemclaw.agent.chemclaw_agent import history_provider
from chemclaw.agent.message_migration import LANGCHAIN_SHAPE, convert_stored_messages
from chemclaw.agent.session_store import (
    _OWNER_LIST,
    InMemoryHistoryProvider,
    PostgresHistoryProvider,
    SessionOwnerStore,
    SessionTurnClaims,
    _session_delete_statements,
    decode_session_cursor,
    encode_session_cursor,
    is_degraded_render,
    message_from_row,
)
from chemclaw.api.tool_results import content_address, store_tool_result
from chemclaw.cli.explain import explain
from chemclaw.core import db
from chemclaw.core.config import settings
from chemclaw.core.identity_context import (
    reset_current_correlation_id,
    set_current_correlation_id,
)
from chemclaw.core.metrics import METRICS
from tests.legacy_rows import legacy_text
from tests.pg import create_checkpoint_tables, migrated_db_or_skip

# The counter that separates "one unreadable legacy row" from "the reader is broken for everyone".
_DEGRADED = "chemclaw_degraded_total"


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
    """Empty one session's rows, so a rerun starts from the state the test describes."""
    async with db.connection(settings.postgres_dsn) as conn:
        async with conn.cursor() as cur:
            await cur.execute("DELETE FROM session_messages WHERE session_id = %s", (session_id,))


async def test_messages_survive_a_new_provider_instance() -> None:
    """Saved messages reload through a fresh provider over the same DSN (proxy for a restart)."""
    writer = await _provider_or_skip()
    session_id = "sess-f3-roundtrip"
    turn = [HumanMessage(content="what is the pKa of phenol?")]
    await writer.save_messages(session_id, turn)

    # A brand-new provider instance (as a restarted pod would build) sees the persisted turn.
    reader = PostgresHistoryProvider()
    loaded = await reader.get_messages(session_id)
    assert any("phenol" in str(m.content) for m in loaded)


async def test_unknown_session_loads_empty() -> None:
    """A session with no rows (or a None id) loads to an empty thread, never an error."""
    provider = await _provider_or_skip()
    assert await provider.get_messages("sess-does-not-exist") == []
    assert await provider.get_messages(None) == []


async def test_session_owner_records_and_reattaches() -> None:
    """Ownership persists and a fresh store instance looks it up — the reattach path (F3)."""
    await migrated_db_or_skip()
    writer = SessionOwnerStore()
    await writer.record("sess-owner-1", "alice")
    await writer.record("sess-owner-1", "mallory")  # idempotent: first writer wins

    reader = SessionOwnerStore()  # a restarted pod would build a fresh instance
    assert await reader.lookup("sess-owner-1") == (True, "alice", None)
    assert await reader.lookup("sess-never-created") == (False, None, None)


async def _spoke_in(session_id: str, text: str = "a turn") -> None:
    """Give a session one stored message, which is what makes it a conversation rather than a row.

    Through the real provider rather than a raw INSERT: the listing derives last-activity from
    `session_messages.created_at`, so the two have to agree about what a turn writes.
    """
    await PostgresHistoryProvider().save_messages(session_id, [HumanMessage(content=text)])


async def test_session_owner_lists_only_its_own_sessions_most_recently_used_first() -> None:
    """Listing is owner-scoped and ordered by last stored message, most recent first.

    A dedicated owner per test keeps the assertion independent of other rows in the shared table.
    """
    await migrated_db_or_skip()
    store = SessionOwnerStore()
    await store.record("sess-list-a", "owner-list-test")
    await store.record("sess-list-b", "owner-list-test")
    await store.record("sess-list-other", "someone-else")
    await _spoke_in("sess-list-a")
    await _spoke_in("sess-list-b")
    await _spoke_in("sess-list-other")

    listed = await store.list_for_owner("owner-list-test")
    assert [session_id for session_id, *_ in listed] == ["sess-list-b", "sess-list-a"]
    assert [row[2] for row in listed] == sorted((row[2] for row in listed), reverse=True)
    assert await store.list_for_owner("owner-with-no-sessions") == []

    # The older conversation, returned to, comes back to the top.
    await _spoke_in("sess-list-a", "and one more thing")
    listed = await store.list_for_owner("owner-list-test")
    assert [session_id for session_id, *_ in listed] == ["sess-list-a", "sess-list-b"]


async def test_session_owner_does_not_list_a_session_nobody_spoke_in() -> None:
    """A created-but-unused session is not listed: the last-activity join drops it."""
    await migrated_db_or_skip()
    store = SessionOwnerStore()
    await store.record("sess-warmed-unused", "owner-warmed-test")
    await store.record("sess-warmed-used", "owner-warmed-test")
    await _spoke_in("sess-warmed-used")

    listed = [session_id for session_id, *_ in await store.list_for_owner("owner-warmed-test")]
    assert listed == ["sess-warmed-used"]


async def test_session_owner_keeps_the_title_its_first_turn_gave_it() -> None:
    """A session keeps the title its first turn gave it.

    The route sets the title on every turn, so the `title IS NULL` guard keeps it stable.
    """
    await migrated_db_or_skip()
    store = SessionOwnerStore()
    await store.record("sess-title", "owner-title-test")
    await _spoke_in("sess-title")

    await store.set_title_if_absent("sess-title", "What is the pKa of acetic acid?")
    await store.set_title_if_absent("sess-title", "And in DMSO?")

    listed = await store.list_for_owner("owner-title-test")
    assert [row[3] for row in listed] == ["What is the pKa of acetic acid?"]


async def test_session_owner_lists_an_unnamed_session_rather_than_dropping_it() -> None:
    """A session whose first turn predates the title column is listed with `title=None`.

    Null is the honest value and the row still belongs in the list — hiding a conversation because
    the service cannot name it would lose history to a schema change.
    """
    await migrated_db_or_skip()
    store = SessionOwnerStore()
    await store.record("sess-untitled", "owner-untitled-test")
    await _spoke_in("sess-untitled")

    listed = await store.list_for_owner("owner-untitled-test")
    assert [(row[0], row[3]) for row in listed] == [("sess-untitled", None)]


async def test_session_owner_listing_carries_the_profile_each_session_runs_under() -> None:
    """The listing returns `profile`, which `GET /plans/pending` filters on.

    `None` means the default profile and must come back as `None`.
    """
    await migrated_db_or_skip()
    store = SessionOwnerStore()
    await store.record("sess-profiled", "owner-profile-list-test", "property-lookup")
    await store.record("sess-unprofiled", "owner-profile-list-test")
    await _spoke_in("sess-profiled")
    await _spoke_in("sess-unprofiled")

    listed = await store.list_for_owner("owner-profile-list-test")
    assert {row[0]: row[4] for row in listed} == {
        "sess-profiled": "property-lookup",
        "sess-unprofiled": None,
    }


async def test_session_owner_lists_the_null_owner_sessions() -> None:
    """A NULL owner matches itself when listing; `owner = NULL` would return nothing.

    `_OWNER_LIST`'s second arm, `o.owner IS NULL AND %s::text IS NULL`, matches these rows.
    """
    await migrated_db_or_skip()
    store = SessionOwnerStore()
    await store.record("sess-list-null", None)
    await _spoke_in("sess-list-null")
    listed = await store.list_for_owner(None)
    assert "sess-list-null" in {session_id for session_id, *_ in listed}


def test_the_owner_predicate_stays_indexable() -> None:
    """`_OWNER_LIST` keeps the two-arm shape `session_owners_owner_idx` can serve.

    `IS NOT DISTINCT FROM` is not btree-searchable, so reverting to it would silently force a
    sequential scan. Static; no database.
    """
    assert "IS NOT DISTINCT FROM" not in _OWNER_LIST, (
        "IS NOT DISTINCT FROM cannot use session_owners_owner_idx (046); every GET /sessions "
        "becomes a sequential scan over a table that is never pruned"
    )
    assert "(o.owner = %s OR (o.owner IS NULL AND %s::text IS NULL))" in _OWNER_LIST


async def test_the_session_listing_uses_the_owner_index() -> None:
    """The planner reaches an owner index for the listing statement.

    With `enable_seqscan = off`, a predicate the index can serve yields an index scan at any row
    count. The retired `IS NOT DISTINCT FROM` predicate is checked too, so if Postgres learns to
    index it, this fails and the workaround can go.
    """
    await migrated_db_or_skip()
    store = SessionOwnerStore()
    await store.record("sess-plan-owner", "owner-plan-test")
    await _spoke_in("sess-plan-owner")
    retired = "SELECT o.session_id FROM session_owners o WHERE o.owner IS NOT DISTINCT FROM %s"
    async with await db.connect(settings.postgres_dsn) as conn:
        async with conn.cursor() as cur:
            await cur.execute("SET LOCAL enable_seqscan = off")
            await cur.execute(
                f"EXPLAIN (COSTS OFF) {_OWNER_LIST}",
                ("owner-plan-test", "owner-plan-test", None, None, None, 20),
            )
            shipped = "\n".join(str(row[0]) for row in await cur.fetchall())
            await cur.execute(f"EXPLAIN (COSTS OFF) {retired}", ("owner-plan-test",))
            before = "\n".join(str(row[0]) for row in await cur.fetchall())
    # Either owner-scoped index: the property is that the listing is served from an index on
    # `owner`, and the planner now prefers `(owner, updated_at DESC, session_id DESC)`.
    assert "session_owners_owner_idx" in shipped or "session_owners_owner_updated_idx" in shipped, (
        "GET /sessions reaches no owner-scoped index; the plan was:\n" + shipped
    )
    assert "session_owners_owner_idx" not in before, (
        "IS NOT DISTINCT FROM now reaches the index, so the two-arm predicate in _OWNER_LIST "
        "(and the notes in migrations 039 and 046) no longer describe this Postgres:\n" + before
    )


async def test_session_owner_records_null_owner() -> None:
    """A session with no Entra oid (the shared dev principal) is still recorded and found."""
    await migrated_db_or_skip()
    store = SessionOwnerStore()
    await store.record("sess-owner-null", None)
    assert await store.lookup("sess-owner-null") == (True, None, None)


async def _claims_or_skip() -> SessionTurnClaims:
    """Return a turn-claim store over a migrated database, or skip if none is reachable."""
    await migrated_db_or_skip()
    return SessionTurnClaims()


async def test_a_second_process_cannot_claim_a_session_that_is_already_running() -> None:
    """Two separate claim stores, modelling two workers, cannot both hold one session.

    The claim is one statement, so check and take cannot interleave; releasing frees the slot.
    """
    worker_a = await _claims_or_skip()
    worker_b = SessionTurnClaims()
    session_id = "sess-d120-exclusive"
    await worker_a.release(session_id, "a")  # a previous run's residue must not decide this

    assert await worker_a.claim(session_id, "a", 60.0) is True
    assert await worker_b.claim(session_id, "b", 60.0) is False
    await worker_a.release(session_id, "a")
    assert await worker_b.claim(session_id, "b", 60.0) is True
    await worker_b.release(session_id, "b")


async def test_a_crashed_workers_claim_ages_out_and_a_refresh_holds_it() -> None:
    """An expired lease is takeable; a refreshed one is not.

    Expiry recovers from a killed worker; refresh keeps a long turn from being declared dead.
    """
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


async def test_the_transcript_read_returns_the_whole_session_not_a_window() -> None:
    """`get_messages` returns the whole session, not a window.

    Its reader is a person reloading the conversation, and a silently truncated transcript looks
    like it started later. Compaction bounds the table by deleting whole pairing components.
    """
    await migrated_db_or_skip()
    provider = PostgresHistoryProvider()
    session_id = "sess-no-window"
    async with db.connection(settings.postgres_dsn) as conn:
        async with conn.cursor() as cur:
            await cur.execute("DELETE FROM session_messages WHERE session_id = %s", (session_id,))
    turns = 80  # comfortably past any plausible default window
    for index in range(turns):
        await provider.save_messages(session_id, [HumanMessage(content=f"question {index}")])

    loaded = await provider.get_messages(session_id)
    assert [m.content for m in loaded] == [f"question {index}" for index in range(turns)], (
        f"the transcript read returned {len(loaded)} of {turns} messages — a window would "
        "make a reloaded conversation look like it began later than it did"
    )


async def test_a_structured_turn_survives_the_round_trip_with_its_calls_intact() -> None:
    """A structured turn survives the round trip with its tool calls intact.

    Asserted by identity (classes, the call and its pairing id), since the degraded fallback would
    still match a substring. The `session_transcript` degraded counter must not move on the happy
    path.
    """
    writer = await _provider_or_skip()
    session_id = "sess-f3-structured"
    await _clear(session_id)
    turn = [
        HumanMessage(content="what is the pKa of phenol?"),
        AIMessage(
            content="let me check",
            tool_calls=[{"name": "predict_pka", "args": {"smiles": "Oc1ccccc1"}, "id": "c-1"}],
        ),
        ToolMessage(content="9.95", tool_call_id="c-1"),
    ]
    before = METRICS.value(_DEGRADED)
    await writer.save_messages(session_id, turn)

    loaded = await PostgresHistoryProvider().get_messages(session_id)

    assert [type(message) for message in loaded] == [HumanMessage, AIMessage, ToolMessage], (
        "the stored shape decided the reader wrong: a tool's answer came back in another voice"
    )
    assert [(c["name"], c["args"], c["id"]) for c in cast(Any, loaded[1]).tool_calls] == [
        ("predict_pka", {"smiles": "Oc1ccccc1"}, "c-1")
    ], "the call the model made is gone from the reloaded turn"
    assert cast(Any, loaded[2]).tool_call_id == "c-1", "the answer no longer names its call"
    assert not [m for m in loaded if is_degraded_render(m)], "a row was recovered, not decoded"
    assert METRICS.value(_DEGRADED) == before, (
        "reading a transcript this system itself wrote counted a degradation, which is the "
        "reader being broken for everyone rather than one legacy row being unreadable"
    )


def test_a_row_that_will_not_convert_is_marked_as_recovered_rather_than_passing_as_a_message() -> (
    None
):
    """A row that will not convert is marked as recovered rather than passing as a message.

    The fallback guesses a message class, so readers must be able to tell a guess from a decoded
    row. No database: this is the reader.
    """
    recovered = message_from_row({"role": "assistant", "contents": ["not a content part"]}, None)
    assert is_degraded_render(recovered), "a recovered row is indistinguishable from a decoded one"

    decoded = message_from_row(message_to_dict(HumanMessage(content="hello")), LANGCHAIN_SHAPE)
    assert not is_degraded_render(decoded), "a decoded row must not be marked as a guess"
    assert decoded.content == "hello"


async def test_the_bounded_user_read_returns_the_chemists_own_words_and_only_those() -> None:
    """`recent_user_texts` returns the chemist's own words and only those, within a bound.

    It is the evidence a `basis="stated"` quote is checked against, so a tool result must never be
    returned as the chemist's words; bounded because it runs once per turn.
    """
    writer = await _provider_or_skip()
    session_id = "sess-stated-quote-window"
    await _clear(session_id)
    for turn in range(4):
        await writer.save_messages(
            session_id,
            [
                HumanMessage(content=f"turn {turn}: 24 wells, no DMF"),
                AIMessage(
                    content="checking",
                    tool_calls=[{"name": "t", "args": {}, "id": f"c-{turn}"}],
                ),
                ToolMessage(content="the plate holds 384 wells", tool_call_id=f"c-{turn}"),
                AIMessage(content="I would use 96 wells"),
            ],
        )

    reader = PostgresHistoryProvider()
    assert await reader.recent_user_texts(session_id, limit=10) == [
        f"turn {turn}: 24 wells, no DMF" for turn in range(4)
    ], "the read returned something other than the chemist's own messages, in order"
    # Bounded, and the bound keeps the *newest* — an older constraint falling out of the window
    # is a refusal a chemist can act on; a newer one falling out is the turn in flight going
    # unquotable.
    assert await reader.recent_user_texts(session_id, limit=2) == [
        "turn 2: 24 wells, no DMF",
        "turn 3: 24 wells, no DMF",
    ]
    assert await reader.recent_user_texts(session_id, limit=0) == []
    assert await reader.recent_user_texts(None, limit=10) == []


async def test_an_unstamped_legacy_row_is_not_offered_as_the_chemists_own_words() -> None:
    """An unstamped legacy row is not offered as the chemist's own words.

    The legacy engine stored rows on every run, so a `user`-role row there may carry tool text. It
    still renders in the transcript.
    """
    writer = await _provider_or_skip()
    session_id = "sess-stated-quote-legacy"
    await _clear(session_id)
    async with db.connection(settings.postgres_dsn) as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "INSERT INTO session_messages (session_id, message) VALUES (%s, %s)",
                (session_id, Jsonb(legacy_text("user", "24 wells, no DMF"))),
            )
    await writer.save_messages(session_id, [HumanMessage(content="ok go ahead")])

    reader = PostgresHistoryProvider()
    assert [type(m) for m in await reader.get_messages(session_id)] == [
        HumanMessage,
        HumanMessage,
    ], "the legacy row stopped rendering in the transcript, which is a separate promise"
    assert await reader.recent_user_texts(session_id, limit=10) == ["ok go ahead"]


async def test_a_converted_legacy_row_is_still_not_offered_as_the_chemists_own_words() -> None:
    """A converted legacy row is still not offered as the chemist's own words.

    `convert_stored_messages` restamps legacy rows, but their provenance is unchanged, so the
    exclusion keys on `message_original`. The conversion runs for real, since it writes that
    discriminator.
    """
    writer = await _provider_or_skip()
    session_id = "sess-stated-quote-converted"
    await _clear(session_id)
    # A MAF `user` row carrying text no person typed — which is the case that matters, since a
    # converted row is indistinguishable from a typed one by shape alone.
    tool_shaped = '{"tool": "screen_hazards", "result": "24 wells, no DMF"}'
    async with db.connection(settings.postgres_dsn) as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "INSERT INTO session_messages (session_id, message) VALUES (%s, %s)",
                (session_id, Jsonb(legacy_text("user", tool_shaped))),
            )
    await writer.save_messages(session_id, [HumanMessage(content="ok go ahead")])

    reader = PostgresHistoryProvider()
    assert await reader.recent_user_texts(session_id, limit=10) == ["ok go ahead"], (
        "the MAF row was quotable before the conversion, so this test proves nothing about it"
    )

    outcome = await convert_stored_messages()
    assert outcome.converted >= 1, "the conversion pass rewrote nothing, so the axis never moved"
    async with db.connection(settings.postgres_dsn) as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT message_shape, message->>'type', message_original IS NOT NULL "
                "FROM session_messages WHERE session_id = %s ORDER BY id LIMIT 1",
                (session_id,),
            )
            converted = await cur.fetchone()
    assert converted == (LANGCHAIN_SHAPE, "human", True), (
        "the row under test was not converted, so the shape predicate would still exclude it"
    )

    assert await reader.recent_user_texts(session_id, limit=10) == ["ok go ahead"], (
        "a converted MAF row became quotable as the chemist's own words"
    )
    assert [type(m) for m in await reader.get_messages(session_id)] == [
        HumanMessage,
        HumanMessage,
    ], "the converted row stopped rendering in the transcript, which is a separate promise"


async def test_the_in_memory_provider_answers_the_bounded_read_the_same_way() -> None:
    """A check that behaves differently under `session_store="memory"` is a check with a bypass."""
    provider = InMemoryHistoryProvider()
    state: dict[str, Any] = {}
    await provider.save_messages(
        "sess-mem",
        [
            HumanMessage(content="24 wells, no DMF"),
            AIMessage(content="I would use 96 wells"),
            HumanMessage(content="ok go ahead"),
        ],
        state=state,
    )
    assert await provider.recent_user_texts("sess-mem", limit=10, state=state) == [
        "24 wells, no DMF",
        "ok go ahead",
    ]
    assert await provider.recent_user_texts("sess-mem", limit=1, state=state) == ["ok go ahead"]
    assert await provider.recent_user_texts("sess-mem", limit=10, state=None) == []


async def test_a_stored_message_carries_the_correlation_id_of_the_turn_that_wrote_it() -> None:
    """A stored message carries the correlation id of the turn that wrote it.

    That id joins a transcript row to its turn's audit rows and job records; `chemclaw explain`
    groups by it. Asserted as the grouping, through the real reconstruction over the real table.
    """
    writer = await _provider_or_skip()
    session_id = "sess-correlated"
    await _clear(session_id)
    for correlation_id, question in (("corr-a", "first question"), ("corr-b", "second")):
        token = set_current_correlation_id(correlation_id)
        try:
            await writer.save_messages(session_id, [HumanMessage(content=question)])
        finally:
            reset_current_correlation_id(token)

    async with db.connection(settings.postgres_dsn) as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT correlation_id FROM session_messages WHERE session_id = %s ORDER BY id",
                (session_id,),
            )
            stamped = [row[0] for row in await cur.fetchall()]
    assert stamped == ["corr-a", "corr-b"], "a message cannot be joined to its own turn"

    report = "\n".join(await explain(session_id))
    assert "── turn corr-a" in report and "── turn corr-b" in report
    assert "unattributed" not in report, (
        "the reconstruction collapsed two turns into one pseudo-turn, which is what an "
        "unstamped row looks like to every reader of this table"
    )


def test_a_cursor_round_trips_its_position_exactly_and_refuses_anything_else() -> None:
    """A cursor round-trips its sort key to the microsecond, and nothing else parses.

    A lossy timestamp would name a different position. Every malformed token raises one
    `ValueError` the route turns into a 422, never a decode error reaching the handler.
    """
    stamp = datetime(2026, 8, 27, 14, 3, 2, 123456, tzinfo=UTC)
    cursor = encode_session_cursor(stamp, "sess-cursor-1")
    assert decode_session_cursor(cursor) == (stamp, "sess-cursor-1")
    assert "sess-cursor-1" not in cursor, "the cursor spells its own contents in the clear"

    for forged in ("", "!!!!", "not-base64!", base64.urlsafe_b64encode(b"no-separator").decode()):
        with pytest.raises(ValueError):
            decode_session_cursor(forged)
    with pytest.raises(ValueError):
        decode_session_cursor(base64.urlsafe_b64encode(b"not-a-timestamp|sess-x").decode())


def test_the_session_listing_pages_past_its_ceiling_without_skipping_or_repeating() -> None:
    """The session listing pages past its ceiling without skipping or repeating.

    All six sessions come back through a page size of two. The list is ordered by last activity
    and mutates while being read, so a keyset cursor is used; a session is created and another
    revived between pages, and every unseen one must still arrive exactly once.
    """

    async def _run() -> list[str]:
        await migrated_db_or_skip()
        store = SessionOwnerStore()
        owner = "owner-page-test"
        sessions = [f"sess-page-{index}" for index in range(6)]
        for session_id in sessions:
            await store.record(session_id, owner)
            await _spoke_in(session_id)

        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(settings, "service_max_listed_sessions", 2)
        try:
            seen: list[str] = []
            cursor: str | None = None
            disturbed = False
            while True:
                page = await store.page_for_owner(owner, after=cursor)
                if not page:
                    break
                seen.extend(session_id for session_id, *_ in page)
                cursor = encode_session_cursor(page[-1][2], page[-1][0])
                if not disturbed:
                    # The list reorders itself under the reader: a brand-new conversation lands
                    # above the cursor, and the newest already-seen one is spoken in again, which
                    # moves it back to the top of an ordering the reader has already passed.
                    disturbed = True
                    await store.record("sess-page-new", owner)
                    await _spoke_in("sess-page-new")
                    await _spoke_in(sessions[-1], "and one more thing")
            return seen
        finally:
            monkeypatch.undo()

    seen = asyncio.run(_run())
    expected = [f"sess-page-{index}" for index in reversed(range(6))]
    assert seen == expected, (
        f"paging returned {seen}; every session must appear exactly once, newest first, even "
        "though the list was reordered between pages"
    )
    assert len(seen) == len(set(seen)), f"a row was served twice while paging: {seen}"


async def _rows(table: str, column: str, value: str) -> int:
    """How many rows of `table` carry `value` in `column` — the delete's own evidence."""
    async with db.connection(settings.postgres_dsn) as conn:
        async with conn.cursor() as cur:
            await cur.execute(f"SELECT count(*) FROM {table} WHERE {column} = %s", (value,))
            row = await cur.fetchone()
    return int(row[0]) if row else 0


def test_deleting_a_session_clears_every_table_it_reaches_and_no_one_elses() -> None:
    """Deleting a session clears every table it reaches, and no one else's.

    The table set is the erasure sweep's own, and the ownership row must go with the rest.
    Content-addressed blobs shared with a bystander session survive.
    """

    async def _run() -> tuple[dict[str, int], dict[str, int], int, int]:
        await migrated_db_or_skip()
        await create_checkpoint_tables()
        store = SessionOwnerStore()
        doomed, bystander = "sess-delete-mine", "sess-delete-theirs"
        for session_id, owner in ((doomed, "owner-delete-test"), (bystander, "owner-delete-other")):
            await store.record(session_id, owner)
            await _spoke_in(session_id)
            async with db.connection(settings.postgres_dsn) as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        "INSERT INTO session_events (session_id, kind) VALUES (%s, 'job-result')",
                        (session_id,),
                    )
                    # All three checkpoint tables, because they are one conversation's graph
                    # state split across three keys with no foreign key between them: a delete
                    # that took `checkpoints` and left the blobs would leave the payload behind.
                    await cur.execute(
                        "INSERT INTO checkpoints "
                        "(thread_id, checkpoint_ns, checkpoint_id, checkpoint, metadata) "
                        "VALUES (%s, '', 'ckpt-1', '{}'::jsonb, '{}'::jsonb)",
                        (session_id,),
                    )
                    await cur.execute(
                        "INSERT INTO checkpoint_blobs "
                        "(thread_id, checkpoint_ns, channel, version, type, blob) "
                        "VALUES (%s, '', 'messages', '1', 'msgpack', %s)",
                        (session_id, b"payload"),
                    )
                    await cur.execute(
                        "INSERT INTO checkpoint_writes (thread_id, checkpoint_ns, checkpoint_id, "
                        "task_id, idx, channel, type, blob) "
                        "VALUES (%s, '', 'ckpt-1', 'task-1', 0, 'messages', 'msgpack', %s)",
                        (session_id, b"payload"),
                    )
                    # A shared session's two tables (`infra/sql/110_shared_sessions.sql`): one
                    # member and one plan author each, so the bystander check covers them too.
                    await cur.execute(
                        "INSERT INTO session_members (session_id, actor) VALUES (%s, 'member')",
                        (session_id,),
                    )
                    await cur.execute(
                        "INSERT INTO plan_authors (session_id, plan_hash, actor) "
                        "VALUES (%s, 'plan', 'member')",
                        (session_id,),
                    )
                    # A waiting message's place in line (`infra/sql/113_session_turn_queue.sql`).
                    await cur.execute(
                        "INSERT INTO session_turn_queue (session_id, sender, lease_until) "
                        "VALUES (%s, 'member', now() + interval '1 minute')",
                        (session_id,),
                    )
                    # A request from another replica to the session's turn, with a frame in
                    # transit (`infra/sql/121_session_turn_remotes.sql`); the frame cascades.
                    await cur.execute(
                        "INSERT INTO session_turn_remotes (id, session_id, holder, kind, actor, "
                        "state, lease_until) "
                        "VALUES (%s, %s, 'h', 'watch', 'member', 'watching', now())",
                        (f"remote-{session_id}", session_id),
                    )
                    await cur.execute(
                        "INSERT INTO session_turn_frames (remote_id, frame) VALUES (%s, NULL)",
                        (f"remote-{session_id}",),
                    )
                    # An artefact and a revision under it (`infra/sql/115_session_exhibits.sql`):
                    # the header is what the delete names, and the revision is what the cascade
                    # has to take with it.
                    await cur.execute(
                        "INSERT INTO session_exhibits (exhibit_id, session_id, kind, title, "
                        "head_revision, head_author_kind, head_author, agent_seen_revision, "
                        "created_by, correlation_id) "
                        "VALUES (%s, %s, 'document', 't', 1, 'agent', 'a', 1, 'a', '')",
                        (f"xb-{session_id[-16:]}", session_id),
                    )
                    await cur.execute(
                        "INSERT INTO session_exhibit_revisions (exhibit_id, revision, "
                        "parent_revision, author_kind, author, change_note, spec, byte_size, "
                        "correlation_id) VALUES (%s, 1, 0, 'agent', 'a', '', "
                        '\'{"kind": "document", "markdown": "x"}\'::jsonb, 1, \'\')',
                        (f"xb-{session_id[-16:]}",),
                    )
                    # An uploaded file (`infra/sql/120_session_attachments.sql`).
                    await cur.execute(
                        "INSERT INTO session_attachments (session_id, name, content_type, body, "
                        "byte_size, uploaded_by) VALUES (%s, 'runs.csv', 'text/csv', 'a,b', 3, "
                        "'member')",
                        (session_id,),
                    )
                await conn.commit()
            await SessionTurnClaims().claim(session_id, f"holder-{session_id}", 60)
            # One result only this session has, and one both of them do — the same bytes under the
            # same content address, which is what the store's dedup makes of an identical answer.
            await store_tool_result(
                session_id=session_id,
                correlation_id="corr-delete",
                tool="screen_hazards",
                text=f"private to {session_id}",
            )
            await store_tool_result(
                session_id=session_id,
                correlation_id="corr-delete",
                tool="screen_hazards",
                text="a result both conversations produced",
            )

        shared_ref = content_address("a result both conversations produced")
        removed = await store.delete_session(doomed)
        survivors = {
            table: await _rows(
                table, "thread_id" if table.startswith("checkpoint") else "session_id", bystander
            )
            for table, _ in _session_delete_statements()
            if table != "tool_result_blobs"
        }
        return (
            removed,
            survivors,
            await _rows("tool_result_links", "session_id", doomed),
            await _rows("tool_result_blobs", "content_hash", shared_ref),
        )

    removed, survivors, doomed_links, shared_blobs = asyncio.run(_run())

    assert set(removed) == {table for table, _ in _session_delete_statements()}, (
        "the report must name every table the sweep covers, so an operator comparing two runs "
        f"sees the same keys: {sorted(removed)}"
    )
    for table in (
        "session_messages",
        "session_events",
        "session_turns",
        "session_members",
        "plan_authors",
        "session_turn_queue",
        "session_turn_remotes",
        "session_exhibits",
        "session_attachments",
        "session_owners",
    ):
        assert removed[table] == 1, f"{table} kept a deleted session's row: {removed}"
    for table in ("checkpoints", "checkpoint_blobs", "checkpoint_writes"):
        assert removed[table] == 1, f"the graph state outlived the conversation: {removed}"
    assert removed["tool_result_blobs"] == 1, (
        "the session's own stored result was not deleted (or the shared one was): "
        f"{removed['tool_result_blobs']} blob(s)"
    )
    assert shared_blobs == 1, (
        "deleting one conversation removed bytes another one links to — the cascade would have "
        "taken the bystander's link row with them"
    )
    assert doomed_links == 1, (
        "the deleted session's link to the shared blob should be the only thing left of it "
        "(DELETE on tool_result_links is deliberately withheld; retention collects it with the "
        f"blob), found {doomed_links}"
    )
    assert all(count == 1 for count in survivors.values()), (
        f"deleting one session took rows from another: {survivors}"
    )


def test_deleting_a_session_leaves_what_belongs_to_the_person() -> None:
    """Deleting a session leaves what belongs to the person: preferences and subscriptions.

    `_ACTOR_SCOPED_ONLY` classifies those tables within the same erasure set.
    """

    async def _run() -> tuple[int, int]:
        await migrated_db_or_skip()
        store = SessionOwnerStore()
        owner = "owner-keeps-their-own"
        await store.record("sess-delete-personal", owner)
        await _spoke_in("sess-delete-personal")
        async with db.connection(settings.postgres_dsn) as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "INSERT INTO user_preferences (owner, key, value) "
                    "VALUES (%s, 'solvent', '2-MeTHF') ON CONFLICT DO NOTHING",
                    (owner,),
                )
                await cur.execute(
                    "INSERT INTO subscriptions (owner, query) VALUES (%s, 'nitration') "
                    "ON CONFLICT DO NOTHING",
                    (owner,),
                )
            await conn.commit()

        await store.delete_session("sess-delete-personal")
        return (
            await _rows("user_preferences", "owner", owner),
            await _rows("subscriptions", "owner", owner),
        )

    preferences, subscriptions = asyncio.run(_run())
    assert (preferences, subscriptions) == (1, 1), (
        "deleting one conversation took data that belongs to the person, not to it: "
        f"{preferences} preference(s), {subscriptions} subscription(s) left"
    )


# The sort key the sidebar orders by. The tests below show the mirrored column has one definition,
# enumerable writers, and that listing membership does not depend on it.
_OWNER_UPDATED_INDEX = "session_owners_owner_updated_idx"


async def _newest_message(session_id: str) -> datetime | None:
    """`max(session_messages.created_at)` for one session — what `updated_at` is defined to be."""
    async with await db.connect(settings.postgres_dsn) as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT max(created_at) FROM session_messages WHERE session_id = %s", (session_id,)
            )
            row = await cur.fetchone()
    return row[0] if row else None


async def _stored_updated_at(session_id: str) -> datetime | None:
    """The mirrored column, read raw."""
    async with await db.connect(settings.postgres_dsn) as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT updated_at FROM session_owners WHERE session_id = %s", (session_id,)
            )
            row = await cur.fetchone()
    return row[0] if row else None


def test_the_sort_key_is_what_it_is_defined_to_be_after_every_writer() -> None:
    """`session_owners.updated_at` is `max(session_messages.created_at)`, at both writers.

    Covers a new session then turns, a transcript then the ownership row (the fork's order), and a
    session with nothing said (NULL). Deleted messages can leave the column stale, but membership
    is the `EXISTS` arm, so such a session is still dropped.
    """

    async def _run() -> tuple[bool, bool, bool, bool, bool, bool]:
        await migrated_db_or_skip()
        store = SessionOwnerStore()
        await store.record("sess-mirror-turns", "owner-mirror")
        await _spoke_in("sess-mirror-turns")
        after_first = await _stored_updated_at("sess-mirror-turns")
        await _spoke_in("sess-mirror-turns", "and one more thing")
        turns_exact = await _stored_updated_at("sess-mirror-turns") == await _newest_message(
            "sess-mirror-turns"
        )
        later = await _stored_updated_at("sess-mirror-turns")
        # Both halves explicitly, because `None < None` is not the comparison this is asking and a
        # fallback of `now()` on each side makes an unmaintained column read as a moving one.
        moved = after_first is not None and later is not None and after_first < later

        # The fork's order: the transcript lands under an id that has no ownership row yet.
        async with db.connection(settings.postgres_dsn) as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "INSERT INTO session_messages (session_id, message, message_shape) "
                    "VALUES (%s, %s, %s)",
                    (
                        "sess-mirror-fork",
                        Jsonb({"type": "human", "content": "copied"}),
                        "langchain",
                    ),
                )
            await conn.commit()
        await store.record("sess-mirror-fork", "owner-mirror")
        fork_exact = await _stored_updated_at("sess-mirror-fork") == await _newest_message(
            "sess-mirror-fork"
        )

        await store.record("sess-mirror-silent", "owner-mirror")
        never_spoken = await _stored_updated_at("sess-mirror-silent") is None

        # What `durable/retention.py` does to a session past its message window: the rows go, the
        # ownership row stays until a later pass, and the mirror still names the activity they
        # carried. The listing must drop it anyway — that is what the `EXISTS` arm is for.
        async with db.connection(settings.postgres_dsn) as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "DELETE FROM session_messages WHERE session_id = %s", ("sess-mirror-turns",)
                )
            await conn.commit()
        stale = await _stored_updated_at("sess-mirror-turns") is not None
        listed = {row[0] for row in await store.page_for_owner("owner-mirror")}
        return turns_exact, moved, fork_exact, never_spoken, stale, "sess-mirror-turns" in listed

    turns_exact, moved, fork_exact, never_spoken, stale, pruned_listed = asyncio.run(_run())

    assert moved, (
        "the second turn did not move the sort key, so the sidebar sorts on stale activity"
    )
    assert turns_exact, "the mirrored sort key is not max(created_at) after a turn"
    assert fork_exact, (
        "an ownership row written after its transcript carries no sort key, so a fork is invisible "
        "to GET /sessions — the failure agent/session_fork.py enumerates as its second"
    )
    assert never_spoken, "a session nothing was said in must carry NULL, not a timestamp"
    assert stale, "the fixture did not reach the case: nothing was left to be stale"
    assert not pruned_listed, (
        "a session whose messages have been pruned is still in the listing, so the mirrored column "
        "— not the table — is deciding which sessions exist; the `EXISTS` arm in `_OWNER_LIST` is "
        "what keeps a stale sort key from inventing a conversation"
    )


def test_the_session_listing_orders_from_an_index_rather_than_sorting_every_session() -> None:
    """The listing answers its `ORDER BY … LIMIT` from an index rather than sorting every session.

    Asked with `enable_sort = off`: the absence of a `Sort` node is what "the LIMIT stops early"
    means. A NULL owner's page still filters and sorts, since `owner = NULL` is not
    index-searchable.
    """

    async def _run() -> str:
        await migrated_db_or_skip()
        store = SessionOwnerStore()
        await store.record("sess-ordered-plan", "owner-ordered-plan")
        await _spoke_in("sess-ordered-plan")
        async with await db.connect(settings.postgres_dsn) as conn:
            async with conn.cursor() as cur:
                await cur.execute("SET LOCAL enable_sort = off")
                await cur.execute(
                    f"EXPLAIN (COSTS OFF) {_OWNER_LIST}",
                    ("owner-ordered-plan", "owner-ordered-plan", None, None, None, 20),
                )
                return "\n".join(str(row[0]) for row in await cur.fetchall())

    plan = asyncio.run(_run())

    assert _OWNER_UPDATED_INDEX in plan, (
        f"the session listing cannot reach {_OWNER_UPDATED_INDEX} (092), so its ORDER BY is a sort "
        "over every session the owner has ever created; the plan was:\n" + plan
    )
    assert "Sort" not in plan, (
        "the listing still sorts to produce its order, which is the cost 092 removed — the page "
        "stops after LIMIT rows only if the index supplies the ordering:\n" + plan
    )


def test_only_the_two_known_statements_write_the_table_the_sort_key_mirrors() -> None:
    """Only the two known statements write `session_messages`, the table the sort key mirrors.

    A third writer that does not maintain `updated_at` fails here by name.
    """
    package = Path(__file__).resolve().parents[1] / "src" / "chemclaw"
    writers = sorted(
        path.relative_to(package).as_posix()
        for path in package.rglob("*.py")
        if "INSERT INTO session_messages" in path.read_text(encoding="utf-8")
    )
    assert writers == ["agent/session_fork.py", "agent/session_store.py"], (
        f"{', '.join(writers)} appends to session_messages. Every writer of that table has to "
        "leave session_owners.updated_at equal to max(created_at) for the session (092) — either "
        "run `_OWNER_TOUCH` in the same transaction, or write the ownership row afterwards through "
        "`_OWNER_INSERT`, which derives it"
    )


def test_the_batch_turn_claims_take_refresh_and_release_only_what_is_theirs() -> None:
    """The batch turn claims take, refresh and release only what is theirs.

    Each is asserted against a claim held by another holder in the same batch.
    """

    async def _run() -> tuple[set[str], set[str], set[str], str | None, str | None]:
        await migrated_db_or_skip()
        claims = SessionTurnClaims()
        ours, theirs = "sess-batch-ours", "sess-batch-theirs"
        assert await claims.claim(theirs, "another-worker", 60.0)
        taken = await claims.claim_many([ours, theirs], "sweep-1", 60.0)
        refreshed = await claims.refresh_many([ours, theirs], "sweep-1", 60.0)
        other = await claims.other_holders([ours, theirs], "sweep-1")
        await claims.release_many([ours, theirs], "sweep-1")
        async with await db.connect(settings.postgres_dsn) as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT session_id, holder FROM session_turns WHERE session_id = ANY(%s)",
                    ([ours, theirs],),
                )
                left = {str(row[0]): str(row[1]) for row in await cur.fetchall()}
        await claims.release(theirs, "another-worker")
        return taken, refreshed, other, left.get(ours), left.get(theirs)

    taken, refreshed, other, ours_left, theirs_left = asyncio.run(_run())

    assert taken == {"sess-batch-ours"}, (
        f"the batch claim took {sorted(taken)}: a session another worker is running a turn on is "
        "not this sweep's to take, however many are asked for at once"
    )
    assert refreshed == {"sess-batch-ours"}, (
        f"the batch refresh extended {sorted(refreshed)} — a claim that is not ours, which is the "
        "takeover `_TURN_REFRESH`'s holder guard exists to refuse"
    )
    assert other == {"sess-batch-theirs"}, (
        f"the sweep was told {sorted(other)} is held elsewhere; that answer is what separates a "
        "genuine takeover from its own erase transaction holding the rows"
    )
    assert ours_left is None, "the batch release left this sweep's own claim behind"
    assert theirs_left == "another-worker", (
        f"the batch release removed another holder's claim (left: {theirs_left}), which is a live "
        "turn's turn slot"
    )


def test_the_two_session_delete_orders_really_do_deadlock() -> None:
    """The route's session delete and the retention delete really do deadlock.

    They take `session_turns` and `session_owners` in opposite orders, and neither order can change.
    Exactly one transaction must abort; which one is deliberately not asserted, because Postgres
    chooses. Real concurrency on two real connections.
    """

    async def _run() -> None:
        await migrated_db_or_skip()
        import psycopg

        session_id = "sess-deadlock-cycle"
        async with db.connection(settings.postgres_dsn) as seed:
            await seed.execute(
                "INSERT INTO session_owners (session_id, owner) VALUES (%s, 'alice') "
                "ON CONFLICT (session_id) DO UPDATE SET owner = 'alice'",
                (session_id,),
            )
            await seed.execute(
                "INSERT INTO session_turns (session_id, holder, expires_at) "
                "VALUES (%s, 'w1', now() + interval '1 hour') "
                "ON CONFLICT (session_id) DO UPDATE SET holder = 'w1'",
                (session_id,),
            )

        holding = asyncio.Event()
        go = asyncio.Event()

        async def _in_order(first: str, second: str, ready: asyncio.Event | None) -> str:
            """Take `first`'s row lock, wait for the other side, then reach for `second`'s."""
            conn = await psycopg.AsyncConnection.connect(settings.postgres_dsn)
            try:
                await conn.execute(f"DELETE FROM {first} WHERE session_id = %s", (session_id,))
                if ready is not None:
                    ready.set()
                await go.wait()
                await conn.execute(f"DELETE FROM {second} WHERE session_id = %s", (session_id,))
                await conn.commit()
                return "committed"
            except (psycopg.errors.DeadlockDetected, psycopg.errors.SerializationFailure):
                await conn.rollback()
                return "aborted"
            finally:
                await conn.close()

        # The two real orders, spelled out rather than imported, because what is under test is that
        # *these two sequences* cycle — a test that ran one order against itself would deadlock
        # never, and one that imported both statement sets would be asserting SQL rather than locks.
        route = asyncio.create_task(_in_order("session_turns", "session_owners", holding))
        prune = asyncio.create_task(_in_order("session_owners", "session_turns", None))
        await asyncio.wait_for(holding.wait(), timeout=30)
        await asyncio.sleep(0.5)
        go.set()
        outcomes = await asyncio.wait_for(asyncio.gather(route, prune), timeout=120)

        assert sorted(outcomes) == ["aborted", "committed"], (
            "the two delete orders did not deadlock, so this run is evidence about nothing: "
            f"{outcomes}"
        )

    asyncio.run(_run())


def test_deleting_a_session_survives_being_the_deadlock_victim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A session delete chosen as the deadlock victim is retried and lands.

    The abort is injected, since the route's window cannot be raced reliably; the sibling test
    proves the cycle. Both the answer and the rows being gone are asserted.
    """

    async def _run() -> None:
        await migrated_db_or_skip()
        import psycopg

        session_id = "sess-deadlock-victim"
        store = SessionOwnerStore()
        await store.record(session_id, "alice")
        await _spoke_in(session_id)

        once = SessionOwnerStore._delete_session_once
        attempts: list[int] = []

        async def _aborts_first(
            self: SessionOwnerStore, sid: str, statements: tuple[tuple[str, str], ...]
        ) -> dict[str, int]:
            attempts.append(1)
            if len(attempts) == 1:
                raise psycopg.errors.DeadlockDetected("deadlock detected")
            return await once(self, sid, statements)

        monkeypatch.setattr(SessionOwnerStore, "_delete_session_once", _aborts_first)
        removed = await store.delete_session(session_id)

        assert len(attempts) == 2, f"the aborted transaction was not tried again: {attempts}"
        assert removed, f"the delete answered with no counts at all: {removed}"
        assert await store.lookup(session_id) == (False, None, None), (
            "the delete answered without removing the ownership row, so the retry reported success "
            "over a transaction that never ran"
        )

    asyncio.run(_run())
