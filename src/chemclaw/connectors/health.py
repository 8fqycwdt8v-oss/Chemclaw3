"""Is each enabled connector actually there? The startup probe behind `/readyz` and `/metrics`.

Answers "which enabled connectors can we reach right now" for the readiness route, the
`chemclaw_connectors_unhealthy` gauge, the `connectors_required` fail-fast check, and (through
`connectors.reachability`) the per-turn breaker. The default posture is degrade loudly: an
unreachable connector does not stop the service, but its absence is visible.

A bundle is asked one question per half it has, and its verdict is the worse of them (`_folded`):
an HTTP `health_url` is probed directly; `jobs:` asks whether anything polls the bundle's queue
(`unpolled` if not); `queued:` tools ask the same of `connector-<name>-interactive`. A bundle with
none of these is `unprobed`, which is not counted as unhealthy.

Only a successful `DescribeTaskQueue` is a verdict; a broker that could not be asked is `unknown`,
which is logged but neither counts nor gates, so a Temporal restart is not a boot failure. Only
the HTTP half feeds the breaker, since a queue verdict says nothing about the MCP socket.
"""

import asyncio
import logging
from datetime import timedelta
from typing import Literal

import httpx
from pydantic import BaseModel, ConfigDict
from temporalio.api.enums.v1 import TaskQueueType
from temporalio.api.taskqueue.v1 import TaskQueue
from temporalio.api.workflowservice.v1 import DescribeTaskQueueRequest
from temporalio.client import Client

from chemclaw.connectors.queues import bundle_queue, interactive_queue
from chemclaw.connectors.reachability import record_reachability
from chemclaw.connectors.registry import enabled, health_url, queues_tools
from chemclaw.core.config import settings
from chemclaw.core.errors import SubsystemUnavailableError
from chemclaw.core.temporal_client import connect

logger = logging.getLogger(__name__)

ConnectorState = Literal["healthy", "unreachable", "unpolled", "unknown", "unprobed"]

# The states meaning "this capability cannot be used right now"; the gauge and the
# `connectors_required` gate share this one definition of down.
UNHEALTHY_STATES: frozenset[ConnectorState] = frozenset({"unreachable", "unpolled"})

# Worst first: the tie-break `_folded` uses between two verdicts about one connector. Undetermined
# ranks above healthy because a half that could not be asked is no evidence of health; `unprobed`
# is last so it only ever describes a bundle with nothing to ask.
_SEVERITY: tuple[ConnectorState, ...] = (
    "unreachable",
    "unpolled",
    "unknown",
    "healthy",
    "unprobed",
)

# `_SEVERITY` as a lookup, so ranking an unlisted state cannot raise out of `probe_connectors`,
# which must never raise. `tests/test_connector_health.py` pins it to `ConnectorState`.
_SEVERITY_RANK: dict[str, int] = {state: rank for rank, state in enumerate(_SEVERITY)}


class ConnectorHealth(BaseModel):
    """One enabled connector's reachability, as the readiness route reports it."""

    model_config = ConfigDict(frozen=True)

    name: str
    state: ConnectorState
    # Why it is unreachable, bounded. Logged and used in `connectors_required`'s refusal, but not in
    # `/readyz`'s unauthenticated body. Empty for healthy and unprobed.
    detail: str = ""

    @property
    def unhealthy(self) -> bool:
        """Whether this verdict counts as a connector being down, for the gauge and the gate.

        `unknown` is deliberately not down: see the module docstring.
        """
        return self.state in UNHEALTHY_STATES


class ConnectorsUnavailable(RuntimeError):
    """`connectors_required` is set and at least one enabled connector could not be reached."""


async def _probe(client: httpx.AsyncClient, name: str, url: str, budget: float) -> ConnectorHealth:
    """Probe one connector's health endpoint, bounded by `budget` seconds of wall clock.

    Any 2xx is healthy: a health route's contract is its status. The client is shared across the
    sweep to avoid a TCP/TLS setup per connector per probe.

    `asyncio.wait_for`, not httpx's `timeout=`, bounds the answer: httpx's timeout is per operation,
    so a server trickling bytes is never late, and `/readyz`'s kubelet `timeoutSeconds` is derived
    from this budget. Bounded per endpoint (the probes run concurrently), so one dark endpoint gets
    its own verdict.
    """
    try:
        response = await asyncio.wait_for(client.get(url), budget)
    except TimeoutError:
        # Named rather than rendered: a bare `TimeoutError` stringifies to "", so the detail would
        # stop exactly where the reason should start. Same defect, same fix, as `_probe_queues`.
        record_reachability(name, reachable=False)
        return ConnectorHealth(
            name=name,
            state="unreachable",
            detail=f"health check did not answer within {budget}s",
        )
    except httpx.HTTPError as exc:
        record_reachability(name, reachable=False)
        return ConnectorHealth(
            name=name, state="unreachable", detail=f"{type(exc).__name__}: {exc}"
        )
    if response.is_success:
        # Readmission half of the breaker: a connector that came back is dialled on the next turn.
        record_reachability(name, reachable=True)
        return ConnectorHealth(name=name, state="healthy")
    record_reachability(name, reachable=False)
    return ConnectorHealth(
        name=name, state="unreachable", detail=f"health check returned {response.status_code}"
    )


