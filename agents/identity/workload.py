"""Workload identity federation: a backend pod mints its own short-lived Entra token (F4-T2).

Backend services (the front door, the workers, the MCP servers) must call Entra-protected
resources as *themselves* without a stored client secret. On OpenShift the pod's ServiceAccount
token is projected to a file; Entra Workload Identity Federation exchanges that SA JWT for an Entra
access token via the OAuth2 *client-credentials* grant with a `client_assertion` (the SA token) —
so no secret is ever at rest (§7, ADR D-A4). This module is that exchange, with a per-scope cache
so a token is reused until shortly before it expires.

The generic LLM credential (`agents/llm_provider.py`) is the one documented exception and does not
go through here.
"""

import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import httpx

from chemclaw.config import settings

# The federated client-credentials assertion type — the SA JWT is presented as the client's proof
# of identity in place of a secret (RFC 7521 / Entra workload identity).
_JWT_BEARER = "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"


class WorkloadIdentityError(RuntimeError):
    """Federation is disabled/misconfigured, or the token exchange failed."""


@dataclass
class _CachedToken:
    """A minted access token and the monotonic time it should be refreshed before."""

    value: str
    expires_at: float


class WorkloadTokenProvider:
    """Mints and caches Entra access tokens for this workload via federation.

    One instance per process (see `default_provider`); it holds the per-scope cache so a token is
    reused until `entra_token_refresh_leeway_seconds` before its expiry. The SA token is read fresh
    from the projected file on every exchange because it too rotates. The transport and clock are
    injectable so tests drive the exchange against an `httpx.MockTransport` with a controllable
    clock — no tenant and no network.
    """

    def __init__(
        self,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Build a provider; inject `transport`/`clock` in tests, default to real ones in prod."""
        self._transport = transport
        self._clock = clock
        self._cache: dict[str, _CachedToken] = {}

    async def get_service_token(self, scope: str) -> str:
        """Return a valid Entra access token for `scope`, minting one if the cache is cold/stale.

        Args:
            scope: The resource scope requested (e.g. `"api://eln/.default"`).

        Returns:
            A bearer access token valid for `scope`.

        Raises:
            WorkloadIdentityError: When federation is disabled, the SA token is unreadable, or the
                token endpoint rejects the exchange.
        """
        if not settings.entra_workload_federation_enabled:
            raise WorkloadIdentityError("workload identity federation is disabled")
        now = self._clock()
        cached = self._cache.get(scope)
        leeway = settings.entra_token_refresh_leeway_seconds
        if cached is not None and now < cached.expires_at - leeway:
            return cached.value
        return await self._exchange(scope, now)

    async def _exchange(self, scope: str, now: float) -> str:
        """Perform the client-credentials exchange and cache the result against `now`."""
        data = {
            "grant_type": "client_credentials",
            "client_id": settings.entra_workload_client_id,
            "scope": scope,
            "client_assertion_type": _JWT_BEARER,
            "client_assertion": _read_sa_token(),
        }
        async with httpx.AsyncClient(
            transport=self._transport, timeout=settings.entra_http_timeout_seconds
        ) as client:
            response = await client.post(settings.entra_token_endpoint, data=data)
        if response.status_code != httpx.codes.OK:
            raise WorkloadIdentityError(
                f"token exchange failed: {response.status_code} {response.text}"
            )
        body = response.json()
        access_token = body.get("access_token")
        expires_in = body.get("expires_in")
        if not access_token or expires_in is None:
            raise WorkloadIdentityError("token endpoint returned no access_token/expires_in")
        token = str(access_token)
        self._cache[scope] = _CachedToken(value=token, expires_at=now + float(expires_in))
        return token


def _read_sa_token() -> str:
    """Read the projected ServiceAccount token, or raise a typed error if it is unreadable."""
    try:
        return Path(settings.entra_sa_token_path).read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise WorkloadIdentityError(
            f"cannot read SA token at {settings.entra_sa_token_path}: {exc}"
        ) from exc


# The process-wide provider: one shared cache so repeated calls for the same scope reuse a token.
default_provider = WorkloadTokenProvider()


async def get_service_token(scope: str) -> str:
    """Mint or reuse a workload access token for `scope` from the process-wide provider."""
    return await default_provider.get_service_token(scope)
