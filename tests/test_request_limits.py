"""What one authenticated caller could do to the front door, and what one upload could.

**Nothing bounded requests.** Two admission controls existed and both were scoped to the expensive
path: the concurrency cap bounds turns in flight, the budget guard (D-144) meters tokens. So a
caller holding both at zero could still drive `GET /proposals`, `GET /jobs`, `GET /schedules` and
`POST /sessions` as fast as the network allowed — every one of which does real work against Temporal
or Postgres. A loop with no LLM call in it was free.

**The upload cap was in the wrong place**, and the mistake is easy to make because the check did
exist.
`parse_attachment` refuses anything over `attachment_max_bytes`. But it runs in the route handler,
and by then Starlette's multipart parser has already consumed the whole body into a spooled temp
file — RAM to 1 MB, then the pod's ephemeral disk. A 5 GB upload was ingested in full and *then*
refused. The cap described what the parser would accept, never what the process would ingest.
"""

import asyncio
import threading

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
    """The finding: an authenticated caller had no request budget at all.

    Driven with an injected clock rather than `sleep`, because a rate limiter tested by sleeping is
    a rate limiter tested at exactly one rate — and the slow assertions are the ones that get
    deleted later.
    """
    limiter = _limiter(burst=2.0)
    limiter.check("chemist", now=100.0)
    limiter.check("chemist", now=100.0)
    with pytest.raises(RateLimited):
        limiter.check("chemist", now=100.0)


def test_the_bucket_refills_continuously_rather_than_at_a_window_edge() -> None:
    """Why a bucket and not a fixed window.

    A fixed window lets a caller spend a whole allowance in its last millisecond and the next in its
    first, so the observed peak is twice the configured rate at the moment a system can least absorb
    it. A bucket has no edge to align to: at 60/min one token is back one second later, and not two.
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
    """An idle caller returns to `burst`, not to an unbounded credit.

    Without the clamp, a caller who waited an hour would accumulate an hour's tokens and could spend
    them all at once — which is precisely the spike the limiter exists to prevent, arrived at from
    the other direction.
    """
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
    """A map keyed by caller identity, with an attacker-influenced key.

    Minting tokens for many `oid`s is exactly the way around a per-principal limit, so the limiter
    would be the thing that fails first — this codebase has fixed unbounded identity-keyed maps
    three times, most recently for metric label series (D-152). Eviction costs the evicted caller
    one free burst and costs the process nothing.
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
    """End to end through the real dependency, because the wiring is the claim.

    The limit is spent inside `require_principal` so it covers every authenticated route and cannot
    be forgotten by a new one; that is only true if the dependency really calls it. `Retry-After` so
    a client backs off by the right amount instead of guessing.
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
    """A throttled probe reads as a down pod, and a throttled scrape as a down target.

    `/healthz`, `/readyz` and `/metrics` do not depend on `require_principal`, which is what keeps
    them out — asserted rather than assumed, because moving the gate to a middleware or an app-level
    dependency (both tempting) would silently catch them.
    """
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
    """413 from the declared `Content-Length`, without the body being consumed.

    This is the layer `parse_attachment` could not be: by the time a route handler runs, the
    multipart parser has already written the whole upload to a spooled temp file. The refusal has
    to happen above the app or it happens after the cost.
    """
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

    def _chunks() -> object:
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
    """The body limit bounds the whole multipart, not the file inside it.

    Set equal to `attachment_max_bytes` it would refuse a file *at* the documented attachment size,
    because the boundaries and part headers push the body over — a limit that makes the neighbouring
    documented limit unreachable.
    """
    from chemclaw.core.config import settings

    assert settings.service_max_request_bytes > settings.attachment_max_bytes


def test_a_declared_oversize_body_is_refused_without_reading_a_byte() -> None:
    """The `Content-Length` check is not a duplicate of the counting path — it is the cheap one.

    The counting path alone already refuses the request, so this looked redundant and a mutation
    that deleted it passed every other test here. What it buys is that a client announcing a 5 GB
    upload is turned away *before* the transfer, rather than after `service_max_request_bytes` of it
    has crossed the network and been parsed. Asserted by driving the middleware directly with a
    sentinel app, because in-process test transport cannot show the difference at the HTTP level.
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

    async def _exercise() -> None:
        scope = {
            "type": "http",
            "headers": [(b"content-length", b"999999999")],
        }
        await BodySizeLimit(_app, max_bytes=1024)(scope, _receive, _send)  # type: ignore[arg-type]

    asyncio.run(_exercise())

    assert not reached, "the app ran for a request already known to be too large"
    assert sent[0]["status"] == 413


# --- parsing an upload is work, and work on the event loop is an outage -------------------------


class _SlowParse:
    """Stands in for a hostile document: real blocking work, released only when the test says so.

    `threading.Event().wait()` rather than a sleep, because the thing under test is *when* a slot
    comes back, and a sleep would make that a race against a duration instead of a fact. It blocks
    a real thread, exactly like the CPU-bound library call it replaces — a fake that awaited would
    prove nothing, since an await is precisely what the defect lacked.
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


def test_a_slow_upload_does_not_stall_every_other_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The finding: `upload_attachment` is `async def` and parsed inline, on one uvicorn worker.

    Size is not cost. A decompression bomb or a hostile font map well inside `attachment_max_bytes`
    holds a CPU for tens of seconds (measured: 33.8 s for a 201 KB PDF on the previously locked
    pypdf), and every session, SSE stream and health probe on the pod waited for it —
    `service_max_concurrent_turns` meters turns, `BodySizeLimit` meters bytes, and neither meters
    parse cost.

    Counterfactual, measured: call `parse_attachment` inline in the route again and this test's
    probe cannot even be *reached* until the parse has finished — the assertion that the upload is
    still in flight is what discriminates, and it fails. A latency bound alone would not have: with
    the loop blocked, the probe still answers quickly once it finally runs.
    """
    parse = _SlowParse()
    monkeypatch.setattr(attachments, "parse_attachment", parse)

    async def _drive() -> None:
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

    asyncio.run(_drive())


