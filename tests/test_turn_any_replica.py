"""A running turn is followed and stopped from a replica that does not hold it.

`D-2026-10-04-a-running-turn-is-reached-through-postgres-from-any-replica`. The defect, measured
with two front-door processes on one database: a turn started on A answered 404 "no turn is
running for this session" to `GET /sessions/{id}/turn/stream` and `POST /sessions/{id}/turn/stop`
on B, and ran on to its answer as if nobody had pressed Stop — because the pump, its readers and
its cancel live in A's memory, and the BFF reaches the front door through the Service.

Two `create_app()` instances, each under its own uvicorn server and event loop, over one migrated
database with the durable stores — the faithful model of two pods (`tests.test_detach._Served`).
Driven at the routes a client calls (`tasks/lessons.md` rule 55):

- a participant follows the turn from B and sees it to the same ending the sender sees on A;
- a Stop sent to B ends the turn on A, and its question settles `stopped`;
- the sender-or-owner rule and the session gate hold on B exactly as on A — a member cannot stop
  somebody else's turn from there (403), and a non-participant can neither follow nor stop it (404);
- an unload stop sent to B is deferred on A, and the sender reattaching on B cancels it.
"""

import asyncio
import json
import threading
import uuid
from collections.abc import AsyncIterator, Callable, Iterator
from typing import Any

import httpx
import pytest
from fastapi import FastAPI, Request
from langchain_core.messages import HumanMessage

from chemclaw.agent import session_members as members_module
from chemclaw.agent.checkpointer import close_checkpointer
from chemclaw.agent.session_store import (
    PostgresHistoryProvider,
    SessionOwnerStore,
    SessionTurnClaims,
    stored_turn_status,
)
from chemclaw.agent.turn_remotes import TurnRemotes
from chemclaw.api.app import create_app
from chemclaw.api.auth import Principal, require_principal
from chemclaw.api.routes.turns import TURN_CORRELATION_HEADER
from chemclaw.core.config import settings
from tests.fakes_turn import Piece, ScriptedTurn
from tests.pg import create_checkpoint_tables, migrated_db_or_skip
from tests.test_detach import _Served

_ANA = Principal(oid="ana-replica", upn="ana@corp", roles=frozenset({"process-chemist"}))
_BEN = Principal(oid="ben-replica", upn="ben@corp", roles=frozenset({"process-chemist"}))
_DAN = Principal(oid="dan-replica", upn="dan@corp", roles=frozenset({"process-chemist"}))
_PEOPLE = {person.oid: person for person in (_ANA, _BEN, _DAN)}


class _Gated(ScriptedTurn):
    """A turn that parks at `hold` until `gate` is set — a `threading.Event`, across two loops."""

    def __init__(self) -> None:
        """Nothing has started yet."""
        self.gate = threading.Event()
        self.started: list[str] = []

    async def stream(self, message: str) -> AsyncIterator[Piece]:
        """Say it started, wait at the gate for a `hold` message, then answer."""
        self.started.append(message)
        yield "working "
        if message.startswith("hold"):
            while not self.gate.is_set():
                await asyncio.sleep(0.01)
        yield f"done {message}"


def _by_header(request: Request) -> Principal:
    """Authenticate by a test header — authentication is not under test, reach is."""
    return _PEOPLE[request.headers["x-test-user"]]


def _as(person: Principal) -> dict[str, str]:
    """The header that makes a request `person`'s."""
    return {"x-test-user": person.oid}


def _no_connectors(_profile: str | None = None) -> list[Any]:
    """No connectors: the turn is the fake's."""
    return []


def _replica(agent: _Gated) -> FastAPI:
    """One front-door replica over the shared durable stores."""
    app = create_app(
        graph_factory=agent.graph_factory,
        connector_factory=_no_connectors,
        owner_store=SessionOwnerStore(),
        turn_claims=SessionTurnClaims(),
    )
    app.dependency_overrides[require_principal] = _by_header
    return app