# What an operator scales when a queue has no poller; the bundle worker and the interactive worker
# are different Deployments in the chart.
_JOBS_REMEDY = (
    "this bundle's jobs would be accepted and never run — check the connector-worker deployment's "
    "replicas"
)
_INTERACTIVE_REMEDY = (
    "this bundle's queued tool calls would wait and never run — check its interactive-worker "
    "deployment (`connectors.<name>.interactive` in the chart)"
)

#: A probe target: the bundle, the queue to ask about, and what to tell an operator if nobody polls.
QueueTarget = tuple[str, str, str]


async def _probe_queue(
    client: Client, name: str, queue: str, budget: float, remedy: str = _JOBS_REMEDY
) -> ConnectorHealth:
    """Ask Temporal whether anything is polling this bundle's queue.

    The workflow queue, because every declared job registers a workflow, while the activity queue
    can
    legitimately be idle. Only a successful response is a verdict (an unpolled queue answers with an
    empty poller list); every failure is `unknown`. Broad `except` because the sweep must never
    raise;
    `CancelledError` is not caught, since a cancelled sweep is not a verdict.
    """
    request = DescribeTaskQueueRequest(
        namespace=settings.temporal_namespace,
        task_queue=TaskQueue(name=queue),
        task_queue_type=TaskQueueType.TASK_QUEUE_TYPE_WORKFLOW,
    )
    try:
        response = await client.workflow_service.describe_task_queue(
            request, timeout=timedelta(seconds=budget)
        )
    except Exception as exc:
        return ConnectorHealth(
            name=name,
            state="unknown",
            detail=f"describe_task_queue({queue!r}) failed: {type(exc).__name__}: {exc}",
        )
    if response.pollers:
        return ConnectorHealth(name=name, state="healthy")
    return ConnectorHealth(
        name=name,
        state="unpolled",
        detail=f"no worker is polling {queue!r}, so {remedy}",
    )


async def _probe_endpoints(targets: list[tuple[str, str]], budget: float) -> list[ConnectorHealth]:
    """Probe every HTTP health route concurrently, over one client for the whole sweep.

    The client's own `timeout=` stays as well, so a probe `wait_for` cancels does not leave a
    half-open connection in the pool; it is not the bound on the answer (see `_probe`).
    """
    if not targets:
        return []
    async with httpx.AsyncClient(timeout=budget, trust_env=False) as client:
        return list(
            await asyncio.gather(*(_probe(client, name, url, budget) for name, url in targets))
        )


async def _describe_queues(targets: list[QueueTarget], budget: float) -> list[ConnectorHealth]:
    """Connect once, then ask every queue concurrently.

    Uses the process-wide `connect()`, so a process already holding a Temporal channel reuses it.
    """
    client = await connect()
    return list(
        await asyncio.gather(*(_probe_queue(client, n, q, budget, r) for n, q, r in targets))
    )


async def _probe_queues(targets: list[QueueTarget], budget: float) -> list[ConnectorHealth]:
    """Probe every durable bundle's queue, or report them all `unknown` if the broker is not there.

    `budget` bounds the connect and the RPC together, because `/readyz`'s kubelet timeout is derived
    from it. `connect()` caches only successful clients, so a bounded failure does not poison the
    singleton. The budget is an argument because the startup sweep needs a larger one: a cold
    connect
    (PEM parsing, mTLS handshake) can otherwise leave too little time for the RPC that tells
    `unpolled` from `unknown`.
    """
    if not targets:
        return []
    try:
        return await asyncio.wait_for(_describe_queues(targets, budget), budget)
    except (SubsystemUnavailableError, TimeoutError) as exc:
        # Every bundle shares the failed dependency, so every bundle gets `unknown`.
        return [
            ConnectorHealth(
                name=name,
                state="unknown",
                # Name the type: a `TimeoutError` renders as an empty string.
                detail=(
                    f"the durable backend could not be reached to ask about {queue!r}: "
                    f"{type(exc).__name__}: {exc}"
                ),
            )
            for name, queue, _ in targets
        ]


