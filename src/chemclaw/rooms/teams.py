"""The Teams side of the bridge: what an activity says, who sent it, and the bot's replies.

Invariants:
- An activity is acted on only after `ChannelVerifier` has checked the Bot Framework token that
  carried it: signature, issuer, audience (this bot) and its `serviceurl` claim against the
  activity's own. Anything else is anyone with the URL.
- A sender is named only by the Entra object id and tenant Teams put on the activity. No id or a
  foreign tenant is `UnidentifiedSender`, and nothing runs on it.
- A reply goes only to a `serviceUrl` on the configured host list, because the reply carries the
  bot's token. That token reaches Teams and nothing else: its scope is the Bot Framework's.
"""

import logging
import os
import re
import time
import uuid
from collections.abc import Callable
from typing import Any
from urllib.parse import quote, urlsplit

import httpx
import jwt
from pydantic import BaseModel, ConfigDict, Field

from chemclaw.core.http import is_loopback_host
from chemclaw.rooms.identity import RoomSender

logger = logging.getLogger(__name__)

_MENTION = re.compile(r"<at>.*?</at>", re.IGNORECASE | re.DOTALL)
_OAUTH_CARD = "application/vnd.microsoft.card.oauth"


class ChannelAuthError(Exception):
    """The Bot Framework token that carried an activity did not verify."""


class UnidentifiedSender(Exception):
    """The activity does not name a person of the configured tenant; `reason` is room-safe."""

    def __init__(self, reason: str) -> None:
        """Carry the sentence the room is told."""
        super().__init__(reason)
        self.reason = reason


class _Wire(BaseModel):
    """Teams sends more than the bridge reads; what it does not read is ignored, not refused."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)


class ChannelAccount(_Wire):
    """A user or bot on an activity."""

    id: str = ""
    name: str = ""
    aad_object_id: str = Field(default="", alias="aadObjectId")


class ConversationAccount(_Wire):
    """The chat, channel or thread an activity belongs to."""

    id: str = ""
    tenant_id: str = Field(default="", alias="tenantId")


class Activity(_Wire):
    """The fields of a Bot Framework activity the bridge reads."""

    type: str = ""
    id: str = ""
    name: str = ""
    text: str = ""
    service_url: str = Field(default="", alias="serviceUrl")
    sender: ChannelAccount = Field(default_factory=ChannelAccount, alias="from")
    conversation: ConversationAccount = Field(default_factory=ConversationAccount)
    channel_data: dict[str, Any] = Field(default_factory=dict, alias="channelData")
    value: dict[str, Any] | None = None


def message_text(activity: Activity) -> str:
    """The words a person typed: the activity's text without the `<at>Bot</at>` mention markup."""
    return _MENTION.sub("", activity.text).strip()


def room_of(activity: Activity) -> str:
    """The channel or chat a thread belongs to: the conversation id without its thread suffix."""
    return activity.conversation.id.split(";", 1)[0]


def sender_of(activity: Activity, *, tenant_id: str) -> RoomSender:
    """The person who sent `activity`, or `UnidentifiedSender`.

    The tenant is read from both places Teams states it, which must agree and must be
    `tenant_id`; the person is the activity's Entra object id.
    """
    stated = {
        value
        for value in (
            str((activity.channel_data.get("tenant") or {}).get("id", "")),
            activity.conversation.tenant_id,
        )
        if value
    }
    if stated != {tenant_id}:
        raise UnidentifiedSender("ChemClaw in Teams serves one organisation only.")
    if not activity.sender.aad_object_id:
        raise UnidentifiedSender("I cannot tell who you are from this message, so I did nothing.")
    return RoomSender(
        tenant_id=tenant_id, object_id=activity.sender.aad_object_id, name=activity.sender.name
    )