@pytest.fixture
def two_replicas(monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[[], tuple[_Gated, Any]]]:
    """Durable stores, identity enforced, a fast relay poll; yields a two-replica factory."""
    monkeypatch.setattr(settings, "session_store", "postgres")
    monkeypatch.setattr(settings, "entra_required", True)
    monkeypatch.setattr(settings, "service_turn_relay_poll_seconds", 0.05)
    monkeypatch.setattr(settings, "service_turn_relay_lease_seconds", 5.0)
    asyncio.run(migrated_db_or_skip())
    asyncio.run(create_checkpoint_tables())
    asyncio.run(close_checkpointer())
    members_module.session_member_store.cache_clear()

    def _make() -> tuple[_Gated, Any]:
        agent = _Gated()
        return agent, (_Served(_replica(agent)), _Served(_replica(agent)))

    yield _make
    members_module.session_member_store.cache_clear()


async def _post(
    client: httpx.AsyncClient, session_id: str, message: str, headers: dict[str, str]
) -> tuple[str, list[dict[str, Any]]]:
    """POST one message and read its stream to the end; the turn's correlation id and events."""
    events: list[dict[str, Any]] = []
    async with client.stream(
        "POST", f"/sessions/{session_id}/messages", json={"message": message}, headers=headers
    ) as response:
        assert response.status_code == 200, await response.aread()
        # The request that starts a turn is the turn: its own correlation id names it.
        correlation = response.headers.get("X-Chemclaw-Correlation-Id", "")
        async for line in response.aiter_lines():
            if line.startswith("data:"):
                events.append(json.loads(line.removeprefix("data:")))
    return correlation, events


async def _watch(
    client: httpx.AsyncClient, session_id: str, person: Principal, attached: asyncio.Event
) -> tuple[str, list[dict[str, Any]]]:
    """Follow the running turn as `person`; set `attached` once the stream is open."""
    events: list[dict[str, Any]] = []
    async with client.stream(
        "GET", f"/sessions/{session_id}/turn/stream", headers=_as(person)
    ) as response:
        assert response.status_code == 200, await response.aread()
        correlation = response.headers.get(TURN_CORRELATION_HEADER, "")
        attached.set()
        async for line in response.aiter_lines():
            if line.startswith("data:"):
                events.append(json.loads(line.removeprefix("data:")))
    return correlation, events


async def _until(predicate: Callable[[], bool], *, seconds: float = 15.0) -> None:
    """Poll a predicate until it holds; fail loudly rather than hang."""
    deadline = asyncio.get_running_loop().time() + seconds
    while not predicate():
        assert asyncio.get_running_loop().time() < deadline, "timed out waiting"
        await asyncio.sleep(0.02)


async def _shared_session(client: httpx.AsyncClient) -> str:
    """A session Ana owns, with Ben admitted as a member; Dan is nobody's."""
    session_id = str((await client.post("/sessions", headers=_as(_ANA))).json()["session_id"])
    admitted = await client.put(f"/sessions/{session_id}/members/{_BEN.oid}", headers=_as(_ANA))
    assert admitted.status_code == 204, admitted.text
    return session_id


