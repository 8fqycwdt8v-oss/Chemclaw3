"""A fork carries the whole thread, leaves the parent alone, and is a session in its own right.

Row counts cannot show blobs copied at the wrong versions, so one test resumes the fork on a
real checkpointer and reads the history back through the graph. Every test needs Postgres and
creates the checkpoint tables first, since `AsyncPostgresSaver.setup()`, not a migration, makes
them.
"""

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import uuid4

import psycopg
import pytest
from langchain_core.language_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage
from psycopg.types.json import Jsonb

from chemclaw.agent import session_fork
from chemclaw.agent.checkpointer import CHECKPOINT_TABLES
from chemclaw.agent.langgraph_agent import build_langgraph_agent
from chemclaw.agent.session_fork import SessionForkError, fork_session
from chemclaw.agent.session_store import PostgresHistoryProvider, SessionOwnerStore
from chemclaw.agent.state import turn_config, turn_input
from chemclaw.core import db
from chemclaw.core.config import settings
from chemclaw.exhibits.models import parse_spec
from chemclaw.exhibits.store import PostgresExhibitStore
from tests.pg import create_checkpoint_tables, migrated_db_or_skip


class _Model(GenericFakeChatModel):
    """A scripted model that can be bound, because `create_agent` binds tools on every request."""

    def bind_tools(self, tools: Any, **kwargs: Any) -> Any:
        """Accept the binding and keep replaying the script."""
        return self


#: A fresh timestamp per run, so seeded threads are always young relative to the sweep.
_NOW = datetime.now(UTC)


def _seeded_ts(index: int) -> str:
    """The `ts` for the `index`-th seeded checkpoint — distinct, and ordered oldest first.

    `_RESTAMP_NEWEST` picks the newest by `ORDER BY ts DESC`, so identical timestamps would make the
    restamp tests unable to tell which row moved. An hour apart, inside any retention window.
    """
    return (_NOW - timedelta(hours=len(_VERSIONS) - index)).isoformat()


#: The checkpoint versions `_seed` writes, named once because `_seeded_ts` counts them.
_VERSIONS = ("1", "2")


async def _seed(
    thread_id: str, *, versions: tuple[str, ...] = _VERSIONS, transcript: bool = True
) -> None:
    """A thread with two checkpoints and one blob per version — the shape a fork must preserve.

    Blobs are shared across checkpoints, so the version-1 blob is referenced by the newest
    checkpoint without belonging to it; copying "the tip" would lose it. `transcript=False` seeds
    the state of a session mid-first-turn: checkpoints written, no transcript row yet.
    """
    async with db.connection(settings.postgres_dsn) as conn:
        async with conn.cursor() as cur:
            for index, version in enumerate(versions, start=1):
                await cur.execute(
                    "INSERT INTO checkpoints "
                    "(thread_id, checkpoint_ns, checkpoint_id, checkpoint, metadata) "
                    "VALUES (%s, '', %s, %s, '{}'::jsonb)",
                    (
                        thread_id,
                        f"ckpt-{index}",
                        # Relative to now, never a literal date, or the fixtures would age into the
                        # retention sweep the aged-fork test runs over the whole schema.
                        Jsonb({"v": 1, "id": f"ckpt-{index}", "ts": _seeded_ts(index)}),
                    ),
                )
                await cur.execute(
                    "INSERT INTO checkpoint_blobs "
                    "(thread_id, checkpoint_ns, channel, version, type, blob) "
                    "VALUES (%s, '', 'messages', %s, 'msgpack', %s)",
                    (thread_id, version, f"payload-{version}".encode()),
                )
                await cur.execute(
                    "INSERT INTO checkpoint_writes "
                    "(thread_id, checkpoint_ns, checkpoint_id, task_id, idx, channel, type, blob) "
                    "VALUES (%s, '', %s, 'task-1', 0, 'messages', 'msgpack', %s)",
                    (thread_id, f"ckpt-{index}", b"payload"),
                )
            if transcript:
                await cur.execute(
                    "INSERT INTO session_messages (session_id, message, message_shape) "
                    "VALUES (%s, %s, 'langchain')",
                    (thread_id, Jsonb({"type": "human", "content": "the parent's question"})),
                )
        await conn.commit()


