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

**The credentials are minted here, and that is not a convenience.** Every locally-served bundle now
declares `auth: mode: bearer`, so its `/mcp` refuses an unauthenticated request — which is the
point, and which would also make `make connectors` 401 every call unless both sides of a loopback
pair agree on a secret. `ensure_dev_tokens` mints one per bundle where the environment does not
already carry it, and `--export-env` prints them as shell exports so a caller that starts core in a
*different* process (`infra/live/processes.sh`) can put the same values in both environments. There
is deliberately no default token: a fixed dev credential in the tree is the thing that eventually
turns up in a deployment.
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

# Where the composite listens. A dev-only affordance, so it is a module constant rather than a
# config field: nothing in a deployment reads it, and inventing a setting for it would be config for
# its own sake (the "config, never magic numbers" rule is about values a *deployment* varies).
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

    Read off the manifests rather than listed here, so a bundle that gains or drops a credential is
    covered the day its manifest changes — the same rule `expensive_actions()` follows for the
    trigger gate, and the reason neither has a list in core to keep up to date.

    **Locally-served bundles only, which is the whole reason this filters.** `chem` and `safety`
    also declare a bearer, and that credential belongs to `Chemclaw3-mcp` — minting a random value
    for it would replace a clear `MissingConnectorCredential` naming the unset variable with a 401
    from a server that has never heard of the token. A secret is only ours to invent when both ends
    of the call are.
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

    Minted, never defaulted. A constant would be a credential committed to the tree, and the one
    thing worse than an unauthenticated dev server is an authenticated one whose password is public
    — the second looks like a control.

    Existing values are left exactly as they are, which is what lets a caller (a live lane, a
    compose file, an operator) decide the secret and have both processes agree on it.

    **Which ones were already there is returned, not inferred.** It cannot be re-derived afterwards,
    because this function writes every value into `os.environ` — so by the time a caller looks, a
    minted token and an operator's are indistinguishable. That is exactly how a real
    `CHEMCLAW_*_MCP_TOKEN` ended up echoed verbatim in the serving banner, which in any wrapped or
    CI invocation is a log.

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


def _export_lines(
    urls: dict[str, str], tokens: dict[str, str], preexisting: frozenset[str] = frozenset()
) -> list[str]:
    """Everything a *separate* core process needs in order to reach and authenticate to these apps.

    One function so the human-readable banner and the `eval`-able output cannot disagree about what
    core needs — the failure mode of two copies here is a lane that starts and 401s every tool call,
    which reads as a broken connector rather than a missing variable.

    `preexisting` names the credentials the operator supplied, and their *values* are replaced with
    a placeholder. Printing a token this process minted is the point — it is random, ephemeral, and
    a second process needs it. Printing one the operator already exported tells them nothing they
    do not have and writes a real credential into whatever captured this output. The default is
    empty, so `--export-env` — which a caller `eval`s and which therefore needs every real value —
    keeps printing them all by simply not passing the argument.
    """
    shown = {
        name: "<already set in your environment>" if name in preexisting else value
        for name, value in tokens.items()
    }
    values = {"CHEMCLAW_CONNECTOR_URLS": json.dumps(urls, separators=(",", ":")), **shown}
    # `shlex.quote`, not hand-written quotes. A minted token is base64url and could never need it,
    # but an operator-supplied one is an arbitrary string, and a value carrying a quote would end
    # the assignment early — turning the rest of a *credential* into shell words that the caller
    # then `eval`s. The mechanical escape costs nothing and removes the question.
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
        # Nothing on stdout but the exports, and no `configure_logging()`: this output is `eval`ed.
        # The composite is built and discarded rather than short-cut, so the URL map printed here
        # is the same object the serving path prints — one reader, no second copy of the pattern.
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
