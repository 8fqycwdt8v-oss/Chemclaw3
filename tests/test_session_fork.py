"""A fork carries the whole thread, leaves the parent alone, and is a session in its own right.

**Three properties, and the third is the one a row count cannot see.** That a fork copied the right
number of rows says nothing about whether the copy *works* — a thread whose blobs were copied at the
wrong versions has exactly the right row count and resumes with holes. So the last test here
resumes the fork on a real checkpointer and reads the history back through the graph, which is the
only assertion that could have caught the failure `agent/session_fork.py`'s docstring is about.

Every test needs a real Postgres and says so by skipping without one (`tests/pg.py`), and every one
that touches a checkpoint table creates it first: `AsyncPostgresSaver.setup()` makes those tables,
not a migration, so a database that has never run the agent does not have them — and on a dev
database an unqualified name resolves through `public` and passes locally while failing in CI.
"""

import asyncio
from typing import Any, cast

import psycopg
import pytest
from langchain_core.language_models import GenericFakeChatModel
from langchain_core.messages import AIMessage
from psycopg.types.json import Jsonb

from chemclaw.agent import session_fork
from chemclaw.agent.checkpointer import CHECKPOINT_TABLES
from chemclaw.agent.langgraph_agent import build_langgraph_agent
from chemclaw.agent.session_fork import SessionForkError, fork_session
from chemclaw.agent.session_store import SessionOwnerStore
from chemclaw.agent.state import turn_config, turn_input
from chemclaw.core import db
from chemclaw.core.config import settings
from tests.pg import create_checkpoint_tables, migrated_db_or_skip


class _Model(GenericFakeChatModel):
    """A scripted model that can be bound, because `create_agent` binds tools on every request."""

    def bind_tools(self, tools: Any, **kwargs: Any) -> Any:
        """Accept the binding and keep replaying the script."""
        return self


