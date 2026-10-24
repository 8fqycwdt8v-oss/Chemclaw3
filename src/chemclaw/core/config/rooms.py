"""The Teams room bridge: which app it is, which tenant it serves and where it may call.

Off by default. Every URL, identifier and scope the bridge uses is declared here; a secret is
named by its variable (`*_token_env`), never carried.
"""

from typing import Self

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings


class RoomSettings(BaseSettings):
    """A Teams channel as a surface of a shared session, each message on its sender's own token."""

    # The switch. Declaring the bridge and binding it into a deployment are separate decisions
    # (`D-2026-09-20-declaring-a-capability-and-binding-it-are-different-decisions`), so nothing
    # starts, and no port opens, until an operator sets this.
    teams_bridge_enabled: bool = False
    # The Entra application (client) id of the bot registration: the audience of every inbound Bot
    # Framework token and the client that exchanges a sender's token on their behalf.
    teams_bot_app_id: str = ""
    # NAMES the environment variable holding the bot registration's client secret. The secret signs
    # two requests only: the on-behalf-of exchange (which needs the sender's own assertion beside
    # it) and the bot's reply token (which reaches Teams, never ChemClaw).
    teams_bot_credential_token_env: str = "CHEMCLAW_TEAMS_BOT_CREDENTIAL"
    # The one Entra tenant whose people may use the bridge; an activity from another is refused.
    teams_tenant_id: str = ""
    # The Entra authority the token endpoint hangs under: `<authority>/<tenant>/oauth2/v2.0/token`.
    teams_authority_url: str = "https://login.microsoftonline.com"
    # The scope the sender's token is exchanged for: ChemClaw's own API (its `entra_audience`).
    teams_api_scope: str = ""
    # How a chemist's Teams client is asked for a silent token: the Azure Bot OAuth connection's
    # name
    # and the resource URI the bot app exposes (`api://botid-<app id>`).
    teams_sso_connection_name: str = ""
    teams_sso_resource_uri: str = ""
    # Bot Framework's published OpenID configuration, from which inbound tokens' signing keys come,
    # and the issuer and reply scope it fixes.
    teams_openid_metadata_url: str = (
        "https://login.botframework.com/v1/.well-known/openidconfiguration"
    )
    teams_channel_token_issuer: str = "https://api.botframework.com"
    teams_connector_scope: str = "https://api.botframework.com/.default"
    # Hosts a reply may be sent to (each matched exactly or as a parent domain), so an activity's
    # `serviceUrl` can never aim the bot's reply token at a host of the sender's choosing.
    teams_service_url_hosts: str = "smba.trafficmanager.net"
    # The front door this bridge posts to; a ChemClaw deployment, not a Microsoft endpoint.
    teams_api_base_url: str = "http://localhost:8080"
    # Where channel -> session bindings persist. A file on a mounted volume, not memory: a restart
    # must not unbind every room.
    teams_bindings_file: str = ""
    teams_host: str = "0.0.0.0"
    teams_port: int = Field(default=3978, gt=0)
    teams_http_timeout_seconds: float = Field(default=10.0, gt=0)
    # Read budget for one message's stream: a turn may wait in the line and then run, so it follows
    # the front door's own turn ceiling (`service_turn_timeout_seconds`) plus this slack.
    teams_stream_slack_seconds: float = Field(default=120.0, ge=0)
    # An exchanged token is dropped this long before it expires, so a message never starts a turn
    # on a credential about to lapse.
    teams_token_leeway_seconds: float = Field(default=120.0, ge=0)
    # People with a held token. A bound, since a token is a credential and the cache is memory.
    teams_token_cache_max: int = Field(default=2000, gt=0)
    teams_jwks_cache_seconds: float = Field(default=3600.0, gt=0)
    # An unknown signing-key id is caller-chosen, so keys are re-fetched for one at most this often.
    teams_jwks_refresh_cooldown_seconds: float = Field(default=60.0, ge=0)
    # The largest activity body read; a Teams activity is a few kilobytes.
    teams_activity_max_bytes: int = Field(default=262144, gt=0)
    # Messages being answered at once; a further one is told the room is busy rather than queued
    # here, since the line that matters is the session's.
    teams_max_inflight: int = Field(default=50, gt=0)
    teams_reply_max_chars: int = Field(default=20000, gt=0)

    @property
    def teams_service_url_host_list(self) -> list[str]:
        """The reply host allowlist, split and lower-cased."""
        return [h.strip().lower() for h in self.teams_service_url_hosts.split(",") if h.strip()]

    @property
    def teams_token_endpoint(self) -> str:
        """The tenant's v2.0 token endpoint, derived from the authority and tenant."""
        return f"{self.teams_authority_url.rstrip('/')}/{self.teams_tenant_id}/oauth2/v2.0/token"

    @model_validator(mode="after")
    def _an_enabled_bridge_is_complete(self) -> Self:
        """An enabled bridge names its app, tenant, scope, SSO resource and a bindings file.

        Refused at start rather than discovered by the first sender: each is a value no default can
        supply, and a bridge missing one would refuse every message with a reason nobody sees.
        """
        if not self.teams_bridge_enabled:
            return self
        missing = [
            name
            for name in (
                "teams_bot_app_id",
                "teams_tenant_id",
                "teams_api_scope",
                "teams_sso_connection_name",
                "teams_sso_resource_uri",
                "teams_bindings_file",
            )
            if not getattr(self, name)
        ]
        if missing:
            raise ValueError(f"teams_bridge_enabled needs {', '.join(missing)} to be set")
        return self
