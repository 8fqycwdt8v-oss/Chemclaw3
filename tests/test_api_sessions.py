"""The session lifecycle routes over a real database: paging the list, and deleting one.

Everything here drives the production app (`create_app`) with the *real* durable stores rather than
the in-memory fakes `tests/test_service.py` injects, because both behaviours under test are
statements about SQL: a keyset page boundary and a twelve-table delete cannot be proven against a
registry that holds a dict. CI provides Postgres; the offline sandbox skips (`tests/pg.py`).

`D-2026-08-27-a-session-list-is-a-cursor-and-a-session-is-deletable` is the decision these pin.
"""

import asyncio
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from langchain_core.messages import HumanMessage

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
from tests.pg import migrated_db_or_skip

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


async def _rows_for(session_id: str) -> int:
    """How many rows of the delete's own table set still name this session."""
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

    Both halves matter. The cursor is what makes the older conversations reachable — before it,
    `service_max_listed_sessions` silently *was* the list. And it is carried in a response header
    rather than in the body precisely because the body is a bare JSON array that the companion UI
    parses as one (`src/api/client.ts`: `request<SessionSummary[]>('/sessions')`): an envelope would
    have broken every deployed client in order to add a field. So this asserts the shape as well as
    the paging — a list of objects, each still carrying exactly the four fields it always had.
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

    Silently answering page one would be the worst of the three: a client that mangles its cursor
    would page forever over the same two rows, which reads as data corruption rather than as the
    bad request it is.
    """
    asyncio.run(migrated_db_or_skip())
    client = _client(_durable_app())
    assert client.get("/sessions", params={"after": "not-a-cursor!"}).status_code == 422


def test_an_owner_deletes_their_own_session_and_it_stops_existing() -> None:
    """204, the durable rows are gone, and the id no longer resolves *on this pod either*.

    The last clause is the one worth a test. The front door holds live sessions in an in-process
    LRU that `_resolve_session` consults before the store, so a delete that only cleared the
    database would leave this process happily serving — and writing new messages into — a
    conversation whose ownership row no longer exists, under an id no session-scoped sweep in this
    system could ever find again.
    """
    asyncio.run(migrated_db_or_skip())
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

    Not 403 — the same refusal `GET /sessions/{id}/messages` gives, because a status that
    distinguished "not yours" from "no such thing" would turn this route into an oracle for which
    session ids exist. That the rows are still there afterwards is the half a status code alone
    would not prove.
    """
    asyncio.run(migrated_db_or_skip())
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

    A delete landing mid-turn would race the turn's own writes: the transcript row and the
    checkpoint the turn is about to commit would arrive *after* the sweep, leaving exactly the
    orphaned rows the sweep exists to prevent. So the delete claims the session's turn slot the same
    way `POST /sessions/{id}/messages` claims it, and refuses on the same 409 when it cannot.

    Both leases are exercised because they answer different questions: the durable claim is another
    *pod* running the turn (the shipped chart runs two replicas), and the in-process lease is this
    one.
    """
    asyncio.run(migrated_db_or_skip())
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
    app.state.active_turns[session_id] = TurnLease(token="live-turn", deadline=float("inf"))
    assert client.delete(f"/sessions/{session_id}").status_code == 409, (
        "a delete was admitted while this process was running a turn on the session"
    )
    assert asyncio.run(_rows_for(session_id)) > 0

    del app.state.active_turns[session_id]
    assert client.delete(f"/sessions/{session_id}").status_code == 204
    assert asyncio.run(_rows_for(session_id)) == 0
