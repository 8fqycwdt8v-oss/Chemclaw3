"""On-Behalf-Of exchange swaps a user token for a downstream token, offline (plan F4-T4).

Proves the OBO flow without a tenant: a fake token endpoint shows the user's token is presented as
the `assertion` with `requested_token_use=on_behalf_of` and the SA JWT as the client_assertion, that
a downstream token comes back, and that a disabled/rejecting endpoint raises a typed error.
"""

import asyncio
from pathlib import Path

import httpx
import pytest

from agents.identity.obo import OboExchangeError, exchange_obo
from chemclaw.config import settings


@pytest.fixture
def _obo_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Enable OBO and point the client-assertion SA-token path at a temp file."""
    sa_file = tmp_path / "sa-token"
    sa_file.write_text("sa-jwt-assertion")
    monkeypatch.setattr(settings, "entra_obo_enabled", True)
    monkeypatch.setattr(settings, "entra_workload_client_id", "wl-client")
    monkeypatch.setattr(settings, "entra_token_endpoint", "https://login.test/token")
    monkeypatch.setattr(settings, "entra_sa_token_path", str(sa_file))


def test_exchanges_user_token_for_downstream(_obo_env: None) -> None:
    """The user token rides as the OBO assertion; a downstream token comes back."""
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"access_token": "downstream-token"})

    token = asyncio.run(
        exchange_obo("user-token", "api://eln/.default", transport=httpx.MockTransport(handler))
    )

    assert token == "downstream-token"
    body = captured[0].content.decode()
    assert "requested_token_use=on_behalf_of" in body
    assert "assertion=user-token" in body  # the user's identity, not the service's
    assert "client_assertion=sa-jwt-assertion" in body  # backend auth via federation


def test_disabled_obo_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """With OBO off there is no exchange path — a typed error."""
    monkeypatch.setattr(settings, "entra_obo_enabled", False)
    with pytest.raises(OboExchangeError):
        asyncio.run(exchange_obo("user-token", "s"))


def test_endpoint_rejection_raises(_obo_env: None) -> None:
    """A non-200 from the token endpoint surfaces as a typed error."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, text="invalid_grant")

    with pytest.raises(OboExchangeError, match="obo exchange failed"):
        asyncio.run(exchange_obo("user-token", "s", transport=httpx.MockTransport(handler)))
