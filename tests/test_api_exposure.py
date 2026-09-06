"""What the front door does when the *socket* is exposed and the configuration says it is not.

`create_app`'s boot guard reads `settings.service_host`, which is a statement of intent rather than
an observation: uvicorn binds whatever `--host` says, and the two are the same string only on the
container path (`deploy/entrypoint.sh` derives one from the other). Measured on 2026-09-06 against a
real uvicorn, both directions were wrong — `CHEMCLAW_SERVICE_HOST=127.0.0.1 uvicorn --host 0.0.0.0`
booted with no warning of any kind and answered `POST /sessions` from an off-box address with 200
and the shared dev principal, while the command `README.md` gives verbatim refused to boot.

So these assertions are about the address a request *arrived on* (`scope["server"]`, which uvicorn
fills from the accepted connection's own `sockname`), not about a setting. A test client carries no
socket at all — its `server` is the base URL's host — which is why every case below states the
address it is driving from.
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

    This is the direction that fails *open*: with `entra_required` off every request is
    `dev-user` and every authorization gate is open, so serving one off-box request is the whole
    exposure the boot guard claims to refuse.
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

    `testserver` is what every other test in this suite drives through, and it is not an address:
    an in-process ASGI call has no accepted connection and therefore no `sockname`. Refusing it
    would refuse the suite rather than an exposure — and the boot guard still covers the
    configuration, which is the only thing knowable before a request exists.
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
