"""The session lifecycle routes over a real database: paging the list, and deleting one.

Drives `create_app` with the real durable stores, because a keyset page boundary and a
twelve-table delete are statements about SQL. Skipped without Postgres (`tests/pg.py`).
Decision: `D-2026-08-27-a-session-list-is-a-cursor-and-a-session-is-deletable`.
"""

import asyncio
import time
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, HumanMessage

from chemclaw.agent.session_store import (
    PostgresHistoryProvider,
    SessionOwnerStore,
    SessionTurnClaims,
    _session_delete_statements,
)
from chemclaw.api.app import create_app
from chemclaw.api.auth import Principal, require_principal
from chemclaw.api.state import TurnLease
from chemclaw.core import db
from chemclaw.core.config import settings
from tests.pg import create_checkpoint_tables, migrated_db_or_skip

_ALICE = Principal(oid="alice-sessions", upn="alice@corp", roles=frozenset())
_BOB = Principal(oid="bob-sessions", upn="bob@corp", roles=frozenset())


def _no_connectors(_profile: str | None = None) -> list[Any]:
    """No connectors: these routes never run a turn, and dialling a fleet would be a hang."""
    return []


def _durable_app() -> FastAPI:
    """The front door over the real session store — the only wiring these routes' claims are about.

    `graph_factory` is stubbed so the app needs no model credential; nothing here posts a message.
    """
    return create_app(
        owner_store=SessionOwnerStore(),
        turn_claims=SessionTurnClaims(),
        connector_factory=_no_connectors,
        graph_factory=lambda *args, **kwargs: None,
    )


def _client(app: FastAPI, principal: Principal = _ALICE) -> TestClient:
    """A client whose every request arrives as `principal` — the auth gate is not under test."""
    app.dependency_overrides[require_principal] = lambda: principal
    return TestClient(app)


async def _conversation(session_id: str, owner: str | None, message: str = "a turn") -> None:
    """One session that exists *and* has been spoken in — which is what makes it listable."""
    await SessionOwnerStore().record(session_id, owner)
    await PostgresHistoryProvider().save_messages(session_id, [HumanMessage(content=message)])


async def _checkpoint_for(session_id: str) -> None:
    """The graph state a fork branches from — the half `_conversation` does not write.

    Written straight into the checkpointer's table, since nothing here runs a turn.
    """
    async with db.connection(settings.postgres_dsn) as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                # `ts` from `now()` rather than a literal date, so `durable/retention.py` never
                # expires this fixture once real time passes it.
                "INSERT INTO checkpoints (thread_id, checkpoint_ns, checkpoint_id, checkpoint, "
                "metadata) VALUES (%s, '', 'ckpt-1', "
                "jsonb_build_object('v', 1, 'id', 'ckpt-1', 'ts', now()::text), '{}'::jsonb) "
                "ON CONFLICT DO NOTHING",
                (session_id,),
            )
        await conn.commit()


async def _rows_for(session_id: str) -> int:
    """How many rows of the delete's own table set still name this session.

    The caller must run `create_checkpoint_tables()` first: the three checkpointer tables are
    counted unqualified, and skipping an absent one would silently stop covering graph state.
    """
    total = 0
    async with db.connection(settings.postgres_dsn) as conn:
        async with conn.cursor() as cur:
            for table, _ in _session_delete_statements():
                if table == "tool_result_blobs":
                    continue  # content-addressed: reached through its link, counted there
                column = "thread_id" if table.startswith("checkpoint") else "session_id"
                await cur.execute(
                    f"SELECT count(*) FROM {table} WHERE {column} = %s", (session_id,)
                )
                row = await cur.fetchone()
                total += int(row[0]) if row else 0
    return total


