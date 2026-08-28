"""What the front door records about a request, and what a turn records about itself.

Every assertion here is about an *observation* rather than about an answer: the access log line,
the RED metrics, the correlation id on the wire, the refusals that used to be silent, and the turn
record `turn_costs.completed` collapsed into a boolean. The reason they are worth pinning is the
same in every case — each of these was measured absent before it was written, so the failure mode
is not "wrong value" but "nothing at all", which no test asserting on a response body can see.

Driven through the real app (`tests.test_service._app`) rather than by calling the middleware
directly, because the two defects this closes were both about *where* something sits in a stack: a
route template is readable only once the router below has run, and a 500 carries the security
headers only if the frame that answers it is inside the one that stamps them.
"""

import asyncio
import logging
from collections.abc import AsyncIterator
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.datastructures import Headers

from chemclaw.agent.turn_usage import TurnUsage
from chemclaw.api.detach import DetachableTurn
from chemclaw.api.events import (
    JobStartedEvent,
    TokenEvent,
    ToolCallEvent,
    ToolFailedEvent,
)
from chemclaw.api.middleware import _CORRELATION_ID, _UNMATCHED_ROUTE, _request_correlation_id
from chemclaw.api.runner import _OUTCOMES, _deadline_passed, _settle_outcome, _TurnLedger
from chemclaw.core.config import settings
from chemclaw.core.metrics import METRICS
from tests.test_service import _app, _FakeAgent

_HEADER = "X-Chemclaw-Correlation-Id"


def _field(record: logging.LogRecord, name: str) -> Any:
    """One `extra=` field off a captured record — `getattr`, because a `LogRecord` has no schema."""
    return getattr(record, name)


@pytest.fixture
def client() -> TestClient:
    """The front door with a fake agent — the same seam every other front-door test uses."""
    return TestClient(_app(_FakeAgent()))


# --------------------------------------------------------------------------------------------
# P4 — the end-of-stream marker that could be dropped, and the reader that then never returned.
# --------------------------------------------------------------------------------------------


async def _burst(count: int) -> AsyncIterator[dict[str, str]]:
    """`count` events with no awaits between them — a token-streamed answer, at full speed."""
    for index in range(count):
        yield {"event": "token", "data": str(index)}


async def _drain(turn: DetachableTurn, *, behind_every: int) -> int:
    """Read the turn to its end as a reader that falls momentarily behind; count what arrived."""
    seen = 0
    async for _event in turn.events():
        seen += 1
        if seen % behind_every == 0:
            # One loop tick with nothing consumed, which is what lets the bounded queue fill and
            # the pump park on `await put` — the state the lost `_DONE` was created in.
            await asyncio.sleep(0)
    return seen


@pytest.mark.parametrize("count", [256, 257, 512, 513])
def test_a_turn_that_fills_the_queue_still_ends_its_stream(count: int) -> None:
    """The reader terminates even when the queue was full at the moment `_DONE` was offered.

    `_pump` blocks on `await put` once the queue fills, so its last blocking put returns with the
    queue full again — and the `finally` one line later offers `_DONE` through `put_nowait`, which
    `_attached_or_discard` drops. Before `_next_event` the reader then awaited a marker that did
    not exist, with the pump task already finished and the queue drained: measured as a permanent
    hang at 256 and 512 events (and at 257/513 with a differently-timed reader — the trigger is the
    queue's state at the last put, not one arithmetic residue). Nothing sends on such a connection,
    so the SSE send timeout never fires and the ping keeps succeeding; it holds a slot against
    `--limit-concurrency` for the pod's lifetime.

    Bounded by `asyncio.wait_for` rather than by the suite's global timeout, so a regression fails
    as this assertion rather than as a run that never finishes.
    """

    async def _run() -> int:
        turn = DetachableTurn(_burst(count), session_id="s")
        return await asyncio.wait_for(_drain(turn, behind_every=64), timeout=5.0)

    assert asyncio.run(_run()) == count