@pytest.mark.parametrize("slow_relay", [False, True], ids=["prompt", "relay-still-writing"])
def test_a_turn_held_on_one_replica_is_followed_to_its_end_from_another(
    two_replicas: Callable[[], tuple[_Gated, Any]],
    monkeypatch: pytest.MonkeyPatch,
    slow_relay: bool,
) -> None:
    """Ben follows Ana's turn from B and sees the same ending Ana sees on A; Dan cannot follow.

    `relay-still-writing` slows each relayed write past several holder polls, which is the window
    the first version lost the answer in: the turn ended, its requests stopped being read, and the
    holder cancelled the relay as if the follower had gone — before it wrote the answer and the end
    marker. The follower then saw the stream stop one token short. 1 run in 20 hit it unslowed.
    """
    if slow_relay:
        real_relay = TurnRemotes.relay

        async def _slow(self: TurnRemotes, *args: Any) -> None:
            await asyncio.sleep(0.3)
            await real_relay(self, *args)

        monkeypatch.setattr(TurnRemotes, "relay", _slow)
    agent, (a, b) = two_replicas()

    async def _run() -> tuple[Any, ...]:
        with a, b:
            async with (
                httpx.AsyncClient(base_url=a.base, timeout=30) as on_a,
                httpx.AsyncClient(base_url=b.base, timeout=30) as on_b,
            ):
                session_id = await _shared_session(on_a)
                sender = asyncio.create_task(_post(on_a, session_id, "hold ana", _as(_ANA)))
                await _until(lambda: agent.started == ["hold ana"])
                assert session_id not in b.app.state.active_turns, "the turn must be A's alone"
                stranger = await on_b.get(f"/sessions/{session_id}/turn/stream", headers=_as(_DAN))
                attached = asyncio.Event()
                watcher = asyncio.create_task(_watch(on_b, session_id, _BEN, attached))
                await asyncio.wait_for(attached.wait(), 15)
                agent.gate.set()
                return stranger.status_code, await sender, await watcher

    stranger, (sent_as, sent), (watched_as, watched) = asyncio.run(_run())
    assert stranger == 404, "a non-participant reached another replica's turn"
    assert sent[-1]["type"] == "answer" and sent[-1]["text"].endswith("done hold ana")
    assert watched, "the view from the other replica carried nothing"
    assert watched[-1] == sent[-1], "the follower on B saw a different ending from the sender"
    assert watched == sent[-len(watched) :], "B's view is not the tail of the sender's stream"
    assert watched_as and watched_as == sent_as, "B's view named a different turn"


@pytest.mark.parametrize("slow_answer", [False, True], ids=["prompt", "claim-released-first"])
def test_a_stop_sent_to_another_replica_stops_the_turn_and_obeys_the_senders_rule(
    two_replicas: Callable[[], tuple[_Gated, Any]],
    monkeypatch: pytest.MonkeyPatch,
    slow_answer: bool,
) -> None:
    """From B: Dan is 404, Ben (a member, not the sender) is 403, Ana's Stop ends the turn on A.

    `claim-released-first` delays the holder's `stopped` answer: the stopped turn's teardown gives
    its claim up before the holder can write it, so the asker sees the claim gone while the request
    still reads `stopping` — a Stop that landed, which a first version reported as 404.
    """
    if slow_answer:
        real_answer = TurnRemotes.answer

        async def _late(self: TurnRemotes, request_id: str, state: Any, *rest: Any) -> None:
            if state == "stopped":
                await asyncio.sleep(1.0)
            await real_answer(self, request_id, state, *rest)

        monkeypatch.setattr(TurnRemotes, "answer", _late)
    agent, (a, b) = two_replicas()

    async def _run() -> tuple[Any, ...]:
        with a, b:
            async with (
                httpx.AsyncClient(base_url=a.base, timeout=30) as on_a,
                httpx.AsyncClient(base_url=b.base, timeout=30) as on_b,
            ):
                session_id = await _shared_session(on_a)
                sender = asyncio.create_task(_post(on_a, session_id, "hold ana", _as(_ANA)))
                await _until(lambda: agent.started == ["hold ana"])
                stop = f"/sessions/{session_id}/turn/stop"
                refused = {
                    "stranger": (await on_b.post(stop, headers=_as(_DAN))).status_code,
                    "member": (await on_b.post(stop, headers=_as(_BEN))).status_code,
                }
                stopped = await on_b.post(stop, headers=_as(_ANA))
                _correlation, events = await asyncio.wait_for(sender, 15)
                a.wait_for_slot_release(session_id)
                after = await on_b.post(stop, headers=_as(_ANA))
                return session_id, refused, stopped, events, after.status_code

    session_id, refused, stopped, events, after = asyncio.run(_run())
    # Read once both replicas are down: while either serves, `db.pooling()` is on for the process,
    # and a pool opened on this test's own loop hangs `asyncio.run`'s task cancellation at exit.
    transcript = asyncio.run(PostgresHistoryProvider().get_messages(session_id))
    assert refused == {"stranger": 404, "member": 403}, refused
    assert stopped.status_code == 200 and stopped.json() == {"stopped": True}, stopped.text
    assert not agent.gate.is_set() and all(event["type"] != "answer" for event in events), (
        "the turn answered: the Stop sent to B never reached A"
    )
    assert after == 404, "a stopped turn still read as running"
    questions = [m for m in transcript if isinstance(m, HumanMessage)]
    assert [stored_turn_status(m) for m in questions] == ["stopped"], questions


