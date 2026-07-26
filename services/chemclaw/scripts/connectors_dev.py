"""Run every enabled local connector in one process — the dev loop for the connector topology.

In a cluster each connector is its own Deployment on its own port, which is the point: independent
scaling, independent failure, independent dependency sets. On a laptop that same topology means N
terminals and N ports, which is friction with no benefit while writing code — so this mounts each
enabled bundle's FastAPI app under `/<name>` of one uvicorn process.

The addresses stay honest either way: a bundle's manifest ships its own per-connector loopback URL,
and this runner is reached by pointing `CHEMCLAW_CONNECTOR_URLS` at the composite instead — the same
override a cluster uses for its Service addresses, so the dev path exercises the production
indirection rather than a special case. The runner prints the exact JSON to set.

Only bundles with a *local* app are mounted: a connector whose server we do not own (a third-party
MCP endpoint) has nothing to run here, and one is skipped rather than reported as broken.
"""

import importlib
import json
import logging
import sys
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager

import uvicorn
from fastapi import FastAPI

from chemclaw.config import settings
from chemclaw.logging import configure_logging
from connectors.registry import enabled

logger = logging.getLogger(__name__)

# Where the composite listens. A dev-only affordance, so it is a module constant rather than a
# config field: nothing in a deployment reads it, and inventing a setting for it would be config for
# its own sake (the "config, never magic numbers" rule is about values a *deployment* varies).
DEV_HOST = "127.0.0.1"
DEV_PORT = 8810


def _local_app(name: str) -> FastAPI | None:
    """Import a bundle's `server/app.py::app`, or `None` when the bundle ships no local server."""
    try:
        module = importlib.import_module(f"connectors.{name}.server.app")
    except ModuleNotFoundError:
        return None
    app = getattr(module, "app", None)
    if not isinstance(app, FastAPI):
        logger.warning("connector %s: server.app exports no FastAPI `app`; skipping", name)
        return None
    return app


def build_composite() -> tuple[FastAPI, dict[str, str]]:
    """Mount every enabled local connector under `/<name>`; report the URLs they are reached at.

    Returns:
        The composite app, and the `connector_urls` mapping that points core at it — printed by
        `main` so the value can be copied straight into `.env`.
    """
    mounted: list[FastAPI] = []
    urls: dict[str, str] = {}

    @asynccontextmanager
    async def lifespan(_composite: FastAPI) -> AsyncIterator[None]:
        """Run every mounted app's own lifespan for the composite's lifetime.

        Starlette does **not** run a mounted sub-app's lifespan, and a connector app's lifespan is
        what starts its MCP session manager — so without this the composite would accept connections
        and then fail every MCP handshake. Entering them here is the whole reason this function
        returns an app rather than just mounting onto a bare `FastAPI`.
        """
        async with AsyncExitStack() as stack:
            for app in mounted:
                await stack.enter_async_context(app.router.lifespan_context(app))
            yield

    composite = FastAPI(title="chemclaw-connectors-dev", lifespan=lifespan)
    for manifest in enabled():
        app = _local_app(manifest.name)
        if app is None:
            continue
        mounted.append(app)
        composite.mount(f"/{manifest.name}", app)
        urls[manifest.name] = f"http://{DEV_HOST}:{DEV_PORT}/{manifest.name}/mcp"
    return composite, urls


def main() -> None:
    """Serve every enabled local connector on one port, printing the override core needs."""
    configure_logging()
    composite, urls = build_composite()
    if not urls:
        print("no enabled connector ships a local server — nothing to run", file=sys.stderr)
        sys.exit(1)
    print(f"serving {len(urls)} connector(s) on http://{DEV_HOST}:{DEV_PORT}")
    print("point core at them with:")
    print(f"  CHEMCLAW_CONNECTOR_URLS='{json.dumps(urls, separators=(',', ':'))}'")
    uvicorn.run(composite, host=DEV_HOST, port=DEV_PORT, log_level=settings.log_level.lower())


if __name__ == "__main__":
    main()
