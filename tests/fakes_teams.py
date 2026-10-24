"""A stand-in Teams and Entra for the room bridge's tests: one `httpx` transport, three hosts.

- `login.test`: Entra's token endpoint. It honours the on-behalf-of grant the way the real one does
  (it needs the sender's own assertion, and mints a token for *that* person, with that person's
  roles), serves the bot's client-credentials token for the Bot Framework scope only, and refuses a
  client-credentials grant for ChemClaw's API, as an app registration with no app role is refused.
- `channel.test`: Bot Framework's OpenID metadata and signing keys.
- `smba.test`: the Teams connector, recording every reply the bridge sends.

The ChemClaw front door is never faked: the tests run the real app and let it validate the minted
tokens, so "posted as its sender" is read from inside the turn.
"""

import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs

import httpx
import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

TENANT = "tenant-acme"
BOT_APP = "bot-app-id"
BOT_SECRET_ENV = "TEST_TEAMS_BOT_CREDENTIAL"
BOT_SECRET = "bot-secret-value"
API_SCOPE = "api://chemclaw-teams/access_as_user"
API_AUDIENCE = "api://chemclaw-teams"
API_ISSUER = "https://issuer.test/teams/v2.0"
CONNECTOR_SCOPE = "https://api.botframework.com/.default"
CHANNEL_ISSUER = "https://api.botframework.com"
SERVICE_URL = "https://smba.test/emea/"
CONNECTION = "chemclaw-sso"
SSO_RESOURCE = f"api://botid-{BOT_APP}"
TOKEN_ENDPOINT = f"https://login.test/{TENANT}/oauth2/v2.0/token"
METADATA_URL = "https://channel.test/openidconfiguration"
JWKS_URL = "https://channel.test/keys"
BOT_TOKEN = "bot-reply-token"


@dataclass(frozen=True)
class Person:
    """Someone in the fake tenant: their object id, display name and app roles."""

    oid: str
    name: str
    roles: frozenset[str] = frozenset()


def _pem(key: rsa.RSAPrivateKey) -> bytes:
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )


@dataclass
class World:
    """The fake Entra, Bot Framework and Teams connector, and everything they were asked."""

    people: dict[str, Person]
    entra_key: rsa.RSAPrivateKey = field(
        default_factory=lambda: rsa.generate_private_key(public_exponent=65537, key_size=2048)
    )
    channel_key: rsa.RSAPrivateKey = field(
        default_factory=lambda: rsa.generate_private_key(public_exponent=65537, key_size=2048)
    )
    exchanges: list[dict[str, str]] = field(default_factory=list)
    credential_grants: list[dict[str, str]] = field(default_factory=list)
    replies: list[dict[str, Any]] = field(default_factory=list)
    unlisted: list[str] = field(default_factory=list)
    minted: dict[str, str] = field(default_factory=dict)
    # A test that wants Entra to misbehave sets this: the oid every exchange is made out to.
    mint_for_instead: str | None = None

    # --- the tokens a real Teams and Entra would have issued ----------------------------------

    def sso_token(self, person: Person, *, tenant: str = TENANT) -> str:
        """What Teams hands the bot after a silent sign-in: the person's token for the bot."""
        return self._sign(
            self.entra_key,
            {
                "aud": SSO_RESOURCE,
                "iss": API_ISSUER,
                "oid": person.oid,
                "tid": tenant,
                "exp": int(time.time() + 3600),
            },
        )

    def channel_token(
        self, *, service_url: str = SERVICE_URL, audience: str = BOT_APP, lifetime: int = 3600
    ) -> str:
        """The Bot Framework token that carries an activity, signed by the channel's key."""
        return self._sign(
            self.channel_key,
            {
                "aud": audience,
                "iss": CHANNEL_ISSUER,
                "serviceurl": service_url,
                "exp": int(time.time() + lifetime),
            },
            kid="channel-key",
        )

    def _sign(self, key: rsa.RSAPrivateKey, claims: dict[str, Any], kid: str = "entra-key") -> str:
        return jwt.encode(claims, _pem(key), algorithm="RS256", headers={"kid": kid})

    def _mint(self, oid: str) -> tuple[str, int]:
        person = self.people[oid]
        token = self._sign(
            self.entra_key,
            {
                "aud": API_AUDIENCE,
                "iss": API_ISSUER,
                "oid": person.oid,
                "tid": TENANT,
                "preferred_username": f"{person.name.lower()}@acme.test",
                "roles": sorted(person.roles),
                "exp": int(time.time() + 3600),
                "jti": uuid.uuid4().hex,
            },
        )
        self.minted[token] = person.oid
        return token, 3600

    # --- the hosts ----------------------------------------------------------------------------

    def transport(self) -> httpx.MockTransport:
        """The one transport the bridge's Entra and Teams clients share."""
        return httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        if host == "login.test":
            return self._token_endpoint(request)
        if host == "channel.test":
            return self._channel_keys(request)
        if host == "smba.test":
            return self._connector(request)
        self.unlisted.append(host)
        return httpx.Response(599, json={"error": f"unexpected host {host}"})

    def _token_endpoint(self, request: httpx.Request) -> httpx.Response:
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        if form.get("client_id") != BOT_APP or form.get("client_secret") != BOT_SECRET:
            return httpx.Response(401, json={"error": "invalid_client"})
        grant = form.get("grant_type", "")
        if grant == "urn:ietf:params:oauth:grant-type:jwt-bearer":
            self.exchanges.append(form)
            if form.get("requested_token_use") != "on_behalf_of" or form.get("scope") != API_SCOPE:
                return httpx.Response(400, json={"error": "invalid_request"})
            assertion = jwt.decode(
                form["assertion"], options={"verify_signature": False}
            )  # the fake trusts what it minted; the real Entra verifies the signature
            oid = self.mint_for_instead or str(assertion["oid"])
            token, lifetime = self._mint(oid)
            return httpx.Response(200, json={"access_token": token, "expires_in": lifetime})
        if grant == "client_credentials":
            self.credential_grants.append(form)
            if form.get("scope") == CONNECTOR_SCOPE:
                return httpx.Response(200, json={"access_token": BOT_TOKEN, "expires_in": 3600})
            return httpx.Response(400, json={"error": "unauthorized_client"})
        return httpx.Response(400, json={"error": "unsupported_grant_type"})

    def _channel_keys(self, request: httpx.Request) -> httpx.Response:
        if str(request.url) == METADATA_URL:
            return httpx.Response(200, json={"jwks_uri": JWKS_URL})
        jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(self.channel_key.public_key()))
        jwk.update({"kid": "channel-key", "use": "sig", "alg": "RS256"})
        return httpx.Response(200, json={"keys": [jwk]})

    def _connector(self, request: httpx.Request) -> httpx.Response:
        if request.headers.get("Authorization") != f"Bearer {BOT_TOKEN}":
            return httpx.Response(401)
        self.replies.append(json.loads(request.content))
        return httpx.Response(200, json={"id": uuid.uuid4().hex})

    # --- reading what the room was told -------------------------------------------------------

    def room_texts(self) -> list[str]:
        """Every text message the bridge sent into Teams, in order."""
        return [str(r["text"]) for r in self.replies if "text" in r]

    def sign_in_cards(self) -> int:
        """How many sign-in cards the bridge sent."""
        return sum(1 for r in self.replies if r.get("attachments"))


def activity(
    person: Person | None,
    text: str = "",
    *,
    kind: str = "message",
    room: str = "19:room@thread.tacv2",
    tenant: str = TENANT,
    name: str = "",
    value: dict[str, Any] | None = None,
    service_url: str = SERVICE_URL,
    aad_object_id: str | None = None,
) -> dict[str, Any]:
    """A Bot Framework activity as Teams would send it for `person` (or for nobody)."""
    sender: dict[str, Any] = {"id": "29:teams-user", "name": person.name if person else ""}
    oid = aad_object_id if aad_object_id is not None else (person.oid if person else "")
    if oid:
        sender["aadObjectId"] = oid
    body: dict[str, Any] = {
        "type": kind,
        "id": uuid.uuid4().hex,
        "serviceUrl": service_url,
        "from": sender,
        "conversation": {"id": room, "tenantId": tenant},
        "channelData": {"tenant": {"id": tenant}},
        "text": text,
    }
    if name:
        body["name"] = name
    if value is not None:
        body["value"] = value
    return body