async def _counts(thread_id: str) -> dict[str, int]:
    """How many rows each of the four tables holds for one thread."""
    counts: dict[str, int] = {}
    async with db.connection(settings.postgres_dsn) as conn, conn.cursor() as cur:
        for table, column in (
            ("checkpoints", "thread_id"),
            ("checkpoint_blobs", "thread_id"),
            ("checkpoint_writes", "thread_id"),
            ("session_messages", "session_id"),
        ):
            await cur.execute(
                f"SELECT count(*) FROM {table} WHERE {column} = %s",
                (thread_id,),
            )
            row = await cur.fetchone()
            counts[table] = int(row[0]) if row else 0
    return counts


def test_a_fork_copies_every_row_of_the_thread_and_leaves_the_parent_alone() -> None:
    """The child holds the parent's whole thread; the parent holds exactly what it held.

    Both halves, because a copy that also mutated the source would satisfy the first on its own —
    and the parent being untouched is the property the whole feature rests on.
    """

    async def _run() -> tuple[dict[str, int], dict[str, int], dict[str, int]]:
        await migrated_db_or_skip()
        await create_checkpoint_tables()
        await _seed("fork-parent")
        before = await _counts("fork-parent")

        child = await fork_session("fork-parent", "owner-1", None)

        return before, await _counts("fork-parent"), await _counts(child)

    before, after, child = asyncio.run(_run())

    assert child == before, f"the fork did not carry the whole thread: {child} vs {before}"
    assert after == before, f"forking mutated the parent: {after} vs {before}"


def test_a_fork_carries_blobs_from_every_version_not_only_the_newest() -> None:
    """The failure a row count would hide: the tip's blob copied and its ancestors' left behind.

    Asserted on the payloads rather than the count, because "two blob rows" is true of both the
    correct copy and one that duplicated the newest version twice.
    """

    async def _run() -> set[bytes]:
        await migrated_db_or_skip()
        await create_checkpoint_tables()
        await _seed("fork-versions")

        child = await fork_session("fork-versions", "owner-1", None)

        async with db.connection(settings.postgres_dsn) as conn, conn.cursor() as cur:
            await cur.execute("SELECT blob FROM checkpoint_blobs WHERE thread_id = %s", (child,))
            return {bytes(row[0]) for row in await cur.fetchall()}

    assert asyncio.run(_run()) == {b"payload-1", b"payload-2"}


def test_the_fork_is_owned_by_the_caller_and_keeps_the_parent_s_profile() -> None:
    """A fork is findable and no wider than what it came from.

    The profile matters more than it looks: a profile only ever *narrows*, so a fork that dropped
    it would hand the caller an agent that can do more than the session they forked.
    """

    async def _run() -> tuple[bool, str | None, str | None]:
        await migrated_db_or_skip()
        await create_checkpoint_tables()
        await _seed("fork-owned")

        child = await fork_session("fork-owned", "owner-42", "safety")

        return await SessionOwnerStore().lookup(child)

    found, owner, profile = asyncio.run(_run())

    assert found
    assert owner == "owner-42"
    assert profile == "safety"


def test_forking_a_session_that_has_taken_no_turn_is_refused() -> None:
    """Nothing to branch from is an error, not an empty session that looks like a fork."""

    async def _run() -> None:
        await migrated_db_or_skip()
        await create_checkpoint_tables()
        await fork_session("fork-nonexistent", "owner-1", None)

    with pytest.raises(SessionForkError, match="no saved state"):
        asyncio.run(_run())


