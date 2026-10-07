"""Per-caller request limits and the request body cap at the front door.

The turn cap and the token budget cover only the expensive path, so cheap routes need a
per-principal request budget. The upload cap must sit above the app: by the time a route runs,
the multipart parser has already consumed the body.
"""

import asyncio
import threading
from collections.abc import Iterable

import httpx
import pytest
from fastapi.testclient import TestClient

from chemclaw.agent import attachments
from chemclaw.agent.attachments import Attachment
from chemclaw.agent.session import TurnSession
from chemclaw.api.app import create_app
from chemclaw.api.rate_limit import RateLimited, RequestLimiter, reset_limiter
from tests.fakes import asgi_client


class _SessionOnlyAgent:
    """Just enough agent for `POST /sessions` to succeed, which is all the body tests need."""

    def __init__(self) -> None:
        """No tools: the middleware under test never reaches one."""
        self.mcp_tools: list[object] = []

    def create_session(self, *, session_id: str) -> TurnSession:
        """Hand back a session, so a 413 is the middleware's doing and not a missing method."""
        return TurnSession(session_id=session_id)


def _app_with_sessions() -> TestClient:
    """A client whose `POST /sessions` really works, so a 413 is the middleware's doing."""
    return TestClient(create_app())


@pytest.fixture(autouse=True)
def _fresh_limiter() -> None:
    """The limiter is process-wide, so a test must not inherit another's buckets."""
    reset_limiter()


def _limiter(
    *, per_minute: float = 60.0, burst: float = 2.0, principals: int = 8
) -> RequestLimiter:
    """A limiter with small numbers, so the boundary is where the assertions can see it."""
    return RequestLimiter(per_minute=per_minute, burst=burst, max_principals=principals)


def test_a_caller_may_burst_and_then_must_wait() -> None:
    """A caller may burst and then must wait, driven with an injected clock rather than `sleep`."""
    limiter = _limiter(burst=2.0)
    limiter.check("chemist", now=100.0)
    limiter.check("chemist", now=100.0)
    with pytest.raises(RateLimited):
        limiter.check("chemist", now=100.0)


def test_the_bucket_refills_continuously_rather_than_at_a_window_edge() -> None:
    """The bucket refills continuously rather than at a window edge.

    A fixed window allows twice the rate across a boundary; a bucket has no edge to align to.
    """
    limiter = _limiter(per_minute=60.0, burst=2.0)
    limiter.check("chemist", now=0.0)
    limiter.check("chemist", now=0.0)
    with pytest.raises(RateLimited):
        limiter.check("chemist", now=0.0)

    limiter.check("chemist", now=1.0)  # exactly one token has refilled
    with pytest.raises(RateLimited):
        limiter.check("chemist", now=1.0)


def test_the_refill_never_exceeds_the_burst() -> None:
    """An idle caller returns to `burst`, not to an unbounded credit."""
    limiter = _limiter(per_minute=60.0, burst=2.0)
    limiter.check("chemist", now=0.0)
    for spent in range(2):
        limiter.check("chemist", now=3600.0 + spent)
    with pytest.raises(RateLimited):
        limiter.check("chemist", now=3600.0)


def test_one_callers_budget_is_not_anothers() -> None:
    """Per principal.

    A shared bucket would be a global limit with extra steps: one busy script would refuse everyone
    else, which is the outage the limiter is supposed to prevent.
    """
    limiter = _limiter(burst=1.0)
    limiter.check("first", now=0.0)
    limiter.check("second", now=0.0)
    with pytest.raises(RateLimited):
        limiter.check("first", now=0.0)


def test_the_bucket_map_cannot_grow_without_bound() -> None:
    """The bucket map, keyed by caller identity, cannot grow without bound.

    Eviction costs the evicted caller one free burst and costs the process nothing.
    """
    limiter = _limiter(principals=3)
    for index in range(50):
        limiter.check(f"principal-{index}", now=float(index))
    assert len(limiter._buckets) == 3


def test_eviction_drops_the_least_recently_seen_not_the_busiest() -> None:
    """LRU order, so a steady caller is not evicted by a flood and handed a fresh burst."""
    limiter = _limiter(principals=2, burst=1.0)
    limiter.check("steady", now=0.0)
    limiter.check("other", now=1.0)
    limiter.check("steady", now=2.0)  # refreshes `steady`, making `other` the oldest
    limiter.check("newcomer", now=3.0)
    assert "steady" in limiter._buckets and "other" not in limiter._buckets


