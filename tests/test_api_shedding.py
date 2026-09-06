"""What the front door does with a request it already knows the database cannot serve.

Measured on 2026-09-06 against a real uvicorn pointed at a port nothing listens on: `/healthz`
answered 200 in 3 ms, `/readyz` answered `503 database unreachable` in 2.03 s and cached it — and
then `POST /sessions` and `GET /sessions` each took the full `pg_pool_timeout_seconds` (10 s) to
answer `server at capacity`, per request. Readiness had the answer in two seconds and every request
went on rediscovering it in ten, so a dead database converts into occupied connections and, under
`--limit-concurrency`, into a queue — for a fact the process was already holding.

These assertions are about that fact being *read* rather than re-derived. The wording is unchanged
and deliberately so (`_database_unavailable` argues it: back off and retry is the client behaviour
either way, and a browser has no business learning which dependency is down).
"""

import time

import pytest
from fastapi.testclient import TestClient

from chemclaw.core.config import settings
from tests.test_service import _app, _FakeAgent


@pytest.fixture
def client() -> TestClient:
    """The front door with a fake agent, as every other front-door test drives it."""
    return TestClient(_app(_FakeAgent()))


def _database_is_down(client: TestClient, *, probed_at: float) -> None:
    """Record the verdict the readiness probe writes, as of `probed_at`."""
    client.app.state.database_reachable = False
    client.app.state.database_probed_at = probed_at


def test_a_request_is_shed_at_once_when_readiness_already_knows(client: TestClient) -> None:
    """The probe's verdict is a fact the process holds; a request must not re-buy it."""
    _database_is_down(client, probed_at=time.monotonic())
    started = time.monotonic()
    response = client.get("/sessions")
    assert response.status_code == 503
    assert response.json()["detail"] == "server at capacity; retry shortly"
    assert time.monotonic() - started < 1.0


def test_a_stale_verdict_is_not_a_verdict(client: TestClient) -> None:
    """Past its cache window the reading is history, so the request goes and finds out itself.

    This is what keeps the shed from outliving the outage: nothing re-probes on its own, so a
    verdict old enough to have been superseded must not shed a request the database would serve.
    """
    _database_is_down(
        client, probed_at=time.monotonic() - settings.service_readiness_cache_seconds - 1
    )
    assert client.get("/sessions").status_code == 200


def test_the_probes_are_never_shed(client: TestClient) -> None:
    """`/readyz` is what clears the verdict, so shedding it would make the outage permanent.

    `/healthz` and `/metrics` come with it, by construction rather than by a path list: none of the
    three depends on `require_principal`, which is where the shed lives — the same structural
    exemption the rate limiter documents.
    """
    _database_is_down(client, probed_at=time.monotonic())
    assert client.get("/healthz").status_code == 200
    assert client.get("/metrics").status_code == 200
