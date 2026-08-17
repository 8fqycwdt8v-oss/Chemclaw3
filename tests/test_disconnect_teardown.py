"""A client that walks away mid-turn frees its session immediately (D-130).

Driven through the **real ASGI contract** — a genuine `http.disconnect` message handed to the real
app — because that is precisely where this defect lived and why it survived a suite that already
had three tests about abandoned turns. sse-starlette answers `http.disconnect` by cancelling its
task group; it never calls `aclose()` on the body iterator. So the teardown runs inside a
*cancelled* task, where the first `await` raises before it does anything, and every existing test
closed the stream by hand instead — the one thing production never does.

Measured before the fix, on a live front door with Postgres: the durable release was entered on
every abandoned turn and completed on none, so the session answered 409 to its own owner for the
full 60-second lease. A chemist who closed a tab could not reopen the conversation for a minute.

What is pinned here:
  1. The durable turn claim is released — not merely *entered* — when the client disconnects.
  2. The session accepts its next turn straight away rather than 409ing until the lease expires.
"""

import asyncio
import json
from collections.abc import MutableMapping
from typing import Any

from chemclaw.api.app import create_app
from chemclaw.core.config import settings


class _RecordingClaims:
    """An in-memory `SessionTurns` that distinguishes an *entered* release from a finished one.

    `release` suspends on a real timer on purpose. The defect is invisible to a fake that never
    yields: a cancelled task runs synchronous code to the end and only raises at a suspension
    point, so a release with no `await` in it would "pass" against the broken code.
    """

    def __init__(self) -> None:
        self.held: dict[str, str] = {}
        self.entered = 0
        self.completed = 0

    async def claim(self, session_id: str, holder: str, lease_seconds: float) -> bool:
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

    `psycopg.errors.AdminShutdown` — the concrete error the chaos run produced — is a
    `psycopg.Error`, so it is deliberately *not* one of the connection errors the release used to
    catch. Standing in for it with a plain `RuntimeError` would test the old narrow tuple instead
    of the contract.
    """

    class Failure(Exception):
        """Neither a ConnectionError, an OSError, nor a RuntimeError — like the real one."""

    async def release(self, session_id: str, holder: str) -> None:
        """Fail after suspending, exactly where a dead database fails."""
        self.entered += 1
        await asyncio.sleep(0.05)
        raise self.Failure("terminating connection due to administrator command")


def test_a_release_that_cannot_reach_the_store_never_escapes_its_task() -> None:
    """Shielding makes the release *run*; it must not make a store failure a stray traceback.

    A shielded task whose awaiter has been cancelled is nobody's to await, so anything it raises
    surfaces only as an unattributed `Task exception was never retrieved`. Measured in chaos
    scenario C4 (Postgres stopped at the instant of the disconnect) before this was widened.
    """
    claims = _BrokenClaims()
    app = create_app(
        connector_factory=lambda _profile: [],
        turn_claims=claims,
    )
    stray: list[dict[str, Any]] = []

    async def _drive() -> None:
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

    asyncio.run(_drive())


def test_a_client_disconnect_releases_the_durable_turn_claim() -> None:
    """The claim is *released*, not merely entered, when the stream is torn down mid-turn.

    Counterfactual: without the `shield` in `_release_turn_claim`, `entered` is 1 and `completed`
    is 0 — the release starts, hits its first suspension point inside a cancelled task, and never
    reaches the store.
    """
    claims = _RecordingClaims()
    app = create_app(
        connector_factory=lambda _profile: [],
        turn_claims=claims,
    )

    async def _drive() -> None:
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

    asyncio.run(_drive())


def test_the_session_accepts_a_new_turn_immediately_after_a_disconnect() -> None:
    """The user-visible half: reopening a closed tab is not refused.

    Asserted separately from the claim bookkeeping because both guards can hold a 409 and only
    checking one of them is how the original diagnosis went wrong twice.
    """
    claims = _RecordingClaims()
    app = create_app(
        connector_factory=lambda _profile: [],
        turn_claims=claims,
    )

    async def _drive() -> None:
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

    asyncio.run(_drive())


async def _post_turn_and_vanish_before_first_byte(app: Any, session_id: str) -> None:
    """POST a turn whose client is gone before the response's first byte is ever accepted.

    The one teardown window neither `finally` covers: the route returns the streaming response
    (`handed_off=True`, so `post_message`'s own finally stands down), but the socket never accepts
    `http.response.start` — the send below blocks forever, exactly like a peer that vanished
    without closing cleanly — and the disconnect arrives first. sse-starlette's disconnect
    listener then cancels its task group while `_stream_response` is still suspended in that
    send, **before the first `__anext__`**, so the turn's async generator is never started and
    an unstarted generator runs no `finally` at all. Deterministic rather than raced: the send
    genuinely cannot complete, so cancellation can only land pre-iteration.
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


def test_a_client_gone_before_the_stream_starts_does_not_wedge_the_session(
    monkeypatch: Any,
) -> None:
    """The in-process turn guard is a lease: the one release-less window cannot 409 forever (A3).

    Both release sites are `finally` blocks — the generator's own, and `post_message`'s
    pre-handoff one — and a client gone after handoff but before the generator's first advance
    runs neither. With a bare set that entry was permanent: the session answered 409 for the
    pod's whole lifetime. With the deadline map the entry leaks identically (asserted below,
    proving the window is real) and then *expires*, so the session's next turn is admitted.

    Counterfactual: revert `active_turns` to a latch (claim without a deadline, or a membership
    test that ignores the deadline) and the final POST answers 409, not 200.
    """
    from chemclaw.core.config import settings

    monkeypatch.setattr(settings, "service_turn_timeout_seconds", 0.2)
    monkeypatch.setattr(settings, "service_turn_admission_timeout_seconds", 0.2)
    app = create_app(
        connector_factory=lambda _profile: [],
    )

    async def _drive() -> None:
        async with app.router.lifespan_context(app):
            _status, payload = await _request(app, "POST", "/sessions", {})
            session_id = json.loads(payload)["session_id"]
            await _post_turn_and_vanish_before_first_byte(app, session_id)

            # The leak is real: no finally ran, so nothing released the slot. (If this fails,
            # the reproduction no longer reproduces the window and the test proves nothing.)
            assert session_id in app.state.active_turns, "the generator ran a finally after all"

            # Within the lease the guard still guards: the entry is indistinguishable from a
            # live turn, so a duplicate submit is refused.
            status, _ = await _request(
                app, "POST", f"/sessions/{session_id}/messages", {"message": "again"}
            )
            assert status == 409

            # Past the lease (turn timeout + admission timeout), the entry is dead weight and
            # must not refuse the session's owner.
            await asyncio.sleep(0.5)
            status, _ = await _request(
                app, "POST", f"/sessions/{session_id}/messages", {"message": "recovered"}
            )
            assert status != 409, "the leaked in-process turn entry never expired (A3)"
            assert status == 200

    asyncio.run(_drive())


# --- the push-back event stream's per-user slot (same window, different resource) --------------


async def _open_event_stream_and_vanish_before_first_byte(app: Any, session_id: str) -> None:
    """Open `GET /sessions/{id}/events` for a client that is gone before the first byte lands.

    The event-stream twin of `_post_turn_and_vanish_before_first_byte`, and the same window:
    `session_events` hands off the streaming response, so its own pre-handoff `finally` stands
    down, while the send below never accepts `http.response.start`. sse-starlette's disconnect
    listener cancels the task group with `_stream_response` still suspended in that send —
    before the body iterator's first `__anext__` — so the generator that holds the slot's
    release in its `finally` is never started, and an unstarted async generator runs no
    `finally` at all.
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
    """A stream slot is held for as long as the *response* is served, not the generator (A3).

    Measured before the fix: five clients that vanish in this window leave
    `event_streams == {"oid-...": 5}` with nothing open, and the sixth honest connect is
    answered `429 too many concurrent event streams; close one and retry` — permanently, for
    that user on that pod, with nothing to close. It survived `gc.collect()`: a never-started
    generator has no `finally` to run and closing it is a no-op.

    Unlike the turn slot next door, this one cannot be a lease: a push-back stream is
    *deliberately* unbounded in lifetime (`stream_new_events` polls until the client leaves), so
    there is no deadline that expires a leak without also evicting a healthy long-lived stream's
    accounting. The release therefore moves to the one scope that ends exactly when the stream
    does — the response's own `__call__`.

    Counterfactual: release the slot only in the generator's `finally` (the shipped behaviour
    before this test) and the assertion below sees one leaked slot per vanished client.
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

    D-167 added approval-consumption to the end of a turn, and the obvious home for it — the
    `finally` — is wrong here. Production reaches teardown by *cancellation* rather than `aclose()`
    (D-130), and an `await` in that block re-raises the cancellation on the spot, skipping every
    step after it.

    **What that block holds has changed, and this docstring is corrected rather than left to rot.**
    It used to carry the five `reset_current_*` calls, so the failure it named was the next turn on
    the worker running under the disconnected user's identity. Those resets now live in
    `api/runner._turn_ambient`, a *synchronous* `@contextmanager` — an `await` cannot be spelled in
    its `finally` at all, and its `__exit__` runs while the cancellation propagates, so the identity
    guarantee is structural rather than dependent on this assertion. What is left here is
    `_book_turn_spend`, and the defect an `await` would reintroduce is a cancelled turn that books
    no tokens and no duration: abandon-and-retry becomes free, which is the cheapest attack on the
    runaway-cost guard.

    Asserted on the *source* rather than by driving a disconnect, because the failure is a property
    of the block: any future `await` added there reintroduces it, whatever that await happens to do.
    The property it used to stand in for is now asserted directly, by driving a real cancellation
    and reading the ambients back — `tests/test_turn_cancellation.py`'s
    `test_a_cancelled_turn_unstamps_every_ambient_it_stamped`.
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