class ChannelVerifier:
    """Checks an inbound activity's Bot Framework token against Bot Framework's published keys."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        metadata_url: str,
        issuer: str,
        audience: str,
        cache_seconds: float,
        refresh_cooldown_seconds: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Bind the metadata URL, the issuer and audience a token must carry, and cache times."""
        self._client = client
        self._metadata_url = metadata_url
        self._issuer = issuer
        self._audience = audience
        self._cache_seconds = cache_seconds
        self._cooldown = refresh_cooldown_seconds
        self._clock = clock
        self._keys: dict[str, Any] = {}
        self._fetched_at: float | None = None

    async def verify(self, authorization: str | None, activity: Activity) -> None:
        """Return when the header carries a valid token for this bot and this activity's service."""
        if not authorization or not authorization.startswith("Bearer "):
            raise ChannelAuthError("no bearer token")
        token = authorization[len("Bearer ") :]
        try:
            kid = jwt.get_unverified_header(token).get("kid")
        except jwt.PyJWTError as exc:
            raise ChannelAuthError("malformed token") from exc
        key = await self._key(str(kid or ""))
        try:
            claims = jwt.decode(
                token,
                key,
                algorithms=["RS256"],
                audience=self._audience,
                issuer=self._issuer,
                options={"require": ["exp", "iss", "aud"]},
            )
        except jwt.InvalidTokenError as exc:
            raise ChannelAuthError(f"invalid token: {exc}") from exc
        claimed = str(claims.get("serviceurl", "")).rstrip("/")
        if not claimed or claimed != activity.service_url.rstrip("/"):
            raise ChannelAuthError("the token's serviceurl is not the activity's")

    async def _key(self, kid: str) -> Any:
        """The public key for `kid`, fetching the key set when it is old or the id is new."""
        age = None if self._fetched_at is None else self._clock() - self._fetched_at
        if (
            age is None
            or age > self._cache_seconds
            or (kid not in self._keys and age >= self._cooldown)
        ):
            await self._refresh()
        try:
            return self._keys[kid]
        except KeyError as exc:
            raise ChannelAuthError(f"no signing key {kid!r}") from exc

    async def _refresh(self) -> None:
        """Fetch the metadata, then the key set it names; a failure leaves the old keys in place."""
        try:
            metadata = (await self._client.get(self._metadata_url)).json()
            jwks = (await self._client.get(str(metadata["jwks_uri"]))).json()
            keys = {
                str(entry["kid"]): jwt.PyJWK.from_dict(entry).key for entry in jwks.get("keys", [])
            }
        except (httpx.HTTPError, ValueError, KeyError, jwt.PyJWTError) as exc:
            logger.warning(
                "Bot Framework signing keys could not be fetched: %s", type(exc).__name__
            )
            self._fetched_at = self._clock()
            return
        self._keys = keys
        self._fetched_at = self._clock()


class BotConnector:
    """The bot's replies into Teams, sent with the bot's own token under the Bot Framework scope.

    This credential posts in a Teams conversation as the bot and is good for nothing at ChemClaw:
    its scope is `connector_scope`, never the API scope a sender's token carries.
    """

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        token_endpoint: str,
        app_id: str,
        secret_env: str,
        scope: str,
        service_hosts: list[str],
        clock: Callable[[], float] = time.time,
    ) -> None:
        """Bind the token endpoint, the bot app, its secret's variable name and the hosts."""
        self._client = client
        self._endpoint = token_endpoint
        self._app_id = app_id
        self._secret_env = secret_env
        self._scope = scope
        self._hosts = service_hosts
        self._clock = clock
        self._token: tuple[str, float] | None = None

    def _permitted(self, service_url: str) -> bool:
        """Whether `service_url` is https (or loopback) on a listed host or a subdomain of one."""
        try:
            parts = urlsplit(service_url)
            host = (parts.hostname or "").lower()
        except ValueError:
            return False
        if parts.scheme != "https" and not (parts.scheme == "http" and is_loopback_host(host)):
            return False
        return any(host == allowed or host.endswith(f".{allowed}") for allowed in self._hosts)

    async def _bot_token(self) -> str:
        """The bot's client-credentials token for the Bot Framework, cached until near expiry."""
        if self._token is not None and self._token[1] - 60 > self._clock():
            return self._token[0]
        response = await self._client.post(
            self._endpoint,
            data={
                "grant_type": "client_credentials",
                "client_id": self._app_id,
                "client_secret": os.environ.get(self._secret_env, ""),
                "scope": self._scope,
            },
        )
        response.raise_for_status()
        body = response.json()
        self._token = (str(body["access_token"]), self._clock() + float(body["expires_in"]))
        return self._token[0]

    async def send(self, service_url: str, conversation_id: str, activity: dict[str, Any]) -> bool:
        """Post `activity` into the conversation; `False` when it could not be sent.

        Never raises: a reply that cannot be delivered is logged, because the turn it describes
        is not the room's to fail.
        """
        if not self._permitted(service_url):
            logger.error("refusing to send a reply to the unlisted service URL host")
            return False
        conversation = quote(conversation_id, safe="")
        url = f"{service_url.rstrip('/')}/v3/conversations/{conversation}/activities"
        try:
            token = await self._bot_token()
            response = await self._client.post(
                url, json=activity, headers={"Authorization": f"Bearer {token}"}
            )
            response.raise_for_status()
        except (httpx.HTTPError, KeyError, ValueError) as exc:
            logger.warning("a reply could not be delivered to Teams: %s", type(exc).__name__)
            return False
        return True


def text_activity(text: str, *, reply_to: str = "") -> dict[str, Any]:
    """A markdown message activity, threaded under `reply_to` when given."""
    activity: dict[str, Any] = {"type": "message", "text": text, "textFormat": "markdown"}
    if reply_to:
        activity["replyToId"] = reply_to
    return activity


def sign_in_activity(
    *, connection_name: str, resource_uri: str, reply_to: str = ""
) -> dict[str, Any]:
    """An OAuth card asking Teams for the person's silent sign-in token for this bot's API."""
    card = {
        "contentType": _OAUTH_CARD,
        "content": {
            "text": "Sign in to ChemClaw so your messages run as you.",
            "connectionName": connection_name,
            "tokenExchangeResource": {"id": uuid.uuid4().hex, "uri": resource_uri},
        },
    }
    activity: dict[str, Any] = {"type": "message", "attachments": [card]}
    if reply_to:
        activity["replyToId"] = reply_to
    return activity