def test_the_session_list_pages_past_its_ceiling_and_stays_a_bare_array(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A client can reach every conversation it owns, and an old client sees no change at all.

    The cursor travels in a response header because the body is a bare JSON array the UI parses as
    one; asserted as a list of objects carrying exactly the four original fields.
    """
    asyncio.run(migrated_db_or_skip())
    sessions = [f"sess-api-page-{index}" for index in range(5)]
    for session_id in sessions:
        asyncio.run(_conversation(session_id, _ALICE.oid))
    monkeypatch.setattr(settings, "service_max_listed_sessions", 2)

    client = _client(_durable_app())
    first = client.get("/sessions")
    body = first.json()
    assert isinstance(body, list) and len(body) == 2, f"the response shape changed: {body}"
    assert set(body[0]) == {"session_id", "created_at", "updated_at", "title"}, (
        f"a client parsing this array sees new fields: {sorted(body[0])}"
    )

    seen = [row["session_id"] for row in body]
    cursor = first.headers.get("X-Next-Cursor")
    pages = 1
    while cursor:
        page = client.get("/sessions", params={"after": cursor})
        assert page.status_code == 200, page.text
        seen.extend(row["session_id"] for row in page.json())
        cursor = page.headers.get("X-Next-Cursor")
        pages += 1
        assert pages < 10, "the cursor never ran out — a page that repeats itself pages forever"

    assert seen == list(reversed(sessions)), (
        f"paging the front door returned {seen}, not every session newest-first exactly once"
    )


def test_a_cursor_the_service_did_not_mint_is_refused_rather_than_answered() -> None:
    """A junk cursor is the caller's error (422), never a 500 and never a silent first page.

    A silent first page would make a client with a mangled cursor page forever over the same rows.
    """
    asyncio.run(migrated_db_or_skip())
    client = _client(_durable_app())
    assert client.get("/sessions", params={"after": "not-a-cursor!"}).status_code == 422


def test_an_owner_deletes_their_own_session_and_it_stops_existing() -> None:
    """204, the durable rows are gone, and the id no longer resolves *on this pod either*.

    `_resolve_session` consults an in-process LRU first, so the delete must evict it too.
    """
    asyncio.run(migrated_db_or_skip())
    asyncio.run(create_checkpoint_tables())
    session_id = "sess-api-delete-mine"
    asyncio.run(_conversation(session_id, _ALICE.oid))

    client = _client(_durable_app())
    assert client.get(f"/sessions/{session_id}/messages").status_code == 200
    assert client.delete(f"/sessions/{session_id}").status_code == 204
    assert asyncio.run(_rows_for(session_id)) == 0, "the conversation's rows outlived it"
    assert client.get(f"/sessions/{session_id}/messages").status_code == 404, (
        "the live in-process handle still answers for a deleted session"
    )
    assert session_id not in {row["session_id"] for row in client.get("/sessions").json()}


def test_a_stranger_cannot_delete_a_session_and_learns_nothing_by_trying() -> None:
    """Deleting is authorized exactly as reading is: a non-owner gets the unknown-id 404.

    Not 403, which would be an oracle for which ids exist; the rows must still be there afterwards.
    """
    asyncio.run(migrated_db_or_skip())
    asyncio.run(create_checkpoint_tables())
    session_id = "sess-api-delete-not-yours"
    asyncio.run(_conversation(session_id, _ALICE.oid))

    app = _durable_app()
    assert _client(app, _BOB).delete(f"/sessions/{session_id}").status_code == 404
    assert asyncio.run(_rows_for(session_id)) > 0, "a stranger's DELETE removed rows anyway"
    # Indistinguishable from the id that was never minted, which is the point.
    assert _client(app, _BOB).delete("/sessions/sess-api-never-existed").status_code == 404
    assert _client(app, _ALICE).delete("/sessions/sess-api-never-existed").status_code == 404


def test_a_session_with_a_turn_in_flight_refuses_the_delete() -> None:
    """409 while a turn is running, from either lease — and nothing is deleted.

    A mid-turn delete would race the turn's own writes and leave orphan rows, so it claims the turn
    slot as `POST /sessions/{id}/messages` does. Both leases: the durable claim (another pod) and
    the in-process lease (this one).
    """
    asyncio.run(migrated_db_or_skip())
    asyncio.run(create_checkpoint_tables())
    session_id = "sess-api-delete-busy"
    asyncio.run(_conversation(session_id, _ALICE.oid))
    asyncio.run(SessionTurnClaims().claim(session_id, "another-worker", 60))

    app = _durable_app()
    client = _client(app)
    assert client.delete(f"/sessions/{session_id}").status_code == 409, (
        "a delete was admitted while another worker held the session's turn claim"
    )
    assert asyncio.run(_rows_for(session_id)) > 0

    asyncio.run(SessionTurnClaims().release(session_id, "another-worker"))
    app.state.active_turns[session_id] = TurnLease(
        token="live-turn", deadline=float("inf"), actor="alice", claimed_at=time.monotonic()
    )
    assert client.delete(f"/sessions/{session_id}").status_code == 409, (
        "a delete was admitted while this process was running a turn on the session"
    )
    assert asyncio.run(_rows_for(session_id)) > 0

    del app.state.active_turns[session_id]
    assert client.delete(f"/sessions/{session_id}").status_code == 204
    assert asyncio.run(_rows_for(session_id)) == 0


def test_a_session_with_a_turn_in_flight_refuses_the_fork() -> None:
    """409 while a turn is running — the same claim pair `DELETE` takes, for the same reason.

    A fork reads five tables statement by statement at READ COMMITTED, so a commit between them
    could copy a checkpoint without its blobs, and the forkability check could change between count
    and copy. Both leases, as in the delete test.
    """
    asyncio.run(migrated_db_or_skip())
    asyncio.run(create_checkpoint_tables())
    session_id = "sess-api-fork-busy"
    asyncio.run(_conversation(session_id, _ALICE.oid))
    asyncio.run(_checkpoint_for(session_id))
    asyncio.run(SessionTurnClaims().claim(session_id, "another-worker", 60))

    app = _durable_app()
    client = _client(app)
    assert client.post(f"/sessions/{session_id}/fork").status_code == 409, (
        "a fork was admitted while another worker held the session's turn claim"
    )

    asyncio.run(SessionTurnClaims().release(session_id, "another-worker"))
    app.state.active_turns[session_id] = TurnLease(
        token="live-turn", deadline=float("inf"), actor="alice", claimed_at=time.monotonic()
    )
    assert client.post(f"/sessions/{session_id}/fork").status_code == 409, (
        "a fork was admitted while this process was running a turn on the session"
    )

    del app.state.active_turns[session_id]
    forked = client.post(f"/sessions/{session_id}/fork")
    assert forked.status_code == 200, forked.text
    # The claim is given back, not held: a fork that leaked the session's slot would lock the
    # parent out of its own next turn for a whole lease.
    assert client.post(f"/sessions/{session_id}/fork").status_code == 200


def test_each_transcript_message_names_the_turn_that_stored_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`correlation_id` comes back from `session_messages`, so a detached turn is found by identity.

    Two turns under two ids; a row stored off the request path has no turn and reads back as `None`,
    not `""`.
    """
    import uuid

    from chemclaw.core.identity_context import (
        reset_current_correlation_id,
        set_current_correlation_id,
    )

    asyncio.run(migrated_db_or_skip())
    session_id = f"sess-api-correlation-{uuid.uuid4().hex[:8]}"

    async def seed() -> None:
        await SessionOwnerStore().record(session_id, _ALICE.oid)
        history = PostgresHistoryProvider()
        await history.save_messages(session_id, [HumanMessage(content="off the request path")])
        for turn in ("turn-one-0001", "turn-two-0002"):
            token = set_current_correlation_id(turn)
            try:
                await history.save_messages(
                    session_id, [HumanMessage(content=turn), AIMessage(content=f"answer {turn}")]
                )
            finally:
                reset_current_correlation_id(token)

    asyncio.run(seed())
    # The durable provider is what the route reads through here; the default is the in-memory one.
    monkeypatch.setattr(settings, "session_store", "postgres")
    transcript = _client(_durable_app()).get(f"/sessions/{session_id}/messages").json()
    assert [(row["text"], row["correlation_id"]) for row in transcript] == [
        ("off the request path", None),
        ("turn-one-0001", "turn-one-0001"),
        ("answer turn-one-0001", "turn-one-0001"),
        ("turn-two-0002", "turn-two-0002"),
        ("answer turn-two-0002", "turn-two-0002"),
    ]