def test_the_pump_delivers_every_event_before_the_stream_ends() -> None:
    """Ending on the pump's *state* must not truncate: everything queued is drained first.

    The fix races `queue.get()` against the pump task, so the failure mode it could have
    introduced is the opposite of the one it closes — noticing the task finished and returning
    while events are still queued. A reader that never yields to the loop until the pump is long
    done is the sharpest form of that case.
    """

    async def _run() -> int:
        turn = DetachableTurn(_burst(100), session_id="s")
        await asyncio.sleep(0.05)  # let the pump finish entirely before the first read
        seen = 0
        async for _event in turn.events():
            seen += 1
        return seen

    assert asyncio.run(_run()) == 100


# --------------------------------------------------------------------------------------------
# P1/P2 — the access log, the RED metrics, and the correlation id.
# --------------------------------------------------------------------------------------------


def test_a_served_request_emits_one_access_record_with_the_route_template(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    """One `http.request` record per request, carrying the template and not the raw path.

    The raw path is attacker-controlled: it is a metric-cardinality bomb and a redaction cost (a
    115 KB request line stalled a pod for 21 s with the logging lock held). The template is bounded
    by the route table, which is a source constant.
    """
    with caplog.at_level(logging.INFO, logger="chemclaw.api.middleware"):
        caplog.clear()
        assert client.get("/healthz").status_code == 200
    records = [r for r in caplog.records if getattr(r, "event", "") == "http.request"]
    assert len(records) == 1, [r.getMessage() for r in caplog.records]
    record = records[0]
    assert _field(record, "route") == "/healthz"
    assert _field(record, "method") == "GET"
    assert _field(record, "status") == 200
    assert _field(record, "duration_ms") >= 0
    assert _field(record, "correlation_id")


def test_the_access_record_names_a_session_route_by_its_template(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    """A path parameter must not reach the label: two sessions are one series, not two."""
    created = client.post("/sessions", json={}).json()["session_id"]
    with caplog.at_level(logging.INFO, logger="chemclaw.api.middleware"):
        # The POST above emits its own access record; clear so `next` below cannot pick it up.
        caplog.clear()
        client.get(f"/sessions/{created}/messages")
    record = next(r for r in caplog.records if getattr(r, "event", "") == "http.request")
    assert _field(record, "route") == "/sessions/{session_id}/messages"
    assert created not in _field(record, "route")
    # The routed session id rides as a *field*, which is where an unbounded value belongs.
    assert _field(record, "session_id") == created


def test_an_unmatched_path_collapses_onto_one_route_label(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    """Whatever a caller invents is one series. This is the cardinality bound, stated as a test."""
    with caplog.at_level(logging.INFO, logger="chemclaw.api.middleware"):
        caplog.clear()
        client.get("/no-such-route-a")
        client.get("/no-such-route-b")
    routes = {
        _field(r, "route") for r in caplog.records if getattr(r, "event", "") == "http.request"
    }
    assert routes == {_UNMATCHED_ROUTE}


def test_requests_and_duration_are_counted_by_route_and_status_class(client: TestClient) -> None:
    """The two RED series, which did not exist in any form: no HTTP metric of any kind."""
    before = METRICS.value("chemclaw_http_requests_total")
    seen, _total = METRICS.observations("chemclaw_http_request_duration_seconds")
    client.get("/healthz")
    assert METRICS.value("chemclaw_http_requests_total") == before + 1
    assert METRICS.observations("chemclaw_http_request_duration_seconds")[0] == seen + 1
    rendered = METRICS.render()
    assert 'chemclaw_http_requests_total{route="/healthz",status_class="2xx"}' in rendered


def test_the_route_status_series_stay_inside_the_registry_cap() -> None:
    """The label set is bounded by the route table, and the bound is *checked* rather than assumed.

    Past `core/metrics._MAX_SERIES_PER_COUNTER` a new series is refused, counted on
    `chemclaw_metric_series_dropped_total` and said once — loud, but the metric undercounts from
    then on. `route` is the FastAPI template plus one fixed `<unmatched>`, so nothing a caller sends
    can grow this; only a new route can.

    **Measured, across 158 front-door tests: 35 series, and no route produced more than three
    status classes** (`2xx`, `4xx`, `5xx` — no route in this app redirects, and 1xx never reaches
    an ASGI `http.response.start`). Three per route is therefore the honest worst case, and the
    arithmetic is asserted against the constant rather than written out in prose: this docstring
    used to spell the cap and the margin as numbers, and both went stale in the same commit that
    raised the cap — it still said the margin was one route when the constant had doubled. The
    route that would make the counter start dropping series turns this red, and the fix is to raise
    the cap in `core/metrics.py`, not to loosen this test.
    """
    from fastapi.routing import APIRoute

    from chemclaw.core.metrics import _MAX_SERIES_PER_COUNTER

    labels = len({route.path for route in _app(_FakeAgent()).routes if isinstance(route, APIRoute)})
    labels += 1  # `<unmatched>`
    assert labels * 3 <= _MAX_SERIES_PER_COUNTER, (
        f"{labels} route labels x 3 status classes exceeds the {_MAX_SERIES_PER_COUNTER}-series "
        "cap, so chemclaw_http_requests_total will start dropping series; raise "
        "_MAX_SERIES_PER_COUNTER in core/metrics.py"
    )


def test_every_response_carries_a_correlation_id(client: TestClient) -> None:
    """22 of 23 routes gave a chemist nothing to quote in a bug report; only the SSE turn did."""
    for path in ("/healthz", "/readyz", "/metrics", "/no-such-route"):
        response = client.get(path)
        assert _CORRELATION_ID.match(response.headers[_HEADER]), (path, response.headers)


def test_a_well_formed_inbound_correlation_id_is_adopted(client: TestClient) -> None:
    """The UI's or the ingress's id survives into this pod, so one click is one trace."""
    mine = "a1b2c3d4e5f60718"
    assert client.get("/healthz", headers={_HEADER: mine}).headers[_HEADER] == mine


@pytest.mark.parametrize("inbound", ["", "short", "has spaces", "x" * 200, "semi;colon"])
def test_a_malformed_inbound_correlation_id_is_replaced_not_sanitised(inbound: str) -> None:
    """The id reaches log lines, `audit_events` and a response header — all three or none."""
    minted = _request_correlation_id(Headers({"X-Chemclaw-Correlation-Id": inbound}))
    assert minted != inbound
    assert _CORRELATION_ID.match(minted)


# --------------------------------------------------------------------------------------------
# P3 — the 422 that emitted zero log records.
# --------------------------------------------------------------------------------------------


def test_a_validation_failure_is_logged_and_counted(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    """Measured before this handler: a 422 produced **zero** log records and moved no metric.

    So a client looping on a malformed body was indistinguishable from silence.
    """
    before = METRICS.value("chemclaw_request_validation_failures_total")
    with caplog.at_level(logging.WARNING, logger="chemclaw.api.middleware"):
        caplog.clear()
        response = client.post("/sessions", json={"profile": ["not", "a", "string"]})
    assert response.status_code == 422
    assert METRICS.value("chemclaw_request_validation_failures_total") == before + 1
    record = next(r for r in caplog.records if getattr(r, "event", "") == "http.validation_failed")
    assert _field(record, "route") == "/sessions"
    assert _field(record, "error_count") >= 1
    assert record.levelno == logging.WARNING


# --------------------------------------------------------------------------------------------
# P6 — the 500 that bypassed the security headers and carried no id.
# --------------------------------------------------------------------------------------------


def _exploding_app() -> FastAPI:
    """The real front door plus one route that raises, to reach the unhandled-error path."""
    app = _app(_FakeAgent())

    @app.get("/boom")
    async def _boom() -> dict[str, str]:  # pragma: no cover - the body never returns
        raise RuntimeError("a DSN and a driver error live in here")

    # In front of the dev chat UI's `Mount("/")`, which `create_app` registers last and which
    # matches *every* path — a route appended after it is never reached.
    app.router.routes.insert(0, app.router.routes.pop())
    return app


def test_an_unhandled_error_answers_json_with_the_correlation_id_and_the_headers(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Starlette's default 500 is served *above* every user middleware, so it had neither.

    Both halves matter: a chemist gets an id to quote, and the browser security headers are on the
    one response that used to lack them. The exception detail stays server-side, in a record that
    carries the same id.
    """
    client = TestClient(_exploding_app(), raise_server_exceptions=False)
    with caplog.at_level(logging.ERROR, logger="chemclaw.api.middleware"):
        caplog.clear()
        response = client.get("/boom")
    assert response.status_code == 500
    body = response.json()
    assert body["correlation_id"] == response.headers[_HEADER]
    assert "internal error" in body["detail"]
    assert "DSN" not in body["detail"] and "driver" not in body["detail"]
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["Content-Security-Policy"]
    logged = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert logged and logged[0].exc_info is not None


def test_the_500_is_counted_under_its_route_and_the_5xx_class() -> None:
    """A route that throws must be findable by `status_class`, which is the point of the label."""
    client = TestClient(_exploding_app(), raise_server_exceptions=False)
    client.get("/boom")
    assert 'chemclaw_http_requests_total{route="/boom",status_class="5xx"}' in METRICS.render()


# --------------------------------------------------------------------------------------------
# P5 — the authorization refusals that were silent by construction.
# --------------------------------------------------------------------------------------------


def test_an_unknown_session_records_the_refusal_it_will_not_disclose(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    """404-not-403 is right, and it makes the server-side record the *only* surviving distinction.

    Without it a session-id enumeration scan is indistinguishable from ordinary 404 traffic — on
    the one surface where that difference matters.
    """
    before = METRICS.value("chemclaw_authz_refusals_total")
    with caplog.at_level(logging.WARNING, logger="chemclaw.api.deps"):
        caplog.clear()
        response = client.get("/sessions/00000000000000000000000000000000/messages")
    assert response.status_code == 404
    assert response.json()["detail"] == "unknown session"
    assert METRICS.value("chemclaw_authz_refusals_total") == before + 1
    record = next(r for r in caplog.records if getattr(r, "event", "") == "authz.refused")
    assert _field(record, "resource") == "session"
    assert _field(record, "reason")
    assert record.levelno == logging.WARNING


# --------------------------------------------------------------------------------------------
# P11/P17 — the refusals that happen before, and above, any route.
# --------------------------------------------------------------------------------------------


def test_a_missing_bearer_token_is_counted_apart_from_an_invalid_one(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The invalid-token path logged; the missing-header path did neither, which hid a whole class.

    "A client is misconfigured and sending no Authorization header at all" and "a healthy service
    nobody is failing against" produced identical evidence, and only the first is something an
    operator can fix. The reasons are a closed three-value set, so the counter is alertable as a
    *rate* while the line stays at INFO — an unauthenticated probe of a public endpoint is ordinary
    internet traffic.
    """
    monkeypatch.setattr(settings, "entra_required", True)
    before = METRICS.value("chemclaw_auth_failures_total")
    # Deliberately not `with TestClient(...)`: entering it runs the app lifespan, whose
    # `configure_logging()` replaces the root handlers and takes `caplog`'s with them. No route
    # here needs the lifespan — a 401 is decided in a dependency.
    client = TestClient(_app(_FakeAgent()))
    with caplog.at_level(logging.INFO, logger="chemclaw.api.auth"):
        caplog.clear()
        assert client.get("/sessions").status_code == 401
        assert (
            client.get("/sessions", headers={"Authorization": "Bearer not.a.jwt"}).status_code
            == 401
        )
    assert METRICS.value("chemclaw_auth_failures_total") == before + 2
    rendered = METRICS.render()
    assert 'chemclaw_auth_failures_total{reason="missing"}' in rendered
    assert 'chemclaw_auth_failures_total{reason="invalid"}' in rendered
    assert any("no bearer token" in record.getMessage() for record in caplog.records)


def test_an_oversized_body_leaves_a_log_line_as_well_as_a_count(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """`BodySizeLimit` answers *above* the access log, so its 413 appeared in no line anywhere.

    An operator watching `chemclaw_requests_too_large_total` rise had a rate and nothing to look
    at. The line names the limit and deliberately not the path — a request line is
    attacker-controlled, and the redaction filter is where that becomes a pod stall.
    """
    monkeypatch.setattr(settings, "service_max_request_bytes", 512)
    before = METRICS.value("chemclaw_requests_too_large_total")
    with caplog.at_level(logging.WARNING, logger="chemclaw.core.asgi"):
        caplog.clear()
        client = TestClient(_app(_FakeAgent()))
        response = client.post(
            "/sessions", content=b"x" * 4000, headers={"Content-Type": "application/json"}
        )
    assert response.status_code == 413
    assert METRICS.value("chemclaw_requests_too_large_total") == before + 1
    assert any("512 byte limit" in record.getMessage() for record in caplog.records)


# --------------------------------------------------------------------------------------------
# P8 — the stream gauges.
# --------------------------------------------------------------------------------------------


def test_the_push_back_stream_capacity_is_published(client: TestClient) -> None:
    """Only *rejections* were counted, so "are we near the cap" was unanswerable until hit."""
    rendered = METRICS.render()
    assert "chemclaw_event_streams_open" in rendered
    assert "chemclaw_event_stream_capacity" in rendered


# --------------------------------------------------------------------------------------------
# A5/D2 — the turn record.
# --------------------------------------------------------------------------------------------


def _ledger(**fields: Any) -> _TurnLedger:
    """A ledger in one of the states a real turn leaves behind."""
    ledger = _TurnLedger(correlation_id="c" * 32, usage=TurnUsage())
    for name, value in fields.items():
        setattr(ledger, name, value)
    return ledger


def test_every_outcome_is_reachable_and_none_is_invented() -> None:
    """The enum is closed and `_settle_outcome` is its only producer.

    A value nothing can write is not an outcome (D-2026-08-26), and a value the settle function can
    produce but the enum does not name is a series no dashboard will ever query. Both directions.
    """

    async def _produced() -> set[str]:
        # Still on a loop, though `timed_out` no longer reads the clock here: the flag is sampled
        # in `run_turn`'s `except` clause at the instant the cancellation lands, because settling
        # runs after the rollback and a Stop at `deadline − ε` behind a slow teardown used to
        # cross the deadline while being torn down. `tests/test_api_review_turn_record.py` is where
        # that instant is pinned; this asserts only that the enum is closed in both directions.
        return {
            _settle_outcome(_ledger(answered=True, answer_parts=["ok"])),
            _settle_outcome(_ledger(answer_parts=["partial"], loop_capped=True, answered=True)),
            _settle_outcome(_ledger(answered=False)),
            _settle_outcome(_ledger(error_code="storage_unavailable")),
            _settle_outcome(_ledger(cancelled=True, timed_out=True)),
            _settle_outcome(_ledger(cancelled=True)),
        }

    assert asyncio.run(_produced()) == set(_OUTCOMES)


def test_the_turn_record_separates_a_tool_failure_from_a_governance_refusal() -> None:
    """A refused call is the control working; a failed call is a step that broke.

    Folding them together reports a correctly-gated turn as a broken one — the exact mistake
    `ToolFailedEvent.reason` was added to prevent, and the reason the ledger gets two columns
    rather than one. The classification is not re-derived here: `reason` is set once, from the
    exception *class*, by `agent/plan_gate.plan_gate_failure_reason`.
    """
    ledger = _ledger()
    for event in (
        ToolCallEvent(tool="predict_pka", arguments="{}"),
        ToolCallEvent(tool="propose_note", arguments="{}"),
        ToolFailedEvent(tool="predict_pka", message="boom"),
        ToolFailedEvent(tool="propose_note", message="refused", reason="plan_gate"),
        JobStartedEvent(job_id="j-1", kind="calc"),
        TokenEvent(text="hello"),
    ):
        ledger.note_event(event)
    assert (ledger.tool_calls, ledger.tool_failures, ledger.tool_refusals) == (2, 1, 1)
    assert ledger.jobs_started == 1
    assert ledger.ttft_seconds is not None and ledger.ttft_seconds >= 0


def test_time_to_first_token_is_the_first_token_not_the_last() -> None:
    """TTFT is the number a chemist experiences; `duration_seconds` is the whole turn.

    A turn that spent 40 s on tools and then streamed instantly and one that stalled 40 s before
    its first word were the same sample under the only measurement that existed. `None` — no token
    at all — is kept as a distinct fact rather than collapsed to zero.
    """
    ledger = _ledger()
    assert ledger.ttft_seconds is None
    ledger.note_event(TokenEvent(text="first"))
    first = ledger.ttft_seconds
    ledger.note_event(TokenEvent(text="second"))
    assert first is not None and ledger.ttft_seconds == first


def test_a_capped_turn_is_not_recorded_as_answered() -> None:
    """`completed` said `True` for a partial answer after the runaway cap — the collapse itself."""
    assert _settle_outcome(_ledger(answered=True, answer_parts=["partial"], loop_capped=True)) == (
        "loop_capped"
    )


def test_a_silent_turn_is_named_rather_than_billed_as_an_answer() -> None:
    """The worst shape a turn can take: no prose, no error a user can retry from."""
    assert _settle_outcome(_ledger(answered=False, answer_parts=[])) == "empty_answer"


def test_a_wall_clock_kill_is_told_apart_from_a_stop() -> None:
    """Both arrive as one `CancelledError`; only the caller's deadline separates them.

    The caller cannot tell the turn afterwards — its own `except TimeoutError` runs after the cost
    row is booked — so the deadline is passed in and compared against the same loop clock the
    timeout schedules itself on. **Where that comparison is taken is the whole of it**: it is
    sampled in the `except` clause beside `cancelled = True` and read here, because settling runs
    after the rollback and a Stop delivered just short of the deadline crossed it while being torn
    down. `tests/test_api_review_turn_record.py` pins the instant; this pins the pair.
    """

    async def _run() -> tuple[str, str]:
        now = asyncio.get_event_loop().time()
        expired = _settle_outcome(
            _ledger(cancelled=True, timed_out=_deadline_passed(now - 1.0), deadline=now - 1.0)
        )
        stopped = _settle_outcome(
            _ledger(cancelled=True, timed_out=_deadline_passed(now + 3600.0), deadline=now + 3600.0)
        )
        return expired, stopped

    assert asyncio.run(_run()) == ("timed_out", "abandoned")


def test_the_turn_runs_under_the_request_s_correlation_id(client: TestClient) -> None:
    """One event, one id — the header a chemist quotes must find the turn's rows.

    `run_turn` minted its own unconditionally, so the id on the wire and the id keying
    `turn_costs`/`audit_events`/`session_messages` were two different strings for one turn. The
    pump task copies the request's context, so the ambient id inside the turn *is* the request's.
    """
    session_id = client.post("/sessions", json={}).json()["session_id"]
    with client.stream("POST", f"/sessions/{session_id}/messages", json={"message": "hi"}) as r:
        header = r.headers[_HEADER]
        events = "".join(r.iter_lines())
    # The turn's own id reaches the client only on an error event, so the answer path is checked
    # through the log record instead — see the test below, which asserts the same equality.
    assert _CORRELATION_ID.match(header)
    assert events


def test_a_turn_writes_one_started_and_one_finished_record(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    """Measured before this: `grep -c logger.info api/runner.py` was **0**.

    A healthy turn produced no log line at all, so a deployment with no `turn_costs` table had no
    record of a turn in any form.
    """
    session_id = client.post("/sessions", json={}).json()["session_id"]
    before = METRICS.value("chemclaw_turns_finished_total")
    with caplog.at_level(logging.INFO, logger="chemclaw.api.runner"):
        caplog.clear()
        with client.stream("POST", f"/sessions/{session_id}/messages", json={"message": "hi"}) as r:
            assert r.status_code == 200
            list(r.iter_lines())
    events = [getattr(record, "event", "") for record in caplog.records]
    assert events.count("turn.started") == 1
    assert events.count("turn.finished") == 1
    finished = next(r for r in caplog.records if getattr(r, "event", "") == "turn.finished")
    started = next(r for r in caplog.records if getattr(r, "event", "") == "turn.started")
    assert _field(finished, "outcome") == "answered"
    # One id for the whole turn, and it is the request's — the pair is what a bug report joins on.
    assert _field(finished, "correlation_id") == _field(started, "correlation_id")
    assert _field(
        finished, "model"
    )  # the attribution `core/metrics.py` and the runbook already claimed
    assert (
        _field(finished, "ttft_seconds") is not None
    )  # P9: the number a chemist actually experiences
    assert METRICS.value("chemclaw_turns_finished_total") == before + 1
    assert 'chemclaw_turns_finished_total{outcome="answered"}' in METRICS.render()


# --------------------------------------------------------------------------------------------
# P14/T2 — the turn span's scope and its join key.
# --------------------------------------------------------------------------------------------


def test_the_answer_phase_runs_inside_the_turn_span(monkeypatch: pytest.MonkeyPatch) -> None:
    """The turn span used to end before the turn did, and the verifier's judge was the casualty.

    The span was pushed onto the `AsyncExitStack`, which closes when the model stream is exhausted
    — so the loop-cap and empty-answer guards, the plan-approval read, `build_answer_event` (a
    *second LLM call* under `verifier_enabled`), the transcript write, the audit flush and the
    `yield` where a client disconnect lands all ran outside it. Measured against `HEAD` with an
    in-memory exporter and a span opened where the judge runs: `same trace id: False`, `parent is
    the turn: False` — an orphan root trace per turn, with the shipped chart's
    `OTEL_LLM_SPANS=true`. After: one trace, the judge a child of `chemclaw.turn`.

    Also the two attributes P14 named. `correlation.id` is the key `audit_events`, `turn_costs`
    and `session_messages` are joined on and was the one attribute absent, so a trace and the rows
    describing the same turn could be matched only by timestamp.
    """
    from opentelemetry import trace as otel_trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    import chemclaw.core.tracing as tracing
    from chemclaw.api import runner_answer

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("chemclaw")
    # The provider is patched in rather than installed globally: `set_tracer_provider` is
    # process-wide and refuses a second call, which would make this test order-dependent.
    monkeypatch.setattr(tracing, "_tracer", lambda: tracer)

    # Read from where it is *defined* and patched where it is *used*, by string: `runner`
    # re-exports it, and mypy refuses an implicit re-export read as an attribute.
    real_build = runner_answer.build_answer_event

    async def _judging(*args: Any, **kwargs: Any) -> Any:
        """Stand in for the verifier's judge: a span opened exactly where that call happens."""
        with tracer.start_as_current_span("judge"):
            pass
        return await real_build(*args, **kwargs)

    monkeypatch.setattr("chemclaw.api.runner.build_answer_event", _judging)

    client = TestClient(_app(_FakeAgent()))
    session_id = client.post("/sessions", json={}).json()["session_id"]
    with client.stream("POST", f"/sessions/{session_id}/messages", json={"message": "hi"}) as r:
        list(r.iter_lines())

    spans = {span.name: span for span in exporter.get_finished_spans()}
    turn, judge = spans["chemclaw.turn"], spans["judge"]
    assert judge.context.trace_id == turn.context.trace_id, "the judge call is an orphan trace"
    assert judge.parent is not None and judge.parent.span_id == turn.context.span_id
    assert turn.attributes is not None
    assert turn.attributes["correlation.id"]
    assert "actor" in turn.attributes
    assert otel_trace is not None  # the import is what proves the SDK is present, not a stub
