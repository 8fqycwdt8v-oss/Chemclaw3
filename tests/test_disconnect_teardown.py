"""A client that walks away mid-turn frees its session immediately (D-130).

Driven through the real ASGI contract — a genuine `http.disconnect` to the real app — because
sse-starlette answers it by cancelling its task group, never `aclose()`, so teardown runs inside a
cancelled task where the first `await` raises. Pinned:

1. The durable turn claim is released — not merely *entered* — on disconnect.
2. The session accepts its next turn immediately rather than 409ing until the lease expires.
"""

import asyncio
import contextlib
import json
from collections.abc import MutableMapping
from typing import Any

import pytest
from starlette.requests import Request

from chemclaw.agent.session_store import SessionOwnerStore
from chemclaw.api.app import create_app
from chemclaw.api.auth import Principal
from chemclaw.api.routes import sessions
from chemclaw.api.state import LiveSession
from chemclaw.core.config import settings


class _RecordingClaims:
    """An in-memory `SessionTurns` that distinguishes an *entered* release from a finished one.

    `release` suspends on a real timer: a cancelled task only raises at a suspension point, so a
    fake that never yields would pass against broken code.
    """

    def __init__(self) -> None:
        self.held: dict[str, str] = {}
        self.entered = 0
        self.completed = 0

    async def claim(
        self, session_id: str, holder: str, lease_seconds: float, *, actor: str | None = None
    ) -> bool:
        """Take the slot unless someone already holds it."""
        if session_id in self.held:
            return False
        self.held[session_id] = holder
        return True

    async def refresh(self, session_id: str, holder: str, lease_seconds: float) -> bool:
        """Extend the claim (an in-memory slot cannot expire, so it stays ours)."""
        return True

    async def release(self, session_id: str, holder: str) -> None:
        """Give the slot back, with a suspension point standing in for the DELETE's round trip."""
        self.entered += 1
        await asyncio.sleep(0.05)
        if self.held.get(session_id) == holder:
            del self.held[session_id]
        self.completed += 1


def _scope(method: str, path: str) -> dict[str, Any]:
    """A minimal ASGI HTTP scope for one request against the front door."""
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.1"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [(b"host", b"testserver"), (b"content-type", b"application/json")],
        "client": ("127.0.0.1", 1234),
        "server": ("testserver", 80),
    }


async def _request(app: Any, method: str, path: str, payload: dict[str, Any]) -> tuple[int, bytes]:
    """Drive one ordinary request to completion and return its status and body."""
    body = json.dumps(payload).encode()
    sent: list[MutableMapping[str, Any]] = []
    delivered = False

    async def _receive() -> MutableMapping[str, Any]:
        nonlocal delivered
        if not delivered:
            delivered = True
            return {"type": "http.request", "body": body, "more_body": False}
        return {"type": "http.disconnect"}

    async def _send(message: MutableMapping[str, Any]) -> None:
        sent.append(message)

    await app(_scope(method, path), _receive, _send)
    status = next(m["status"] for m in sent if m["type"] == "http.response.start")
    chunks = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return int(status), chunks


async def _post_turn_and_vanish(app: Any, session_id: str) -> int:
    """POST a turn, then send `http.disconnect` the moment the first token reaches the wire.

    This is the exact message uvicorn delivers when a browser tab closes mid-stream, and handing
    it to the app unmodified is the whole point: no test client can express it.
    """
    body = json.dumps({"message": "hello"}).encode()
    gone = asyncio.Event()
    delivered = False
    status = 0

    async def _receive() -> MutableMapping[str, Any]:
        nonlocal delivered
        if not delivered:
            delivered = True
            return {"type": "http.request", "body": body, "more_body": False}
        await gone.wait()
        return {"type": "http.disconnect"}

    async def _send(message: MutableMapping[str, Any]) -> None:
        nonlocal status
        if message["type"] == "http.response.start":
            status = int(message["status"])
        elif message["type"] == "http.response.body" and b"token" in message.get("body", b""):
            gone.set()

    await app(_scope("POST", f"/sessions/{session_id}/messages"), _receive, _send)
    return status


