"""Whose token a room message travels on: the sender's own, exchanged for them, never shared.

A chat room is not a principal. Every message reaches ChemClaw as its sender, on a token Entra
issued *for that person*, so roles, gates, spend and memory are theirs
(`D-2026-09-27-in-a-shared-session-the-sender-governs`).

Invariants:
- A token is held under the (tenant, object id) it was issued to, and `TokenCache.get` returns only
  that person's; there is no lookup without a sender.
- The exchange takes the sender's own assertion beside the bridge's client secret. The bridge holds
  no grant that mints a ChemClaw token without one, and the exchanged token's `oid` and `tid` are
  checked against the sender before it is cached, so a token for somebody else is refused here.
- Claims are read unverified, as a consistency check only: Entra verifies the assertion on the
  exchange and ChemClaw verifies the resulting token on every request.
"""

import logging
import os
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx
import jwt

logger = logging.getLogger(__name__)

_OBO_GRANT = "urn:ietf:params:oauth:grant-type:jwt-bearer"


class ExchangeRefused(Exception):
    """A sender's token could not be exchanged; `reason` is safe to show in a room."""

    def __init__(self, reason: str, *, consent_required: bool = False) -> None:
        """Carry the room-safe sentence, and whether an administrator's consent would fix it."""
        super().__init__(reason)
        self.reason = reason
        self.consent_required = consent_required


@dataclass(frozen=True)
class RoomSender:
    """A person as the room names them: tenant, Entra object id and display name."""

    tenant_id: str
    object_id: str
    name: str = ""

    @property
    def key(self) -> tuple[str, str]:
        """The identity a token is held under."""
        return (self.tenant_id, self.object_id)


@dataclass(frozen=True)
class AccessToken:
    """A bearer token for ChemClaw's API, and when it stops being valid (epoch seconds)."""

    value: str = field(repr=False)
    expires_at: float


class TokenExchanger(Protocol):
    """Turns a sender's own assertion into a token for ChemClaw's API, as that sender."""

    async def exchange(self, sender: RoomSender, assertion: str) -> AccessToken:
        """The token Entra issues `sender` for ChemClaw, or `ExchangeRefused`."""
        ...


def _unverified(token: str) -> dict[str, Any]:
    """A JWT's claims without checking its signature; `{}` for anything that is not a JWT."""
    try:
        claims = jwt.decode(token, options={"verify_signature": False})
    except jwt.PyJWTError:
        return {}
    return claims if isinstance(claims, dict) else {}


def _belongs_to(token: str, sender: RoomSender) -> bool:
    """Whether the token's own `oid` and `tid` name `sender`."""
    claims = _unverified(token)
    return claims.get("oid") == sender.object_id and claims.get("tid") == sender.tenant_id


class EntraOnBehalfOf:
    """The real exchange: Entra's on-behalf-of grant, one POST to the tenant's token endpoint.

    Plain HTTP rather than a client library because the grant is one form post and the library
    would also bring a token cache that outlives the sender's room session. The client secret is
    read from the environment variable `secret_env` names on every call, so a rotated secret is
    live at once and no file or setting carries it.
    """

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        token_endpoint: str,
        app_id: str,
        secret_env: str,
        scope: str,
        clock: Callable[[], float] = time.time,
    ) -> None:
        """Bind the endpoint, the bot app, the secret's variable name and the target scope."""
        self._client = client
        self._endpoint = token_endpoint
        self._app_id = app_id
        self._secret_env = secret_env
        self._scope = scope
        self._clock = clock

    async def exchange(self, sender: RoomSender, assertion: str) -> AccessToken:
        """Exchange `assertion` (the sender's own token for this bot) for a ChemClaw token."""
        if not _belongs_to(assertion, sender):
            raise ExchangeRefused("The sign-in token is not for the person who sent the message.")
        secret = os.environ.get(self._secret_env, "")
        if not secret:
            logger.error("the bot credential variable %s is empty", self._secret_env)
            raise ExchangeRefused("This bridge is not configured to sign anyone in.")
        try:
            response = await self._client.post(
                self._endpoint,
                data={
                    "grant_type": _OBO_GRANT,
                    "client_id": self._app_id,
                    "client_secret": secret,
                    "assertion": assertion,
                    "scope": self._scope,
                    "requested_token_use": "on_behalf_of",
                },
            )
        except httpx.HTTPError as exc:
            logger.warning(
                "the on-behalf-of exchange could not reach Entra: %s", type(exc).__name__
            )
            raise ExchangeRefused("Sign-in is unavailable right now; try again shortly.") from exc
        body = _json(response)
        if response.status_code != 200:
            error = str(body.get("error", ""))
            logger.info("on-behalf-of exchange refused: %s", error)
            consent = error in {"interaction_required", "consent_required"}
            raise ExchangeRefused(
                "ChemClaw needs an administrator's consent before it can act for you in Teams."
                if consent
                else "Entra refused to sign you in to ChemClaw.",
                consent_required=consent,
            )
        value = body.get("access_token")
        lifetime = body.get("expires_in")
        if not isinstance(value, str) or not isinstance(lifetime, int | float):
            raise ExchangeRefused("Entra's answer was not a token.")
        if not _belongs_to(value, sender):
            logger.error(
                "an exchange for %s returned a token naming someone else", sender.object_id
            )
            raise ExchangeRefused("Entra returned a token for someone else; it was discarded.")
        return AccessToken(value=value, expires_at=self._clock() + float(lifetime))


def _json(response: httpx.Response) -> dict[str, Any]:
    """The response body as an object, `{}` when it is not one."""
    try:
        body = response.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


class TokenCache:
    """The tokens this process holds, one per person, bounded and dropped before they expire.

    Memory only: a token is a credential, and a restart costing each person one silent sign-in is
    cheaper than a credential at rest.
    """

    def __init__(
        self,
        *,
        max_entries: int,
        leeway_seconds: float,
        clock: Callable[[], float] = time.time,
    ) -> None:
        """Hold at most `max_entries` tokens, each unusable `leeway_seconds` before its expiry."""
        self._max = max_entries
        self._leeway = leeway_seconds
        self._clock = clock
        self._held: OrderedDict[tuple[str, str], AccessToken] = OrderedDict()

    def put(self, sender: RoomSender, token: AccessToken) -> None:
        """Hold `token` as `sender`'s, evicting the longest-held person's when full."""
        self._held[sender.key] = token
        self._held.move_to_end(sender.key)
        while len(self._held) > self._max:
            self._held.popitem(last=False)

    def get(self, sender: RoomSender) -> AccessToken | None:
        """`sender`'s own token while it is usable, else `None` (and it is forgotten)."""
        token = self._held.get(sender.key)
        if token is None:
            return None
        if token.expires_at - self._leeway <= self._clock():
            del self._held[sender.key]
            return None
        return token

    def drop(self, sender: RoomSender) -> None:
        """Forget `sender`'s token, for one the front door refused."""
        self._held.pop(sender.key, None)

    def __len__(self) -> int:
        """How many people's tokens are held."""
        return len(self._held)
