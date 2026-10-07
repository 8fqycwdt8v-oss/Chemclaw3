"""Run every enabled local connector in one process — the dev loop for the connector topology.

In a cluster each connector is its own Deployment and port; locally this mounts each enabled
bundle's FastAPI app under `/<name>` of one uvicorn process. Core reaches it through
`CHEMCLAW_CONNECTOR_URLS` — the same override a cluster uses — and the runner prints the JSON to
set. Bundles without a local app (third-party endpoints) are skipped.

Every locally served bundle declares `auth: mode: bearer`, so `ensure_dev_tokens` mints a random
token per bundle the environment does not set, and `--export-env` prints them as shell exports so a
core process started elsewhere (`infra/live/processes.sh`) can use the same values. There is no
default token: a fixed dev credential in the tree eventually reaches a deployment.
"""

import argparse
import importlib
import json
import logging
import os
import secrets
import shlex
import sys
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager

import uvicorn
from fastapi import FastAPI

from chemclaw.connectors.manifest import BearerAuth, HttpEndpoint
from chemclaw.connectors.registry import enabled
from chemclaw.core.config import settings
from chemclaw.core.logging import configure_logging

logger = logging.getLogger(__name__)

# Where the composite listens; a dev-only constant, since no deployment varies it.
DEV_HOST = "127.0.0.1"
DEV_PORT = 8810


def _local_app(name: str) -> FastAPI | None:
    """The `app` from a bundle's `connectors/<name>/server/app.py`, or `None` if it ships none."""
    try:
        module = importlib.import_module(f"chemclaw.connectors.{name}.server.app")
    except ModuleNotFoundError:
        return None
    app = getattr(module, "app", None)
    if not isinstance(app, FastAPI):
        logger.warning("connector %s: server.app exports no FastAPI `app`; skipping", name)
        return None
    return app


def bearer_token_envs() -> dict[str, str]:
    """The `/mcp` credential variable of every bundle *this runner serves*, keyed by connector name.

    Read off the manifests, so it follows them. Only locally served bundles: `chem` and `safety`
    credentials belong to `Chemclaw3-mcp`, and inventing one would turn a clear
    `MissingConnectorCredential` into an opaque 401.
    """
    return {
        manifest.name: manifest.endpoint.auth.token_env
        for manifest in enabled()
        if isinstance(manifest.endpoint, HttpEndpoint)
        and isinstance(manifest.endpoint.auth, BearerAuth)
        and _local_app(manifest.name) is not None
    }


def ensure_dev_tokens() -> tuple[dict[str, str], frozenset[str]]:
    """Fill in a random token for every credential variable the environment does not already set.

    Minted, never defaulted: a constant would be a public password that looks like a control.
    Existing values are kept, so a caller can choose the secret for both processes. Which were
    already set is returned, because after this writes `os.environ` a minted token and an operator's
    are indistinguishable, and operator tokens must not be echoed.

    Returns:
        Every credential variable and its value, and the subset that was already set.
    """
    resolved: dict[str, str] = {}
    preexisting: set[str] = set()
    for env_var in sorted(set(bearer_token_envs().values())):
        existing = os.environ.get(env_var)
        if existing:
            preexisting.add(env_var)
        token = existing or secrets.token_urlsafe(24)
        os.environ[env_var] = token
        resolved[env_var] = token
    return resolved, frozenset(preexisting)


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

        Starlette does not run a mounted sub-app's lifespan, and a connector app's lifespan starts
        its MCP session manager; without this every MCP handshake would fail.
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


def _export_lines(
    urls: dict[str, str], tokens: dict[str, str], preexisting: frozenset[str] = frozenset()
) -> list[str]:
    """Everything a *separate* core process needs in order to reach and authenticate to these apps.

    One function, so the banner and the `eval`-able output cannot disagree. `preexisting` names
    operator-supplied credentials, whose values are masked: only tokens this process minted are
    printed. `--export-env` passes none, so it prints every real value.
    """
    shown = {
        name: "<already set in your environment>" if name in preexisting else value
        for name, value in tokens.items()
    }
    values = {"CHEMCLAW_CONNECTOR_URLS": json.dumps(urls, separators=(",", ":")), **shown}
    # `shlex.quote`: an operator-supplied value may contain a quote, which would split the
    # credential into shell words the caller then `eval`s.
    return [f"export {name}={shlex.quote(value)}" for name, value in sorted(values.items())]


def main(argv: list[str] | None = None) -> int:
    """Serve every enabled local connector on one port, printing what core needs to reach them."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--export-env",
        action="store_true",
        help="Print the shell exports core needs (URLs and per-connector tokens) and exit, "
        'for `eval "$(...)"` in a script that starts both processes.',
    )
    args = parser.parse_args(argv)

    # Before the apps are built, so a bundle's own middleware resolves a credential that exists.
    tokens, preexisting = ensure_dev_tokens()
    composite, urls = build_composite()
    if args.export_env:
        # Only the exports on stdout and no `configure_logging()`: this output is `eval`ed. The
        # composite is built so the URL map is the same object the serving path prints.
        print("\n".join(_export_lines(urls, tokens)))
        return 0

    configure_logging()
    if not urls:
        print("no enabled connector ships a local server — nothing to run", file=sys.stderr)
        return 1
    print(f"serving {len(urls)} connector(s) on http://{DEV_HOST}:{DEV_PORT}")
    print("point core at them with:")
    # The banner, unlike `--export-env` above, is read by a person and captured by whatever ran it.
    for line in _export_lines(urls, tokens, preexisting=preexisting):
        print(f"  {line}")
    uvicorn.run(composite, host=DEV_HOST, port=DEV_PORT, log_level=settings.log_level.lower())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