class _BrokenClaims(_RecordingClaims):
    """A store whose `release` fails the way a stopped Postgres does.

    `psycopg.errors.AdminShutdown` is a `psycopg.Error`, not a connection error, so a plain
    `RuntimeError` stand-in would test a narrower contract.
    """

    class Failure(Exception):
        """Neither a ConnectionError, an OSError, nor a RuntimeError — like the real one."""

    async def release(self, session_id: str, holder: str) -> None:
        """Fail after suspending, exactly where a dead database fails."""
        self.entered += 1
        await asyncio.sleep(0.05)
        raise self.Failure("terminating connection due to administrator command")


async def test_a_release_that_cannot_reach_the_store_never_escapes_its_task() -> None:
    """A release that cannot reach the store never escapes its task.

    A shielded task whose awaiter was cancelled is nobody's to await, so anything it raised would
    surface only as an unattributed `Task exception was never retrieved`.
    """
    claims = _BrokenClaims()
    app = create_app(
        connector_factory=lambda _profile: [],
        turn_claims=claims,
    )
    stray: list[dict[str, Any]] = []

    asyncio.get_running_loop().set_exception_handler(lambda _loop, ctx: stray.append(ctx))
    async with app.router.lifespan_context(app):
        _status, payload = await _request(app, "POST", "/sessions", {})
        session_id = json.loads(payload)["session_id"]
        await _post_turn_and_vanish(app, session_id)
        for _ in range(100):
            if claims.entered:
                break
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.2)

        assert claims.entered == 1, "the release never ran"
        assert stray == [], f"the failed release escaped as a loop-level error: {stray}"
        # And the lease is what covers the session, which is the documented contract.
        assert claims.held != {}, "the claim was somehow cleared by a release that failed"


async def test_a_client_disconnect_releases_the_durable_turn_claim() -> None:
    """The claim is *released*, not merely entered, when the stream is torn down mid-turn.

    Without the `shield` in `_release_turn_claim` the release would start, hit its first suspension
    point in a cancelled task, and never reach the store.
    """
    claims = _RecordingClaims()
    app = create_app(
        connector_factory=lambda _profile: [],
        turn_claims=claims,
    )

    async with app.router.lifespan_context(app):
        _status, payload = await _request(app, "POST", "/sessions", {})
        session_id = json.loads(payload)["session_id"]
        await _post_turn_and_vanish(app, session_id)
        # The shielded release is a task of its own, so it lands just after the request that
        # started it returns. Waiting on the recorder rather than sleeping a fixed amount
        # keeps the test from encoding a timing guess.
        for _ in range(100):
            if claims.completed:
                break
            await asyncio.sleep(0.01)

        assert claims.entered == 1, "the turn never even tried to release its claim"
        assert claims.completed == 1, (
            "the release was entered but never finished — the session stays 409 until the "
            "lease expires"
        )
        assert claims.held == {}, f"the claim outlived the turn: {claims.held}"


async def test_the_session_accepts_a_new_turn_immediately_after_a_disconnect() -> None:
    """The user-visible half: reopening a closed tab is not refused.

    Asserted separately from the claim bookkeeping because both guards can hold a 409 and only
    checking one of them is how the original diagnosis went wrong twice.
    """
    claims = _RecordingClaims()
    app = create_app(
        connector_factory=lambda _profile: [],
        turn_claims=claims,
    )

    async with app.router.lifespan_context(app):
        _status, payload = await _request(app, "POST", "/sessions", {})
        session_id = json.loads(payload)["session_id"]
        await _post_turn_and_vanish(app, session_id)
        for _ in range(100):
            if claims.completed:
                break
            await asyncio.sleep(0.01)

        assert app.state.active_turns == {}, "the in-process turn slot leaked"
        status = await _post_turn_and_vanish(app, session_id)
        assert status != 409, "the session refused its owner's next turn after a disconnect"
        assert status == 200


