"""Workload identity federation exchanges + caches an Entra token, offline (plan F4-T2).

Proves the pod-mints-its-own-token path without a tenant or network: a fake token endpoint
(`httpx.MockTransport`) and a controllable clock show that the SA token is exchanged for an access
token, that the token is cached and reused until near expiry, that it is refreshed once stale, and
that a disabled/rejecting endpoint raises a typed error.
"""

import asyncio
from pathlib import Path

import httpx
import pytest

from agents.identity.workload import WorkloadIdentityError, WorkloadTokenProvider
from chemclaw.config import settings


@pytest.fixture
def _federation_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Turn federation on and point the SA-token path at a temp file with a fake assertion."""
    sa_file = tmp_path / "sa-token"
    sa_file.write_text("sa-jwt-assertion")
    monkeypatch.setattr(settings, "entra_workload_federation_enabled", True)
    monkeypatch.setattr(settings, "entra_workload_client_id", "wl-client")
    monkeypatch.setattr(settings, "entra_token_endpoint", "https://login.test/token")
    monkeypatch.setattr(settings, "entra_sa_token_path", str(sa_file))
    monkeypatch.setattr(settings, "entra_token_refresh_leeway_seconds", 60.0)


class _Clock:
    """A hand-cranked monotonic clock so expiry is deterministic in tests."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _counting_transport(
    captured: list[httpx.Request], expires_in: int = 3600
) -> httpx.MockTransport:
    """A transport that records each request and returns a token with `expires_in`."""

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        token = f"access-{len(captured)}"
        return httpx.Response(200, json={"access_token": token, "expires_in": expires_in})

    return httpx.MockTransport(handler)


def test_exchanges_sa_token_for_access_token(_federation_env: None) -> None:
    """The SA JWT is presented as a client_assertion and an access token comes back."""
    captured: list[httpx.Request] = []
    provider = WorkloadTokenProvider(transport=_counting_transport(captured), clock=_Clock())

    token = asyncio.run(provider.get_service_token("api://eln/.default"))

    assert token == "access-1"
    body = captured[0].content.decode()
    assert "grant_type=client_credentials" in body
    assert "client_assertion=sa-jwt-assertion" in body  # the SA token, read fresh from the file
    assert "scope=api%3A%2F%2Feln%2F.default" in body


def test_caches_until_near_expiry(_federation_env: None) -> None:
    """A second call inside the validity window reuses the cached token (one exchange)."""
    captured: list[httpx.Request] = []
    clock = _Clock()
    provider = WorkloadTokenProvider(transport=_counting_transport(captured), clock=clock)

    first = asyncio.run(provider.get_service_token("s"))
    clock.now += 100.0  # still far from the 3600s expiry minus 60s leeway
    second = asyncio.run(provider.get_service_token("s"))

    assert first == second == "access-1"
    assert len(captured) == 1  # no second exchange


def test_refreshes_once_stale(_federation_env: None) -> None:
    """Past expiry-minus-leeway the token is re-minted."""
    captured: list[httpx.Request] = []
    clock = _Clock()
    provider = WorkloadTokenProvider(transport=_counting_transport(captured), clock=clock)

    first = asyncio.run(provider.get_service_token("s"))
    clock.now += 3600.0  # now beyond expiry
    second = asyncio.run(provider.get_service_token("s"))

    assert first == "access-1"
    assert second == "access-2"
    assert len(captured) == 2


def test_concurrent_misses_do_one_exchange(_federation_env: None) -> None:
    """N concurrent cold-cache callers for one scope collapse onto a single exchange (D-054)."""
    captured: list[httpx.Request] = []
    provider = WorkloadTokenProvider(transport=_counting_transport(captured), clock=_Clock())

    async def _race() -> list[str]:
        return await asyncio.gather(*(provider.get_service_token("s") for _ in range(10)))

    tokens = asyncio.run(_race())

    assert tokens == ["access-1"] * 10  # everyone got the one minted token
    assert len(captured) == 1  # not a thundering herd of 10 exchanges


def test_disabled_federation_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """With federation off there is no token path — a typed error, not a silent None."""
    monkeypatch.setattr(settings, "entra_workload_federation_enabled", False)
    with pytest.raises(WorkloadIdentityError):
        asyncio.run(WorkloadTokenProvider().get_service_token("s"))


def test_endpoint_rejection_raises(_federation_env: None) -> None:
    """A non-200 from the token endpoint surfaces as a typed error."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="invalid_client")

    provider = WorkloadTokenProvider(transport=httpx.MockTransport(handler), clock=_Clock())
    with pytest.raises(WorkloadIdentityError, match="token exchange failed"):
        asyncio.run(provider.get_service_token("s"))
