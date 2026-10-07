"""A busy shared session is a line, every participant can watch a turn, and the inbox finds members.

- The line: a message sent during another turn waits in order and runs as its own sender; only
  the sender or owner may withdraw it, a removed member's message does not run, a full line is a
  409, and a stranger can neither join nor read it.
- Fan-out: each participant follows the turn on its own buffer; a stalled watcher is cut off
  with `stream_lagged`, and leaving never stops the turn.
- The inbox: `GET /plans/pending` lists a plan the caller authored in a session they are a
  member of, and no one else's.

HTTP cases run under a real uvicorn server (`tests.test_detach._Served`), since the in-process
transports buffer a response whole.
"""

import asyncio
import json
import threading
import time
import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import Request

from chemclaw.agent import plan_approval_store as store_module
from chemclaw.agent import session_members as members_module
from chemclaw.agent.plan_gate import plan_identity
from chemclaw.agent.session_members import session_member_store
from chemclaw.agent.session_queue import InMemoryTurnQueue, QueueRefused, TurnQueue
from chemclaw.api import auth
from chemclaw.api.auth import Principal, require_principal
from chemclaw.api.detach import _QUEUE_SIZE, DetachableTurn
from chemclaw.api.routes import turns as turns_module
from chemclaw.core.config import settings
from chemclaw.core.identity_context import get_current_actor, get_current_roles
from tests.fakes_turn import Piece, ScriptedTurn
from tests.test_detach import _Served
from tests.test_plan_inbox import _BOB, _Inbox, _steps
from tests.test_service import _app, _FakeOwnerStore

_ANA = Principal(oid="ana-line", upn="ana@corp", roles=frozenset({"process-chemist"}))
_BEN = Principal(oid="ben-line", upn="ben@corp", roles=frozenset({"analyst"}))
_CAT = Principal(oid="cat-line", upn="cat@corp", roles=frozenset({"qa-reviewer"}))
_DAN = Principal(oid="dan-line", upn="dan@corp", roles=frozenset({"process-chemist"}))
_PEOPLE = {person.oid: person for person in (_ANA, _BEN, _CAT, _DAN)}
_AUDIENCE = "api://chemclaw-line"
_ISSUER = "https://issuer.test/line/v2.0"


async def _reauthorize_by_header(request: Request, principal: Principal) -> Principal:
    """The head-of-line re-check, re-resolving the sender the way `_by_header` first did.

    These cases authenticate by header, so the token re-check is replaced; the token-expiry test
    restores the real `auth.reauthorize`.
    """
    fresh = _by_header(request)
    assert fresh.oid == principal.oid
    return fresh