async def _post_turn_and_vanish_before_first_byte(app: Any, session_id: str) -> None:
    """POST a turn whose client is gone before the response's first byte is ever accepted.

    The route has handed off (so `post_message`'s finally stands down), but the send of
    `http.response.start` blocks forever and the disconnect arrives first, so sse-starlette cancels
    before the generator's first `__anext__` and no `finally` runs. Deterministic: the send cannot
    complete, so cancellation can only land pre-iteration.
    """
    body = json.dumps({"message": "hello"}).encode()
    delivered = False

    async def _receive() -> MutableMapping[str, Any]:
        nonlocal delivered
        if not delivered:
            delivered = True
            return {"type": "http.request", "body": body, "more_body": False}
        return {"type": "http.disconnect"}

    async def _send(message: MutableMapping[str, Any]) -> None:
        if message["type"] == "http.response.start":
            await asyncio.Event().wait()  # the wire never accepts the first byte

    await app(_scope("POST", f"/sessions/{session_id}/messages"), _receive, _send)


async def test_a_client_gone_before_the_stream_starts_does_not_wedge_the_session(
    monkeypatch: Any,
) -> None:
    """A client gone before the stream starts does not wedge the session.

    In that window neither release `finally` runs, so the in-process turn guard is a lease: the
    entry leaks (asserted, proving the window is real) and then *expires*, admitting the next turn.
    A latch without a deadline would 409 for the pod's lifetime.
    """
    from chemclaw.core.config import settings

    monkeypatch.setattr(settings, "service_turn_timeout_seconds", 0.2)
    monkeypatch.setattr(settings, "service_turn_admission_timeout_seconds", 0.2)
    app = create_app(
        connector_factory=lambda _profile: [],
    )

    async with app.router.lifespan_context(app):
        _status, payload = await _request(app, "POST", "/sessions", {})
        session_id = json.loads(payload)["session_id"]
        await _post_turn_and_vanish_before_first_byte(app, session_id)

        # The leak is real: no finally ran, so nothing released the slot. (If this fails,
        # the reproduction no longer reproduces the window and the test proves nothing.)
        assert session_id in app.state.active_turns, "the generator ran a finally after all"

        leaked = app.state.active_turns[session_id].token

        # Within the lease the guard still guards: the entry is indistinguishable from a
        # live turn, so a duplicate submit does not run — it waits in the session's line
        # (`D-2026-10-01-a-queued-message-waits-in-its-senders-request`).
        status, _ = await _request(
            app, "POST", f"/sessions/{session_id}/messages", {"message": "again"}
        )
        assert status == 200
        assert await app.state.turn_queue.waiting(session_id), "the duplicate did not wait"
        assert app.state.active_turns[session_id].token == leaked, "it ran beside the leak"

        # Past the lease (turn timeout + admission timeout), the entry is dead weight and
        # must not hold the session's owner: the waiting message takes the turn itself.
        async with asyncio.timeout(10):
            while await app.state.turn_queue.waiting(session_id):
                await asyncio.sleep(0.05)
        lease = app.state.active_turns.get(session_id)
        assert lease is None or lease.token != leaked, (
            "the leaked in-process turn entry never expired (A3)"
        )


# --- the push-back event stream's per-user slot (same window, different resource) --------------


async def _open_event_stream_and_vanish_before_first_byte(app: Any, session_id: str) -> None:
    """Open `GET /sessions/{id}/events` for a client that is gone before the first byte lands.

    The event-stream twin of `_post_turn_and_vanish_before_first_byte`: cancellation lands before
    the body iterator's first `__anext__`, so the generator holding the slot's release never starts.
    """
    delivered = False

    async def _receive() -> MutableMapping[str, Any]:
        nonlocal delivered
        if not delivered:
            delivered = True
            return {"type": "http.request", "body": b"", "more_body": False}
        return {"type": "http.disconnect"}

    async def _send(message: MutableMapping[str, Any]) -> None:
        if message["type"] == "http.response.start":
            await asyncio.Event().wait()  # the wire never accepts the first byte

    await app(_scope("GET", f"/sessions/{session_id}/events"), _receive, _send)


