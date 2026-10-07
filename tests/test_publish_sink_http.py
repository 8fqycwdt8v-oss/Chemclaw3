"""What the HTTP result sink calls a delivery, and what it refuses to.

A returning `deliver()` is read by `_drain_one` as every record durable at the far end, so an
unclassified response would be a false claim of publication that cannot be requeued. Driven
through the real `HttpResultSink` and `httpx.AsyncClient` with only the transport scripted, so the
client's redirect policy is under test.
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

    The client's redirect policy is the driver's own, so a test can tell "refused the redirect" from
    "followed it elsewhere".
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
    """A redirect is refused rather than reported as delivered.

    A 3xx means the batch did not land where the manifest addressed it (ingress http->https upgrades
    and renamed proxy paths do this). Refused, not retried: the fix is the manifest's `url`, and
    dead-lettering tells the operator.
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
    """Success is `2xx` and nothing else: an unnamed response class raises."""
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
