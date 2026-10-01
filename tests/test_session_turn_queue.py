"""A busy shared session is a line, every participant can watch a turn, and the inbox finds members.

`D-2026-10-01-a-queued-message-waits-in-its-senders-request`. Three properties, each driven at the
outermost thing production calls (`tasks/lessons.md` rule 55):

- **The line.** A message sent while another turn runs waits in the session's line, in order, and
  runs as *its own sender* — their oid and their roles, read from inside the running graph. Only the
  sender or the owner may withdraw it; a member removed while waiting does not have it run; a full
  line is a 409; a stranger can neither join it nor read it.
- **Fan-out.** Any participant can follow the running turn, each on a buffer of their own: a stalled
  watcher is cut off with `stream_lagged` without holding the turn or the sender, a watcher leaving
  never stops the turn, and nobody can watch a session they are not in.
- **The inbox.** `GET /plans/pending` lists a plan the caller authored in a session they are only a
  member of — and neither the owner's plan there nor a member's plan in the owner's inbox.

The HTTP cases run the app under a real uvicorn server on loopback (`tests.test_detach._Served`),
because "a second request while the first stream is open" cannot be expressed through `TestClient`
or httpx's ASGI transport, both of which buffer a response whole.
"""

import asyncio
import json
import threading
import time
import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any

import httpx
import pytest
from fastapi import Request

from chemclaw.agent import plan_approval_store as store_module
from chemclaw.agent import session_members as members_module
from chemclaw.agent.plan_gate import plan_identity
from chemclaw.agent.session_members import session_member_store
from chemclaw.agent.session_queue import InMemoryTurnQueue, QueueRefused, TurnQueue
from chemclaw.api.auth import Principal, require_principal
from chemclaw.api.detach import _QUEUE_SIZE, DetachableTurn
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


@pytest.fixture(autouse=True)
def _stores(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Identity enforced, in-process stores obtained through their real factories, a fast poll."""
    monkeypatch.setattr(settings, "session_store", "memory")
    monkeypatch.setattr(settings, "entra_required", True)
    monkeypatch.setattr(settings, "service_turn_queue_poll_seconds", 0.05)
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
        tail = [event async for event in stalled]
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
        watcher = turn.watch()
        assert watcher is not None
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

    And the mirror: Alice's inbox does not list Bob's plan in her own session, because only its
    author may decide it. Plans are distinguished by their steps, so each assertion names whose
    plan it saw.
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
