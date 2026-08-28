"""The durable session store persists and resumes a conversation (plan Phase F3-T1).

The round-trip test runs against a real database (CI provides Postgres; the offline sandbox has
none, so it skips). The provider-selection test is a pure unit test with no database — it proves
`build_agent` swaps the history provider by config, which is the wiring that makes sessions durable.
"""

import asyncio
from typing import Any, cast

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage, message_to_dict

from chemclaw.agent.chemclaw_agent import history_provider
from chemclaw.agent.message_migration import LANGCHAIN_SHAPE
from chemclaw.agent.session_store import (
    InMemoryHistoryProvider,
    PostgresHistoryProvider,
    SessionOwnerStore,
    SessionTurnClaims,
    is_degraded_render,
    message_from_row,
)
from chemclaw.cli.explain import explain
from chemclaw.core import db
from chemclaw.core.config import settings
from chemclaw.core.identity_context import (
    reset_current_correlation_id,
    set_current_correlation_id,
)
from chemclaw.core.metrics import METRICS
from tests.pg import migrated_db_or_skip

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


async def _spoke_in(session_id: str, text: str = "a turn") -> None:
    """Give a session one stored message, which is what makes it a conversation rather than a row.

    Through the real provider rather than a raw INSERT: the listing derives last-activity from
    `session_messages.created_at`, so the two have to agree about what a turn writes.
    """
    await PostgresHistoryProvider().save_messages(session_id, [HumanMessage(content=text)])


def test_session_owner_lists_only_its_own_sessions_most_recently_used_first() -> None:
    """Listing is owner-scoped and most-recently-used first — the sidebar `GET /sessions` renders.

    A dedicated owner string per test: the table is shared across this module's cases, so scoping
    to a real owner is also what keeps the assertion independent of the other rows in there.

    Ordered by the last stored message, not by when the row was created. Those disagree for exactly
    the conversation a chemist is most likely to want — an old one they have come back to — which
    under the previous ordering was pinned to the bottom of the list forever.
    """

    async def _run() -> None:
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

    asyncio.run(_run())


def test_session_owner_does_not_list_a_session_nobody_spoke_in() -> None:
    """A created-but-unused session is not a conversation and is not listed as one.

    The companion UI creates the session on the first keystroke so the first message costs one
    round-trip instead of two, so every abandoned draft leaves an ownership row behind. The lateral
    join that establishes last-activity is what drops them: no messages, no `max(created_at)`, no
    row. Deriving the two facts in one query is why this needs no separate cleanup job.
    """

    async def _run() -> None:
        await migrated_db_or_skip()
        store = SessionOwnerStore()
        await store.record("sess-warmed-unused", "owner-warmed-test")
        await store.record("sess-warmed-used", "owner-warmed-test")
        await _spoke_in("sess-warmed-used")

        listed = [session_id for session_id, *_ in await store.list_for_owner("owner-warmed-test")]
        assert listed == ["sess-warmed-used"]

    asyncio.run(_run())


def test_session_owner_keeps_the_title_its_first_turn_gave_it() -> None:
    """A conversation is named by how it started, and a later turn must not rename it.

    The route calls this on every turn — it has no cheap way to know which one is first — so the
    `title IS NULL` guard is what makes that safe. Without it a sidebar entry would change under a
    chemist on every message, which is the one thing a navigation label must not do.
    """

    async def _run() -> None:
        await migrated_db_or_skip()
        store = SessionOwnerStore()
        await store.record("sess-title", "owner-title-test")
        await _spoke_in("sess-title")

        await store.set_title_if_absent("sess-title", "What is the pKa of acetic acid?")
        await store.set_title_if_absent("sess-title", "And in DMSO?")

        listed = await store.list_for_owner("owner-title-test")
        assert [row[3] for row in listed] == ["What is the pKa of acetic acid?"]

    asyncio.run(_run())


def test_session_owner_lists_an_unnamed_session_rather_than_dropping_it() -> None:
    """A session whose first turn predates the title column is listed with `title=None`.

    Null is the honest value and the row still belongs in the list — hiding a conversation because
    the service cannot name it would lose history to a schema change.
    """

    async def _run() -> None:
        await migrated_db_or_skip()
        store = SessionOwnerStore()
        await store.record("sess-untitled", "owner-untitled-test")
        await _spoke_in("sess-untitled")

        listed = await store.list_for_owner("owner-untitled-test")
        assert [(row[0], row[3]) for row in listed] == [("sess-untitled", None)]

    asyncio.run(_run())