def test_an_unload_stop_sent_to_another_replica_waits_and_the_senders_reattach_cancels_it(
    two_replicas: Callable[[], tuple[_Gated, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A page unloading sends its stop to B; reloading, it reattaches on B; the turn answers."""
    monkeypatch.setattr(settings, "service_turn_unload_grace_seconds", 2.0)
    agent, (a, b) = two_replicas()

    async def _run() -> tuple[Any, ...]:
        with a, b:
            async with (
                httpx.AsyncClient(base_url=a.base, timeout=30) as on_a,
                httpx.AsyncClient(base_url=b.base, timeout=30) as on_b,
            ):
                session_id = await _shared_session(on_a)
                sender = asyncio.create_task(_post(on_a, session_id, "hold ana", _as(_ANA)))
                await _until(lambda: agent.started == ["hold ana"])
                deferred = await on_b.post(
                    f"/sessions/{session_id}/turn/stop?reason=unload", headers=_as(_ANA)
                )
                attached = asyncio.Event()
                watcher = asyncio.create_task(_watch(on_b, session_id, _ANA, attached))
                await asyncio.wait_for(attached.wait(), 15)
                # Past the grace window: a stop that was not cancelled would have landed by now.
                await asyncio.sleep(3.0)
                agent.gate.set()
                _sent_as, events = await sender
                _watched_as, watched = await watcher
                return deferred, events, watched

    deferred, events, watched = asyncio.run(_run())
    assert deferred.status_code == 200, deferred.text
    assert deferred.json() == {"stopped": False, "deferred": True}
    assert events[-1]["type"] == "answer", "the reattach on B did not cancel the unload stop on A"
    assert watched[-1] == events[-1]


def test_a_follow_and_a_stop_on_the_holding_replica_are_unchanged(
    two_replicas: Callable[[], tuple[_Gated, Any]],
) -> None:
    """The local path still answers itself: a session with no turn anywhere is 404 on both."""
    _agent, (a, b) = two_replicas()

    async def _run() -> list[int]:
        with a, b:
            async with (
                httpx.AsyncClient(base_url=a.base, timeout=30) as on_a,
                httpx.AsyncClient(base_url=b.base, timeout=30) as on_b,
            ):
                session_id = await _shared_session(on_a)
                return [
                    (await client.request(method, path, headers=_as(_ANA))).status_code
                    for client in (on_a, on_b)
                    for method, path in (
                        ("GET", f"/sessions/{session_id}/turn/stream"),
                        ("POST", f"/sessions/{session_id}/turn/stop"),
                    )
                ]

    assert asyncio.run(_run()) == [404, 404, 404, 404]


def test_a_request_names_one_turn_so_the_next_turn_never_serves_it() -> None:
    """The holder reads requests by `(session, holder)`: a request for an old turn is invisible.

    A request is addressed to the claim it was written against, so a follow or a stop meant for a
    turn that has since ended cannot be served by the next turn on the session — which would stop
    somebody else's question.
    """

    async def _run() -> tuple[list[str], list[str]]:
        await migrated_db_or_skip()
        session_id = f"sess-remote-{uuid.uuid4().hex[:10]}"
        await SessionOwnerStore().record(session_id, _ANA.oid, None)
        remotes = TurnRemotes()
        asked = await remotes.ask(session_id, "old-turn", "stop", _ANA.oid, 30.0)
        try:
            for_next = [r.id for r in await remotes.pending([(session_id, "next-turn")])]
            for_old = [r.id for r in await remotes.pending([(session_id, "old-turn")])]
        finally:
            await remotes.withdraw(asked)
        return for_next, for_old

    for_next, for_old = asyncio.run(_run())
    assert for_next == [], "a request for an ended turn reached the next one"
    assert len(for_old) == 1