def test_forking_before_the_first_answer_is_refused_rather_than_minting_an_unlistable_session() -> (
    None
):
    """Forking before the first answer is refused rather than minting an unlistable session.

    The owner listing needs a transcript row, which is written only once an answer is assembled. The
    refusal and the absence of any minted rows are both asserted, so a guard that raised after the
    copy would fail.
    """

    async def _run() -> tuple[list[str], int]:
        await migrated_db_or_skip()
        await create_checkpoint_tables()
        await _seed("fork-mid-first-turn", transcript=False)
        await SessionOwnerStore().record("fork-mid-first-turn", "owner-mid", None)
        with pytest.raises(SessionForkError, match="not answered"):
            await fork_session("fork-mid-first-turn", "owner-mid", None)
        rows = await SessionOwnerStore().page_for_owner("owner-mid")
        async with db.connection(settings.postgres_dsn) as conn, conn.cursor() as cur:
            await cur.execute(
                "SELECT count(*) FROM session_owners WHERE owner = %s", ("owner-mid",)
            )
            row = await cur.fetchone()
        return [str(row_[0]) for row_ in rows], int(row[0]) if row else 0

    listed, owned = asyncio.run(_run())

    assert listed == [], f"a session the listing cannot show was minted anyway: {listed}"
    assert owned == 1, (
        f"the refused fork left {owned - 1} ownership row(s) behind — a session with rows in the "
        "store and no way to reach it"
    )


def test_the_fork_resumes_with_the_parent_s_history_and_then_diverges() -> None:
    """The fork resumes with the parent's history and then diverges.

    A real checkpointer and compiled graph: the fork's turn sees the parent's first, and then the
    two threads move independently.
    """

    async def _run() -> tuple[list[str], list[str]]:
        await migrated_db_or_skip()
        await create_checkpoint_tables()
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
        from psycopg_pool import AsyncConnectionPool

        pool = AsyncConnectionPool(
            conninfo=settings.postgres_dsn,
            kwargs={"autocommit": True},
            min_size=0,
            max_size=4,
            open=False,
        )
        await pool.open()
        try:
            saver = AsyncPostgresSaver(cast(Any, pool))
            parent = "fork-live-parent"

            def graph(answer: str) -> Any:
                return build_langgraph_agent(
                    model=_Model(messages=iter([AIMessage(content=answer)])),
                    checkpointer=saver,
                )

            await graph("the parent's answer").ainvoke(
                turn_input("first question"), config=turn_config(parent)
            )
            # A turn writes two records; driving the graph directly writes only the checkpoint, so
            # the transcript row is written here to make the session forkable.
            await PostgresHistoryProvider().save_messages(
                parent, [HumanMessage(content="first question")]
            )
            child = await fork_session(parent, "owner-1", None)

            # A turn on each side, after the fork. Divergence is what makes them two threads.
            await graph("the child's answer").ainvoke(
                turn_input("child question"), config=turn_config(child)
            )
            await graph("the parent's second answer").ainvoke(
                turn_input("parent question"), config=turn_config(parent)
            )

            async def texts(thread: str) -> list[str]:
                state = await saver.aget_tuple(cast(Any, turn_config(thread)))
                assert state is not None
                return [str(m.content) for m in state.checkpoint["channel_values"]["messages"]]

            return await texts(parent), await texts(child)
        finally:
            await pool.close()

    parent_texts, child_texts = asyncio.run(_run())

    # The fork inherited the parent's first exchange...
    assert "first question" in parent_texts
    assert "first question" in child_texts, "the fork did not carry the parent's history"
    # ...and then the two went their own ways.
    assert "child question" in child_texts
    assert "child question" not in parent_texts, "the fork wrote into its parent's thread"
    assert "parent question" in parent_texts
    assert "parent question" not in child_texts, "the parent wrote into the fork's thread"


