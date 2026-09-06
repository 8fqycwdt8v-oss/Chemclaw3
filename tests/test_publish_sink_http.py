"""What the HTTP result sink calls a delivery — and what it refuses to.

This file exists because the sink's failure mode is the most expensive one this system has: a
`deliver()` that returns is read by `durable/publish_results._drain_one` as "every record in this
batch is durable at the far end", and the outbox then writes `state='delivered'`. A response class
the classifier does not name is therefore not an unhandled case; it is a **positive false claim**
that science was published, on a row `requeue_failed` can never bring back and retention will
delete.

Driven through the real `HttpResultSink` and a real `httpx.AsyncClient` — only the transport is
scripted — so the client's own redirect policy is part of what is under test rather than something
this file assumes.
"""

import asyncio

import httpx
import pytest

from chemclaw.publish.driver import SinkRejectedError, SinkUnavailableError
from chemclaw.publish.drivers.http import HttpResultSink
from chemclaw.publish.record import (
    Conditions,
    ResultRecord,
    Subject,
    SubjectMember,
    TheoryLevel,
)


def _record(ref: str = "http-probe") -> ResultRecord:
    """A minimal valid record — this file is about the response, not the chemistry."""
    return ResultRecord(
        calc_ref=ref,
        calc_type="pka",
        subject=Subject(
            kind="molecule",
            members=[SubjectMember(ordinal=0, role="subject", smiles="CCO")],
            label="CCO",
        ),
        conditions=Conditions(),
        level=TheoryLevel(method="GFN2-xTB"),
    )


def _sink_answering(
    monkeypatch: pytest.MonkeyPatch, responses: list[httpx.Response], seen: list[str]
) -> HttpResultSink:
    """A real sink whose transport answers `responses` in order, recording every URL dialled.

    The client is the real one: `trust_env`, the timeout and — the point of this helper — the
    redirect policy are whatever the driver asked for, so a test can tell "refused the redirect"
    from "followed it and was answered by somewhere else".
    """
    remaining = list(responses)

    def _handle(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return remaining.pop(0) if remaining else httpx.Response(200)

    real_client = httpx.AsyncClient

    def _scripted(*args: object, **kwargs: object) -> httpx.AsyncClient:
        kwargs["transport"] = httpx.MockTransport(_handle)
        return real_client(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(httpx, "AsyncClient", _scripted)
    return HttpResultSink(name="probe", tenant_id="t", url="http://127.0.0.1:1/results")


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_a_redirect_is_refused_rather_than_reported_as_delivered(
    monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    """A 3xx must raise, because the batch did not land where the manifest addressed it.

    Measured on the unfixed driver against a real listener answering `302 + Location:`: the POST
    was received, the endpoint wrote nothing, `deliver()` **returned**, and
    `result_publications` read `state='delivered'` with `delivered_at` set — while the driver's own
    log line for that same call said `sink.failed ... -> 3xx`. A redirect is not exotic: it is what
    an ingress does for an http->https upgrade and what a proxy does for a renamed path.

    Refused rather than retried, because no retry to the same URL changes the answer — the fix is
    the manifest's `url`, and dead-lettering is how an operator is told that.
    """
    seen: list[str] = []
    sink = _sink_answering(
        monkeypatch,
        [httpx.Response(status, headers={"location": "https://elsewhere.invalid/results"})],
        seen,
    )

    with pytest.raises(SinkRejectedError) as refusal:
        asyncio.run(sink.deliver([_record()]))

    assert str(status) in str(refusal.value)
    assert seen == ["http://127.0.0.1:1/results"], (
        "the sink must not follow a redirect: the records are confidential chemistry and the "
        "request may carry a bearer token, neither of which may reach an address no manifest named"
    )


def test_an_informational_or_unknown_non_2xx_is_not_a_delivery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Success is `2xx` and nothing else — stated as a closed rule, not an open one.

    The classifier used to be written as two rejections with an implicit `return` for everything
    else, which made every response class nobody thought of a silent success. This asserts the
    inversion: an unnamed class raises.
    """
    sink = _sink_answering(monkeypatch, [httpx.Response(199)], [])
    with pytest.raises(SinkRejectedError):
        asyncio.run(sink.deliver([_record()]))


@pytest.mark.parametrize("status", [200, 201, 202, 204])
def test_every_2xx_is_a_delivery(monkeypatch: pytest.MonkeyPatch, status: int) -> None:
    """A receiver answering 201 or 204 took the batch; the sink must not invent a failure."""
    sink = _sink_answering(monkeypatch, [httpx.Response(status)], [])
    asyncio.run(sink.deliver([_record()]))


@pytest.mark.parametrize("status", [408, 429, 500, 502, 503, 504])
def test_a_retryable_status_stays_retryable(monkeypatch: pytest.MonkeyPatch, status: int) -> None:
    """The overloaded/restarting classes keep their retry, which the 2xx-only rule must not eat."""
    sink = _sink_answering(monkeypatch, [httpx.Response(status)], [])
    with pytest.raises(SinkUnavailableError):
        asyncio.run(sink.deliver([_record()]))


def test_a_content_refusal_is_still_permanent(monkeypatch: pytest.MonkeyPatch) -> None:
    """422 is the receiver's statement about the content, and no retry changes it."""
    sink = _sink_answering(monkeypatch, [httpx.Response(422, text="bad property")], [])
    with pytest.raises(SinkRejectedError) as refusal:
        asyncio.run(sink.deliver([_record()]))
    assert "bad property" in str(refusal.value)
