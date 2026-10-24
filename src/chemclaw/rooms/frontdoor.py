"""ChemClaw's front door as the bridge uses it: five routes, each called with one person's token.

Invariant: every method takes the `AccessToken` it sends, so there is no call the bridge can make
that is not on somebody's behalf, and none that falls back to a credential of its own.
"""

import json
import logging
from collections.abc import AsyncIterator
from typing import Any

import httpx

from chemclaw.rooms.identity import AccessToken

logger = logging.getLogger(__name__)

# A token no tenant issued: a deployment that answers it with anything but 401 is not checking
# tokens, and every message the bridge posted would run as the dev principal.
_BOGUS_BEARER = "Bearer not-a-token"


class FrontDoorRefused(Exception):
    """The front door answered a bridge request with a refusal; `status` is its HTTP status."""

    def __init__(self, status: int, code: str = "") -> None:
        """Carry the status and the `code` the answer named, when it named one."""
        super().__init__(f"front door answered {status} {code}".strip())
        self.status = status
        self.code = code


def _bearer(token: AccessToken) -> dict[str, str]:
    """The Authorization header for `token`."""
    return {"Authorization": f"Bearer {token.value}"}


def _refusal(response: httpx.Response) -> FrontDoorRefused:
    """The refusal a non-success response is, with the `code` its JSON detail names."""
    code = ""
    try:
        detail = response.json().get("detail")
    except ValueError:
        detail = None
    if isinstance(detail, dict):
        code = str(detail.get("code", ""))
    return FrontDoorRefused(response.status_code, code)


class FrontDoor:
    """The bridge's client of the ChemClaw API; the `httpx` client carries the base URL."""

    def __init__(self, client: httpx.AsyncClient, *, stream_timeout: httpx.Timeout) -> None:
        """Bind the client, and the timeout a message's stream is read under."""
        self._client = client
        self._stream_timeout = stream_timeout

    async def enforces_authentication(self) -> bool:
        """Whether the deployment turns an invalid token away (401), which the bridge needs."""
        response = await self._client.get("/sessions", headers={"Authorization": _BOGUS_BEARER})
        return response.status_code == 401

    async def create_session(self, token: AccessToken) -> str:
        """A new session owned by the token's person."""
        response = await self._client.post("/sessions", headers=_bearer(token))
        if response.status_code != 200:
            raise _refusal(response)
        return str(response.json()["session_id"])

    async def can_reach(self, token: AccessToken, session_id: str) -> bool:
        """Whether the token's person is the session's owner or a member."""
        response = await self._client.get(f"/sessions/{session_id}/members", headers=_bearer(token))
        if response.status_code == 200:
            return True
        if response.status_code in {403, 404}:
            return False
        raise _refusal(response)

    async def waiting(self, token: AccessToken, session_id: str) -> int:
        """How many messages wait in the session's line."""
        response = await self._client.get(f"/sessions/{session_id}/queue", headers=_bearer(token))
        if response.status_code != 200:
            raise _refusal(response)
        return len(response.json().get("waiting", []))

    async def post_message(
        self, token: AccessToken, session_id: str, text: str
    ) -> AsyncIterator[dict[str, Any]]:
        """Post `text` to the session as the token's person and yield the turn's events."""
        async with self._client.stream(
            "POST",
            f"/sessions/{session_id}/messages",
            json={"message": text},
            headers=_bearer(token),
            timeout=self._stream_timeout,
        ) as response:
            if response.status_code != 200:
                await response.aread()
                raise _refusal(response)
            async for line in response.aiter_lines():
                if not line.startswith("data:"):
                    continue
                try:
                    event = json.loads(line.removeprefix("data:"))
                except ValueError:
                    continue
                if isinstance(event, dict):
                    yield event