def test_a_copy_that_fails_partway_leaves_no_half_session_behind() -> None:
    """A copy that fails partway leaves no half session behind.

    The copy is pointed at a missing fourth table, so three tables are written when it raises; the
    single transaction must roll them back.
    """

    async def _run() -> None:
        """Point the copy at a table that does not exist, so it fails after three succeed."""
        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(
            session_fork, "CHECKPOINT_TABLES", (*CHECKPOINT_TABLES, "no_such_table")
        )
        try:
            # The specific error, not a blind `Exception`: a bare catch would pass if the fork
            # failed for some entirely unrelated reason and prove nothing about atomicity.
            with pytest.raises(psycopg.errors.UndefinedTable):
                await fork_session("fork-atomic", "owner-1", None)
        finally:
            monkeypatch.undo()

    async def _threads() -> set[str]:
        """Every thread id in the checkpoint table right now."""
        async with db.connection(settings.postgres_dsn) as conn, conn.cursor() as cur:
            await cur.execute("SELECT DISTINCT thread_id FROM checkpoints")
            return {str(row[0]) for row in await cur.fetchall()}

    async def _drive() -> tuple[set[str], set[str]]:
        """The thread-id set before the failed fork and after it."""
        await migrated_db_or_skip()
        await create_checkpoint_tables()
        await _seed("fork-atomic")
        before = await _threads()
        await _run()
        return before, await _threads()

    before, after = asyncio.run(_drive())

    # Compare the set of threads before and after, since other tests share the schema and the failed
    # fork never returned its id.
    assert after == before, f"a failed fork left threads behind: {sorted(after - before)}"


def test_a_memory_deployment_says_it_cannot_fork_rather_than_failing_oddly() -> None:
    """With no durable store the fork route answers 501, not 404 or 500."""
    from fastapi.testclient import TestClient

    from tests.test_service import _app

    client = TestClient(_app())
    session_id = client.post("/sessions").json()["session_id"]

    response = client.post(f"/sessions/{session_id}/fork")

    assert response.status_code == 501
    assert "durable session store" in response.json()["detail"]


async def _seed_tool_result(session_id: str, text: str) -> str:
    """One stored tool result for `session_id`, returning its content hash."""
    import hashlib

    content_hash = hashlib.sha256(text.encode()).hexdigest()
    async with db.connection(settings.postgres_dsn) as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "INSERT INTO tool_result_blobs (content_hash, byte_size, data) "
                "VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                (content_hash, len(text.encode()), text.encode()),
            )
            await cur.execute(
                "INSERT INTO tool_result_links (session_id, content_hash, tool) "
                "VALUES (%s, %s, 'gather_evidence') ON CONFLICT DO NOTHING",
                (session_id, content_hash),
            )
        await conn.commit()
    return content_hash


async def _thread_age_days(thread_id: str) -> float:
    """How old the newest checkpoint of `thread_id` claims to be, in days."""
    async with db.connection(settings.postgres_dsn) as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT EXTRACT(EPOCH FROM (now() - max((checkpoint->>'ts')::timestamptz))) / 86400 "
            "FROM checkpoints WHERE thread_id = %s",
            (thread_id,),
        )
        row = await cur.fetchone()
        return float(row[0]) if row and row[0] is not None else -1.0


def test_a_fork_of_an_aged_conversation_is_not_expired_the_moment_it_is_made() -> None:
    """The fork's retention clock starts at the fork, not at the parent's last turn.

    Otherwise the next sweep deletes the fork's thread while its transcript survives, and its next
    turn runs with no history. Asserted through the real sweep.
    """

    async def _run() -> tuple[float, dict[str, int], dict[str, int]]:
        await migrated_db_or_skip()
        await create_checkpoint_tables()
        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(settings, "retention_checkpoints_days", 30)
        monkeypatch.setattr(settings, "retention_session_messages_days", 0)
        monkeypatch.setattr(settings, "retention_session_events_days", 0)
        try:
            await _seed("fork-aged")
            # Age the parent well past the window, the way a real conversation ages.
            async with db.connection(settings.postgres_dsn) as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        "UPDATE checkpoints SET checkpoint = jsonb_set(checkpoint, '{ts}', "
                        "to_jsonb((now() - interval '400 days')::text)) WHERE thread_id = %s",
                        ("fork-aged",),
                    )
                await conn.commit()

            child = await fork_session("fork-aged", "owner-1", None)
            age = await _thread_age_days(child)

            from chemclaw.durable.retention import prune_expired_rows

            await prune_expired_rows()
            return age, await _counts(child), await _counts("fork-aged")
        finally:
            monkeypatch.undo()

    age, child_after, parent_after = asyncio.run(_run())

    assert age < 1.0, f"the fork was born {age:.0f} days old — it inherited the parent's clock"
    assert child_after["checkpoints"] > 0, (
        "the retention sweep deleted the fork's whole thread: the session still lists and its "
        "transcript still renders, but its next turn would run with no history"
    )
    # The parent is genuinely expired and is *meant* to go — that is what makes the assertion above
    # about the fork's own clock rather than about the sweep having done nothing.
    assert parent_after["checkpoints"] == 0, "the parent was not expired, so this proves nothing"