def test_a_client_gone_before_the_event_stream_starts_frees_its_per_user_slot(
    monkeypatch: Any,
) -> None:
    """A stream slot is held for as long as the *response* is served, not the generator.

    Otherwise each vanished client leaks a per-user slot until the user is refused with 429 forever
    on that pod. It cannot be a lease, because a push-back stream is deliberately unbounded in
    lifetime, so the release lives in the response's own `__call__`, the scope that ends with the
    stream.
    """
    from chemclaw.api import app as front_door

    async def _never_yields(_session_id: str, **_kwargs: Any) -> Any:
        """A tailer with nothing to deliver — what a live stream does almost all the time."""
        await asyncio.Event().wait()
        yield None  # pragma: no cover - unreachable, and that is the point

    monkeypatch.setattr(front_door, "stream_new_events", _never_yields)
    app = create_app(
        connector_factory=lambda _profile: [],
    )

    async def _drive() -> None:
        async with app.router.lifespan_context(app):
            _status, payload = await _request(app, "POST", "/sessions", {})
            session_id = json.loads(payload)["session_id"]

            for _ in range(settings.service_max_event_streams_per_user):
                await _open_event_stream_and_vanish_before_first_byte(app, session_id)
                assert app.state.event_streams == {}, (
                    f"an abandoned event stream kept its per-user slot: {app.state.event_streams}"
                )

            # The user-visible half: an honest client must still be admitted afterwards. Driven
            # to a real 200 rather than inferred from the ledger, because the ledger is exactly
            # what the defect corrupted.
            started: list[int] = []

            async def _receive() -> MutableMapping[str, Any]:
                return {"type": "http.disconnect"}

            async def _send(message: MutableMapping[str, Any]) -> None:
                if message["type"] == "http.response.start":
                    started.append(int(message["status"]))

            await app(_scope("GET", f"/sessions/{session_id}/events"), _receive, _send)
            assert started == [200], (
                f"an honest reconnect was refused after abandoned streams: {started}"
            )

    asyncio.run(_drive())


# --- the plan gate must not put an `await` in the teardown path (review of D-167) --------------


def test_a_disconnected_turn_still_resets_every_ambient_context_var() -> None:
    """`run_turn`'s `finally` must stay synchronous, or a disconnect skips the turn's spend ledger.

    Teardown arrives by cancellation (D-130), and an `await` in that block re-raises on the spot,
    skipping `_book_turn_spend`, which would make abandon-and-retry free. Asserted on the source,
    because any future `await` there reintroduces it. The ambient resets live in the synchronous
    `api/runner._turn_ambient`, driven by
    `tests/test_turn_cancellation.py::test_a_cancelled_turn_unstamps_every_ambient_it_stamped`.
    """
    import ast
    from pathlib import Path

    source = (
        Path(__file__).resolve().parents[1] / "src" / "chemclaw" / "api" / "runner.py"
    ).read_text()
    run_turn = next(
        node
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "run_turn"
    )
    finalizer = next(
        node for node in ast.walk(run_turn) if isinstance(node, ast.Try) and node.finalbody
    )
    awaits = [n for stmt in finalizer.finalbody for n in ast.walk(stmt) if isinstance(n, ast.Await)]
    assert not awaits, (
        f"run_turn's finally block awaits ({len(awaits)} found); on the cancellation path that "
        "skips the context-var resets below it and leaks the turn's ambient identity"
    )


# --- the slot two routes hold across an awaited release ---------------------------------------