@pytest.fixture(autouse=True)
def _stores(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Identity enforced, in-process stores obtained through their real factories, a fast poll."""
    monkeypatch.setattr(settings, "session_store", "memory")
    monkeypatch.setattr(settings, "entra_required", True)
    monkeypatch.setattr(settings, "service_turn_queue_poll_seconds", 0.05)
    monkeypatch.setattr(turns_module, "reauthorize", _reauthorize_by_header)
    members_module.session_member_store.cache_clear()
    store_module.plan_approval_store.cache_clear()
    yield
    members_module.session_member_store.cache_clear()
    store_module.plan_approval_store.cache_clear()


class _Ledger(ScriptedTurn):
    """Records, from inside the running graph, who each turn ran as and in what order.

    A message starting with `hold` parks until the test sets `gate` — a `threading.Event`, because
    the server's loop is on another thread from the test's.
    """

    def __init__(self) -> None:
        """No turn has run yet."""
        self.gate = threading.Event()
        self.ran: list[tuple[str, str | None, frozenset[str]]] = []

    async def stream(self, message: str) -> AsyncIterator[Piece]:
        """Record the ambient identity the tools would see, then answer (after the gate)."""
        self.ran.append((message, get_current_actor(), frozenset(get_current_roles())))
        yield "working "
        if message.startswith("hold"):
            while not self.gate.is_set():
                await asyncio.sleep(0.01)
        yield f"done {message}"


def _by_header(request: Request) -> Principal:
    """Authenticate by a test header — authentication is not under test, identity is."""
    return _PEOPLE[request.headers["x-test-user"]]


def _served(agent: _Ledger) -> _Served:
    """The production app, principals by header, durable ownership faked, model faked."""
    app = _app(agent, owner_store=_FakeOwnerStore())
    app.dependency_overrides[require_principal] = _by_header
    return _Served(app)


def _as(person: Principal) -> dict[str, str]:
    """The header that makes a request `person`'s."""
    return {"x-test-user": person.oid}


async def _post(
    client: httpx.AsyncClient, person: Principal, session_id: str, message: str
) -> tuple[int, list[dict[str, Any]]]:
    """POST one message as `person` and read its stream to the end; `(status, events)`."""
    events: list[dict[str, Any]] = []
    async with client.stream(
        "POST",
        f"/sessions/{session_id}/messages",
        json={"message": message},
        headers=_as(person),
    ) as response:
        if response.status_code != 200:
            await response.aread()
            return response.status_code, [{"detail": response.text}]
        async for line in response.aiter_lines():
            if line.startswith("data:"):
                events.append(json.loads(line.removeprefix("data:")))
    return 200, events


async def _watch(
    client: httpx.AsyncClient, person: Principal, session_id: str, attached: asyncio.Event
) -> list[dict[str, Any]]:
    """Follow the session's running turn as `person`; set `attached` once the stream is open."""
    events: list[dict[str, Any]] = []
    async with client.stream(
        "GET", f"/sessions/{session_id}/turn/stream", headers=_as(person)
    ) as response:
        assert response.status_code == 200, await response.aread()
        attached.set()
        async for line in response.aiter_lines():
            if line.startswith("data:"):
                events.append(json.loads(line.removeprefix("data:")))
    return events


async def _shared_session(client: httpx.AsyncClient, *members: Principal) -> str:
    """A session Ana owns with `members` admitted."""
    session_id = str((await client.post("/sessions", headers=_as(_ANA))).json()["session_id"])
    for member in members:
        admitted = await client.put(
            f"/sessions/{session_id}/members/{member.oid}", headers=_as(_ANA)
        )
        assert admitted.status_code == 204, admitted.text
    return session_id


async def _until(predicate: Any, *, seconds: float = 10.0) -> None:
    """Poll an async predicate until it holds; fail loudly rather than hang."""
    deadline = time.monotonic() + seconds
    while not await predicate():
        if time.monotonic() > deadline:  # pragma: no cover - only on a regression
            raise AssertionError("the condition never held")
        await asyncio.sleep(0.02)


async def _line(client: httpx.AsyncClient, session_id: str) -> list[dict[str, Any]]:
    """The session's line as its owner reads it."""
    response = await client.get(f"/sessions/{session_id}/queue", headers=_as(_ANA))
    assert response.status_code == 200, response.text
    waiting: list[dict[str, Any]] = response.json()["waiting"]
    return waiting


def _ran(agent: _Ledger, served: _Served, session_id: str) -> None:
    """Let the held turn go and wait for every turn on the session to finish."""
    agent.gate.set()
    served.wait_for_slot_release(session_id)


# --- the line ------------------------------------------------------------------------------------


def test_a_message_sent_during_anothers_turn_waits_in_order_and_runs_as_its_sender() -> None:
    """Ben then Cat, behind Ana's turn: in that order, each as themselves, each told their place."""
    agent = _Ledger()

    async def _run() -> tuple[Any, ...]:
        with _served(agent) as served:
            async with httpx.AsyncClient(base_url=served.base, timeout=30) as client:
                session_id = await _shared_session(client, _BEN, _CAT)
                ana = asyncio.create_task(_post(client, _ANA, session_id, "hold ana"))
                await _until(lambda: _started(agent, 1))
                ben = asyncio.create_task(_post(client, _BEN, session_id, "ben asks"))
                await _until(lambda: _waiting(client, session_id, 1))
                cat = asyncio.create_task(_post(client, _CAT, session_id, "cat asks"))
                await _until(lambda: _waiting(client, session_id, 2))
                line = await _line(client, session_id)
                assert len(agent.ran) == 1, "a waiting message ran beside the turn ahead of it"
                _ran(agent, served, session_id)
                return line, await ana, await ben, await cat

    line, ana, ben, cat = asyncio.run(_run())

    assert [(entry["sender"], entry["position"]) for entry in line] == [
        (_BEN.oid, 0),
        (_CAT.oid, 1),
    ]
    assert [(message, actor, roles) for message, actor, roles in agent.ran] == [
        ("hold ana", _ANA.oid, _ANA.roles),
        ("ben asks", _BEN.oid, _BEN.roles),
        ("cat asks", _CAT.oid, _CAT.roles),
    ], "a queued message ran out of order or under somebody else's identity"
    for status, events in (ana, ben, cat):
        assert status == 200 and events[-1]["type"] == "answer", events
    ben_places = [e["position"] for e in ben[1] if e["type"] == "queued" and e["ticket"]]
    cat_places = [e["position"] for e in cat[1] if e["type"] == "queued" and e["ticket"]]
    assert ben_places == [0], ben[1]
    assert cat_places[0] == 1 and cat_places[-1] == 0, cat[1]


async def _started(agent: _Ledger, count: int) -> bool:
    """Whether `count` turns have reached the model."""
    return len(agent.ran) >= count


async def _waiting(client: httpx.AsyncClient, session_id: str, count: int) -> bool:
    """Whether the session's line holds `count` messages."""
    return len(await _line(client, session_id)) == count


def test_a_stranger_can_neither_join_the_line_nor_read_it_nor_withdraw_from_it() -> None:
    """Every queue route answers a non-participant the same 404 an unknown session gets."""
    agent = _Ledger()

    async def _run() -> tuple[list[int], list[dict[str, Any]]]:
        with _served(agent) as served:
            async with httpx.AsyncClient(base_url=served.base, timeout=30) as client:
                session_id = await _shared_session(client, _BEN)
                ana = asyncio.create_task(_post(client, _ANA, session_id, "hold ana"))
                await _until(lambda: _started(agent, 1))
                ben = asyncio.create_task(_post(client, _BEN, session_id, "ben asks"))
                await _until(lambda: _waiting(client, session_id, 1))
                ticket = (await _line(client, session_id))[0]["ticket"]
                statuses = [
                    (await _post(client, _DAN, session_id, "let me in"))[0],
                    (
                        await client.get(f"/sessions/{session_id}/queue", headers=_as(_DAN))
                    ).status_code,
                    (
                        await client.delete(
                            f"/sessions/{session_id}/queue/{ticket}", headers=_as(_DAN)
                        )
                    ).status_code,
                ]
                line = await _line(client, session_id)
                _ran(agent, served, session_id)
                await ana
                await ben
                return statuses, line

    statuses, line = asyncio.run(_run())
    assert statuses == [404, 404, 404], statuses
    assert [entry["sender"] for entry in line] == [_BEN.oid], "a stranger changed the line"
    assert [message for message, _actor, _roles in agent.ran] == ["hold ana", "ben asks"]


def test_only_the_sender_or_the_owner_withdraws_a_waiting_message() -> None:
    """Cat cannot withdraw Ben's; Ben can withdraw his own; Ana, the owner, can withdraw Cat's."""
    agent = _Ledger()

    async def _run() -> tuple[Any, ...]:
        with _served(agent) as served:
            async with httpx.AsyncClient(base_url=served.base, timeout=30) as client:
                session_id = await _shared_session(client, _BEN, _CAT)
                ana = asyncio.create_task(_post(client, _ANA, session_id, "hold ana"))
                await _until(lambda: _started(agent, 1))
                ben = asyncio.create_task(_post(client, _BEN, session_id, "ben asks"))
                await _until(lambda: _waiting(client, session_id, 1))
                cat = asyncio.create_task(_post(client, _CAT, session_id, "cat asks"))
                await _until(lambda: _waiting(client, session_id, 2))
                ben_ticket, cat_ticket = [e["ticket"] for e in await _line(client, session_id)]

                def _withdraw(person: Principal, ticket: int) -> Any:
                    return client.delete(
                        f"/sessions/{session_id}/queue/{ticket}", headers=_as(person)
                    )

                refused = (await _withdraw(_CAT, ben_ticket)).status_code
                own = (await _withdraw(_BEN, ben_ticket)).status_code
                owners = (await _withdraw(_ANA, cat_ticket)).status_code
                gone = (await _withdraw(_ANA, cat_ticket)).status_code
                ben_result = await ben
                cat_result = await cat
                _ran(agent, served, session_id)
                await ana
                return refused, own, owners, gone, ben_result, cat_result

    refused, own, owners, gone, ben, cat = asyncio.run(_run())
    assert (refused, own, owners, gone) == (403, 204, 204, 404)
    for status, events in (ben, cat):
        assert status == 200
        assert events[-1]["type"] == "error" and events[-1]["code"] == "queue_cancelled", events
    assert [message for message, _actor, _roles in agent.ran] == ["hold ana"], (
        "a withdrawn message ran anyway"
    )


def test_a_member_removed_while_waiting_does_not_have_their_message_run() -> None:
    """Membership is re-read at the head of the line, not trusted from when the message was sent."""
    agent = _Ledger()

    async def _run() -> list[dict[str, Any]]:
        with _served(agent) as served:
            async with httpx.AsyncClient(base_url=served.base, timeout=30) as client:
                session_id = await _shared_session(client, _BEN)
                ana = asyncio.create_task(_post(client, _ANA, session_id, "hold ana"))
                await _until(lambda: _started(agent, 1))
                ben = asyncio.create_task(_post(client, _BEN, session_id, "ben asks"))
                await _until(lambda: _waiting(client, session_id, 1))
                removed = await client.delete(
                    f"/sessions/{session_id}/members/{_BEN.oid}", headers=_as(_ANA)
                )
                assert removed.status_code == 204
                _ran(agent, served, session_id)
                await ana
                status, events = await ben
                assert status == 200
                return events

    events = asyncio.run(_run())
    assert events[-1]["type"] == "error" and events[-1]["code"] == "queue_cancelled", events
    assert [message for message, _actor, _roles in agent.ran] == ["hold ana"]


def test_a_full_line_is_refused_409(monkeypatch: pytest.MonkeyPatch) -> None:
    """Past `service_turn_queue_max` waiting messages the next is refused, before any stream."""
    monkeypatch.setattr(settings, "service_turn_queue_max", 1)
    agent = _Ledger()

    async def _run() -> tuple[int, str]:
        with _served(agent) as served:
            async with httpx.AsyncClient(base_url=served.base, timeout=30) as client:
                session_id = await _shared_session(client, _BEN, _CAT)
                ana = asyncio.create_task(_post(client, _ANA, session_id, "hold ana"))
                await _until(lambda: _started(agent, 1))
                ben = asyncio.create_task(_post(client, _BEN, session_id, "ben asks"))
                await _until(lambda: _waiting(client, session_id, 1))
                status, events = await _post(client, _CAT, session_id, "cat asks")
                _ran(agent, served, session_id)
                await ana
                await ben
                return status, str(events)

    status, detail = asyncio.run(_run())
    assert status == 409 and "already waiting" in detail, detail
    # A code beside the sentence (Chemclaw3 #503), so a client tells a full line from a sender
    # already waiting without matching prose.
    assert '"code":"queue_full"' in detail, detail
    assert [message for message, _actor, _roles in agent.ran] == ["hold ana", "ben asks"]


# --- fan-out -------------------------------------------------------------------------------------


def test_every_participant_can_follow_the_running_turn_and_nobody_else_can(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ben and Cat see Ana's turn to its end; Dan cannot watch it; nobody watches past the cap."""
    monkeypatch.setattr(settings, "service_turn_max_watchers", 2)
    agent = _Ledger()

    async def _run() -> tuple[Any, ...]:
        with _served(agent) as served:
            async with httpx.AsyncClient(base_url=served.base, timeout=30) as client:
                session_id = await _shared_session(client, _BEN, _CAT)
                dans = str((await client.post("/sessions", headers=_as(_DAN))).json()["session_id"])
                ana = asyncio.create_task(_post(client, _ANA, session_id, "hold ana"))
                await _until(lambda: _started(agent, 1))
                attached = [asyncio.Event(), asyncio.Event()]
                ben = asyncio.create_task(_watch(client, _BEN, session_id, attached[0]))
                cat = asyncio.create_task(_watch(client, _CAT, session_id, attached[1]))
                for event in attached:
                    await asyncio.wait_for(event.wait(), 10)
                stream = f"/sessions/{session_id}/turn/stream"
                refused = {
                    "stranger": (await client.get(stream, headers=_as(_DAN))).status_code,
                    "past the cap": (await client.get(stream, headers=_as(_ANA))).status_code,
                    "another session": (
                        await client.get(f"/sessions/{dans}/turn/stream", headers=_as(_BEN))
                    ).status_code,
                    "no turn there": (
                        await client.get(f"/sessions/{dans}/turn/stream", headers=_as(_DAN))
                    ).status_code,
                }
                _ran(agent, served, session_id)
                return refused, await ana, await ben, await cat

    refused, (status, sender), ben, cat = asyncio.run(_run())
    assert refused == {
        "stranger": 404,
        "past the cap": 429,
        "another session": 404,
        "no turn there": 404,
    }, refused
    assert status == 200 and sender[-1]["type"] == "answer"
    for watcher in (ben, cat):
        assert watcher[-1] == sender[-1], "a watcher saw a different ending from the sender"
        assert watcher == sender[-len(watcher) :], "a watcher's view is not the sender's tail"


async def _burst(count: int, gate: asyncio.Event | None = None) -> AsyncIterator[dict[str, str]]:
    """`count` events, yielding to the loop between each so a *reading* reader keeps up."""
    for index in range(count):
        if gate is not None and index == 1:
            await gate.wait()
        yield {"event": "token", "data": str(index)}
        await asyncio.sleep(0)


def test_a_stalled_watcher_is_cut_off_without_holding_the_turn_or_the_sender() -> None:
    """One watcher never reads; the sender still gets every event and the turn still finishes."""

    async def _run() -> tuple[int, list[dict[str, str]], bool]:
        count = _QUEUE_SIZE + 50
        turn = DetachableTurn(_burst(count), session_id="s-fan")
        stalled = turn.watch()
        assert stalled is not None
        seen = 0
        async for _event in turn.events():
            seen += 1
        finished = not turn.running
        tail = [event async for event in stalled.events]
        return seen, tail, finished

    seen, tail, finished = asyncio.run(_run())
    assert seen == _QUEUE_SIZE + 50, "the sender lost events to a stalled watcher"
    assert finished
    assert len(tail) == _QUEUE_SIZE + 1, "the cut-off watcher did not keep what it had buffered"
    assert tail[-1]["event"] == "error" and "stream_lagged" in tail[-1]["data"], tail[-1]


def test_a_watcher_leaving_never_stops_the_turn_even_under_the_old_posture() -> None:
    """Only the sender's departure is a detach; `survive_disconnect=False` still stops on *that*."""

    async def _run() -> tuple[bool, bool]:
        gate = asyncio.Event()
        turn = DetachableTurn(_burst(5, gate), session_id="s-leave", survive_disconnect=False)
        watch = turn.watch()
        assert watch is not None
        watcher = watch.events
        reader = asyncio.ensure_future(anext(watcher))
        await asyncio.sleep(0.01)
        reader.cancel()
        await watcher.aclose()
        still_running = turn.running
        gate.set()
        async for _event in turn.events():
            pass
        return still_running, not turn.running

    still_running, finished = asyncio.run(_run())
    assert still_running, "a watcher closing their view stopped the sender's turn"
    assert finished


# --- re-authorized at the head, and bounded (issue #503) -----------------------------------------
# `D-2026-10-02-a-queued-message-is-re-authorized-at-the-head-of-the-line`.


def _sign(key: Any, person: Principal, lifetime: float) -> str:
    """A real RS256 token for `person`, expiring `lifetime` seconds from now."""
    claims = {
        "aud": _AUDIENCE,
        "iss": _ISSUER,
        "exp": int(time.time() + lifetime),
        "oid": person.oid,
        "preferred_username": person.upn,
        "roles": sorted(person.roles),
    }
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    return jwt.encode(claims, pem, algorithm="RS256")


def test_a_message_whose_token_expires_while_it_waits_does_not_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A message whose sender's token expires while it waits ends `queue_cancelled` and never runs.

    Real tokens, nothing patched in the seam.
    """
    monkeypatch.setattr(turns_module, "reauthorize", auth.reauthorize)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    monkeypatch.setattr(settings, "entra_audience", _AUDIENCE)
    monkeypatch.setattr(settings, "entra_issuer", _ISSUER)
    monkeypatch.setattr(auth, "_signing_key", lambda _token: key.public_key())
    agent = _Ledger()

    def _bearer(person: Principal, lifetime: float) -> dict[str, str]:
        return {"Authorization": f"Bearer {_sign(key, person, lifetime)}"}

    async def _run() -> list[dict[str, Any]]:
        with _Served(_app(agent, owner_store=_FakeOwnerStore())) as served:
            async with httpx.AsyncClient(base_url=served.base, timeout=30) as client:
                ana = _bearer(_ANA, 600)
                created = await client.post("/sessions", headers=ana)
                assert created.status_code == 200, created.text
                session_id = str(created.json()["session_id"])
                admitted = await client.put(
                    f"/sessions/{session_id}/members/{_BEN.oid}", headers=ana
                )
                assert admitted.status_code == 204, admitted.text

                async def _send(headers: dict[str, str], message: str) -> list[dict[str, Any]]:
                    events: list[dict[str, Any]] = []
                    async with client.stream(
                        "POST",
                        f"/sessions/{session_id}/messages",
                        json={"message": message},
                        headers=headers,
                    ) as response:
                        assert response.status_code == 200, await response.aread()
                        async for line in response.aiter_lines():
                            if line.startswith("data:"):
                                events.append(json.loads(line.removeprefix("data:")))
                    return events

                held = asyncio.create_task(_send(ana, "hold ana"))
                await _until(lambda: _started(agent, 1))
                ben = asyncio.create_task(_send(_bearer(_BEN, 2), "ben asks"))
                events = await asyncio.wait_for(ben, 15)
                _ran(agent, served, session_id)
                await held
                return events

    events = asyncio.run(_run())
    assert events[-1]["type"] == "error" and events[-1]["code"] == "queue_cancelled", events
    assert "sign-in expired" in events[-1]["message"], events[-1]
    assert [message for message, _actor, _roles in agent.ran] == ["hold ana"], (
        "a message ran on a token that had expired while it waited"
    )


class _TwoGates(_Ledger):
    """`_Ledger` plus a second hold — `park` — released by its own event, not `gate`."""

    def __init__(self) -> None:
        """Both gates closed."""
        super().__init__()
        self.park = threading.Event()

    async def stream(self, message: str) -> AsyncIterator[Piece]:
        """Park a `park` message on its own gate; otherwise behave as `_Ledger`."""
        if not message.startswith("park"):
            async for piece in super().stream(message):
                yield piece
            return
        self.ran.append((message, get_current_actor(), frozenset(get_current_roles())))
        yield "parked "
        while not self.park.is_set():
            await asyncio.sleep(0.01)
        yield f"done {message}"


def test_a_sender_at_their_cap_when_their_turn_comes_is_refused_at_the_head(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The per-actor turn cap is counted again when a waiting message reaches the head of the line.
    """
    agent = _TwoGates()

    async def _run() -> list[dict[str, Any]]:
        with _served(agent) as served:
            async with httpx.AsyncClient(base_url=served.base, timeout=30) as client:
                session_id = await _shared_session(client, _BEN)
                bens = str((await client.post("/sessions", headers=_as(_BEN))).json()["session_id"])
                ana = asyncio.create_task(_post(client, _ANA, session_id, "hold ana"))
                await _until(lambda: _started(agent, 1))
                ben = asyncio.create_task(_post(client, _BEN, session_id, "ben asks"))
                await _until(lambda: _waiting(client, session_id, 1))
                own = asyncio.create_task(_post(client, _BEN, bens, "park ben"))
                await _until(lambda: _started(agent, 2))
                monkeypatch.setattr(settings, "service_max_concurrent_turns_per_actor", 1)
                _ran(agent, served, session_id)
                await ana
                status, events = await ben
                assert status == 200
                agent.park.set()
                await own
                return events

    events = asyncio.run(_run())
    assert events[-1]["type"] == "error", (events, agent.ran)
    assert events[-1]["code"] == "queue_cancelled", events
    assert "as many turns running" in events[-1]["message"], events[-1]
    assert [message for message, _actor, _roles in agent.ran] == ["hold ana", "park ben"]


def test_a_waiting_message_counts_against_its_senders_cap_elsewhere(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ben waiting in Ana's session is a turn he will run: with a cap of one, his next is 429.

    Before #503 the cap read running turns only, so a chemist could park a message in every
    shared session and have them all start, past the cap, the moment the lines moved.
    """
    monkeypatch.setattr(settings, "service_max_concurrent_turns_per_actor", 1)
    agent = _Ledger()

    async def _run() -> int:
        with _served(agent) as served:
            async with httpx.AsyncClient(base_url=served.base, timeout=30) as client:
                session_id = await _shared_session(client, _BEN)
                bens = str((await client.post("/sessions", headers=_as(_BEN))).json()["session_id"])
                ana = asyncio.create_task(_post(client, _ANA, session_id, "hold ana"))
                await _until(lambda: _started(agent, 1))
                ben = asyncio.create_task(_post(client, _BEN, session_id, "ben asks"))
                await _until(lambda: _waiting(client, session_id, 1))
                status, _events = await _post(client, _BEN, bens, "and another")
                _ran(agent, served, session_id)
                await ana
                await ben
                return status

    assert asyncio.run(_run()) == 429
    assert [message for message, _actor, _roles in agent.ran] == ["hold ana", "ben asks"]


def test_a_process_holding_its_most_waiters_refuses_the_next_429(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Waiters per process are bounded by what the socket budget charges for them.

    `service_max_concurrent_turns` × `service_turn_queue_max` is 1 here, so Cat is refused by the
    process before the session's own line is asked — a 429 with `Retry-After`, not the line's 409.
    """
    monkeypatch.setattr(settings, "service_max_concurrent_turns", 1)
    monkeypatch.setattr(settings, "service_turn_queue_max", 1)
    agent = _Ledger()

    async def _run() -> tuple[int, str | None, dict[tuple[str, str], int]]:
        with _served(agent) as served:
            async with httpx.AsyncClient(base_url=served.base, timeout=30) as client:
                session_id = await _shared_session(client, _BEN, _CAT)
                ana = asyncio.create_task(_post(client, _ANA, session_id, "hold ana"))
                await _until(lambda: _started(agent, 1))
                ben = asyncio.create_task(_post(client, _BEN, session_id, "ben asks"))
                await _until(lambda: _waiting(client, session_id, 1))
                refused = await client.post(
                    f"/sessions/{session_id}/messages",
                    json={"message": "cat asks"},
                    headers=_as(_CAT),
                )
                _ran(agent, served, session_id)
                await ana
                await ben
                await _until(lambda: _quiet(served))
                return (
                    refused.status_code,
                    refused.headers.get("Retry-After"),
                    dict(served.app.state.queue_waiters),
                )

    status, retry_after, left = asyncio.run(_run())
    assert status == 429 and retry_after, (status, retry_after)
    assert left == {}, f"a waiter's place outlived its message: {left}"


async def _quiet(served: _Served) -> bool:
    """Whether the process holds no waiter and no turn any more."""
    return not served.app.state.queue_waiters and not served.app.state.active_turns


def test_a_cut_off_watcher_still_counts_until_its_stream_closes() -> None:
    """Being cut off ends delivery, not the socket — so the cap counts the view until it closes.

    Before #503 a lagged reader left the count the moment the pump dropped it, so a participant
    could stall a view, have it cut off and open another, past `service_turn_max_watchers`.
    """

    async def _run() -> tuple[int, int, int]:
        turn = DetachableTurn(_burst(_QUEUE_SIZE + 50), session_id="s-cap")
        stalled = turn.watch()
        assert stalled is not None
        async for _event in turn.events():
            pass
        after_cut_off = turn.watchers
        stalled.close()
        after_close = turn.watchers
        stalled.close()  # idempotent
        return after_cut_off, after_close, turn.watchers

    after_cut_off, after_close, twice = asyncio.run(_run())
    assert after_cut_off == 1, "a cut-off view stopped counting while its stream was still open"
    assert (after_close, twice) == (0, 0)


def test_a_watch_takes_one_of_the_watchers_stream_slots_and_gives_it_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With one stream per user, Ben's watch is his one: a second is 429; it returns at the end."""
    monkeypatch.setattr(settings, "service_max_event_streams_per_user", 1)
    agent = _Ledger()

    async def _run() -> tuple[int, dict[str, int], dict[str, int]]:
        with _served(agent) as served:
            async with httpx.AsyncClient(base_url=served.base, timeout=30) as client:
                session_id = await _shared_session(client, _BEN)
                ana = asyncio.create_task(_post(client, _ANA, session_id, "hold ana"))
                await _until(lambda: _started(agent, 1))
                attached = asyncio.Event()
                ben = asyncio.create_task(_watch(client, _BEN, session_id, attached))
                await asyncio.wait_for(attached.wait(), 10)
                held = dict(served.app.state.event_streams)
                second = await client.get(f"/sessions/{session_id}/turn/stream", headers=_as(_BEN))
                _ran(agent, served, session_id)
                await ana
                await ben
                await _until(lambda: _no_streams(served))
                return second.status_code, held, dict(served.app.state.event_streams)

    status, held, after = asyncio.run(_run())
    assert held == {_BEN.oid: 1}, held
    assert status == 429
    assert after == {}


async def _no_streams(served: _Served) -> bool:
    """Whether every long-lived stream slot on the process has been returned."""
    return not served.app.state.event_streams


def test_a_member_removed_mid_turn_stops_receiving_it(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ben watches Ana's turn; Ana removes him; the answer reaches Ana and not Ben."""
    monkeypatch.setattr(settings, "service_turn_watch_recheck_seconds", 0.01)
    agent = _Ledger()

    async def _run() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        with _served(agent) as served:
            async with httpx.AsyncClient(base_url=served.base, timeout=30) as client:
                session_id = await _shared_session(client, _BEN)
                ana = asyncio.create_task(_post(client, _ANA, session_id, "hold ana"))
                await _until(lambda: _started(agent, 1))
                attached = asyncio.Event()
                ben = asyncio.create_task(_watch(client, _BEN, session_id, attached))
                await asyncio.wait_for(attached.wait(), 10)
                removed = await client.delete(
                    f"/sessions/{session_id}/members/{_BEN.oid}", headers=_as(_ANA)
                )
                assert removed.status_code == 204
                await asyncio.sleep(0.05)
                _ran(agent, served, session_id)
                status, sender = await ana
                assert status == 200
                return sender, await asyncio.wait_for(ben, 10)

    sender, watched = asyncio.run(_run())
    assert sender[-1]["type"] == "answer"
    assert not any(event["type"] == "answer" for event in watched), (
        "a member removed mid-turn was still sent its answer"
    )


# --- the line's store, both backends --------------------------------------------------------------


async def _exercise(queue: TurnQueue, session_id: str) -> None:
    """The contract both backends must honour, driven identically."""
    lease = 60.0
    first = await queue.enqueue(session_id, "ben", capacity=2, lease_seconds=lease)
    second = await queue.enqueue(session_id, "cat", capacity=2, lease_seconds=lease)
    assert first < second
    with pytest.raises(QueueRefused) as waiting:
        await queue.enqueue(session_id, "ben", capacity=5, lease_seconds=lease)
    assert waiting.value.reason == "waiting"
    with pytest.raises(QueueRefused) as full:
        await queue.enqueue(session_id, "dan", capacity=2, lease_seconds=lease)
    assert full.value.reason == "full"
    assert await queue.position(session_id, first, lease) == 0
    assert await queue.position(session_id, second, lease) == 1
    assert [entry.sender for entry in await queue.waiting(session_id)] == ["ben", "cat"]
    await queue.leave(session_id, first)
    await queue.leave(session_id, first)  # idempotent
    assert await queue.position(session_id, first, lease) is None
    assert await queue.position(session_id, second, lease) == 0
    # A lapsed place stops counting as ahead of anybody, and the next enqueue sweeps it.
    third = await queue.enqueue(session_id, "dan", capacity=5, lease_seconds=0.05)
    fourth = await queue.enqueue(session_id, "eve", capacity=5, lease_seconds=lease)
    await asyncio.sleep(0.2)
    assert await queue.position(session_id, fourth, lease) == 1  # only `second` is ahead
    assert await queue.position(session_id, third, lease) is None
    await queue.leave(session_id, second)
    await queue.leave(session_id, fourth)
    assert await queue.waiting(session_id) == []


async def test_the_in_process_line_honours_the_contract() -> None:
    """Order, both refusals, positions, leaving and lapsing — in process."""
    await _exercise(InMemoryTurnQueue(), "s-memory")


async def test_the_durable_line_honours_the_same_contract_and_goes_with_its_session() -> None:
    """The same contract over Postgres, then the cascade that ends a waiter when a session goes."""
    from chemclaw.agent.session_queue import SessionTurnQueue
    from chemclaw.agent.session_store import SessionOwnerStore
    from chemclaw.core import db
    from tests.pg import migrated_db_or_skip

    await migrated_db_or_skip()
    session_id = f"sess-line-{uuid.uuid4().hex[:8]}"
    await SessionOwnerStore().record(session_id, "ana-line")
    queue = SessionTurnQueue()
    await _exercise(queue, session_id)

    ticket = await queue.enqueue(session_id, "ben", capacity=2, lease_seconds=60)
    async with db.connection(settings.postgres_dsn) as conn:
        async with conn.cursor() as cur:
            await cur.execute("DELETE FROM session_owners WHERE session_id = %s", (session_id,))
        await conn.commit()
    assert await queue.position(session_id, ticket, 60) is None, "the line outlived its session"


# --- the inbox ------------------------------------------------------------------------------------


def test_the_inbox_lists_a_plan_the_caller_authored_in_a_session_they_are_only_a_member_of(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bob's own plan in Alice's session reaches Bob's inbox; Alice's plan there does not.

    And Alice's inbox does not list Bob's plan, since only its author may decide it.
    """
    monkeypatch.setattr(settings, "harness_enabled", True)
    monkeypatch.setattr(settings, "harness_autonomy", "plan_only")
    inbox = _Inbox(monkeypatch)
    inbox.add_session("s-bobs-plan", owner="alice", profile=None, plan=["bob's step"])
    inbox.add_session("s-alices-plan", owner="alice", profile=None, plan=["alice's step"])
    inbox.add_session("s-unattributed", owner="alice", profile=None, plan=["nobody's step"])
    for session_id, author in (("s-bobs-plan", "bob"), ("s-alices-plan", "alice")):
        lines = inbox.todos[session_id] or []
        asyncio.run(
            inbox.approvals.record_author(session_id, plan_identity(_steps(lines)) or "", author)
        )
        asyncio.run(session_member_store().add(session_id, "bob"))
    asyncio.run(session_member_store().add("s-unattributed", "bob"))

    alices = {row["session_id"] for row in inbox.get()["plans"]}
    inbox.app.dependency_overrides[require_principal] = lambda: _BOB
    bobs = inbox.get()

    assert alices == {"s-alices-plan", "s-unattributed"}, (
        "the owner's inbox listed a member's plan, or lost her own"
    )
    assert {row["session_id"] for row in bobs["plans"]} == {"s-bobs-plan"}, (
        "a member's inbox missed their own plan, or listed one only the owner may decide"
    )
    assert bobs["considered"] >= 3


def test_an_inbox_row_says_whose_conversation_the_plan_is_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`PendingPlan.owner` names the conversation's owner, on own rows and on a member's.

    Without it a client could open a shared session as its own and show owner controls. The
    in-process membership store is given the owner the durable store reads from `session_owners`.
    """
    monkeypatch.setattr(settings, "harness_enabled", True)
    monkeypatch.setattr(settings, "harness_autonomy", "plan_only")
    inbox = _Inbox(monkeypatch)
    inbox.add_session("s-alices-own", owner="alice", profile=None, plan=["alice's step"])
    inbox.add_session("s-bobs-plan", owner="alice", profile=None, plan=["bob's step"])
    lines = inbox.todos["s-bobs-plan"] or []
    asyncio.run(
        inbox.approvals.record_author("s-bobs-plan", plan_identity(_steps(lines)) or "", "bob")
    )
    store = session_member_store()
    asyncio.run(store.add("s-bobs-plan", "bob"))
    unowned = store.shared_with

    async def _with_owner(actor: str) -> list[members_module.SharedSession]:
        return [row._replace(owner="alice") for row in await unowned(actor)]

    monkeypatch.setattr(store, "shared_with", _with_owner)

    alices = {row["session_id"]: row["owner"] for row in inbox.get()["plans"]}
    inbox.app.dependency_overrides[require_principal] = lambda: _BOB
    bobs = {row["session_id"]: row["owner"] for row in inbox.get()["plans"]}

    assert alices == {"s-alices-own": "alice"}
    assert bobs == {"s-bobs-plan": "alice"}, "a member's row must name the session's owner"
