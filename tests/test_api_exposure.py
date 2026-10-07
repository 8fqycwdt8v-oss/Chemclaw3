"""What the front door does when the *socket* is exposed and the configuration says it is not.

`settings.service_host` states intent; uvicorn binds whatever `--host` says. So these assertions
are about the address a request arrived on (`scope["server"]`, from the accepted socket's
`sockname`). A test client carries no socket, so every case states the address it drives from.
"""

from typing import Any

import pytest
from fastapi.testclient import TestClient

from chemclaw.core.config import settings
from chemclaw.core.metrics import METRICS
from tests.test_service import _app, _FakeAgent

# An address in TEST-NET-1 (RFC 5737): a literal that parses as a routable IP, which is the whole
# property under test. The port is arbitrary — only the host half of `scope["server"]` is read.
_OFF_BOX = "http://192.0.2.2:8000"
_LOOPBACK = "http://127.0.0.1:8000"


@pytest.fixture
def unauthenticated(monkeypatch: pytest.MonkeyPatch) -> None:
    """The dev posture the guard exists for: no identity, no explicit opt-out."""
    monkeypatch.setattr(settings, "entra_required", False)
    monkeypatch.setattr(settings, "service_allow_insecure", False)


def _get(base_url: str, **kwargs: Any) -> Any:
    """One authenticated route driven as though the request arrived on `base_url`'s address."""
    with TestClient(_app(_FakeAgent()), base_url=base_url, **kwargs) as client:
        return client.get("/profiles")


def test_an_off_box_request_is_refused_when_every_gate_is_open(unauthenticated: None) -> None:
    """A request that arrived on a routable address must not be served the dev principal.

    With `entra_required` off every request is `dev-user` with every gate open, so this direction
    fails open.
    """
    before = METRICS.value("chemclaw_auth_failures_total")
    response = _get(_OFF_BOX)
    assert response.status_code == 503, response.text
    assert "capacity" not in response.text
    assert METRICS.value("chemclaw_auth_failures_total") == before + 1


def test_a_loopback_request_is_served_when_every_gate_is_open(unauthenticated: None) -> None:
    """Local dev is untouched: the arrival address is unreachable from the network."""
    assert _get(_LOOPBACK).status_code == 200


def test_an_in_process_caller_is_not_a_socket(unauthenticated: None) -> None:
    """A scope whose `server` is a *name* carries no socket, so there is no exposure to judge.

    An in-process ASGI call (`testserver`) has no `sockname`; the boot guard still covers the
    configuration.
    """
    assert _get("http://testserver").status_code == 200


def test_the_explicit_opt_out_still_serves_an_off_box_request(unauthenticated: None) -> None:
    """`service_allow_insecure=true` is the conscious decision, and it stays the way out."""
    settings.service_allow_insecure = True
    try:
        assert _get(_OFF_BOX).status_code == 200
    finally:
        settings.service_allow_insecure = False


def test_the_guard_is_inert_where_identity_is_enforced(monkeypatch: pytest.MonkeyPatch) -> None:
    """With Entra enforced there is no dev principal to leak, so an off-box request is a 401.

    Stated as its own case because a guard that answered 503 here would hide every real
    authentication failure behind an availability error.
    """
    monkeypatch.setattr(settings, "entra_required", True)
    monkeypatch.setattr(settings, "service_allow_insecure", False)
    assert _get(_OFF_BOX).status_code == 401