async def _seed(thread_id: str, *, versions: tuple[str, ...] = ("1", "2")) -> None:
    """A thread with two checkpoints and one blob per version — the shape a fork must preserve.

    **Two versions is the point, not padding.** `checkpoint_blobs` rows are shared across a
    thread's checkpoints, so a channel written at version 1 and unchanged since is referenced by
    the newest checkpoint without belonging to it. A fork that copied "the tip" would take the
    version-2 row and leave version 1 behind, which no assertion about the newest checkpoint could
    detect.
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
                        Jsonb({"v": 1, "id": f"ckpt-{index}", "ts": "2026-08-29T00:00:00+00:00"}),
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


def test_the_fork_resumes_with_the_parent_s_history_and_then_diverges() -> None:
    """The assertion no row count can make: the copy actually *works* as a thread.

    A real checkpointer, a real compiled graph, and three turns — one on the parent, then a fork,
    then one on each. What is proven is that the fork's second turn sees the parent's first (so the
    thread was carried, not merely counted) and that the two threads then move independently (so
    the fork is a branch rather than an alias).
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
    """The atomicity claim, asserted rather than trusted to the connection's defaults.

    `fork_session` copies four tables and says it does so in one transaction. That is only true if
    the connection is *not* autocommit — it is not (`db.connection` yields `autocommit=False`), but
    "not true today" and "cannot become true" are different, and a pool option flipped somewhere
    else would turn a failed fork into a session that lists in `GET /sessions` and cannot load.

    Failure is injected by pointing the copy at a fourth table that does not exist, so three
    tables' rows are already written when it raises — the exact shape a partial fork would take.
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

    # **The set, not a count over the whole table.** The fork mints its own child id and the call
    # raises before returning it, so there is no id to look for — and the first version of this
    # assertion counted every row not belonging to the parent, which passed alone and failed beside
    # the other tests in this file, whose threads share the schema. Comparing the set before with
    # the set after names exactly this fork's leftovers and nobody else's.
    assert after == before, f"a failed fork left threads behind: {sorted(after - before)}"


def test_a_memory_deployment_says_it_cannot_fork_rather_than_failing_oddly() -> None:
    """The route is registered and reachable, and answers honestly with no durable store.

    An API-level case beside the store-level ones above, because the two can fail independently: a
    correct `fork_session` behind an unregistered route is a 404, and a registered route that
    assumed a store would be a 500 on the shipped `session_store=memory` default. 501 says the
    deployment does not have the feature, which is the true answer — a fork copies a *thread*, and
    a memory deployment has none to copy.
    """
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

    **The failure this pins is silent and total.** `durable/retention.py` expires a thread on
    `max((checkpoint->>'ts')::timestamptz)`, and a copied checkpoint carries the parent's `ts`. So a
    fork of a conversation last touched a year ago was already past the window when it was created:
    the next sweep deleted its whole thread while `session_owners` and `session_messages` survived,
    leaving a session that lists, opens, and renders every turn of its transcript — and then takes
    its next turn with **no history at all**, because context comes from the checkpointer and not
    from the rows the chemist can see.

    Asserted through the real sweep rather than on the timestamp alone, because the timestamp is
    only interesting for what retention does with it.
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
    """No copied transcript can exist without the ownership row that makes erasure find it.

    `agent/leaver.py` scopes erasure through `SELECT session_id FROM session_owners WHERE owner =
    ANY(...)`. A fork whose rows landed without an ownership row is therefore **structurally
    unreachable** by the one sweep that must never miss anything — a chemist's transcript survives
    their own erasure while the report says it was complete.

    Injected at the ownership write specifically, because that is the statement the first version
    of this module ran *after* the commit, on a separate round trip.
    """

    async def _run() -> None:
        """Fail the ownership write specifically, after the copy has already written its rows."""
        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(
            session_fork,
            "_RECORD_OWNER",
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

    # **The set, not a count.** This file's other tests create sessions in the same schema, so
    # "how many transcript rows exist" is not a question about this fork — the first version of
    # this assertion counted theirs, passed alone and failed beside them. The same isolation
    # mistake the atomicity test above already had to be corrected for.
    assert after == before, (
        f"a failed fork stranded transcript rows under {sorted(after - before)} with no ownership "
        "row — erasure scopes through session_owners and cannot reach them"
    )


def test_a_fork_can_still_fetch_the_tool_results_its_transcript_points_at() -> None:
    """The fork carries the links, so a stored result resolves instead of collapsing to a preview.

    A `session_messages` row holds a `result_ref` handle; `api/tool_results.py` resolves it through
    `tool_result_links` joined on `session_id`. Copy the transcript without the links and every
    handle in the fork resolves to nothing — the chemist sees the 400-character preview where the
    parent shows what the tool actually returned (`D-2026-08-09-a-preview-is-not-a-result`).

    The blob is shared rather than copied, which the hash assertion pins: a fork must cost one row
    per result, not a second copy of the bytes.
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
    """The success path of `POST /sessions/{id}/fork`, which nothing exercised.

    **Both arguments the route passes are security-relevant and neither was covered.** Mutating the
    handler to `fork_session(session_id, "somebody-else", None)` left 64 tests green: the fork
    would land under a principal who never asked for it, and would drop the parent's profile —
    which is attenuation-only, so restoring the default *widens* what the child may do. That is the
    exact widening `session_fork`'s docstring argues against and `test_the_fork_is_owned_by_the_
    caller_and_keeps_the_parent_s_profile` pins one layer down, at a function the route could stop
    calling correctly without anything noticing.

    Driven through the real app with a durable store, because the seam under test *is* the handler.
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
    """A caller error is reported as one — the mapping `SessionForkError` exists to produce.

    Untested until now: dropping the `except SessionForkError` clause turns this into an unhandled
    exception and a 500, which tells a chemist "the service broke" about a request that was simply
    made too early. 409 says *this session has taken no turn yet*, which is actionable.
    """
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
