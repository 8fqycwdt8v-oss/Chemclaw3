"""Is each enabled connector actually there? — the startup probe behind `/readyz` and `/metrics`.

Moving capability out of the agent's process makes it a network dependency, and the honest thing to
do with a new failure mode is to *report* it rather than to discover it mid-conversation. This
module answers "which enabled connectors can we reach right now" for three consumers: the readiness
route (so an operator sees it), the `chemclaw_connectors_unhealthy` gauge (so it alerts), and the
`connectors_required` fail-fast check (so a deployment that prefers death to degradation gets it).

The default posture is **degrade loudly**: an unreachable connector does not stop the service, its
tools simply are not reachable that turn and the failure is visible in three places. Silently
dropping the connector — the availability-maximizing option — is rejected on purpose: an agent that
quietly loses a capability answers worse without anyone knowing why.

A connector with no `health_url` is reported `unprobed`, not `healthy`. We control our own bundles
and give them `/healthz`; a third-party MCP server may expose nothing, and guessing a path there
would manufacture false alarms. `unprobed` is the truthful third state, and it is deliberately not
counted as unhealthy.

**And there is a fourth consumer now: the per-turn open path**
(`D-2026-08-27-the-breaker-is-the-readiness-verdict-already-taken`). Every verdict this sweep
reaches is recorded in `connectors.reachability`, which `connectors.transport` reads before
dialling — so a connector this pod has just found unreachable does not cost
`connector_open_timeout_seconds` again on the next turn. That memory lives in its own module rather
than here because this one imports `connectors.registry`, which imports `connectors.transport`: a
reader in the transport would close the cycle.
"""

import asyncio
import logging
from typing import Literal

import httpx
from pydantic import BaseModel, ConfigDict

from chemclaw.connectors.reachability import record_reachability
from chemclaw.connectors.registry import enabled, health_url
from chemclaw.core.config import settings

logger = logging.getLogger(__name__)

ConnectorState = Literal["healthy", "unreachable", "unprobed"]


class ConnectorHealth(BaseModel):
    """One enabled connector's reachability, as the readiness route reports it."""

    model_config = ConfigDict(frozen=True)

    name: str
    state: ConnectorState
    # Why it is unreachable — a bounded message. It reaches the WARNING each failed probe logs and
    # `connectors_required`'s startup refusal; it is deliberately **not** in `/readyz`'s body, which
    # is unauthenticated and therefore reports a count rather than naming the fleet. Empty for the
    # healthy and unprobed states.
    detail: str = ""


class ConnectorsUnavailable(RuntimeError):
    """`connectors_required` is set and at least one enabled connector could not be reached."""


async def _probe(client: httpx.AsyncClient, name: str, url: str) -> ConnectorHealth:
    """Probe one connector's health endpoint, bounded by `connector_health_timeout_seconds`.

    Any 2xx counts as healthy: a health route's contract is its status, and demanding a body shape
    would couple us to every connector's internals — including third-party servers we do not own.

    The client is passed in rather than built here: one per connector meant six TCP setups (and,
    behind an mTLS ingress, six handshakes) on every readiness probe, which the kubelet runs every
    10 seconds per pod.
    """
    try:
        response = await client.get(url)
    except httpx.HTTPError as exc:
        record_reachability(name, reachable=False)
        return ConnectorHealth(
            name=name, state="unreachable", detail=f"{type(exc).__name__}: {exc}"
        )
    if response.is_success:
        # The readmission half of the breaker: this sweep runs every readiness probe, so a
        # connector that came back is dialled again on the very next turn rather than waiting out
        # `connector_breaker_window_seconds`.
        record_reachability(name, reachable=True)
        return ConnectorHealth(name=name, state="healthy")
    record_reachability(name, reachable=False)
    return ConnectorHealth(
        name=name, state="unreachable", detail=f"health check returned {response.status_code}"
    )


async def probe_connectors() -> list[ConnectorHealth]:
    """Probe every enabled connector concurrently; never raises, so a caller can always report.

    Concurrent because probes are independent and a serial sweep would make startup wait for the sum
    of the timeouts rather than the slowest one. One `httpx.AsyncClient` for the whole sweep, so a
    fleet of N connectors costs one client rather than N.
    """
    targets: list[tuple[str, str]] = []
    unprobed = []
    for manifest in enabled():
        # Through the registry, never off the manifest: the deployment's `connector_urls` override
        # moves where a connector actually is, and reading the declared URL here probed the
        # loopback dev default in every cluster (D-131).
        probe_url = health_url(manifest)
        if probe_url:
            targets.append((manifest.name, probe_url))
        else:
            # No endpoint (a jobs-only connector), stdio (spawned per turn, nothing to probe), or an
            # HTTP endpoint that declares no health route.
            unprobed.append(ConnectorHealth(name=manifest.name, state="unprobed"))
    probed: list[ConnectorHealth] = []
    if targets:
        async with httpx.AsyncClient(timeout=settings.connector_health_timeout_seconds) as client:
            probed = list(
                await asyncio.gather(*(_probe(client, name, url) for name, url in targets))
            )
    return sorted([*probed, *unprobed], key=lambda health: health.name)


async def check_connectors_at_startup() -> list[ConnectorHealth]:
    """Probe the enabled connectors at startup, logging it and honoring `connectors_required`.

    Returns:
        Every enabled connector's health, for the readiness route and the unhealthy gauge to read.

    Raises:
        ConnectorsUnavailable: When `connectors_required` is set and at least one enabled connector
            is unreachable — the fail-fast posture a deployment can opt into, where serving with
            a silently reduced tool surface is worse than not serving.
    """
    health = await probe_connectors()
    unreachable = [item for item in health if item.state == "unreachable"]
    if unreachable:
        # WARNING, not ERROR, on the default path: the service is deliberately still serving.
        logger.warning(
            "connectors unreachable at startup: %s",
            ", ".join(f"{item.name} ({item.detail})" for item in unreachable),
        )
        if settings.connectors_required:
            raise ConnectorsUnavailable(
                "connectors_required is set but these connectors are unreachable: "
                + ", ".join(item.name for item in unreachable)
            )
    summary = ", ".join(f"{item.name}={item.state}" for item in health)
    logger.info("connectors: %s", summary or "none enabled")
    return health