def test_uploads_past_the_parse_cap_are_shed_rather_than_queued(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A burst of hostile uploads must not pile threads into the pool that validates tokens.

    Queuing *threads* would move the outage one layer out: `chemclaw.api.auth` validates every
    bearer token through `asyncio.to_thread`, so uploads that hog the default executor stall
    authentication for everyone. Shed with a retryable 503 instead — the same answer the turn
    admission gives.

    **The queue window is zero here so this test asks one half of the policy.** The policy has two
    halves and they pull against each other: shedding at the cap with no wait at all fails the
    ordinary case (four spreadsheets dropped on the UI at once came back as two 200s and two 503s),
    so the parse gate waits a bounded time before shedding. Removing the wait isolates what must
    hold under *sustained* load — that the wait ends in a shed rather than in an unbounded queue.
    The burst half is `test_a_burst_inside_the_queue_window_is_served_rather_than_shed` below, and
    neither test is meaningful without the other.
    """
    from chemclaw.core.config import settings

    parse = _SlowParse()
    monkeypatch.setattr(attachments, "parse_attachment", parse)
    monkeypatch.setattr(settings, "attachment_max_concurrent_parses", 1)
    monkeypatch.setattr(settings, "attachment_parse_queue_seconds", 0)

    async def _drive() -> None:
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

    asyncio.run(_drive())


def test_a_burst_inside_the_queue_window_is_served_rather_than_shed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ordinary case the bare cap got wrong: several files dropped on the UI at once.

    An unremarkable 482 KB spreadsheet takes about 1.3 s to parse, so with a cap of two, four
    simultaneous uploads measured as `[200, 200, 503, 503]` — a chemist selecting four files got
    two hard failures out of a system that was working normally. Shedding is the right answer to
    sustained overload and the wrong one to a burst, and a clock is what tells them apart.

    Every upload is released together, so the claim is that the last two *waited* rather than were
    refused: with no queue they could not have been.
    """
    from chemclaw.core.config import settings

    parse = _SlowParse()
    monkeypatch.setattr(attachments, "parse_attachment", parse)
    monkeypatch.setattr(settings, "attachment_max_concurrent_parses", 2)
    monkeypatch.setattr(settings, "attachment_parse_queue_seconds", 10)

    async def _drive() -> None:
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

    asyncio.run(_drive())


def test_a_shed_upload_is_counted(monkeypatch: pytest.MonkeyPatch) -> None:
    """Shedding is the cap working, and it was invisible from outside the pod until now.

    Without a counter an operator cannot distinguish a replica refusing every upload from one
    nobody is uploading to — the lesson `chemclaw_turns_shed_total` already exists for, applied to
    the other resource. Asserted as a delta rather than an absolute, so the test does not depend on
    what else in the session incremented it.
    """
    from chemclaw.core.config import settings
    from chemclaw.core.metrics import METRICS

    parse = _SlowParse()
    monkeypatch.setattr(attachments, "parse_attachment", parse)
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


def test_a_worker_thread_that_never_starts_gives_its_slot_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A slot stands for a running thread, so a thread that never started must not hold one.

    Between claiming the slot and attaching `give_back` to the future there was no guard: if
    `loop.run_in_executor` itself raised — the default executor shut down during pod drain, a loop
    closing under a cancelled request — the slot was taken and nothing would ever give it back.
    `_ParseSlots` is a module singleton with no reset, so the loss is permanent and process-wide.

    Measured on the unguarded code with a cap of 2: two raises took `in_flight` from 0 to 2, and
    every subsequent upload on that replica was answered with a retryable 503 reading "2 uploads
    are already being parsed on this replica" — false, and the exact opposite of the observability
    the shed counter was added to give the operator.

    The assertion is on the counter rather than on the status code because that is the durable
    damage: the request that triggered it fails either way, and what matters is the replica after.
    """
    from chemclaw.core.config import settings

    monkeypatch.setattr(settings, "attachment_max_concurrent_parses", 2)
    monkeypatch.setattr(settings, "attachment_parse_queue_seconds", 10.0)

    def _executor_is_gone(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("cannot schedule new futures after shutdown")

    async def _drive() -> None:
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

    asyncio.run(_drive())


def test_a_parse_past_its_timeout_is_refused_and_keeps_its_slot_until_the_thread_ends(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two halves of one contract, and the second is what makes the cap true.

    The client stops waiting after `attachment_parse_timeout_seconds` and is told so (422 — the
    file is unreadable *here*, and sending it again would do the same thing). But Python cannot
    kill the thread, so the slot must stay taken until that thread actually ends: releasing it when
    the request gives up would let one attacker hold every CPU while the counter reads zero.
    """
    from chemclaw.core.config import settings

    parse = _SlowParse()
    monkeypatch.setattr(attachments, "parse_attachment", parse)
    monkeypatch.setattr(settings, "attachment_parse_timeout_seconds", 0.2)

    async def _drive() -> None:
        app = create_app()
        async with asgi_client(app) as client:
            session_id = (await client.post("/sessions")).json()["session_id"]
            refused = await _upload(client, session_id)
            assert refused.status_code == 422
            assert "0.2s" in refused.json()["detail"]

            # The thread is still running, so the slot it stands for is still taken.
            assert attachments._PARSE_SLOTS.in_flight == 1

            parse.release.set()
            async with asyncio.timeout(5):
                while attachments._PARSE_SLOTS.in_flight:
                    await asyncio.sleep(0.01)

    asyncio.run(_drive())
