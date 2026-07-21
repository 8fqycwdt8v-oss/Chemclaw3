"""On-Behalf-Of exchange: a user-scoped backend call swaps the user's token for a downstream one.

Plan F4-T4. When a backend acts *for a specific user* against a user-scoped resource (ELN/LIMS), it
must not use its own service identity — it exchanges the user's access token On-Behalf-Of for a
token scoped to the downstream resource (OAuth2 OBO, RFC 7523), so the downstream sees the real user
and applies their entitlements. The backend authenticates to the token endpoint with its federated
SA assertion (D-045), never a stored secret.

Generic and dormant: no concrete user-scoped source exists yet (the first — a custom Snowflake ELN
connector — is deferred behind the F7 seam). A source opts in later by calling `exchange_obo`.
"""

import httpx

from agents.identity.workload import read_sa_token
from chemclaw.config import settings

# The OBO grant and the federated client-assertion type (RFC 7523 / Entra).
_OBO_GRANT = "urn:ietf:params:oauth:grant-type:jwt-bearer"
_JWT_BEARER = "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"


class OboExchangeError(RuntimeError):
    """OBO is disabled/misconfigured, or the token exchange failed."""


async def exchange_obo(
    user_token: str, scope: str, *, transport: httpx.AsyncBaseTransport | None = None
) -> str:
    """Exchange a user's access token On-Behalf-Of for a token scoped to a downstream resource.

    Args:
        user_token: The requesting user's Entra access token (from the front-door `Principal`).
        scope: The downstream resource scope (e.g. `"api://eln/.default"`).
        transport: Injected in tests to drive the exchange against a fake endpoint; `None` in prod.

    Returns:
        A downstream access token carrying the user's identity.

    Raises:
        OboExchangeError: When OBO is disabled or the token endpoint rejects the exchange.
    """
    if not settings.entra_obo_enabled:
        raise OboExchangeError("on-behalf-of exchange is disabled")
    data = {
        "grant_type": _OBO_GRANT,
        "client_id": settings.entra_workload_client_id,
        "client_assertion_type": _JWT_BEARER,
        "client_assertion": read_sa_token(),
        "assertion": user_token,
        "scope": scope,
        "requested_token_use": "on_behalf_of",
    }
    async with httpx.AsyncClient(
        transport=transport, timeout=settings.entra_http_timeout_seconds
    ) as client:
        response = await client.post(settings.entra_token_endpoint, data=data)
    if response.status_code != httpx.codes.OK:
        raise OboExchangeError(f"obo exchange failed: {response.status_code} {response.text}")
    access_token = response.json().get("access_token")
    if not access_token:
        raise OboExchangeError("obo endpoint returned no access_token")
    return str(access_token)