class _ParkingClaims(_RecordingClaims):
    """A claim store whose `release` parks until the test lets it go.

    Needed to deliver a *second* cancellation while the shielded durable release is in flight.
    """

    def __init__(self) -> None:
        """Start with nothing held and nobody inside `release`."""
        super().__init__()
        self.inside = asyncio.Event()
        self.let_go = asyncio.Event()

    async def release(self, session_id: str, holder: str) -> None:
        """Announce that the release started, then wait for the test before finishing it."""
        self.entered += 1
        self.inside.set()
        await self.let_go.wait()
        self.held.pop(session_id, None)
        self.completed += 1


class _ParkingOwners(SessionOwnerStore):
    """The durable registry, with the one call `delete_session` awaits parked forever.

    A subclass of the real store because the route reaches this method only behind
    `isinstance(owners, SessionOwnerStore)`. Nothing here opens a connection.
    """

    def __init__(self, started: asyncio.Event) -> None:
        """Bind the real store's DSN, and hold the flag saying the route reached this call."""
        super().__init__()
        self._started = started

    async def delete_session(self, session_id: str) -> dict[str, int]:
        """Park inside the route's durable work, holding the session's turn slot."""
        self._started.set()
        await asyncio.sleep(3600)
        raise AssertionError("unreachable")  # pragma: no cover


def _slot_after_a_recancelled_route(
    monkeypatch: "pytest.MonkeyPatch", *, route_name: str
) -> dict[str, Any]:
    """Cancel one of the two slot-holding routes twice, and hand back the leftover slot map.

    The first cancellation runs the route's `finally` into the shielded durable release; the second
    lands while that release is parked, skipping whatever the block had not reached. The in-process
    slot is claimed with `deadline=math.inf`, so if it is the one skipped nothing ever sweeps it.
    """
    claims = _ParkingClaims()
    started = asyncio.Event()

    async def _never_returns(*_args: Any, **_kwargs: Any) -> str:
        """Stand in for the durable work the fork route awaits while holding the slot."""
        started.set()
        await asyncio.sleep(3600)
        raise AssertionError("unreachable")  # pragma: no cover

    monkeypatch.setattr(sessions, "fork_session", _never_returns)
    route = getattr(sessions, route_name)

    async def _drive() -> dict[str, Any]:
        app = create_app(owner_store=_ParkingOwners(started), turn_claims=claims)
        request = Request(_scope("POST", "/sessions/s-recancel") | {"app": app})
        principal = Principal(oid="alice", upn="alice@corp", roles=frozenset())
        # `delete_session` takes the session gate as a route dependency rather than as a
        # parameter (it needs the check, not the handle), so the two signatures differ by one.
        live = LiveSession(session=object(), owner="alice", profile=None)
        args: tuple[Any, ...] = (request, "s-recancel", principal)
        if route_name == "fork_session_route":
            args += (live,)
        task = asyncio.create_task(route(*args))
        await started.wait()
        task.cancel()
        await claims.inside.wait()  # the finally ran and is parked in the shielded release
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        claims.let_go.set()
        await asyncio.sleep(0)
        slots: dict[str, Any] = app.state.active_turns
        return slots

    return asyncio.run(_drive())


@pytest.mark.parametrize("route_name", ["fork_session_route", "delete_session"])
def test_a_recancelled_route_still_hands_back_the_sessions_turn_slot(
    monkeypatch: "pytest.MonkeyPatch", route_name: str
) -> None:
    """The in-process slot must come back even when the awaited release does not.

    Both routes claim it with `deadline=math.inf`, so the `finally` is the only release; it must
    come before the `await`, since a re-cancel inside the await would skip it and 409 the owner for
    the life of the pod. The durable half expires on its own lease, so losing it is survivable.
    """
    slots = _slot_after_a_recancelled_route(monkeypatch, route_name=route_name)

    assert slots == {}, (
        f"{route_name} left {slots} behind after a re-cancel; an infinite-deadline slot nothing "
        "sweeps 409s the session's own owner for the life of the pod"
    )
