"""What the front door lets a caller put into a log record, a metric or a 422 body.

Every test pins a bound. `SecretRedactingFilter` regex-scans each record while holding the logging
lock on the interpreter serving every SSE stream, so an unbounded caller-supplied string is a
throughput problem. Driven through the real app where the defect needs a stack, and against the
handler directly where no shipped route produces the shape.
"""

import asyncio
import json
import logging
import re
from pathlib import Path
from typing import Any

import pytest
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient
from starlette.requests import Request
from starlette.types import Message, Receive, Scope, Send

from chemclaw.api.middleware import (
    _MAX_LOGGED_CHARS,
    _RequestObservability,
    _validation_failed,
    clip_for_log,
)
from chemclaw.core.config import settings
from tests.conftest import log_field
from tests.test_service import _app, _FakeAgent

# Long enough that no cap could be an accident, and the length the review measured.
_SHOUT = "Q" * 6000


def _records(caplog: pytest.LogCaptureFixture, logger_name: str) -> list[logging.LogRecord]:
    """Every captured record from one logger — the access log and the auth gate are separate."""
    return [record for record in caplog.records if record.name == logger_name]


# --------------------------------------------------------------------------------------------
# 1 — the access log's session id, on a request that never authenticated.
# --------------------------------------------------------------------------------------------