def test_a_forks_ownership_row_commits_with_its_data_or_not_at_all() -> None:
    """A fork's ownership row commits with its data or not at all.

    Erasure finds sessions through `session_owners`, so copied rows without an owner row would
    survive their owner's erasure. The failure is injected at the ownership write.
    """

    async def _run() -> None:
        """Fail the ownership write specifically, after the copy has already written its rows."""
        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(
            session_fork,
            "_OWNER_INSERT",
            "INSERT INTO no_such_owners (a, b, c) VALUES (%s, %s, %s)",
        )
        try:
            with pytest.raises(psycopg.errors.UndefinedTable):
                await fork_session("fork-atomic-owner", "owner-1", None)
        finally:
            monkeypatch.undo()

    async def _message_sessions() -> set[str]:
        """Every session id that currently holds a transcript row."""
        async with db.connection(settings.postgres_dsn) as conn, conn.cursor() as cur:
            await cur.execute("SELECT DISTINCT session_id FROM session_messages")
            return {str(r[0]) for r in await cur.fetchall()}

    async def _drive() -> tuple[set[str], set[str]]:
        await migrated_db_or_skip()
        await create_checkpoint_tables()
        await _seed("fork-atomic-owner")
        before = await _message_sessions()
        await _run()
        return before, await _message_sessions()

    before, after = asyncio.run(_drive())

    # The set, not a count: other tests create sessions in the same schema.
    assert after == before, (
        f"a failed fork stranded transcript rows under {sorted(after - before)} with no ownership "
        "row — erasure scopes through session_owners and cannot reach them"
    )


def test_a_fork_can_still_fetch_the_tool_results_its_transcript_points_at() -> None:
    """A fork can still fetch the tool results its transcript points at.

    `result_ref` resolves through `tool_result_links` joined on `session_id`, so the links are
    copied. The blob is shared, not duplicated.
    """

    async def _run() -> tuple[list[str], list[str]]:
        await migrated_db_or_skip()
        await create_checkpoint_tables()
        await _seed("fork-results")
        content_hash = await _seed_tool_result("fork-results", "the full tool output")

        child = await fork_session("fork-results", "owner-1", None)

        async with db.connection(settings.postgres_dsn) as conn, conn.cursor() as cur:
            await cur.execute(
                "SELECT content_hash FROM tool_result_links WHERE session_id = %s", (child,)
            )
            child_hashes = [str(r[0]) for r in await cur.fetchall()]
        return child_hashes, [content_hash]

    child_hashes, parent_hashes = asyncio.run(_run())

    assert child_hashes == parent_hashes, (
        "the fork holds no link to the parent's tool results, so every result_ref in its "
        "transcript resolves to nothing and renders as a preview"
    )


def test_the_route_forks_under_the_caller_and_keeps_the_parents_profile() -> None:
    """`POST /sessions/{id}/fork` forks under the caller and keeps the parent's profile.

    Both are security-relevant: the wrong owner, or a dropped attenuation-only profile, would widen
    access. Driven through the real app with a durable store.
    """
    from fastapi.testclient import TestClient

    from chemclaw.agent.session_store import SessionOwnerStore
    from chemclaw.api.auth import Principal, require_principal
    from tests.test_service import _app

    async def _prepare() -> str:
        await migrated_db_or_skip()
        await create_checkpoint_tables()
        parent = "fork-route-parent"
        await _seed(parent)
        await SessionOwnerStore().record(parent, "alice", "safety")
        return parent

    parent = asyncio.run(_prepare())

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(settings, "session_store", "postgres")
        app = _app()
        app.dependency_overrides[require_principal] = lambda: Principal(
            oid="alice", upn="a@corp", roles=frozenset()
        )
        client = TestClient(app)
        response = client.post(f"/sessions/{parent}/fork")

    assert response.status_code == 200, response.text
    child = response.json()["session_id"]

    found, owner, profile = asyncio.run(SessionOwnerStore().lookup(child))
    assert found
    assert owner == "alice", f"the fork landed under {owner!r} rather than the caller"
    assert profile == "safety", (
        f"the fork dropped the parent's profile (got {profile!r}) — a profile only ever narrows, "
        "so the child can now do more than the session it was forked from"
    )