def test_session_owner_listing_carries_the_profile_each_session_runs_under() -> None:
    """The listing returns `profile`, which is what `GET /plans/pending` filters on.

    Not cosmetic and not for the sidebar: it is the one column that says whether a session can be
    holding a plan waiting on a decision, and the alternative to reading it here is one serialized
    checkpointer statement per conversation to discover the same thing. `None` is a real value and
    means the default profile — `agent.profiles.get_profile(None)` resolves exactly that — so it
    must come back rather than being normalised into a name the row does not hold.
    """

    async def _run() -> None:
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
        await _spoke_in("sess-list-null")
        listed = await store.list_for_owner(None)
        assert "sess-list-null" in {session_id for session_id, *_ in listed}

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


def test_a_structured_turn_survives_the_round_trip_with_its_calls_intact() -> None:
    """The shape stamp decides how a row is read, and nothing else asserted that it decides right.

    `test_messages_survive_a_new_provider_instance` asserts a substring, and a substring is exactly
    what the degraded fallback produces: deleting `message_from_row`'s `LANGCHAIN_SHAPE` branch
    sends every row this system writes through the legacy converter, which refuses it, and the
    recovered prose still contains "phenol". Measured with that branch removed, the transcript came
    back as flat prose — the `AIMessage` with `tool_calls == []` and the `ToolMessage` as an
    `AIMessage` with no `tool_call_id`, so a reloaded conversation attributes the tool's answer to
    the model's own voice and loses the call that produced it — while the whole suite stayed green.

    So the assertion is identity, not substring: the classes, the call and the id that pairs the
    two. **And the counter on the happy path**, because that is the other half of what the wide
    catch costs. `chemclaw_degraded_total{subsystem=session_transcript}` is what separates "one
    unreadable legacy row" from "the reader is broken for everyone", and a reader that degrades
    every row looks identical to a healthy one unless something asserts the counter stays put.
    """

    async def _run() -> None:
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

    asyncio.run(_run())


def test_a_row_that_will_not_convert_is_marked_as_recovered_rather_than_passing_as_a_message() -> (
    None
):
    """A guess must not be readable as the record — the fallback's own failure mode.

    The catch is deliberately wide (a chemist must not lose a conversation to one bad row) and what
    it returns is an ordinary message of a guessed class carrying the row's prose. Unmarked, that
    is a forgery every reader downstream accepts: `chemclaw.cli.explain` printed a guessed speaker
    as the audit record, and no test could tell a decoded transcript from a recovered one, which is
    what let a deleted dispatch branch pass 251 tests.

    No database: this is the reader, not the store.
    """
    recovered = message_from_row({"role": "assistant", "contents": ["not a content part"]}, None)
    assert is_degraded_render(recovered), "a recovered row is indistinguishable from a decoded one"

    decoded = message_from_row(message_to_dict(HumanMessage(content="hello")), LANGCHAIN_SHAPE)
    assert not is_degraded_render(decoded), "a decoded row must not be marked as a guess"
    assert decoded.content == "hello"


def test_a_stored_message_carries_the_correlation_id_of_the_turn_that_wrote_it() -> None:
    """The only key between what was said and what was run, asserted at both ends.

    `save_messages` stamps `get_current_correlation_id()` so a transcript row joins to the audit
    rows and job records of its own turn (D-2026-07-31-the-audit-chain-is-versioned). Stamping `""`
    instead passes every test that touches the store, the explain CLI, the audit trail and the
    pairing closure — and a blank column is not a missing feature, it reads exactly like a row
    written before the id existed. `chemclaw explain` groups by that column, so every turn in the
    session collapses into one "unattributed" pseudo-turn and the report is wrong in the one way
    nobody double-checks: tool calls printed under a question that did not cause them.

    So this asserts the *grouping*, through the real reconstruction over the real table, and not
    only the column: the existing renderer test builds `(role, text)` tuples by hand and never
    proves the two halves are joinable in the first place.
    """

    async def _run() -> None:
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

    asyncio.run(_run())