def test_a_limited_request_is_a_429_carrying_how_long_to_wait(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A limited request is a 429 with `Retry-After`, through the real dependency.

    The limit is spent in `require_principal`, so every authenticated route is covered.
    """
    monkeypatch.setattr("chemclaw.core.config.settings.service_rate_limit_per_minute", 60.0)
    monkeypatch.setattr("chemclaw.core.config.settings.service_rate_limit_burst", 1.0)
    reset_limiter()

    with TestClient(create_app()) as client:
        assert client.get("/profiles").status_code == 200
        refused = client.get("/profiles")

    assert refused.status_code == 429
    assert int(refused.headers["Retry-After"]) >= 1


def test_the_probes_are_never_limited(monkeypatch: pytest.MonkeyPatch) -> None:
    """The probes and `/metrics` are never limited; a throttled probe reads as a down pod."""
    monkeypatch.setattr("chemclaw.core.config.settings.service_rate_limit_per_minute", 60.0)
    monkeypatch.setattr("chemclaw.core.config.settings.service_rate_limit_burst", 1.0)
    reset_limiter()

    with TestClient(create_app()) as client:
        for _ in range(10):
            assert client.get("/healthz").status_code == 200
            assert client.get("/metrics").status_code == 200


def test_the_limiter_is_off_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """0 in code, on in the chart — the shape `budget_enabled` already uses (REV-16).

    A CLI, a test and a single-user dev run have no reason to be throttled, and a limiter that fires
    there is one people switch off everywhere.
    """
    from chemclaw.core.config import settings

    assert settings.service_rate_limit_per_minute == 0.0
    with TestClient(create_app()) as client:
        assert all(client.get("/profiles").status_code == 200 for _ in range(50))


# --- the request body ceiling -----------------------------------------------------------------


def test_an_oversized_body_is_refused_before_anything_reads_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An oversized body is refused with 413 from `Content-Length`, before anything reads it."""
    monkeypatch.setattr("chemclaw.core.config.settings.service_max_request_bytes", 1024)

    with _app_with_sessions() as client:
        response = client.post("/sessions", content=b"x" * 4096)

    assert response.status_code == 413
    assert "limit" in response.json()["detail"]


def test_an_undeclared_body_is_still_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    """A client that simply omits `Content-Length` must not walk past the ceiling.

    Checking only the header would be a bound on honest clients, which is not a bound. The chunked
    path counts bytes as they arrive and stops the moment it crosses.
    """
    monkeypatch.setattr("chemclaw.core.config.settings.service_max_request_bytes", 1024)

    def _chunks() -> Iterable[bytes]:
        for _ in range(10):
            yield b"x" * 512

    with _app_with_sessions() as client:
        response = client.post("/sessions", content=_chunks())

    assert response.status_code == 413


def test_an_ordinary_request_passes_through_untouched(monkeypatch: pytest.MonkeyPatch) -> None:
    """The bound must be invisible below it.

    A middleware that mangles ordinary traffic is worse than no middleware, so the passing case is
    asserted as explicitly as the refusing one.
    """
    monkeypatch.setattr("chemclaw.core.config.settings.service_max_request_bytes", 1_000_000)

    with _app_with_sessions() as client:
        response = client.post("/sessions", json={"profile": None})

    assert response.status_code == 200


def test_the_ceiling_leaves_room_for_the_envelope_around_an_attachment() -> None:
    """The body limit leaves room for the multipart envelope around a maximum-size attachment."""
    from chemclaw.core.config import settings

    assert settings.service_max_request_bytes > settings.attachment_max_bytes


async def test_a_declared_oversize_body_is_refused_without_reading_a_byte() -> None:
    """A declared oversize body is refused without reading a byte.

    The counting path would refuse it too, but only after the transfer; driven at the middleware
    with a sentinel app because in-process transport cannot show the difference.
    """
    from chemclaw.core.asgi import BodySizeLimit

    reached = False

    async def _app(_scope: object, _receive: object, _send: object) -> None:
        nonlocal reached
        reached = True

    sent: list[dict[str, object]] = []

    async def _send(message: dict[str, object]) -> None:
        sent.append(message)

    async def _receive() -> dict[str, object]:  # pragma: no cover - must never be awaited
        raise AssertionError("the body was read despite a declared size over the limit")

    scope = {
        "type": "http",
        "headers": [(b"content-length", b"999999999")],
    }
    await BodySizeLimit(_app, max_bytes=1024)(scope, _receive, _send)  # type: ignore[arg-type]

    assert not reached, "the app ran for a request already known to be too large"
    assert sent[0]["status"] == 413


# --- parsing an upload is work, and work on the event loop is an outage -------------------------


class _SlowParse:
    """Stands in for a hostile document: real blocking work, released only when the test says so.

    It blocks a real thread on an `Event`, so when the slot returns is a fact rather than a race.
    """

    def __init__(self) -> None:
        """Start blocked, with nothing parsed yet."""
        self.release = threading.Event()
        self.started = threading.Event()
        self.calls = 0

    def __call__(self, name: str, raw: bytes, declared_type: str | None = None) -> Attachment:
        """Block until released, then return a plausible parse of the upload."""
        self.calls += 1
        self.started.set()
        self.release.wait(timeout=10)
        return Attachment(name=name, content_type="text/csv", text="a,b", rows=1)


async def _upload(client: httpx.AsyncClient, session_id: str) -> httpx.Response:
    """POST one small CSV to a session's attachment route."""
    return await client.post(
        f"/sessions/{session_id}/attachments",
        files={"file": ("runs.csv", b"a,b\n1,2\n", "text/csv")},
    )


async def test_a_slow_upload_does_not_stall_every_other_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A slow upload does not stall every other request on the worker.

    Parse cost is not bounded by size, so parsing runs off the event loop. The discriminating
    assertion is that the probe answers while the upload is still in flight.
    """
    parse = _SlowParse()
    monkeypatch.setattr(attachments, "parse_attachment_isolated", parse)

    app = create_app()
    async with asgi_client(app) as client:
        session_id = (await client.post("/sessions")).json()["session_id"]
        upload = asyncio.create_task(_upload(client, session_id))
        await asyncio.to_thread(parse.started.wait, 5)

        # The pod is mid-parse. A liveness probe now decides whether the container is killed.
        async with asyncio.timeout(2):
            probe = await client.get("/healthz")
        assert probe.status_code == 200
        assert not upload.done(), (
            "the probe answered only because the parse had already finished — this run does "
            "not exercise the window at all"
        )

        parse.release.set()
        assert (await upload).status_code == 200


async def test_uploads_past_the_parse_cap_are_shed_rather_than_queued(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Uploads past the parse cap are shed with a retryable 503 rather than queued.

    Queued threads would stall token validation, which shares the default executor. The queue
    window is zero here to test sustained load; the burst half is the next test.
    """
    from chemclaw.core.config import settings

    parse = _SlowParse()
    monkeypatch.setattr(attachments, "parse_attachment_isolated", parse)
    monkeypatch.setattr(settings, "attachment_max_concurrent_parses", 1)
    monkeypatch.setattr(settings, "attachment_parse_queue_seconds", 0)

    app = create_app()
    async with asgi_client(app) as client:
        session_id = (await client.post("/sessions")).json()["session_id"]
        first = asyncio.create_task(_upload(client, session_id))
        await asyncio.to_thread(parse.started.wait, 5)

        shed = [(await _upload(client, session_id)).status_code for _ in range(3)]
        assert shed == [503, 503, 503], shed
        assert parse.calls == 1, "a shed upload was parsed anyway"

        parse.release.set()
        assert (await first).status_code == 200


async def test_a_burst_inside_the_queue_window_is_served_rather_than_shed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A burst inside the queue window is served rather than shed.

    All uploads are released together, so the last ones must have waited rather than been refused.
    """
    from chemclaw.core.config import settings

    parse = _SlowParse()
    monkeypatch.setattr(attachments, "parse_attachment_isolated", parse)
    monkeypatch.setattr(settings, "attachment_max_concurrent_parses", 2)
    monkeypatch.setattr(settings, "attachment_parse_queue_seconds", 10)

    app = create_app()
    async with asgi_client(app) as client:
        session_id = (await client.post("/sessions")).json()["session_id"]
        uploads = [asyncio.create_task(_upload(client, session_id)) for _ in range(4)]
        await asyncio.to_thread(parse.started.wait, 5)
        parse.release.set()
        codes = sorted(response.status_code for response in await asyncio.gather(*uploads))
        assert codes == [200, 200, 200, 200], codes
        assert parse.calls == 4, "an upload was answered without being parsed"
        assert attachments._PARSE_SLOTS.in_flight == 0, "a queued upload kept its slot"


def test_a_shed_upload_is_counted(monkeypatch: pytest.MonkeyPatch) -> None:
    """A shed upload is counted, asserted as a delta."""
    from chemclaw.core.config import settings
    from chemclaw.core.metrics import METRICS

    parse = _SlowParse()
    monkeypatch.setattr(attachments, "parse_attachment_isolated", parse)
    monkeypatch.setattr(settings, "attachment_max_concurrent_parses", 1)
    monkeypatch.setattr(settings, "attachment_parse_queue_seconds", 0)

    async def _drive() -> float:
        app = create_app()
        async with asgi_client(app) as client:
            session_id = (await client.post("/sessions")).json()["session_id"]
            before = METRICS.value("chemclaw_attachment_parses_shed_total")
            first = asyncio.create_task(_upload(client, session_id))
            await asyncio.to_thread(parse.started.wait, 5)
            assert (await _upload(client, session_id)).status_code == 503
            parse.release.set()
            await first
            return METRICS.value("chemclaw_attachment_parses_shed_total") - before

    assert asyncio.run(_drive()) == 1


async def test_a_worker_thread_that_never_starts_gives_its_slot_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A worker thread that never starts gives its slot back.

    If `run_in_executor` raises, the module-wide slot would otherwise be lost for the life of the
    process. Asserted on the slot counter, since that is the lasting damage.
    """
    from chemclaw.core.config import settings

    monkeypatch.setattr(settings, "attachment_max_concurrent_parses", 2)
    monkeypatch.setattr(settings, "attachment_parse_queue_seconds", 10.0)

    def _executor_is_gone(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("cannot schedule new futures after shutdown")

    assert attachments._PARSE_SLOTS.in_flight == 0, "a previous test leaked a slot"
    loop = asyncio.get_running_loop()
    monkeypatch.setattr(loop, "run_in_executor", _executor_is_gone)
    for _ in range(2):
        with pytest.raises(RuntimeError):
            await attachments.parse_attachment_off_loop("a.txt", b"hello")
        assert attachments._PARSE_SLOTS.in_flight == 0, (
            "the slot for a worker thread that never started was never returned"
        )

    # And the replica still parses: the leak's real cost is every upload after it.
    monkeypatch.undo()
    parsed = await attachments.parse_attachment_off_loop("b.txt", b"hello")
    assert parsed.text == "hello"


async def test_a_parse_past_its_timeout_is_refused_to_its_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A parse past its timeout is refused to its client with 422 naming the budget.

    The fake parse blocks on an `Event` and cannot be killed, so this test covers only the
    bookkeeping: the slot is released when the worker thread ends.
    `tests/test_parse_isolation.py` drives freeing the slot from a real slow parse.
    """
    from chemclaw.core.config import settings

    parse = _SlowParse()
    monkeypatch.setattr(attachments, "parse_attachment_isolated", parse)
    monkeypatch.setattr(settings, "attachment_parse_timeout_seconds", 0.2)
    # No grace: the thread's own deadline is what normally fires, and this fake has none, so the
    # caller's backstop is the control under test and must not sit through five spare seconds.
    monkeypatch.setattr(settings, "attachment_parse_reap_grace_seconds", 0.0)

    app = create_app()
    async with asgi_client(app) as client:
        session_id = (await client.post("/sessions")).json()["session_id"]
        refused = await _upload(client, session_id)
        assert refused.status_code == 422
        assert "0.2s" in refused.json()["detail"]

        parse.release.set()
        async with asyncio.timeout(5):
            while attachments._PARSE_SLOTS.in_flight:
                await asyncio.sleep(0.01)