def test_forking_a_session_with_no_state_is_a_409_not_a_500() -> None:
    """Forking a session with no state is a 409, via `SessionForkError`, not a 500."""
    from fastapi.testclient import TestClient

    from chemclaw.agent.session_store import SessionOwnerStore
    from chemclaw.api.auth import Principal, require_principal
    from tests.test_service import _app

    async def _prepare() -> str:
        await migrated_db_or_skip()
        await create_checkpoint_tables()
        empty = "fork-route-empty"
        await SessionOwnerStore().record(empty, "alice", None)
        return empty

    empty = asyncio.run(_prepare())

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(settings, "session_store", "postgres")
        app = _app()
        app.dependency_overrides[require_principal] = lambda: Principal(
            oid="alice", upn="a@corp", roles=frozenset()
        )
        client = TestClient(app)
        response = client.post(f"/sessions/{empty}/fork")

    assert response.status_code == 409, f"expected a caller error, got {response.status_code}"
    assert "no saved state" in response.json()["detail"]


def test_a_forks_transcript_is_as_young_as_the_fork_and_survives_the_sweep() -> None:
    """A fork's transcript is as young as the fork and survives the message-retention sweep.

    `created_at` dates the session for the listing and for retention, so copying it verbatim would
    sink the fork in the sidebar or prune its transcript. The message window is enabled here.
    """

    async def _run() -> tuple[float, dict[str, int]]:
        await migrated_db_or_skip()
        await create_checkpoint_tables()
        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(settings, "retention_checkpoints_days", 30)
        monkeypatch.setattr(settings, "retention_session_messages_days", 30)
        monkeypatch.setattr(settings, "retention_session_events_days", 0)
        try:
            await _seed("fork-transcript-age")
            async with db.connection(settings.postgres_dsn) as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        "UPDATE checkpoints SET checkpoint = jsonb_set(checkpoint, '{ts}', "
                        "to_jsonb((now() - interval '400 days')::text)) WHERE thread_id = %s",
                        ("fork-transcript-age",),
                    )
                    await cur.execute(
                        "UPDATE session_messages SET created_at = now() - interval '400 days' "
                        "WHERE session_id = %s",
                        ("fork-transcript-age",),
                    )
                await conn.commit()

            child = await fork_session("fork-transcript-age", "owner-1", None)

            async with db.connection(settings.postgres_dsn) as conn, conn.cursor() as cur:
                await cur.execute(
                    "SELECT EXTRACT(EPOCH FROM (now() - max(created_at))) / 86400 "
                    "FROM session_messages WHERE session_id = %s",
                    (child,),
                )
                row = await cur.fetchone()
                age = float(row[0]) if row and row[0] is not None else -1.0

            from chemclaw.durable.retention import prune_expired_rows

            await prune_expired_rows()
            return age, await _counts(child)
        finally:
            monkeypatch.undo()

    age, after = asyncio.run(_run())

    assert age < 1.0, (
        f"the fork's newest message is {age:.0f} days old — it kept the parent's clock"
    )
    assert after["session_messages"] > 0, (
        "the sweep deleted the fork's transcript, so the session no longer appears in "
        "GET /sessions at all — the fork exists and the chemist cannot find it"
    )
    assert after["checkpoints"] > 0, "the checkpoint half regressed"