def test_an_unauthenticated_401_books_no_session_id_from_the_path(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unauthenticated 401 books no session id from the path.

    `path_params` is filled at route match, before any dependency runs, so the id is stamped by
    `bind_request_session` inside the ownership gate; on a 401 the field is empty.
    """
    monkeypatch.setattr(settings, "entra_required", True)
    client = TestClient(_app(_FakeAgent()))
    with caplog.at_level(logging.INFO):
        response = client.get(f"/sessions/{_SHOUT}/messages")

    assert response.status_code == 401
    (access,) = [r for r in _records(caplog, "chemclaw.api.middleware") if hasattr(r, "route")]
    stamped = log_field(access, "session_id")
    assert stamped == "", (
        f"a caller who never authenticated put {len(stamped)} characters into the access log's "
        "session_id"
    )


def test_the_owner_s_session_id_still_reaches_the_access_log(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The other direction, or the fix above would be indistinguishable from deleting the field.

    A resolved, owned session is exactly the case the field exists for: it is what joins an access
    line to `turn_costs`, `audit_events` and the transcript.
    """
    client = TestClient(_app(_FakeAgent()))
    session_id = client.post("/sessions").json()["session_id"]
    with caplog.at_level(logging.INFO):
        assert client.get(f"/sessions/{session_id}/messages").status_code == 200

    stamped = [
        r
        for r in _records(caplog, "chemclaw.api.middleware")
        if getattr(r, "session_id", "") == session_id
    ]
    assert stamped, "the access log lost the session id of a request the ownership gate resolved"


def test_the_logged_session_id_is_clipped_even_once_it_is_owned() -> None:
    """Belt as well as braces: the binder clips, so no id length can reach the filter unbounded.

    The gate is what makes the id *the caller's own session*; the clip is what makes its length
    this repository's business rather than the store's.
    """
    long_id = "s" * 4000
    clipped = clip_for_log(long_id)
    assert len(clipped) < 200 and clipped.startswith("s" * _MAX_LOGGED_CHARS)
    assert clipped.endswith(f"(+{4000 - _MAX_LOGGED_CHARS})"), (
        "a clipped value that does not say it was clipped reads as a short one"
    )


# --------------------------------------------------------------------------------------------
# 2 — the authentication gate's own log line, which runs before any credential is checked.
# --------------------------------------------------------------------------------------------


def test_the_missing_token_line_names_the_route_not_the_path(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Measured at 6,054 characters, from a caller that had presented nothing.

    This is the same hazard `_RequestObservability` cites as the reason its `route` label is the
    template — the raw path is whatever somebody types, and a route template is a source constant.
    """
    monkeypatch.setattr(settings, "entra_required", True)
    client = TestClient(_app(_FakeAgent()))
    with caplog.at_level(logging.INFO):
        assert client.get(f"/sessions/{_SHOUT}/messages").status_code == 401

    (refused,) = [
        r for r in _records(caplog, "chemclaw.api.auth") if "no bearer token" in r.getMessage()
    ]
    message = refused.getMessage()
    assert "Q" * 100 not in message, f"the caller's path is in the log line ({len(message)} chars)"
    assert "/sessions/{session_id}/messages" in message, (
        "the line no longer says which route was hit; the template is what makes it useful"
    )


# --------------------------------------------------------------------------------------------
# 3 — the authorization refusal's target, which is by definition an id nothing recognised.
# --------------------------------------------------------------------------------------------


def test_the_authz_refusal_clips_the_id_it_refused(caplog: pytest.LogCaptureFixture) -> None:
    """`target` reaches `_refuse` straight off the path and was interpolated at full length.

    Measured: an 8,000-character id produced an 8,093-character WARNING, once in the message and
    once in the `target` field, both of which `SecretRedactingFilter` then scans.
    """
    client = TestClient(_app(_FakeAgent()))
    with caplog.at_level(logging.WARNING):
        assert client.get(f"/sessions/{'Z' * 8000}/messages").status_code == 404

    (refusal,) = [r for r in _records(caplog, "chemclaw.api.deps") if hasattr(r, "target")]
    target = log_field(refusal, "target")
    assert len(refusal.getMessage()) < 400, (
        f"the refused id is still interpolated whole ({len(refusal.getMessage())} chars)"
    )
    assert len(target) <= _MAX_LOGGED_CHARS + 16
    assert target.startswith("Z" * 32), "the clip left nothing to recognise the id by"


# --------------------------------------------------------------------------------------------
# 4 and 5 — the 422, which is the one handler that answers with the caller's own bytes.
# --------------------------------------------------------------------------------------------


def test_a_validation_failure_does_not_reflect_the_body_back(client: TestClient) -> None:
    """A validation failure does not reflect the body back.

    `_MAX_VALIDATION_ERRORS` bounds the error count, but a v2 error object embeds its `input`, so
    one error could echo the whole body; the input is clipped.
    """
    session_id = client.post("/sessions").json()["session_id"]
    body = {"message": {"junk": "Y" * 200_000}}
    response = client.post(f"/sessions/{session_id}/messages", json=body)

    assert response.status_code == 422
    assert len(response.content) < 2_000, (
        f"the 422 reflected {len(response.content)} bytes of a 200 KB body"
    )
    (detail,) = response.json()["detail"]
    assert detail["loc"][-1] == "message" and detail["type"], (
        "the client can no longer tell which field was wrong, which is what the body is for"
    )


def test_a_validation_error_location_cannot_carry_the_caller_s_own_key(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The `loc` tail is a caller-chosen string for `extra_forbidden` and for a bad dict key.

    No shipped route produces either shape, so this drives the handler, which is registered for
    every route there will ever be.
    """
    key = "K" * 5000
    error = {"type": "extra_forbidden", "loc": ("body", key), "msg": "Extra inputs", "input": key}
    request = Request({"type": "http", "method": "POST", "path": "/sessions", "headers": []})

    async def _drive() -> Any:
        return await _validation_failed(request, RequestValidationError([error]))

    with caplog.at_level(logging.WARNING):
        response = asyncio.run(_drive())

    (logged,) = [r for r in caplog.records if hasattr(r, "first_locations")]
    (location,) = log_field(logged, "first_locations")
    assert len(location) <= _MAX_LOGGED_CHARS + 16, f"{len(location)} characters of caller key"
    assert len(response.body) < 1_000, f"the body echoed {len(response.body)} bytes"


def test_an_error_object_that_is_not_a_mapping_still_answers_the_caller(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An error object that is not a mapping still answers the caller.

    `_render_errors` passes a non-mapping error through, so the log line built before it must too,
    or the 422 becomes a 500 with a 422 already counted. Driven directly, since no route produces
    this shape.
    """
    request = Request({"type": "http", "method": "POST", "path": "/sessions", "headers": []})

    async def _drive() -> Any:
        return await _validation_failed(request, RequestValidationError(["a bare string"]))

    with caplog.at_level(logging.WARNING):
        response = asyncio.run(_drive())

    assert response.status_code == 422
    assert json.loads(response.body)["detail"] == ["a bare string"]
    (logged,) = [r for r in caplog.records if hasattr(r, "first_locations")]
    assert log_field(logged, "first_locations") == [], (
        "an error object with no `loc` to read must drop out of the log line, not out of the "
        "handler"
    )


# --------------------------------------------------------------------------------------------
# 14 — a response-start message that omits `headers`, which the ASGI spec permits.
# --------------------------------------------------------------------------------------------


def test_a_response_start_without_headers_is_still_answered() -> None:
    """A response start without headers is still answered.

    `MutableHeaders(scope=message)` raises `KeyError` when `headers` is absent, which would reach
    the `if answered: raise` arm and send nothing; the `setdefault` runs first.
    """

    async def _bare(scope: Scope, receive: Receive, send: Send) -> None:
        await send({"type": "http.response.start", "status": 200})
        await send({"type": "http.response.body", "body": b"ok"})

    sent: list[Message] = []

    async def _send(message: Message) -> None:
        sent.append(message)

    async def _receive() -> Message:  # pragma: no cover - the app under test never reads a body
        return {"type": "http.request", "body": b"", "more_body": False}

    scope: Scope = {"type": "http", "method": "GET", "path": "/x", "headers": []}
    asyncio.run(_RequestObservability(_bare)(scope, _receive, _send))

    assert [message["type"] for message in sent] == [
        "http.response.start",
        "http.response.body",
    ], f"a spec-legal response was swallowed; the client got {sent}"
    assert sent[0]["status"] == 200
    assert any(name.lower() == b"x-chemclaw-correlation-id" for name, _ in sent[0]["headers"]), (
        "the correlation id was not stamped onto the response it defaulted the headers for"
    )


# --------------------------------------------------------------------------------------------
# 10 — the registry's series cap, which is a constant and was three copies of a stale number.
# --------------------------------------------------------------------------------------------


def test_the_front_door_s_prose_does_not_restate_the_series_cap() -> None:
    """The front door's prose does not restate the series cap.

    The number lives in `core/metrics.py` and the arithmetic is asserted in
    `tests/test_api_observability.py`; a prose copy goes stale unchecked.
    """
    stale = re.compile(r"\b64\b")
    for path in (
        Path("src/chemclaw/api/middleware.py"),
        Path("src/chemclaw/api/runner.py"),
        Path("tests/test_api_observability.py"),
    ):
        for number, line in enumerate(path.read_text().splitlines(), start=1):
            if "series" in line or "cap of" in line:
                assert not stale.search(line), f"{path}:{number} restates the cap: {line.strip()}"
