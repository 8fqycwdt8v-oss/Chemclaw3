"""What the front door does with a request it already knows the database cannot serve.

When `/readyz` has cached "database unreachable", requests read that verdict and answer at once
instead of each waiting out `pg_pool_timeout_seconds`. The wording is unchanged: clients back off
and retry either way, and a browser need not learn which dependency is down.
"""

import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from chemclaw.core.config import settings
from tests.test_service import _app, _FakeAgent


@pytest.fixture
def app() -> FastAPI:
    """The front door with a fake agent, as every other front-door test drives it.

    The `FastAPI` object is the fixture because tests write `app.state`, which `TestClient.app`'s
    type lacks.
    """
    return _app(_FakeAgent())


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    """A client onto that same app, so a verdict written below is the one the request reads."""
    return TestClient(app)


def _database_is_down(app: FastAPI, *, probed_at: float) -> None:
    """Record the verdict the readiness probe writes, as of `probed_at`."""
    app.state.database_reachable = False
    app.state.database_probed_at = probed_at


def test_a_request_is_shed_at_once_when_readiness_already_knows(
    app: FastAPI, client: TestClient
) -> None:
    """The probe's verdict is a fact the process holds; a request must not re-buy it."""
    _database_is_down(app, probed_at=time.monotonic())
    started = time.monotonic()
    response = client.get("/sessions")
    assert response.status_code == 503
    assert response.json()["detail"] == "server at capacity; retry shortly"
    assert time.monotonic() - started < 1.0


def test_a_stale_verdict_is_not_a_verdict(app: FastAPI, client: TestClient) -> None:
    """Past its cache window the reading is history, so the request goes and finds out itself.

    This is what keeps the shed from outliving the outage: nothing re-probes on its own, so a
    verdict old enough to have been superseded must not shed a request the database would serve.
    """
    _database_is_down(
        app, probed_at=time.monotonic() - settings.service_readiness_cache_seconds - 1
    )
    assert client.get("/sessions").status_code == 200


def test_the_probes_are_never_shed(app: FastAPI, client: TestClient) -> None:
    """`/readyz` is what clears the verdict, so shedding it would make the outage permanent.

    `/healthz` and `/metrics` are exempt too, structurally: none depends on `require_principal`,
    where the shed lives.
    """
    _database_is_down(app, probed_at=time.monotonic())
    assert client.get("/healthz").status_code == 200
    assert client.get("/metrics").status_code == 200