def test_deleting_a_fork_and_its_parent_reclaims_the_shared_blob() -> None:
    """Deleting a fork and its parent reclaims the shared blob.

    The blob delete spares content linked by other sessions, counting only links whose session
    still exists; links cannot be deleted, so orphans would otherwise keep the blob forever.
    """

    async def _run() -> tuple[int, int]:
        await migrated_db_or_skip()
        await create_checkpoint_tables()
        from chemclaw.agent.session_store import SessionOwnerStore

        owners = SessionOwnerStore()

        parent = "fork-blob-parent"
        await _seed(parent)
        await owners.record(parent, "owner-1", None)
        await _seed_tool_result(parent, "the full tool output for the blob test")

        child = await fork_session(parent, "owner-1", None)
        await owners.delete_session(child)
        await owners.delete_session(parent)

        async with db.connection(settings.postgres_dsn) as conn, conn.cursor() as cur:
            await cur.execute(
                "SELECT count(*) FROM tool_result_blobs WHERE content_hash IN "
                "(SELECT content_hash FROM tool_result_links WHERE session_id IN (%s, %s))",
                (parent, child),
            )
            blob_row = await cur.fetchone()
            blobs = int(blob_row[0]) if blob_row else 0
            await cur.execute(
                "SELECT count(*) FROM tool_result_links WHERE session_id IN (%s, %s)",
                (parent, child),
            )
            link_row = await cur.fetchone()
            links = int(link_row[0]) if link_row else 0
        return blobs, links

    blobs, links = asyncio.run(_run())

    assert (blobs, links) == (0, 0), (
        f"{blobs} blob(s) and {links} link row(s) outlived every session that named them — a fork "
        "made its parent's tool results permanently unreclaimable"
    )


def test_only_the_newest_checkpoint_is_restamped_and_the_rest_keep_the_parents_times() -> None:
    """Only the newest checkpoint is restamped; the rest keep the parent's times.

    Restamping the oldest, or every row, would rewrite parent-authored history. `_seeded_ts` spaces
    the checkpoints so the three cases are distinguishable.
    """

    async def _run() -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
        await migrated_db_or_skip()
        await create_checkpoint_tables()
        await _seed("fork-restamp-selection")

        child = await fork_session("fork-restamp-selection", "owner-1", None)

        async def _stamps(thread: str) -> list[tuple[str, str]]:
            async with db.connection(settings.postgres_dsn) as conn, conn.cursor() as cur:
                await cur.execute(
                    "SELECT checkpoint_id, checkpoint->>'ts' FROM checkpoints "
                    "WHERE thread_id = %s ORDER BY checkpoint_id",
                    (thread,),
                )
                return [(str(r[0]), str(r[1])) for r in await cur.fetchall()]

        return await _stamps(child), await _stamps("fork-restamp-selection")

    child_stamps, parent_stamps = asyncio.run(_run())

    assert len(child_stamps) == 2, f"the fixture no longer seeds two checkpoints: {child_stamps}"
    (older_id, older_ts), (newer_id, newer_ts) = child_stamps
    assert (older_id, newer_id) == ("ckpt-1", "ckpt-2")

    # The newest moved to now...
    assert newer_ts != dict(parent_stamps)[newer_id], (
        "the newest checkpoint kept the parent's timestamp — retention still dates this fork from "
        "the parent's last turn"
    )
    # ...and the older one did not, which is the half that fails when every row is restamped.
    assert older_ts == dict(parent_stamps)[older_id], (
        "the older checkpoint was rewritten too, destroying the parent-authored history the fork "
        "copied in order to preserve"
    )
    # And the parent is untouched throughout.
    assert dict(parent_stamps)[newer_id] == _seeded_ts(2)


