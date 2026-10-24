"""Run the Teams bridge: build it from configuration, refuse an unsafe start, serve `/api/messages`.

`build_bridge` is the one place settings become collaborators, so a test builds the same object
with fake transports. `main` refuses to start when the bridge is off, and when the front door it
would post to does not check tokens, since a deployment that accepts any bearer would run every
message as one stand-in principal.
"""

import asyncio
import json
import logging
from typing import Any

import httpx
import uvicorn
from fastapi import FastAPI, Request, Response

from chemclaw.core.asgi import NO_FRAMEWORK_TELEMETRY, transport_bounds
from chemclaw.core.config import Settings, settings
from chemclaw.core.logging import configure_logging
from chemclaw.rooms.bindings import BindingStore
from chemclaw.rooms.bridge import Bridge, BridgeConfig
from chemclaw.rooms.frontdoor import FrontDoor
from chemclaw.rooms.identity import EntraOnBehalfOf, TokenCache
from chemclaw.rooms.teams import BotConnector, ChannelVerifier

logger = logging.getLogger(__name__)


def build_bridge(
    config: Settings,
    *,
    entra: httpx.AsyncClient,
    teams: httpx.AsyncClient,
    api: httpx.AsyncClient,
) -> Bridge:
    """The bridge `config` describes, over three HTTP clients: Entra, Teams and the front door.

    Kept separate so each destination is one client with one decision about where it may go, and a
    test can stand a fake behind any of them.
    """
    stream_timeout = httpx.Timeout(
        config.teams_http_timeout_seconds,
        read=config.service_turn_timeout_seconds + config.teams_stream_slack_seconds,
    )
    return Bridge(
        verifier=ChannelVerifier(
            teams,
            metadata_url=config.teams_openid_metadata_url,
            issuer=config.teams_channel_token_issuer,
            audience=config.teams_bot_app_id,
            cache_seconds=config.teams_jwks_cache_seconds,
            refresh_cooldown_seconds=config.teams_jwks_refresh_cooldown_seconds,
        ),
        exchanger=EntraOnBehalfOf(
            entra,
            token_endpoint=config.teams_token_endpoint,
            app_id=config.teams_bot_app_id,
            secret_env=config.teams_bot_credential_token_env,
            scope=config.teams_api_scope,
        ),
        tokens=TokenCache(
            max_entries=config.teams_token_cache_max,
            leeway_seconds=config.teams_token_leeway_seconds,
        ),
        bindings=BindingStore(config.teams_bindings_file),
        front_door=FrontDoor(api, stream_timeout=stream_timeout),
        connector=BotConnector(
            teams,
            token_endpoint=config.teams_token_endpoint,
            app_id=config.teams_bot_app_id,
            secret_env=config.teams_bot_credential_token_env,
            scope=config.teams_connector_scope,
            service_hosts=config.teams_service_url_host_list,
        ),
        config=BridgeConfig(
            tenant_id=config.teams_tenant_id,
            sso_connection_name=config.teams_sso_connection_name,
            sso_resource_uri=config.teams_sso_resource_uri,
            max_inflight=config.teams_max_inflight,
            reply_max_chars=config.teams_reply_max_chars,
        ),
    )


def create_app(bridge: Bridge, *, max_body_bytes: int) -> FastAPI:
    """The ASGI app Bot Framework posts activities to; `bridge` decides what each one means."""
    app = FastAPI(
        title="chemclaw-teams-bridge",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        telemetry=NO_FRAMEWORK_TELEMETRY,
    )

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        """Liveness: the process is serving."""
        return {"status": "ok"}

    @app.post("/api/messages")
    async def messages(request: Request) -> Response:
        """One Bot Framework activity; answered at once, its turn continues in the background."""
        body = await request.body()
        if len(body) > max_body_bytes:
            return Response(status_code=413)
        try:
            raw: Any = json.loads(body)
        except ValueError:
            return Response(status_code=400)
        if not isinstance(raw, dict):
            return Response(status_code=400)
        reply = await bridge.handle(request.headers.get("Authorization"), raw)
        if reply.body is None:
            return Response(status_code=reply.status)
        return Response(
            content=json.dumps(reply.body), status_code=reply.status, media_type="application/json"
        )

    return app


async def refuse_unenforcing_front_door(front_door: FrontDoor) -> None:
    """Stop the bridge from starting against a front door that does not check tokens."""
    if not await front_door.enforces_authentication():
        raise SystemExit(
            "The ChemClaw front door at CHEMCLAW_TEAMS_API_BASE_URL does not turn an invalid "
            "token away (CHEMCLAW_ENTRA_REQUIRED is false there), so every message would run as "
            "one stand-in principal. The bridge will not start."
        )


async def _serve() -> None:
    """Build the bridge from `settings`, check the front door, and serve until stopped."""
    timeout = settings.teams_http_timeout_seconds
    clients = {
        "entra": httpx.AsyncClient(timeout=timeout, trust_env=False),
        "teams": httpx.AsyncClient(timeout=timeout, trust_env=False),
        "api": httpx.AsyncClient(
            base_url=settings.teams_api_base_url, timeout=timeout, trust_env=False
        ),
    }
    bridge = build_bridge(
        settings, entra=clients["entra"], teams=clients["teams"], api=clients["api"]
    )
    try:
        await refuse_unenforcing_front_door(bridge.front_door)
        server = uvicorn.Server(
            uvicorn.Config(
                create_app(bridge, max_body_bytes=settings.teams_activity_max_bytes),
                host=settings.teams_host,
                port=settings.teams_port,
                log_config=None,
                **transport_bounds(),
            )
        )
        await server.serve()
    finally:
        await bridge.drain()
        for client in clients.values():
            await client.aclose()


def main() -> None:
    """The process entry: refuse unless the bridge is enabled, then serve."""
    configure_logging()
    if not settings.teams_bridge_enabled:
        raise SystemExit(
            "The Teams bridge is off. Set CHEMCLAW_TEAMS_BRIDGE_ENABLED=true and the values "
            "docs/guides/teams-room-bridge.md names; nothing is started by default."
        )
    asyncio.run(_serve())
