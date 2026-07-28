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

from agent_framework import AgentSession

from service.app import create_app


class _Update:
    """A minimal streamed update, shaped as `service.runner` duck-types it."""

    def __init__(self, text: str = "") -> None:
        self.text = text
        self.contents: list[object] = []
        self.user_input_requests: list[object] = []


class _StreamingAgent:
    """Streams one token, then never finishes — so only the disconnect ends the turn."""

    mcp_tools: list[Any] = []

    def create_session(self, *, session_id: str) -> AgentSession:
        """Build the turn's session object (the app calls this once per conversation)."""
        return AgentSession(session_id=session_id)

    def run(  # noqa: D102 - a fake agent's run, documented by its class
        self,
        message: str,
        *,
        stream: bool,
        session: AgentSession,
        **_run_options: Any,
    ) -> Any:
        async def _gen() -> Any:
            yield _Update(text="tok")
            while True:
                await asyncio.sleep(0.05)

        return _gen()


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

    async def refresh(self, session_id: str, holder: str, lease_seconds: float) -> None:
        """Extend the holder's claim (a no-op for an in-memory slot that cannot expire)."""

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
        agent_factory=lambda _profile: _StreamingAgent(),
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
        agent_factory=lambda _profile: _StreamingAgent(),
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
        agent_factory=lambda _profile: _StreamingAgent(),
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

            assert app.state.active_turns == set(), "the in-process turn slot leaked"
            status = await _post_turn_and_vanish(app, session_id)
            assert status != 409, "the session refused its owner's next turn after a disconnect"
            assert status == 200

    asyncio.run(_drive())