def _folded(verdicts: list[ConnectorHealth]) -> list[ConnectorHealth]:
    """One row per connector, worst half first, with every half's reason kept.

    A bundle with an endpoint and jobs is only as usable as its worse half (`_SEVERITY` orders
    them).
    Details are joined, not picked, because the halves name different deployments. A single-half
    connector folds to itself.
    """
    halves: dict[str, list[ConnectorHealth]] = {}
    for verdict in verdicts:
        halves.setdefault(verdict.name, []).append(verdict)
    folded = []
    for name, both in halves.items():
        # An unranked state sorts with `unknown`: no evidence of health, and not gated on.
        both.sort(key=lambda health: _SEVERITY_RANK.get(health.state, _SEVERITY_RANK["unknown"]))
        folded.append(
            ConnectorHealth(
                name=name,
                state=both[0].state,
                detail="; ".join(health.detail for health in both if health.detail),
            )
        )
    return sorted(folded, key=lambda health: health.name)


async def probe_connectors(budget: float | None = None) -> list[ConnectorHealth]:
    """Probe every enabled connector concurrently; never raises, so a caller can always report.

    Args:
        budget: Seconds one connector's probe may take, in both halves. `None` means
            `connector_health_timeout_seconds`, read at call time so overrides are honoured; the
            startup sweep passes its own.

    The HTTP and queue halves are gathered together, so a sweep costs the slower fan-out rather than
    the sum. Each half a bundle has (endpoint, `jobs:` queue, `queued:` interactive queue) is asked
    additively, because each names a separate Deployment that can be missing on its own.
    """
    bound = settings.connector_health_timeout_seconds if budget is None else budget
    endpoints: list[tuple[str, str]] = []
    queues: list[QueueTarget] = []
    unprobed: list[ConnectorHealth] = []
    for manifest in enabled():
        # Through the registry, never off the manifest: `connector_urls` moves where a connector
        # really is.
        probe_url = health_url(manifest)
        if probe_url:
            endpoints.append((manifest.name, probe_url))
        if manifest.jobs:
            # Durable work of its own: ask the queue that work runs on, whether or not it also
            # serves an endpoint.
            queues.append((manifest.name, bundle_queue(manifest.name), _JOBS_REMEDY))
        if queues_tools(manifest):
            # Queued tool calls wait on their own queue with its own worker, which can be missing
            # while the rest
            # is fine.
            queues.append((manifest.name, interactive_queue(manifest.name), _INTERACTIVE_REMEDY))
        if not probe_url and not manifest.jobs and not queues_tools(manifest):
            # Nothing to ask: no endpoint and no durable work, stdio (spawned per turn), or an
            # HTTP endpoint that declares no health route and queues nothing.
            unprobed.append(ConnectorHealth(name=manifest.name, state="unprobed"))
    probed, polled = await asyncio.gather(
        _probe_endpoints(endpoints, bound), _probe_queues(queues, bound)
    )
    return _folded([*probed, *polled, *unprobed])


async def check_connectors_at_startup() -> list[ConnectorHealth]:
    """Probe the enabled connectors at startup, logging it and honoring `connectors_required`.

    Uses `connector_startup_health_timeout_seconds`, not the poll's budget: this sweep runs once,
    pays
    the cold Temporal connect, and its verdict is final for the boot, so it must leave the RPC
    enough
    time to tell `unpolled` from `unknown`.

    Returns:
        Every enabled connector's health, for the readiness route and the unhealthy gauge to read.

    Raises:
        ConnectorsUnavailable: When `connectors_required` is set and at least one enabled connector
            is unreachable or unpolled.
    """
    health = await probe_connectors(settings.connector_startup_health_timeout_seconds)
    down = [item for item in health if item.unhealthy]
    if down:
        # WARNING, not ERROR, on the default path: the service is deliberately still serving.
        logger.warning(
            "connectors unreachable at startup: %s",
            ", ".join(f"{item.name} ({item.state}: {item.detail})" for item in down),
        )
        if settings.connectors_required:
            raise ConnectorsUnavailable(
                "connectors_required is set but these connectors are unreachable: "
                + ", ".join(f"{item.name} ({item.state})" for item in down)
            )
    # A separate warning: the probe did not run, so this is no evidence either way. It never gates,
    # since the broker is shared by every durable bundle.
    unknown = [item for item in health if item.state == "unknown"]
    if unknown:
        logger.warning(
            "connector reachability could not be determined at startup (not counted as down): %s",
            ", ".join(f"{item.name} ({item.detail})" for item in unknown),
        )
    summary = ", ".join(f"{item.name}={item.state}" for item in health)
    logger.info("connectors: %s", summary or "none enabled")
    return health