def test_a_fork_carries_each_artefact_as_it_stands_under_a_new_id() -> None:
    """Head revision only, as revision 1 of a new id, noted with where it came from.

    The parent keeps its history and its ids; the child's copy keeps who wrote the words it
    carries, and a chemist's edit the parent's agent was never told of is still unseen in the fork.
    """

    async def _run() -> tuple[list[Any], list[Any], dict[str, Any]]:
        await migrated_db_or_skip()
        await create_checkpoint_tables()
        parent = uuid4().hex
        await _seed(parent)
        store = PostgresExhibitStore()
        plan = await store.create(
            parent,
            title="Plan",
            spec=parse_spec({"kind": "document", "markdown": "# Plan\n\nDry the THF."}),
            author_kind="agent",
            author="oid-ana",
        )
        await store.append(
            parent,
            plan.exhibit_id,
            spec=parse_spec({"kind": "document", "markdown": "# Plan\n\nDry the 2-MeTHF."}),
            parent_revision=1,
            author_kind="human",
            author="oid-ben",
            change_note="solvent",
        )
        notes = await store.create(
            parent,
            title="Notes",
            spec=parse_spec({"kind": "document", "markdown": "seen"}),
            author_kind="agent",
            author="oid-ana",
        )
        child = await fork_session(parent, "owner-1", None)
        views = {}
        for state in await store.states(child):
            xid = state.header.exhibit_id
            views[state.header.title] = (
                state,
                await store.view(child, xid),
                await store.revisions(child, xid),
            )
        return (
            [h.exhibit_id for h in await store.headers(parent)],
            [plan.exhibit_id, notes.exhibit_id],
            views,
        )

    parent_ids, originals, views = asyncio.run(_run())
    assert sorted(parent_ids) == sorted(originals), "forking touched the parent's artefacts"
    assert set(views) == {"Plan", "Notes"}
    plan_state, plan, plan_history = views["Plan"]
    assert plan.exhibit_id not in originals
    assert (plan.revision, plan.head_revision, plan.parent_revision) == (1, 1, 0)
    assert plan.change_note == f"forked from {originals[0]} r2"
    assert (plan.author_kind, plan.author, plan.head_author) == ("human", "oid-ben", "oid-ben")
    assert plan.spec == parse_spec({"kind": "document", "markdown": "# Plan\n\nDry the 2-MeTHF."})
    assert [entry.revision for entry in plan_history] == [1]
    assert plan_state.agent_seen_revision == 0, "the chemist's unseen edit became seen in the fork"
    notes_state, notes, _ = views["Notes"]
    assert notes.change_note == f"forked from {originals[1]} r1"
    assert notes_state.agent_seen_revision == 1


def test_a_forked_artefact_carries_every_figure_people_introduced_across_its_history() -> None:
    """A forked artefact records the union of the source's chemist-introduced figures.

    The fork copies only the head, so figures from earlier revisions must be carried explicitly.
    """

    def _table(*values: float) -> Any:
        rows = [{"y": value} for value in values]
        return parse_spec({"kind": "table", "columns": [{"key": "y", "label": "Y"}], "rows": rows})

    async def _run() -> tuple[list[str], list[str]]:
        await migrated_db_or_skip()
        await create_checkpoint_tables()
        parent = uuid4().hex
        await _seed(parent)
        store = PostgresExhibitStore()
        made = await store.create(
            parent, title="T", spec=_table(1.5), author_kind="agent", author="oid-ana"
        )
        await store.append(
            parent,
            made.exhibit_id,
            spec=_table(1.5, 76.5),
            parent_revision=1,
            author_kind="human",
            author="oid-ben",
            chemist_figures=["76.5"],
        )
        # A pre-119 person's revision: nothing recorded, so it is derived ("81").
        await store.append(
            parent,
            made.exhibit_id,
            spec=_table(1.5, 76.5, 81),
            parent_revision=2,
            author_kind="human",
            author="oid-ben",
        )
        await store.append(
            parent,
            made.exhibit_id,
            spec=_table(1.5, 76.5, 81, 2.25),
            parent_revision=3,
            author_kind="agent",
            author="oid-ana",
        )
        child = await fork_session(parent, "owner-1", None)
        [copied] = await store.headers(child)
        return (
            await store.chemist_figures(parent, made.exhibit_id),
            await store.chemist_figures(child, copied.exhibit_id),
        )

    source, forked = asyncio.run(_run())
    assert source == ["76.5", "81"]
    assert forked == source
